"""Identity, sessions, API keys and authorisation.

Three ways to authenticate:
  * LDAP / Active Directory  — the primary path for humans.
  * Local break-glass admin  — so an operator is never locked out when the
    directory is unreachable.
  * API key                  — machine-to-machine, landing-tier writes only.
"""
import hashlib
import logging
import secrets
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from app.config import settings
from app.models import ApiKey, AuditEvent, GroupRoleMapping, Role, User, UserSource
from app.services import ldap_auth

log = logging.getLogger(__name__)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
ALGORITHM = "HS256"

# Role capability matrix. Explicit beats implicit for anything security related.
PERMISSIONS: Dict[str, set] = {
    # Every permission listed here is enforced on at least one route. A
    # security matrix that advertises capabilities nothing checks is worse
    # than no matrix at all, so unused entries are not carried "for later".
    Role.ADMIN.value: {
        "model:read", "model:write", "model:publish", "model:drop",
        "data:read", "data:write", "data:write_direct",
        "staging:read", "staging:edit", "staging:approve", "staging:reject",
        "audit:read", "user:manage", "apikey:manage", "settings:manage",
        "pipeline:run",
    },
    Role.STEWARD.value: {
        "model:read",
        "data:read", "data:write",
        "staging:read", "staging:edit", "staging:approve", "staging:reject",
        "audit:read", "pipeline:run",
    },
    Role.READER.value: {"model:read", "data:read", "staging:read"},
    # Service accounts may only deposit into landing. They cannot approve —
    # that would defeat the entire maker-checker design.
    Role.SERVICE.value: {"data:write", "model:read"},
    # ---- Workstream 2: the legacy `steward` split into a maker and a checker,
    # plus a maker who may bypass review when explicitly authorised.
    # Editor: submits changes through the workflow. Deliberately WITHOUT
    # staging:approve / staging:reject — a maker must never sign off their work.
    Role.EDITOR.value: {
        "model:read", "data:read", "data:write",
        "staging:read", "staging:edit",
    },
    # Approver: reviews and decides on others' submissions. No data:write — an
    # approver is a checker, not a submitter.
    Role.APPROVER.value: {
        "model:read", "data:read",
        "staging:read", "staging:edit", "staging:approve", "staging:reject",
    },
    # Power user: everything an editor has, PLUS data:write_direct — the
    # authorised bypass that lets a valid write be auto-approved straight to the
    # golden tier (still through the pipeline's apply function; see data.py).
    Role.POWER_USER.value: {
        "model:read", "data:read", "data:write", "data:write_direct",
        "staging:read", "staging:edit",
    },
}

# Role -> the per-domain actions it implies, used when an LDAP group grants a
# role *within a single domain* (GroupRoleMapping.domain). These map onto the
# same action vocabulary as User.domain_permissions / entity_permissions.
ROLE_DOMAIN_ACTIONS: Dict[str, set] = {
    Role.READER.value: {"read"},
    Role.EDITOR.value: {"read", "write"},
    Role.APPROVER.value: {"read", "approve"},
    Role.POWER_USER.value: {"read", "write"},
    Role.STEWARD.value: {"read", "write", "approve"},
    Role.ADMIN.value: {"read", "write", "approve"},
    Role.SERVICE.value: {"write"},
}


# The domain every entity with a null/blank ``domain`` is treated as belonging
# to for scoping purposes. Seeded by bootstrap. Normalising here makes both the
# conferral layer (domain_roles) and the restriction layer (domain_permissions /
# elevated-key allowed_domains) behave predictably for domain-less entities
# instead of silently falling through (Minor 4).
DEFAULT_DOMAIN = "default"


def _norm_domain(entity_domain: Optional[str]) -> str:
    return (entity_domain or "").strip() or DEFAULT_DOMAIN


