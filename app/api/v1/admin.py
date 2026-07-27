"""Administration: users, roles, API keys, audit, LDAP group mappings."""
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.api.deps import (
    client_ip,
    get_current_principal,
    require_admin,
    require_permission,
)
from app.config import settings
from app.db import check_connection, get_db, get_engine
from app.models import (
    ApiKey,
    AuditEvent,
    Domain,
    Entity,
    FieldMapping,
    GroupRoleMapping,
    Notification,
    NotificationTemplate,
    Role,
    User,
    UserSource,
    WorkflowTask,
)
from app.schemas.models import (
    ApiKeyCreate,
    DomainIn,
    DomainOut,
    FieldMappingIn,
    FieldMappingOut,
    GroupMappingIn,
    NotificationTemplateIn,
    NotificationTestIn,
    ReassignIn,
    TerminateIn,
    UserPermissionsIn,
)
from app.services import notifications, scheduler as scheduler_svc, workflow
from app.services.auth import PERMISSIONS, hash_password, issue_api_key
from app.services.identifiers import IdentifierError, validate_ident

router = APIRouter(prefix="/admin", tags=["administration"])

# Domains are a top-level resource (/domains), not nested under /admin, because
# the list endpoint is reachable by any authenticated user (UI pickers).
domain_router = APIRouter(prefix="/domains", tags=["domains"])


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
            "domain_permissions": u.domain_permissions,
            "domain_roles": u.domain_roles,
        }
        for u in q.order_by(User.username).all()
    ]


@router.put("/users/{username}/permissions")
def set_user_permissions(
    username: str,
    payload: UserPermissionsIn,
    db: Session = Depends(get_db),
    principal: User = Depends(require_permission("user:manage")),
):
    """Set a user's access overrides.

    Any map may be omitted to leave it unchanged. ``entity_permissions`` and
    ``domain_permissions`` are RESTRICTION allow-lists consulted by
    can_access_entity (entity first, then domain). ``domain_roles`` is the
    CONFERRAL map: it GRANTS the listed roles' permissions within a domain,
    unioned onto the user's global roles by effective_permissions().
    """
    user = db.query(User).filter(User.username == username).one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail=f"User '{username}' not found.")
    before = {"entity_permissions": user.entity_permissions,
              "domain_permissions": user.domain_permissions,
              "domain_roles": user.domain_roles}
    if payload.entity_permissions is not None:
        user.entity_permissions = payload.entity_permissions
    if payload.domain_permissions is not None:
        user.domain_permissions = payload.domain_permissions
    if payload.domain_roles is not None:
        user.domain_roles = payload.domain_roles
    user.updated_by = principal.username
    db.add(
        AuditEvent(actor=principal.username, actor_roles=principal.roles,
                   action="user_permissions_changed", record_id=username,
                   tier="meta", before_value=before,
                   after_value={"entity_permissions": user.entity_permissions,
                                "domain_permissions": user.domain_permissions,
                                "domain_roles": user.domain_roles})
    )
    return {
        "username": username,
        "entity_permissions": user.entity_permissions,
        "domain_permissions": user.domain_permissions,
        "domain_roles": user.domain_roles,
        "note": "LDAP users have roles and domain_roles recomputed from directory "
        "groups on next login." if user.source == UserSource.LDAP.value else None,
    }


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
                 "editor": "Submits changes through the workflow. Cannot approve.",
                 "approver": "Reviews and decides on others' submissions. Does not submit data.",
                 "power_user": "Editor who may apply valid writes straight to live (direct edit).",
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
         "domain": m.domain, "description": m.description, "is_active": m.is_active}
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
        group_dn=payload.group_dn, role=payload.role, domain=payload.domain,
        description=payload.description, created_by=principal.username,
    )
    db.add(m)
    db.flush()
    return {"id": str(m.id), "group_dn": m.group_dn, "role": m.role,
            "domain": m.domain}


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
            "elevated": k.elevated, "allowed_domains": k.allowed_domains,
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
        elevated=payload.elevated, allowed_domains=payload.allowed_domains,
        actor=principal.username,
    )
    db.add(
        AuditEvent(actor=principal.username, actor_roles=principal.roles,
                   action="apikey_created", record_id=str(record.id), tier="meta",
                   detail={"name": payload.name,
                           "allowed_entities": payload.allowed_entities,
                           "elevated": payload.elevated,
                           "allowed_domains": payload.allowed_domains})
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


# -------------------------------------------------------------------- domains
def _domain_out(db: Session, d: Domain) -> DomainOut:
    count = db.query(Entity).filter(Entity.domain == d.name).count()
    return DomainOut.model_validate(d, from_attributes=True).model_copy(
        update={"entity_count": count}
    )


@domain_router.get("", response_model=List[DomainOut])
def list_domains(
    db: Session = Depends(get_db),
    _: User = Depends(get_current_principal),
):
    """List governance domains. Reachable by any authenticated user so the UI
    can populate domain pickers."""
    return [_domain_out(db, d) for d in db.query(Domain).order_by(Domain.name).all()]


@domain_router.post("", response_model=DomainOut, status_code=status.HTTP_201_CREATED)
def create_domain(
    payload: DomainIn,
    db: Session = Depends(get_db),
    principal: User = Depends(require_permission("settings:manage")),
):
    try:
        name = validate_ident(payload.name, kind="domain name")
    except IdentifierError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if db.query(Domain).filter(Domain.name == name).first():
        raise HTTPException(status_code=409, detail=f"Domain '{name}' already exists.")
    d = Domain(
        name=name,
        display_name=payload.display_name or name.replace("_", " ").title(),
        description=payload.description,
        requires_approval=payload.requires_approval,
        retention_days=payload.retention_days,
        default_soft_delete=payload.default_soft_delete,
        created_by=principal.username, updated_by=principal.username,
    )
    db.add(d)
    db.flush()
    db.add(
        AuditEvent(actor=principal.username, actor_roles=principal.roles,
                   action="domain_create", record_id=name, tier="meta",
                   detail={"name": name})
    )
    return _domain_out(db, d)


@domain_router.get("/{name}", response_model=DomainOut)
def get_domain(
    name: str,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("settings:manage")),
):
    d = db.query(Domain).filter(Domain.name == name).one_or_none()
    if d is None:
        raise HTTPException(status_code=404, detail=f"Domain '{name}' not found.")
    return _domain_out(db, d)


