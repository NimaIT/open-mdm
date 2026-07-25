"""Native LDAP / Active Directory authentication.

Authentication flow (the standard, safe pattern):
  1. Bind as the service account (or anonymously) to search the directory.
  2. Locate the user's DN via the configured filter.
  3. Re-bind *as that user* with the supplied password. Success proves the
     credentials — we never retrieve or compare password hashes ourselves.
  4. Resolve group memberships (optionally following nested AD groups).
  5. Map group DNs onto application roles.

A local break-glass admin exists so an operator cannot be locked out when the
directory is unreachable.
"""
import logging
import re
import ssl
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ldap3 import ALL, SIMPLE, SUBTREE, Connection, Server, Tls
from ldap3.core.exceptions import (
    LDAPBindError,
    LDAPException,
    LDAPInvalidCredentialsResult,
    LDAPSocketOpenError,
)

from app.config import settings
from app.models import Role

log = logging.getLogger(__name__)

# AD extensible match that walks nested group membership transitively.
LDAP_MATCHING_RULE_IN_CHAIN = "1.2.840.113556.1.4.1941"


class LDAPConfigurationError(RuntimeError):
    pass


class LDAPAuthenticationError(Exception):
    """Credentials rejected by the directory."""


class LDAPUnavailableError(Exception):
    """The directory could not be reached."""


@dataclass
class DirectoryUser:
    username: str
    dn: str
    email: Optional[str] = None
    display_name: Optional[str] = None
    groups: List[str] = field(default_factory=list)
    roles: List[str] = field(default_factory=list)
    raw: Dict = field(default_factory=dict)


def _escape_filter(value: str) -> str:
    """RFC 4515 escaping — prevents LDAP filter injection.

    Without this, a username like ``*)(objectClass=*`` would alter the filter's
    meaning and could authenticate the wrong principal.
    """
    out = []
    for ch in value:
        if ch == "\\":
            out.append(r"\5c")
        elif ch == "*":
            out.append(r"\2a")
        elif ch == "(":
            out.append(r"\28")
        elif ch == ")":
            out.append(r"\29")
        elif ch == "\x00":
            out.append(r"\00")
        elif ch == "/":
            out.append(r"\2f")
        else:
            out.append(ch)
    return "".join(out)


def _build_server() -> Server:
    tls = None
    if settings.LDAP_USE_SSL or settings.LDAP_START_TLS:
        validate = ssl.CERT_REQUIRED if settings.LDAP_TLS_VERIFY else ssl.CERT_NONE
        tls = Tls(
            validate=validate,
            ca_certs_file=settings.LDAP_CA_CERT_FILE,
            version=ssl.PROTOCOL_TLS_CLIENT if settings.LDAP_TLS_VERIFY else None,
        )
    return Server(
        settings.LDAP_SERVER,
        use_ssl=settings.LDAP_USE_SSL,
        get_info=ALL,
        tls=tls,
        connect_timeout=settings.LDAP_TIMEOUT_SECONDS,
    )


def _connect(
    server: Server, user: Optional[str], password: Optional[str], *, server_pool=None
) -> Connection:
    conn = Connection(
        server,
        user=user,
        password=password,
        authentication=SIMPLE if user else None,
        auto_bind=False,
        raise_exceptions=True,
        receive_timeout=settings.LDAP_TIMEOUT_SECONDS,
    )
    if settings.LDAP_START_TLS and not settings.LDAP_USE_SSL:
        conn.open()
        conn.start_tls()
    conn.bind()
    return conn


def _search_base() -> str:
    return settings.LDAP_USER_SEARCH_BASE or settings.LDAP_BASE_DN


def _group_base() -> str:
    return settings.LDAP_GROUP_SEARCH_BASE or settings.LDAP_BASE_DN


def find_user(conn: Connection, username: str) -> Optional[Dict]:
    """Locate a user entry. Returns the raw ldap3 entry dict, or None."""
    safe = _escape_filter(username)
    ldap_filter = settings.LDAP_USER_FILTER.replace("{username}", safe)
    attrs = [
        settings.LDAP_ATTR_USERNAME,
        settings.LDAP_ATTR_EMAIL,
        settings.LDAP_ATTR_DISPLAY_NAME,
        "memberOf",
        "distinguishedName",
    ]
    conn.search(
        search_base=_search_base(),
        search_filter=ldap_filter,
        search_scope=SUBTREE,
        attributes=attrs,
    )
    if not conn.entries:
        return None
    entry = conn.entries[0]
    return {
        "dn": entry.entry_dn,
        "attributes": {k: v for k, v in entry.entry_attributes_as_dict.items()},
    }


def resolve_groups(conn: Connection, user_dn: str, member_of: List[str]) -> List[str]:
    """Return the group DNs a user belongs to.

    With nested groups enabled we ask AD to walk the chain via
    LDAP_MATCHING_RULE_IN_CHAIN, which resolves indirect membership that a
    plain memberOf read would miss.
    """
    groups = {g for g in (member_of or []) if g}

    if settings.LDAP_NESTED_GROUPS:
        try:
            safe_dn = _escape_filter(user_dn)
            conn.search(
                search_base=_group_base(),
                search_filter=(
                    f"(member:{LDAP_MATCHING_RULE_IN_CHAIN}:={safe_dn})"
                ),
                search_scope=SUBTREE,
                attributes=["distinguishedName", "cn"],
            )
            for entry in conn.entries:
                groups.add(entry.entry_dn)
        except LDAPException as exc:
            # Non-AD directories don't implement the matching rule; the direct
            # memberOf values we already have remain valid.
            log.warning("Nested group resolution unavailable: %s", exc)

    return sorted(groups)