def domain_roles_to_permissions(domain_roles: Optional[Dict[str, List[str]]]) -> Dict[str, List[str]]:
    """Translate {domain: [role, ...]} into {domain: [action, ...]}.

    Used to fold domain-scoped LDAP role grants into ``User.domain_permissions``
    so ``can_access_entity`` can evaluate them uniformly.
    """
    out: Dict[str, List[str]] = {}
    for domain, roles in (domain_roles or {}).items():
        actions: set = set()
        for r in roles or []:
            actions |= ROLE_DOMAIN_ACTIONS.get(r, set())
        out[domain] = sorted(actions)
    return out


class AuthError(Exception):
    pass




def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    if not hashed:
        return False
    try:
        return pwd_context.verify(password, hashed)
    except ValueError:
        return False


# ------------------------------------------------------------------- sessions
def create_access_token(
    *, username: str, roles: List[str], source: str, extra: Optional[Dict] = None
) -> str:
    now = datetime.utcnow()
    payload = {
        "sub": username,
        "roles": roles,
        "src": source,
        "iat": now,
        "exp": now + timedelta(minutes=settings.ACCESS_TOKEN_TTL_MINUTES),
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=ALGORITHM)


def decode_access_token(token: str) -> Dict:
    try:
        return jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError as exc:
        raise AuthError(f"Invalid or expired session: {exc}") from exc


# --------------------------------------------------------------- group mapping
def load_group_mappings(db: Session) -> List[Tuple[str, str, Optional[str]]]:
    """Active group->role mappings as ``(group_dn, role, domain)`` triples.

    ``domain`` is ``None`` for a global grant (the shape of every legacy row).
    ``map_roles`` accepts both 2- and 3-tuples, so this widening is backward
    compatible with any caller that still passes pairs.
    """
    rows = (
        db.query(GroupRoleMapping)
        .filter(GroupRoleMapping.is_active.is_(True))
        .all()
    )
    return [(r.group_dn, r.role, r.domain) for r in rows]


# ------------------------------------------------------------- authentication
def authenticate_user(
    db: Session, username: str, password: str, *, ip: Optional[str] = None
) -> User:
    """Authenticate a human. LDAP first, local break-glass as fallback."""
    username = (username or "").strip()
    if not username or not password:
        raise AuthError("Username and password are required.")

    ldap_error: Optional[str] = None

    if settings.LDAP_ENABLED:
        try:
            dir_user = ldap_auth.authenticate(
                username, password, mappings=load_group_mappings(db)
            )
            user = _upsert_ldap_user(db, dir_user)
            if not user.roles:
                _audit(db, username, "login_denied", ip,
                       success=False,
                       detail={"reason": "authenticated but no group grants a role",
                               "groups": dir_user.groups})
                raise AuthError(
                    "Authentication succeeded but your directory groups do not "
                    "grant access to this application. Contact an administrator."
                )
            _audit(db, username, "login", ip, detail={"source": "ldap",
                                                      "roles": user.roles})
            return user
        except ldap_auth.LDAPAuthenticationError as exc:
            ldap_error = str(exc)
        except (ldap_auth.LDAPUnavailableError,
                ldap_auth.LDAPConfigurationError) as exc:
            # Directory down or misconfigured — fall through to break-glass.
            log.error("LDAP unavailable, allowing local fallback: %s", exc)
            ldap_error = f"Directory unavailable: {exc}"

    local = (
        db.query(User)
        .filter(User.username == username, User.source == UserSource.LOCAL.value)
        .one_or_none()
    )
    if local and local.is_active and verify_password(password, local.password_hash):
        local.last_login_at = datetime.utcnow()
        _audit(db, username, "login", ip,
               detail={"source": "local_breakglass",
                       "ldap_note": ldap_error})
        return local

    _audit(db, username, "login_failed", ip, success=False,
           detail={"reason": ldap_error or "invalid credentials"})
    raise AuthError(ldap_error or "Invalid username or password.")