@domain_router.put("/{name}", response_model=DomainOut)
def update_domain(
    name: str,
    payload: DomainIn,
    db: Session = Depends(get_db),
    principal: User = Depends(require_permission("settings:manage")),
):
    d = db.query(Domain).filter(Domain.name == name).one_or_none()
    if d is None:
        raise HTTPException(status_code=404, detail=f"Domain '{name}' not found.")
    # The name is the stable key referenced by Entity.domain — do not rename it
    # here (it would silently orphan member entities).
    d.display_name = payload.display_name or d.display_name
    d.description = payload.description
    d.requires_approval = payload.requires_approval
    d.retention_days = payload.retention_days
    d.default_soft_delete = payload.default_soft_delete
    d.updated_by = principal.username
    db.flush()
    db.add(
        AuditEvent(actor=principal.username, actor_roles=principal.roles,
                   action="domain_update", record_id=name, tier="meta")
    )
    return _domain_out(db, d)


@domain_router.delete("/{name}")
def delete_domain(
    name: str,
    db: Session = Depends(get_db),
    principal: User = Depends(require_permission("settings:manage")),
):
    d = db.query(Domain).filter(Domain.name == name).one_or_none()
    if d is None:
        raise HTTPException(status_code=404, detail=f"Domain '{name}' not found.")
    if name == "default":
        raise HTTPException(status_code=409,
                            detail="The built-in 'default' domain cannot be deleted.")
    referencing = db.query(Entity).filter(Entity.domain == name).count()
    if referencing:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Domain '{name}' is referenced by {referencing} entit"
                f"{'y' if referencing == 1 else 'ies'}. Reassign them first."
            ),
        )
    db.delete(d)
    db.add(
        AuditEvent(actor=principal.username, actor_roles=principal.roles,
                   action="domain_delete", record_id=name, tier="meta")
    )
    return {"domain": name, "deleted": True}


# ---------------------------------------------------------- field mappings (EX-3)
@router.get("/field-mappings", response_model=List[FieldMappingOut])
def list_field_mappings(
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("settings:manage")),
    entity_name: Optional[str] = None,
    source_system: Optional[str] = None,
):
    """List configured source->target field mappings, filterable (EX-3)."""
    q = db.query(FieldMapping)
    if entity_name:
        q = q.filter(FieldMapping.entity_name == entity_name)
    if source_system:
        q = q.filter(FieldMapping.source_system == source_system)
    rows = q.order_by(
        FieldMapping.entity_name, FieldMapping.source_field
    ).all()
    return [FieldMappingOut.model_validate(m) for m in rows]


