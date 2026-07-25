"""Administration: users, roles, API keys, audit, LDAP group mappings."""
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import require_admin, require_permission
from app.db import check_connection, get_db
from app.models import ApiKey, AuditEvent, GroupRoleMapping, Role, User, UserSource
from app.schemas.models import ApiKeyCreate, GroupMappingIn
from app.services.auth import PERMISSIONS, hash_password, issue_api_key

router = APIRouter(prefix="/admin", tags=["administration"])


# ------------------------------------------------------------------- users
@router.get("/users")
def list_users(
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("user:manage")),
    source: Optional[str] = None,
):
    q = db.query(User)
    if source:
        q = q.filter(User.source == source)
    return [
        {
            "id": str(u.id), "username": u.username, "email": u.email,
            "display_name": u.display_name, "source": u.source, "roles": u.roles,
            "is_active": u.is_active, "last_login_at": u.last_login_at,
            "dn": u.dn, "entity_permissions": u.entity_permissions,
        }
        for u in q.order_by(User.username).all()
    ]


@router.put("/users/{username}/roles")
def set_user_roles(
    username: str,
    roles: List[str],
    db: Session = Depends(get_db),
    principal: User = Depends(require_permission("user:manage")),
):
    """Override a user's roles.

    Note: LDAP users have roles recomputed from their directory groups at each
    login, so an override here is transient for them — change the group mapping
    instead for a durable result.
    """
    valid = {r.value for r in Role}
    unknown = [r for r in roles if r not in valid]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown role(s): {', '.join(unknown)}. Valid: {', '.join(sorted(valid))}",
        )
    user = db.query(User).filter(User.username == username).one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail=f"User '{username}' not found.")
    before = user.roles
    user.roles = roles
    user.updated_by = principal.username
    db.add(
        AuditEvent(actor=principal.username, actor_roles=principal.roles,
                   action="user_roles_changed", record_id=username, tier="meta",
                   before_value={"roles": before}, after_value={"roles": roles})
    )
    return {
        "username": username, "roles": roles,
        "note": "LDAP users have roles refreshed from directory groups on next login."
        if user.source == UserSource.LDAP.value else None,
    }


@router.put("/users/{username}/status")
def set_user_status(
    username: str,
    is_active: bool,
    db: Session = Depends(get_db),
    principal: User = Depends(require_permission("user:manage")),
):
    user = db.query(User).filter(User.username == username).one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail=f"User '{username}' not found.")
    if user.username == principal.username and not is_active:
        raise HTTPException(status_code=400,
                            detail="You cannot disable your own account.")
    user.is_active = is_active
    db.add(
        AuditEvent(actor=principal.username, actor_roles=principal.roles,
                   action="user_status_changed", record_id=username, tier="meta",
                   detail={"is_active": is_active})
    )
    return {"username": username, "is_active": is_active}


@router.get("/roles")
def list_roles(_: User = Depends(require_permission("user:manage"))):
    return {
        "roles": [
            {"name": r, "permissions": sorted(p),
             "description": {
                 "admin": "Full control: model design, DDL publishing, users, settings.",
                 "steward": "Reviews, edits and approves staged data. Cannot change models.",
                 "reader": "Read-only access to golden records.",
                 "service": "Machine account. Writes into landing only; cannot approve.",
             }.get(r, "")}
            for r, p in PERMISSIONS.items()
        ]
    }


# ------------------------------------------------------------- group mappings
@router.get("/ldap/group-mappings")
def list_group_mappings(
    db: Session = Depends(get_db), _: User = Depends(require_admin)
):
    return [
        {"id": str(m.id), "group_dn": m.group_dn, "role": m.role,
         "description": m.description, "is_active": m.is_active}
        for m in db.query(GroupRoleMapping).order_by(GroupRoleMapping.role).all()
    ]


@router.post("/ldap/group-mappings", status_code=status.HTTP_201_CREATED)
def create_group_mapping(
    payload: GroupMappingIn,
    db: Session = Depends(get_db),
    principal: User = Depends(require_admin),
):
    valid = {r.value for r in Role}
    if payload.role not in valid:
        raise HTTPException(status_code=422,
                            detail=f"Unknown role. Valid: {', '.join(sorted(valid))}")
    existing = (
        db.query(GroupRoleMapping)
        .filter(GroupRoleMapping.group_dn == payload.group_dn,
                GroupRoleMapping.role == payload.role)
        .one_or_none()
    )
    if existing:
        raise HTTPException(status_code=409, detail="Mapping already exists.")
    m = GroupRoleMapping(
        group_dn=payload.group_dn, role=payload.role,
        description=payload.description, created_by=principal.username,
    )
    db.add(m)
    db.flush()
    return {"id": str(m.id), "group_dn": m.group_dn, "role": m.role}