def _upsert_ldap_user(db: Session, dir_user: ldap_auth.DirectoryUser) -> User:
    user = (
        db.query(User).filter(User.username == dir_user.username).one_or_none()
    )
    if user is None:
        user = User(
            username=dir_user.username,
            source=UserSource.LDAP.value,
            created_by="ldap-sync",
        )
        db.add(user)
    user.email = dir_user.email or user.email
    user.display_name = dir_user.display_name or user.display_name
    user.dn = dir_user.dn
    user.roles = dir_user.roles
    # Domain-scoped group grants CONFER roles within a domain (AC-3). They are
    # recomputed from the directory on every login, exactly like global roles,
    # and stored as {domain: [role, ...]} so effective_permissions() can union
    # them onto the global grants. With no domain-scoped mappings this is {} —
    # identical to the pre-Workstream-2 behaviour. (domain_permissions is left
    # untouched here: it is an admin-managed RESTRICTION layer, not driven by the
    # directory.)
    user.domain_roles = dict(getattr(dir_user, "domain_roles", None) or {})
    user.source = UserSource.LDAP.value
    user.is_active = True
    user.last_login_at = datetime.utcnow()
    user.updated_by = "ldap-sync"
    db.flush()
    return user


def ensure_local_admin(db: Session) -> Optional[Dict]:
    """Create the break-glass admin if configured and absent."""
    if not settings.LOCAL_ADMIN_ENABLED:
        return None
    existing = (
        db.query(User)
        .filter(User.username == settings.LOCAL_ADMIN_USERNAME)
        .one_or_none()
    )
    if existing:
        return {"created": False, "username": existing.username}

    password = settings.LOCAL_ADMIN_PASSWORD
    generated = False
    if not password:
        password = secrets.token_urlsafe(18)
        generated = True

    db.add(
        User(
            username=settings.LOCAL_ADMIN_USERNAME,
            display_name="Local Administrator (break-glass)",
            source=UserSource.LOCAL.value,
            password_hash=hash_password(password),
            roles=[Role.ADMIN.value],
            created_by="bootstrap",
        )
    )
    db.flush()
    return {
        "created": True,
        "username": settings.LOCAL_ADMIN_USERNAME,
        "generated_password": password if generated else None,
    }


# --------------------------------------------------------------------- api keys
def issue_api_key(
    db: Session,
    *,
    name: str,
    source_system: Optional[str] = None,
    allowed_entities: Optional[List[str]] = None,
    expires_at: Optional[datetime] = None,
    elevated: bool = False,
    allowed_domains: Optional[List[str]] = None,
    actor: str = "system",
) -> Tuple[ApiKey, str]:
    """Create an API key. The plaintext is returned once and never stored."""
    # Minor 3: an elevated key with no allowed_domains can write to EVERY domain.
    # That is a deliberate but easily-overlooked footgun, so make it loud.
    if elevated and not (allowed_domains or []):
        log.warning(
            "Elevated API key '%s' issued with NO allowed_domains: it has "
            "UNRESTRICTED cross-domain write reach. Scope it with allowed_domains "
            "unless a truly global service integration is intended.",
            name,
        )
    raw = f"mdm_{secrets.token_urlsafe(32)}"
    prefix = raw[:12]
    record = ApiKey(
        name=name,
        key_prefix=prefix,
        key_hash=hashlib.sha256(raw.encode()).hexdigest(),
        source_system=source_system,
        allowed_entities=allowed_entities or [],
        elevated=elevated,
        allowed_domains=allowed_domains or [],
        expires_at=expires_at,
        created_by=actor,
    )
    db.add(record)
    db.flush()
    return record, raw


def authenticate_api_key(db: Session, raw_key: str) -> Tuple[User, ApiKey]:
    if not raw_key or not raw_key.startswith("mdm_"):
        raise AuthError("Malformed API key.")
    digest = hashlib.sha256(raw_key.encode()).hexdigest()
    record = (
        db.query(ApiKey)
        .filter(ApiKey.key_prefix == raw_key[:12], ApiKey.is_active.is_(True))
        .one_or_none()
    )
    # Constant-time comparison to avoid leaking the hash via timing.
    if record is None or not secrets.compare_digest(record.key_hash, digest):
        raise AuthError("Invalid API key.")
    if record.expires_at and record.expires_at < datetime.utcnow():
        raise AuthError("API key has expired.")

    record.last_used_at = datetime.utcnow()
    # An elevated key trades entity-level scoping for domain-level *write* reach:
    # it may write to any entity within allowed_domains (or, when that list is
    # empty, every domain), even domains where human users are restricted. It
    # still carries only the SERVICE role, so it can never approve — elevation is
    # broadened write scope, not approval power.
    domain_permissions: Dict[str, List[str]] = {}
    if record.elevated:
        if not (record.allowed_domains or []):
            log.warning(
                "Elevated API key '%s' authenticated with no allowed_domains — "
                "it can write across ALL domains.",
                record.name,
            )
        domain_permissions = {d: ["write"] for d in (record.allowed_domains or [])}
    principal = User(
        username=f"apikey:{record.name}",
        display_name=record.name,
        source=UserSource.SERVICE.value,
        roles=[Role.SERVICE.value],
        entity_permissions={e: ["write"] for e in (record.allowed_entities or [])},
        domain_permissions=domain_permissions,
        is_active=True,
    )
    return principal, record