def _normalize_dn(dn: str) -> str:
    """Case- and whitespace-insensitive DN comparison key."""
    return re.sub(r"\s*,\s*", ",", (dn or "").strip().lower())


def map_roles(group_dns: List[str], mappings: Optional[List[Tuple[str, str]]] = None) -> List[str]:
    """Map directory groups onto application roles.

    ``mappings`` comes from the database (admin-editable); the environment
    variables act as the bootstrap fallback.
    """
    normalized = {_normalize_dn(g) for g in group_dns}
    roles: set = set()

    pairs: List[Tuple[str, str]] = list(mappings or [])
    if not pairs:
        for dn, role in (
            (settings.LDAP_ADMIN_GROUP_DN, Role.ADMIN.value),
            (settings.LDAP_STEWARD_GROUP_DN, Role.STEWARD.value),
            (settings.LDAP_READER_GROUP_DN, Role.READER.value),
        ):
            if dn:
                pairs.append((dn, role))

    for dn, role in pairs:
        if _normalize_dn(dn) in normalized:
            roles.add(role)

    return sorted(roles)


def authenticate(
    username: str, password: str, *, mappings: Optional[List[Tuple[str, str]]] = None
) -> DirectoryUser:
    """Authenticate against the directory and resolve roles.

    Raises LDAPAuthenticationError on bad credentials and
    LDAPUnavailableError when the directory cannot be reached — the caller
    needs to distinguish these to decide whether to fall back to the local
    break-glass account.
    """
    if not settings.LDAP_ENABLED:
        raise LDAPConfigurationError("LDAP authentication is disabled.")
    if not username or not password:
        raise LDAPAuthenticationError("Username and password are required.")

    server = _build_server()

    # ---- 1. service bind for the lookup
    try:
        search_conn = _connect(
            server, settings.LDAP_BIND_DN, settings.LDAP_BIND_PASSWORD
        )
    except LDAPSocketOpenError as exc:
        raise LDAPUnavailableError(f"Cannot reach {settings.LDAP_SERVER}: {exc}") from exc
    except (LDAPBindError, LDAPInvalidCredentialsResult) as exc:
        raise LDAPConfigurationError(
            f"Service account bind failed — check LDAP_BIND_DN / "
            f"LDAP_BIND_PASSWORD: {exc}"
        ) from exc

    try:
        entry = find_user(search_conn, username)
        if entry is None:
            raise LDAPAuthenticationError("Invalid username or password.")
        user_dn = entry["dn"]
        attrs = entry["attributes"]

        # ---- 2. re-bind as the user to verify the password
        try:
            user_conn = _connect(server, user_dn, password)
        except (LDAPBindError, LDAPInvalidCredentialsResult) as exc:
            raise LDAPAuthenticationError("Invalid username or password.") from exc
        except LDAPSocketOpenError as exc:
            raise LDAPUnavailableError(str(exc)) from exc

        try:
            groups = resolve_groups(search_conn, user_dn, attrs.get("memberOf") or [])
        finally:
            try:
                user_conn.unbind()
            except LDAPException:
                pass

        roles = map_roles(groups, mappings)

        def _first(key: str) -> Optional[str]:
            v = attrs.get(key)
            if isinstance(v, list):
                return str(v[0]) if v else None
            return str(v) if v else None

        return DirectoryUser(
            username=_first(settings.LDAP_ATTR_USERNAME) or username,
            dn=user_dn,
            email=_first(settings.LDAP_ATTR_EMAIL),
            display_name=_first(settings.LDAP_ATTR_DISPLAY_NAME),
            groups=groups,
            roles=roles,
            raw={k: v for k, v in attrs.items() if k != "memberOf"},
        )
    finally:
        try:
            search_conn.unbind()
        except LDAPException:
            pass


def test_connection() -> Dict:
    """Admin diagnostic: verify server reachability and the service bind."""
    result: Dict = {"server": settings.LDAP_SERVER, "enabled": settings.LDAP_ENABLED}
    if not settings.LDAP_ENABLED:
        result["ok"] = False
        result["error"] = "LDAP is disabled (set LDAP_ENABLED=true)."
        return result
    try:
        server = _build_server()
        conn = _connect(server, settings.LDAP_BIND_DN, settings.LDAP_BIND_PASSWORD)
        result["ok"] = True
        result["bound_as"] = conn.extend.standard.who_am_i() or settings.LDAP_BIND_DN
        result["start_tls"] = settings.LDAP_START_TLS
        result["use_ssl"] = settings.LDAP_USE_SSL
        conn.unbind()
    except LDAPSocketOpenError as exc:
        result.update(ok=False, error=f"Cannot reach server: {exc}")
    except LDAPException as exc:
        result.update(ok=False, error=str(exc))
    return result