@router.delete("/ldap/group-mappings/{mapping_id}")
def delete_group_mapping(
    mapping_id: str,
    db: Session = Depends(get_db),
    principal: User = Depends(require_admin),
):
    m = db.query(GroupRoleMapping).filter(GroupRoleMapping.id == mapping_id).one_or_none()
    if m is None:
        raise HTTPException(status_code=404, detail="Mapping not found.")
    db.delete(m)
    return {"deleted": True, "id": mapping_id}


# ------------------------------------------------------------------ api keys
@router.get("/api-keys")
def list_api_keys(
    db: Session = Depends(get_db), _: User = Depends(require_permission("apikey:manage"))
):
    return [
        {
            "id": str(k.id), "name": k.name, "key_prefix": k.key_prefix,
            "source_system": k.source_system, "allowed_entities": k.allowed_entities,
            "is_active": k.is_active, "expires_at": k.expires_at,
            "last_used_at": k.last_used_at, "created_at": k.created_at,
        }
        for k in db.query(ApiKey).order_by(ApiKey.created_at.desc()).all()
    ]


@router.post("/api-keys", status_code=status.HTTP_201_CREATED)
def create_api_key(
    payload: ApiKeyCreate,
    db: Session = Depends(get_db),
    principal: User = Depends(require_permission("apikey:manage")),
):
    """Issue a service API key. The secret is shown exactly once."""
    record, raw = issue_api_key(
        db, name=payload.name, source_system=payload.source_system,
        allowed_entities=payload.allowed_entities, expires_at=payload.expires_at,
        actor=principal.username,
    )
    db.add(
        AuditEvent(actor=principal.username, actor_roles=principal.roles,
                   action="apikey_created", record_id=str(record.id), tier="meta",
                   detail={"name": payload.name,
                           "allowed_entities": payload.allowed_entities})
    )
    return {
        "id": str(record.id), "name": record.name, "api_key": raw,
        "key_prefix": record.key_prefix,
        "warning": "Store this key now — it cannot be retrieved again.",
        "usage": "Send it as the X-API-Key header on write requests.",
    }


@router.delete("/api-keys/{key_id}")
def revoke_api_key(
    key_id: str,
    db: Session = Depends(get_db),
    principal: User = Depends(require_permission("apikey:manage")),
):
    k = db.query(ApiKey).filter(ApiKey.id == key_id).one_or_none()
    if k is None:
        raise HTTPException(status_code=404, detail="API key not found.")
    k.is_active = False
    db.add(
        AuditEvent(actor=principal.username, actor_roles=principal.roles,
                   action="apikey_revoked", record_id=key_id, tier="meta")
    )
    return {"id": key_id, "revoked": True}


# --------------------------------------------------------------------- audit
@router.get("/audit")
def audit_log(
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("audit:read")),
    entity_name: Optional[str] = None,
    actor: Optional[str] = None,
    action: Optional[str] = None,
    record_id: Optional[str] = None,
    limit: int = Query(100, le=1000),
    offset: int = Query(0, ge=0),
):
    q = db.query(AuditEvent)
    if entity_name:
        q = q.filter(AuditEvent.entity_name == entity_name)
    if actor:
        q = q.filter(AuditEvent.actor == actor)
    if action:
        q = q.filter(AuditEvent.action == action)
    if record_id:
        q = q.filter(AuditEvent.record_id == record_id)
    total = q.count()
    rows = q.order_by(AuditEvent.occurred_at.desc()).limit(limit).offset(offset).all()
    return {
        "meta": {"total": total, "limit": limit, "offset": offset},
        "data": [
            {
                "id": str(r.id), "occurred_at": r.occurred_at, "actor": r.actor,
                "actor_roles": r.actor_roles, "action": r.action,
                "entity_name": r.entity_name, "record_id": r.record_id,
                "tier": r.tier, "detail": r.detail, "success": r.success,
                "before_value": r.before_value, "after_value": r.after_value,
                "ip_address": r.ip_address,
            }
            for r in rows
        ],
    }


@router.get("/system")
def system_info(_: User = Depends(require_admin)):
    from app.config import settings

    return {
        "database": check_connection(),
        "schemas": settings.all_schemas,
        "ldap_enabled": settings.LDAP_ENABLED,
        "ldap_server": settings.LDAP_SERVER if settings.LDAP_ENABLED else None,
        "segregation_of_duties": settings.ENFORCE_SEGREGATION_OF_DUTIES,
        "auto_promote_landing": settings.AUTO_PROMOTE_LANDING,
        "soft_delete": settings.SOFT_DELETE,
        "environment": settings.ENVIRONMENT,
    }
