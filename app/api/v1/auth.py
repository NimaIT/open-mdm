"""Authentication endpoints."""
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from app.api.deps import client_ip, get_current_principal, principal_context, require_admin
from app.config import settings
from app.db import get_db
from app.models import User
from app.schemas.models import LoginRequest, TokenResponse
from app.services.auth import (
    AuthError,
    authenticate_user,
    create_access_token,
    permissions_for,
)
from app.services import ldap_auth

router = APIRouter(tags=["authentication"])


@router.post("/auth/login", response_model=TokenResponse)
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    """Authenticate against LDAP/AD, falling back to the local break-glass admin."""
    try:
        user = authenticate_user(
            db, payload.username, payload.password, ip=client_ip(request)
        )
    except AuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)
        ) from exc

    token = create_access_token(
        username=user.username, roles=user.roles or [], source=user.source
    )
    response.set_cookie(
        settings.SESSION_COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        secure=settings.is_production,
        max_age=settings.ACCESS_TOKEN_TTL_MINUTES * 60,
    )
    return TokenResponse(
        access_token=token,
        expires_in_minutes=settings.ACCESS_TOKEN_TTL_MINUTES,
        username=user.username,
        roles=user.roles or [],
        permissions=sorted(permissions_for(user.roles)),
    )


@router.post("/auth/logout")
def logout(response: Response):
    response.delete_cookie(settings.SESSION_COOKIE_NAME)
    return {"detail": "Signed out."}


@router.get("/auth/me")
def me(principal: User = Depends(get_current_principal)):
    return principal_context(principal)


@router.get("/auth/ldap/test")
def ldap_test(_: User = Depends(require_admin)):
    """Admin diagnostic for directory connectivity."""
    return ldap_auth.test_connection()