# ---------------------------------------------------------------- authorisation
def permissions_for(roles: List[str]) -> set:
    granted: set = set()
    for role in roles or []:
        granted |= PERMISSIONS.get(role, set())
    return granted


def has_permission(roles: List[str], permission: str) -> bool:
    return permission in permissions_for(roles)


def effective_permissions(user: "User", entity_domain: Optional[str] = None) -> set:
    """The permissions a user actually holds for an entity in ``entity_domain``.

    ``permissions_for(user.roles)`` (global grants) UNION the permissions
    conferred by any ``domain_roles`` grant scoped to that domain. This is the
    CONFERRAL model behind AC-3: a user whose global ``roles`` is empty but who
    holds ``domain_roles = {"finance": ["approver"]}`` gains the approver
    permissions inside ``finance`` and nowhere else.

    With ``domain_roles`` empty this is exactly ``permissions_for(user.roles)`` —
    identical to pre-Workstream-2 behaviour.
    """
    perms = permissions_for(user.roles)
    dom_roles = getattr(user, "domain_roles", None) or {}
    if dom_roles:
        conferred = dom_roles.get(_norm_domain(entity_domain)) or []
        perms = perms | permissions_for(conferred)
    return perms


def can_access_entity(
    user: User,
    entity_name: str,
    action: str = "read",
    *,
    entity_domain: Optional[str] = None,
) -> bool:
    """Access check applied on top of role permissions, most specific first.

    Precedence:
      1. ``entity_permissions`` — the per-entity override (most specific).
      2. ``domain_permissions`` — keyed by the entity's ``domain``.
      3. Global role permission — the fall-through.

    Both override maps are additive allow-lists. When BOTH are empty this is a
    no-op that returns ``True`` for everyone, so behaviour with no overrides set
    is identical to before Workstream 2. When some override map IS set but
    neither matches this entity/domain, a human still falls through (allowed)
    while a SERVICE key is confined to what it was explicitly granted.
    """
    ent_over = user.entity_permissions or {}
    dom_over = getattr(user, "domain_permissions", None) or {}
    # A null/blank entity domain is treated as the seeded 'default' domain, so a
    # domain-scoped restriction (or an elevated key's allowed_domains) applies
    # predictably to domain-less entities rather than silently not matching.
    entity_domain = _norm_domain(entity_domain)

    # 1. Entity-level override wins outright when it names this entity.
    if ent_over and entity_name in ent_over:
        return action in (ent_over.get(entity_name) or [])

    # 2. Domain-level override, matched on the entity's domain.
    if dom_over and entity_domain in dom_over:
        return action in (dom_over.get(entity_domain) or [])

    # 3. Fall through.
    if not ent_over and not dom_over:
        return True
    # A restriction map exists but did not match this entity/domain: a service
    # key must not reach beyond what it was explicitly scoped to.
    return user.source != UserSource.SERVICE.value


def _audit(
    db: Session,
    actor: str,
    action: str,
    ip: Optional[str],
    *,
    success: bool = True,
    detail: Optional[Dict] = None,
) -> None:
    db.add(
        AuditEvent(
            actor=actor,
            action=action,
            ip_address=ip,
            success=success,
            detail=detail or {},
        )
    )
