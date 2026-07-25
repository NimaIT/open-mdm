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
        "data:read", "data:write",
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
}


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
def load_group_mappings(db: Session) -> List[Tuple[str, str]]:
    rows = (
        db.query(GroupRoleMapping)
        .filter(GroupRoleMapping.is_active.is_(True))
        .all()
    )
    return [(r.group_dn, r.role) for r in rows]


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
    actor: str = "system",
) -> Tuple[ApiKey, str]:
    """Create an API key. The plaintext is returned once and never stored."""
    raw = f"mdm_{secrets.token_urlsafe(32)}"
    prefix = raw[:12]
    record = ApiKey(
        name=name,
        key_prefix=prefix,
        key_hash=hashlib.sha256(raw.encode()).hexdigest(),
        source_system=source_system,
        allowed_entities=allowed_entities or [],
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
    principal = User(
        username=f"apikey:{record.name}",
        display_name=record.name,
        source=UserSource.SERVICE.value,
        roles=[Role.SERVICE.value],
        entity_permissions={e: ["write"] for e in (record.allowed_entities or [])},
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


def can_access_entity(user: User, entity_name: str, action: str = "read") -> bool:
    """Per-entity override check, applied on top of role permissions."""
    overrides = user.entity_permissions or {}
    if not overrides:
        return True
    if entity_name not in overrides:
        # A service key scoped to specific entities must not reach others.
        return user.source != UserSource.SERVICE.value
    return action in (overrides.get(entity_name) or [])


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