@router.post("/field-mappings", response_model=FieldMappingOut,
             status_code=status.HTTP_201_CREATED)
def create_field_mapping(
    payload: FieldMappingIn,
    db: Session = Depends(get_db),
    principal: User = Depends(require_permission("settings:manage")),
):
    """Create a field mapping. Applied during promotion (landing -> staging)."""
    try:
        entity_name = validate_ident(payload.entity_name, kind="entity name")
    except IdentifierError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    m = FieldMapping(
        source_system=payload.source_system or None,
        entity_name=entity_name,
        source_field=payload.source_field,
        target_field=payload.target_field,
        transform=payload.transform,
        default_value=payload.default_value,
        enabled=payload.enabled,
        created_by=principal.username, updated_by=principal.username,
    )
    db.add(m)
    db.flush()
    db.add(
        AuditEvent(actor=principal.username, actor_roles=principal.roles,
                   action="field_mapping_create", entity_name=entity_name,
                   record_id=str(m.id), tier="meta",
                   detail={"source_field": m.source_field,
                           "target_field": m.target_field,
                           "source_system": m.source_system})
    )
    return FieldMappingOut.model_validate(m)


@router.get("/field-mappings/{mapping_id}", response_model=FieldMappingOut)
def get_field_mapping(
    mapping_id: str,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("settings:manage")),
):
    m = db.query(FieldMapping).filter(FieldMapping.id == mapping_id).one_or_none()
    if m is None:
        raise HTTPException(status_code=404, detail="Field mapping not found.")
    return FieldMappingOut.model_validate(m)


@router.put("/field-mappings/{mapping_id}", response_model=FieldMappingOut)
def update_field_mapping(
    mapping_id: str,
    payload: FieldMappingIn,
    db: Session = Depends(get_db),
    principal: User = Depends(require_permission("settings:manage")),
):
    m = db.query(FieldMapping).filter(FieldMapping.id == mapping_id).one_or_none()
    if m is None:
        raise HTTPException(status_code=404, detail="Field mapping not found.")
    try:
        entity_name = validate_ident(payload.entity_name, kind="entity name")
    except IdentifierError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    m.source_system = payload.source_system or None
    m.entity_name = entity_name
    m.source_field = payload.source_field
    m.target_field = payload.target_field
    m.transform = payload.transform
    m.default_value = payload.default_value
    m.enabled = payload.enabled
    m.updated_by = principal.username
    db.flush()
    db.add(
        AuditEvent(actor=principal.username, actor_roles=principal.roles,
                   action="field_mapping_update", entity_name=entity_name,
                   record_id=str(m.id), tier="meta")
    )
    return FieldMappingOut.model_validate(m)


@router.delete("/field-mappings/{mapping_id}")
def delete_field_mapping(
    mapping_id: str,
    db: Session = Depends(get_db),
    principal: User = Depends(require_permission("settings:manage")),
):
    m = db.query(FieldMapping).filter(FieldMapping.id == mapping_id).one_or_none()
    if m is None:
        raise HTTPException(status_code=404, detail="Field mapping not found.")
    db.add(
        AuditEvent(actor=principal.username, actor_roles=principal.roles,
                   action="field_mapping_delete", entity_name=m.entity_name,
                   record_id=mapping_id, tier="meta")
    )
    db.delete(m)
    return {"id": mapping_id, "deleted": True}


# ------------------------------------------------------- workflow administration
def _task_age_seconds(task: WorkflowTask) -> float:
    created = task.created_at
    if created is None:
        return 0.0
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - created).total_seconds()


