"""Shared FastAPI dependencies: current principal, permission gates, entity lookup."""
from typing import List, Optional

from fastapi import Cookie, Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import Entity, Role, User, UserSource
from app.services.auth import (
    AuthError,
    authenticate_api_key,
    can_access_entity,
    decode_access_token,
    effective_permissions,
    has_permission,
    permissions_for,
)


def client_ip(request: Request) -> Optional[str]:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


def get_current_principal(
    request: Request,
    db: Session = Depends(get_db),
    authorization: Optional[str] = Header(default=None),
    x_api_key: Optional[str] = Header(default=None),
    mdm_session: Optional[str] = Cookie(default=None),
) -> User:
    """Resolve the caller from an API key, bearer token or session cookie."""
    # 1. API key (machine-to-machine)
    raw_key = x_api_key
    if not raw_key and authorization and authorization.lower().startswith("apikey "):
        raw_key = authorization.split(" ", 1)[1].strip()
    if raw_key:
        try:
            principal, _record = authenticate_api_key(db, raw_key)
            _bind_log_actor(principal)
            return principal
        except AuthError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)
            ) from exc

    # 2. Bearer token / session cookie (humans)
    token = mdm_session
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        claims = decode_access_token(token)
    except AuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)
        ) from exc

    username = claims.get("sub")
    user = db.query(User).filter(User.username == username).one_or_none()
    if user is None:
        # Token valid but the account is gone (e.g. removed from the directory).
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Account no longer exists.",
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Account is disabled."
        )
    # Roles come from the database, not the token, so a revocation takes effect
    # immediately rather than at token expiry.
    _bind_log_actor(user)
    return user


def _bind_log_actor(principal: User) -> None:
    """Best-effort: stamp the resolved caller onto the request log context (AO-1)."""
    try:
        from app.services.logging_config import bind_actor

        bind_actor(getattr(principal, "username", None))
    except Exception:  # pragma: no cover - logging must never affect auth
        pass


def require_permission(permission: str):
    """Dependency factory gating a route on a single permission."""

    def _dep(principal: User = Depends(get_current_principal)) -> User:
        if not has_permission(principal.roles, permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Permission '{permission}' is required. "
                    f"Your roles: {', '.join(principal.roles) or 'none'}."
                ),
            )
        return principal

    return _dep


def require_roles(*roles: str):
    def _dep(principal: User = Depends(get_current_principal)) -> User:
        if not set(roles) & set(principal.roles or []):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"One of these roles is required: {', '.join(roles)}.",
            )
        return principal

    return _dep


require_admin = require_roles(Role.ADMIN.value)
require_steward = require_roles(Role.ADMIN.value, Role.STEWARD.value)


def get_entity(entity_name: str, db: Session = Depends(get_db)) -> Entity:
    entity = db.query(Entity).filter(Entity.name == entity_name.lower()).one_or_none()
    if entity is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Entity '{entity_name}' is not defined.",
        )
    return entity


def get_deployed_entity(entity: Entity = Depends(get_entity)) -> Entity:
    if not entity.is_deployed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Entity '{entity.name}' is defined but not published. "
                "An administrator must publish it before data can be written."
            ),
        )
    return entity


def check_entity_access(entity: Entity, principal: User, action: str = "read") -> None:
    if not can_access_entity(
        principal, entity.name, action, entity_domain=entity.domain
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"You do not have '{action}' access to entity '{entity.name}'.",
        )


def require_entity_permission(permission: str, action: str):
    """Entity-scoped gate: the single dependency the data & stewardship routes use.

    It resolves the path entity, then applies two layers in order:

      1. CONFERRAL (grants capability). The caller must hold ``permission`` in
         ``effective_permissions(principal, entity.domain)`` — i.e. their global
         role permissions UNION any ``domain_roles`` grant scoped to this
         entity's domain. This is what lets a domain-only user (empty global
         ``roles``) act inside their granted domain and nowhere else (AC-3).

      2. RESTRICTION (narrows capability). The coarse ``entity_permissions`` /
         ``domain_permissions`` allow-lists and the service-key asymmetry are
         then applied via ``can_access_entity``.

    Precedence: entity_permissions > domain_permissions (restriction) applied on
    top of the conferral grant. The restriction layer is deliberately
    read/write-grained, so the finer verbs (approve / reject / edit / write) all
    map to the coarse ``write`` action here; read verbs map to ``read``. Only the
    conferral layer distinguishes the fine permissions.
    """
    coarse_action = "read" if action == "read" else "write"

    def _dep(
        entity: Entity = Depends(get_deployed_entity),
        principal: User = Depends(get_current_principal),
    ) -> User:
        if permission not in effective_permissions(principal, entity.domain):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Permission '{permission}' is required for entity "
                    f"'{entity.name}'"
                    + (f"' (domain '{entity.domain}')" if entity.domain else "")
                    + f". Your roles: {', '.join(principal.roles) or 'none'}."
                ),
            )
        # Restriction layer + service-key narrowing (unchanged semantics).
        check_entity_access(entity, principal, coarse_action)
        return principal

    return _dep


def principal_context(principal: User) -> dict:
    roles = principal.roles or []
    domain_roles = getattr(principal, "domain_roles", None) or {}
    # UI hints must reflect what the user can do in ANY domain — a domain-only
    # approver/editor (empty global ``roles``) still needs to see the approve /
    # write / direct-edit controls for their granted domain. Aggregate the
    # permissions of the global roles UNION every domain_roles grant. This is a
    # HINT for showing/hiding controls only; the routes remain authoritative and
    # enforce the correct domain per-entity via ``require_entity_permission``.
    agg_roles = list(roles) + [r for rs in domain_roles.values() for r in (rs or [])]
    agg_perms = permissions_for(agg_roles)
    return {
        "username": principal.username,
        "display_name": principal.display_name or principal.username,
        "roles": roles,
        "permissions": sorted(agg_perms),
        "source": principal.source,
        "is_admin": Role.ADMIN.value in roles,
        "is_steward": Role.STEWARD.value in roles,
        # Workstream 2 / UI-2: everything the frontend needs to gate on.
        "entity_permissions": principal.entity_permissions or {},
        "domain_permissions": getattr(principal, "domain_permissions", None) or {},
        "domain_roles": domain_roles,
        "can_direct_edit": "data:write_direct" in agg_perms,
        "can_approve": "staging:approve" in agg_perms,
        "can_write": "data:write" in agg_perms,
    }
