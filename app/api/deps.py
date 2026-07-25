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
    return user


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
    if not can_access_entity(principal, entity.name, action):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"You do not have '{action}' access to entity '{entity.name}'.",
        )


def principal_context(principal: User) -> dict:
    return {
        "username": principal.username,
        "display_name": principal.display_name or principal.username,
        "roles": principal.roles or [],
        "permissions": sorted(permissions_for(principal.roles)),
        "source": principal.source,
        "is_admin": Role.ADMIN.value in (principal.roles or []),
        "is_steward": Role.STEWARD.value in (principal.roles or []),
    }