@router.get("/workflows")
def list_workflows(
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("settings:manage")),
    status_filter: Optional[str] = Query(None, alias="status"),
    entity_name: Optional[str] = None,
    domain: Optional[str] = None,
    assignee: Optional[str] = None,
    limit: int = Query(200, le=1000),
):
    """Cross-entity view of workflow tasks so stuck ones are visible (GC-6).

    Defaults to active (pending_review / changes_requested) tasks; pass
    ``status`` to see a specific status (including terminal ones). Each row
    carries ``age_seconds`` so an operator can spot tasks that have languished."""
    q = db.query(WorkflowTask)
    if status_filter:
        q = q.filter(WorkflowTask.status == status_filter)
    else:
        q = q.filter(WorkflowTask.status.in_(list(workflow.ACTIVE_STATUSES)))
    # Defence-in-depth (MAJOR-1): a task whose entity has been deleted points at a
    # staging table that no longer exists — hide it so operators don't open a
    # broken task. Entity cleanup terminates these, but this guards stragglers.
    q = q.filter(WorkflowTask.entity_name.in_(db.query(Entity.name)))
    if entity_name:
        q = q.filter(WorkflowTask.entity_name == entity_name)
    if domain:
        q = q.filter(WorkflowTask.domain == domain)
    if assignee:
        q = q.filter(
            (WorkflowTask.assigned_to == assignee)
            | (WorkflowTask.claimed_by == assignee)
        )
    rows = (
        q.order_by(WorkflowTask.priority.desc(), WorkflowTask.created_at.asc())
        .limit(limit)
        .all()
    )
    return {
        "meta": {"count": len(rows), "limit": limit},
        "data": [
            workflow.task_to_dict(t, age_seconds=_task_age_seconds(t)) for t in rows
        ],
    }


@router.post("/workflows/{task_id}/terminate")
def terminate_workflow(
    task_id: str,
    payload: TerminateIn,
    request: Request,
    db: Session = Depends(get_db),
    principal: User = Depends(require_permission("settings:manage")),
):
    """Force-close a stuck task (GC-6). Terminal, zero golden-record impact."""
    task = db.query(WorkflowTask).filter(WorkflowTask.id == task_id).one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found.")
    entity = db.query(Entity).filter(Entity.name == task.entity_name).one_or_none()
    ip = client_ip(request)
    try:
        if entity is not None:
            with get_engine().begin() as conn:
                result = workflow.terminate(
                    db, conn, entity, task, actor=principal.username,
                    reason=payload.reason, actor_roles=principal.roles, ip_address=ip,
                )
        else:
            result = workflow.terminate(
                db, None, None, task, actor=principal.username,
                reason=payload.reason, actor_roles=principal.roles, ip_address=ip,
            )
    except workflow.WorkflowError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.add(
        AuditEvent(actor=principal.username, actor_roles=principal.roles,
                   action="workflow_terminate", entity_name=task.entity_name,
                   record_id=str(task.staging_id), tier="staging", ip_address=ip,
                   detail={"task_id": task_id, "reason": payload.reason})
    )
    if entity is not None:
        # Notify submitter + assignee AFTER termination committed (best-effort).
        notifications.safe_enqueue(
            db, event=notifications.EVENT_TERMINATED, entity=entity,
            staging_id=task.staging_id, actor=principal.username,
            comment=payload.reason,
        )
    return result


@router.post("/workflows/{task_id}/reassign")
def reassign_workflow(
    task_id: str,
    payload: ReassignIn,
    request: Request,
    db: Session = Depends(get_db),
    principal: User = Depends(require_permission("settings:manage")),
):
    """Reassign a task to a reviewer, appending a reassign event (GC-6)."""
    task = db.query(WorkflowTask).filter(WorkflowTask.id == task_id).one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found.")
    ip = client_ip(request)
    try:
        result = workflow.reassign(
            db, task, assignee=payload.assignee, actor=principal.username,
            actor_roles=principal.roles, comment=payload.note, ip_address=ip,
        )
    except workflow.WorkflowError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.add(
        AuditEvent(actor=principal.username, actor_roles=principal.roles,
                   action="workflow_reassign", entity_name=task.entity_name,
                   record_id=str(task.staging_id), tier="staging", ip_address=ip,
                   detail={"task_id": task_id, "assignee": payload.assignee})
    )
    return result


# ------------------------------------- downstream distribution & scheduler (W5)
@router.get("/distribution")
def distribution(
    _: User = Depends(require_permission("settings:manage")),
):
    """List published entities and their distribution materialized views (DD-1)."""
    return {
        "schema": settings.SCHEMA_PUBLISH,
        "entities": scheduler_svc.distribution_status(),
    }


@router.post("/distribution/refresh")
def refresh_distribution(
    db: Session = Depends(get_db),
    principal: User = Depends(require_permission("pipeline:run")),
    entity: Optional[str] = Query(None, description="Refresh a single entity."),
):
    """Refresh all distribution matviews now, or just ``?entity=<name>`` (DD-2)."""
    result = scheduler_svc.refresh_views(entity_name=entity)
    db.add(
        AuditEvent(actor=principal.username, actor_roles=principal.roles,
                   action="distribution_refresh", entity_name=entity, tier="meta",
                   detail={"count": result.get("count")})
    )
    return result


@router.post("/retention/run")
def run_retention_now(
    db: Session = Depends(get_db),
    principal: User = Depends(require_permission("pipeline:run")),
    entity: Optional[str] = Query(None, description="Prune a single entity."),
):
    """Run retention now: prune aged landing / history rows (never golden). DD-2."""
    result = scheduler_svc.run_retention(entity_name=entity)
    db.add(
        AuditEvent(actor=principal.username, actor_roles=principal.roles,
                   action="retention_run", entity_name=entity, tier="meta",
                   detail=result.get("retention"))
    )
    return result


@router.get("/scheduler")
def scheduler_status(
    _: User = Depends(require_permission("settings:manage")),
):
    """Scheduler state: enabled? intervals? last-run timestamps per job (DD-2)."""
    return scheduler_svc.scheduler_info()


@router.get("/logging")
def logging_status(
    _: User = Depends(require_permission("settings:manage")),
):
    """Active structured-logging config (AO-1): JSON? level? streams? files?"""
    from app.services.logging_config import logging_config_info

    return logging_config_info()


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
        "notifications": {
            "enabled": settings.NOTIFICATIONS_ENABLED,
            "transport": settings.NOTIFICATION_TRANSPORT,
            "app_base_url": settings.APP_BASE_URL,
            "from": settings.NOTIFICATION_FROM,
            "smtp_configured": bool(settings.SMTP_HOST),
        },
    }


# ------------------------------------------------- notification templates (NT-2)
def _template_out(t: NotificationTemplate) -> dict:
    return {
        "id": str(t.id), "domain": t.domain, "event": t.event,
        "subject": t.subject, "body": t.body, "recipients": t.recipients,
        "from_address": t.from_address, "enabled": t.enabled,
        "created_at": t.created_at, "updated_at": t.updated_at,
    }


@router.get("/notification-templates")
def list_notification_templates(
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("settings:manage")),
    event: Optional[str] = None,
    domain: Optional[str] = None,
):
    """List configured per-domain / global notification templates (NT-2)."""
    q = db.query(NotificationTemplate)
    if event:
        q = q.filter(NotificationTemplate.event == event)
    if domain:
        q = q.filter(NotificationTemplate.domain == domain)
    rows = q.order_by(NotificationTemplate.event, NotificationTemplate.domain).all()
    return {
        "defaults": {
            ev: {"subject": tpl["subject"], "body": tpl["body"]}
            for ev, tpl in notifications.DEFAULT_TEMPLATES.items()
        },
        "data": [_template_out(t) for t in rows],
    }


@router.post("/notification-templates", status_code=status.HTTP_201_CREATED)
def create_notification_template(
    payload: NotificationTemplateIn,
    db: Session = Depends(get_db),
    principal: User = Depends(require_permission("settings:manage")),
):
    """Create a per-domain (or global, domain=null) notification template."""
    if payload.event not in notifications.EVENTS:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown event. Valid: {', '.join(sorted(notifications.EVENTS))}",
        )
    domain = payload.domain or None
    existing = (
        db.query(NotificationTemplate)
        .filter(NotificationTemplate.domain.is_(None) if domain is None
                else NotificationTemplate.domain == domain,
                NotificationTemplate.event == payload.event)
        .one_or_none()
    )
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"A template for ({domain or 'global'}, {payload.event}) already exists.",
        )
    t = NotificationTemplate(
        domain=domain, event=payload.event, subject=payload.subject,
        body=payload.body, recipients=payload.recipients or [],
        from_address=payload.from_address,
        enabled=payload.enabled if payload.enabled is not None else True,
        created_by=principal.username, updated_by=principal.username,
    )
    db.add(t)
    db.flush()
    db.add(
        AuditEvent(actor=principal.username, actor_roles=principal.roles,
                   action="notification_template_create", record_id=str(t.id),
                   tier="meta", detail={"domain": domain, "event": payload.event})
    )
    return _template_out(t)


@router.get("/notification-templates/{template_id}")
def get_notification_template(
    template_id: str,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("settings:manage")),
):
    t = db.query(NotificationTemplate).filter(
        NotificationTemplate.id == template_id).one_or_none()
    if t is None:
        raise HTTPException(status_code=404, detail="Template not found.")
    return _template_out(t)


@router.put("/notification-templates/{template_id}")
def update_notification_template(
    template_id: str,
    payload: NotificationTemplateIn,
    db: Session = Depends(get_db),
    principal: User = Depends(require_permission("settings:manage")),
):
    t = db.query(NotificationTemplate).filter(
        NotificationTemplate.id == template_id).one_or_none()
    if t is None:
        raise HTTPException(status_code=404, detail="Template not found.")
    if payload.event not in notifications.EVENTS:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown event. Valid: {', '.join(sorted(notifications.EVENTS))}",
        )
    t.domain = payload.domain or None
    t.event = payload.event
    t.subject = payload.subject
    t.body = payload.body
    t.recipients = payload.recipients or []
    t.from_address = payload.from_address
    if payload.enabled is not None:
        t.enabled = payload.enabled
    t.updated_by = principal.username
    db.flush()
    db.add(
        AuditEvent(actor=principal.username, actor_roles=principal.roles,
                   action="notification_template_update", record_id=str(t.id),
                   tier="meta")
    )
    return _template_out(t)


@router.delete("/notification-templates/{template_id}")
def delete_notification_template(
    template_id: str,
    db: Session = Depends(get_db),
    principal: User = Depends(require_permission("settings:manage")),
):
    t = db.query(NotificationTemplate).filter(
        NotificationTemplate.id == template_id).one_or_none()
    if t is None:
        raise HTTPException(status_code=404, detail="Template not found.")
    db.delete(t)
    db.add(
        AuditEvent(actor=principal.username, actor_roles=principal.roles,
                   action="notification_template_delete", record_id=template_id,
                   tier="meta")
    )
    return {"id": template_id, "deleted": True}


# ------------------------------------------------ notification outbox (NT-1/NT-3)
@router.get("/notifications")
def list_notifications(
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("settings:manage")),
    status_filter: Optional[str] = Query(None, alias="status"),
    domain: Optional[str] = None,
    event: Optional[str] = None,
    limit: int = Query(100, le=1000),
    offset: int = Query(0, ge=0),
):
    """Outbox viewer: notifications newest-first, filterable by status/domain/event."""
    q = db.query(Notification)
    if status_filter:
        q = q.filter(Notification.status == status_filter)
    if domain:
        q = q.filter(Notification.domain == domain)
    if event:
        q = q.filter(Notification.event == event)
    total = q.count()
    rows = (q.order_by(Notification.created_at.desc())
            .limit(limit).offset(offset).all())
    return {
        "meta": {"total": total, "limit": limit, "offset": offset,
                 "transport": settings.NOTIFICATION_TRANSPORT},
        "data": [notifications.to_dict(n) for n in rows],
    }


@router.post("/notifications/flush")
def flush_notifications(
    db: Session = Depends(get_db),
    principal: User = Depends(require_permission("settings:manage")),
):
    """Dispatch all queued/failed notifications via the configured transport."""
    counts = notifications.dispatch(db)
    db.add(
        AuditEvent(actor=principal.username, actor_roles=principal.roles,
                   action="notifications_flush", tier="meta", detail=counts)
    )
    return counts


@router.post("/notifications/{notification_id}/resend")
def resend_notification(
    notification_id: str,
    db: Session = Depends(get_db),
    principal: User = Depends(require_permission("settings:manage")),
):
    """Re-queue and dispatch a single notification."""
    n = db.query(Notification).filter(
        Notification.id == notification_id).one_or_none()
    if n is None:
        raise HTTPException(status_code=404, detail="Notification not found.")
    if not (n.to_addresses or []):
        raise HTTPException(status_code=400,
                            detail="Notification has no recipients to send to.")
    n.status = notifications.STATUS_QUEUED
    n.error = None
    db.flush()
    counts = notifications.dispatch(db, [notification_id])
    return {"id": notification_id, "counts": counts,
            "notification": notifications.to_dict(n)}


@router.post("/notifications/test")
def test_notification(
    payload: NotificationTestIn,
    db: Session = Depends(get_db),
    principal: User = Depends(require_permission("settings:manage")),
):
    """Send a one-off test notification via SMTP to validate the configuration."""
    n = notifications.send_test(db, payload.to_address)
    return {"sent": n.status == notifications.STATUS_SENT,
            "notification": notifications.to_dict(n)}
