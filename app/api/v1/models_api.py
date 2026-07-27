"""Data-model configuration: design, import/export, publish DDL."""
import io
from datetime import datetime
from typing import List, Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from sqlalchemy.orm import Session

from app.api.deps import get_entity, require_admin, require_permission
from app.config import settings
from app.db import get_db, get_ddl_engine
from app.models import (
    Attribute,
    AuditEvent,
    Domain,
    Entity,
    EntityStatus,
    ModelVersion,
    User,
)
from app.schemas.models import (
    EntityIn,
    EntityOut,
    EntitySummary,
    PublishRequest,
)
from app.services import bootstrap as bootstrap_svc
from app.services import workflow
from app.services.ddl import (
    apply_plan,
    build_alter_plan,
    build_create_plan,
    build_drop_plan,
    reconcile_enum_constraints,
    reconcile_matview,
    reconcile_reference_constraints,
)
from app.services.identifiers import IdentifierError, validate_column_name, validate_ident
from app.services.model_io import (
    apply_import,
    diff_against_existing,
    entity_to_dict,
    export_models,
    parse_document,
    validate_document,
)

router = APIRouter(prefix="/models", tags=["data models"])


@router.get("", response_model=List[EntitySummary])
def list_entities(
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("model:read")),
    domain: Optional[str] = None,
    status_filter: Optional[str] = Query(None, alias="status"),
):
    q = db.query(Entity)
    if domain:
        q = q.filter(Entity.domain == domain)
    if status_filter:
        q = q.filter(Entity.status == status_filter)
    out = []
    for e in q.order_by(Entity.name).all():
        out.append(
            EntitySummary(
                id=str(e.id), name=e.name, display_name=e.display_name,
                domain=e.domain, status=e.status, version=e.version,
                attribute_count=len(e.attributes),
            )
        )
    return out


@router.post("", response_model=EntityOut, status_code=status.HTTP_201_CREATED)
def create_entity(
    payload: EntityIn,
    db: Session = Depends(get_db),
    principal: User = Depends(require_permission("model:write")),
):
    try:
        name = validate_ident(payload.name, kind="entity name")
        for a in payload.attributes:
            validate_column_name(a.name)
    except IdentifierError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if db.query(Entity).filter(Entity.name == name).first():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Entity '{name}' already exists.",
        )
    if not payload.attributes:
        raise HTTPException(
            status_code=422, detail="An entity must define at least one attribute."
        )

    # DM-1: an entity inherits its domain's lifecycle DEFAULTS for any field the
    # caller did not explicitly send. `model_fields_set` tells us precisely which
    # fields were provided, so an explicit value always wins over the domain.
    requires_approval = payload.requires_approval
    soft_delete = payload.soft_delete
    retention_days = payload.retention_days
    provided = payload.model_fields_set
    if payload.domain:
        dom = db.query(Domain).filter(Domain.name == payload.domain).one_or_none()
        if dom is not None:
            if "requires_approval" not in provided:
                requires_approval = dom.requires_approval
            if "soft_delete" not in provided:
                soft_delete = dom.default_soft_delete
            if "retention_days" not in provided:
                retention_days = dom.retention_days

    entity = Entity(
        name=name,
        display_name=payload.display_name or name.replace("_", " ").title(),
        description=payload.description,
        domain=payload.domain,
        kind=payload.kind,
        requires_approval=requires_approval,
        soft_delete=soft_delete,
        auto_approve_threshold=payload.auto_approve_threshold,
        retention_days=retention_days,
        status=EntityStatus.DRAFT.value,
        created_by=principal.username,
        updated_by=principal.username,
    )
    db.add(entity)
    db.flush()
    for idx, a in enumerate(payload.attributes):
        data = a.model_dump()
        data["position"] = data.get("position") or idx
        data["display_name"] = data.get("display_name") or a.name.replace("_", " ").title()
        db.add(Attribute(entity_id=entity.id, created_by=principal.username, **data))
    db.flush()
    db.refresh(entity)
    db.add(
        ModelVersion(entity_id=entity.id, version=1, snapshot=entity_to_dict(entity),
                     change_note="Created", created_by=principal.username)
    )
    db.add(
        AuditEvent(actor=principal.username, actor_roles=principal.roles,
                   action="model_create", entity_name=entity.name, tier="meta",
                   detail={"attributes": len(payload.attributes)})
    )
    return EntityOut.model_validate(entity)


@router.get("/{entity_name}", response_model=EntityOut)
def get_entity_detail(
    entity: Entity = Depends(get_entity),
    _: User = Depends(require_permission("model:read")),
):
    return EntityOut.model_validate(entity)


@router.put("/{entity_name}", response_model=EntityOut)
def update_entity(
    payload: EntityIn,
    entity: Entity = Depends(get_entity),
    db: Session = Depends(get_db),
    principal: User = Depends(require_permission("model:write")),
):
    """Replace an entity definition. Metadata only — DDL requires publish."""
    try:
        for a in payload.attributes:
            validate_column_name(a.name)
    except IdentifierError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    entity.display_name = payload.display_name or entity.display_name
    entity.description = payload.description
    entity.domain = payload.domain
    entity.kind = payload.kind
    entity.requires_approval = payload.requires_approval
    entity.soft_delete = payload.soft_delete
    entity.retention_days = payload.retention_days
    entity.version += 1
    entity.updated_by = principal.username
    if entity.status == EntityStatus.PUBLISHED.value:
        entity.status = EntityStatus.MODIFIED.value

    current = {a.name: a for a in entity.attributes}
    incoming = {a.name: a for a in payload.attributes}
    for idx, (name, a) in enumerate(incoming.items()):
        data = a.model_dump()
        data["position"] = data.get("position") or idx
        if name in current:
            for k, v in data.items():
                setattr(current[name], k, v)
            current[name].updated_by = principal.username
        else:
            data["display_name"] = data.get("display_name") or name.replace("_", " ").title()
            db.add(Attribute(entity_id=entity.id, created_by=principal.username, **data))
    for name, obj in current.items():
        if name not in incoming:
            db.delete(obj)

    db.flush()
    db.refresh(entity)
    db.add(
        ModelVersion(entity_id=entity.id, version=entity.version,
                     snapshot=entity_to_dict(entity), change_note="Updated via API",
                     created_by=principal.username)
    )
    return EntityOut.model_validate(entity)


@router.get("/{entity_name}/ddl")
def preview_ddl(
    entity: Entity = Depends(get_entity),
    _: User = Depends(require_permission("model:read")),
):
    """Dry-run: show exactly what publishing would execute."""
    engine = get_ddl_engine()
    with engine.connect() as conn:
        plan = (
            build_alter_plan(conn, entity)
            if entity.is_deployed
            else build_create_plan(entity)
        )
    return {
        "entity": entity.name,
        "mode": "alter" if entity.is_deployed else "create",
        **plan.to_dict(),
    }


@router.post("/{entity_name}/publish")
def publish_entity(
    payload: PublishRequest,
    entity: Entity = Depends(get_entity),
    db: Session = Depends(get_db),
    principal: User = Depends(require_permission("model:publish")),
):
    """Generate and apply the DDL for this entity across all four tiers."""
    engine = get_ddl_engine()
    with engine.connect() as conn:
        plan = (
            build_alter_plan(conn, entity)
            if entity.is_deployed
            else build_create_plan(entity)
        )

    if payload.dry_run:
        return {"dry_run": True, "entity": entity.name, **plan.to_dict()}

    if plan.is_destructive and not payload.confirm_destructive:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": (
                    "This change would drop columns or narrow types, which can "
                    "destroy data. Re-submit with confirm_destructive=true if "
                    "you are certain."
                ),
                "destructive_changes": plan.destructive,
                "warnings": plan.warnings,
            },
        )

    try:
        with engine.begin() as conn:
            result = apply_plan(
                conn, plan, allow_destructive=payload.confirm_destructive
            )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"DDL execution failed and was rolled back: {exc}",
        ) from exc

    entity.status = EntityStatus.PUBLISHED.value
    entity.published_version = entity.version
    entity.published_at = datetime.utcnow()
    entity.updated_by = principal.username

    # Reference foreign keys are wired up after the tables exist, from both
    # directions (this entity's outbound refs + already-published children that
    # reference it). This never aborts a publish — missing parents or violating
    # data degrade to warnings.
    reference_added: List[str] = []
    try:
        with engine.begin() as conn:
            recon = reconcile_reference_constraints(
                conn, entity, db.query(Entity).all()
            )
        reference_added = recon.get("added", [])
        plan.warnings.extend(recon.get("warnings", []))
    except Exception as exc:  # noqa: BLE001 — reference wiring is best-effort
        plan.warnings.append(f"Reference-constraint reconciliation skipped: {exc}")

    # Enum allow-list CHECKs are reconciled on the live tier so that editing or
    # removing an enum on an already-published column updates the constraint.
    try:
        with engine.begin() as conn:
            enum_recon = reconcile_enum_constraints(conn, entity)
        plan.warnings.extend(enum_recon.get("warnings", []))
    except Exception as exc:  # noqa: BLE001 — enum wiring is best-effort
        plan.warnings.append(f"Enum-constraint reconciliation skipped: {exc}")

    # Downstream distribution (DD-1): (re)create the mdm_pub materialized view.
    # A matview's columns are fixed at creation, so this drops + recreates it to
    # reflect any column change. Never hard-fails a publish — matview errors
    # degrade to a warning, consistent with reference / enum reconciliation.
    distribution_matview: List[str] = []
    try:
        with engine.begin() as conn:
            mv_recon = reconcile_matview(conn, entity)
        distribution_matview = mv_recon.get("created", [])
        plan.warnings.extend(mv_recon.get("warnings", []))
    except Exception as exc:  # noqa: BLE001 — distribution is best-effort
        plan.warnings.append(f"Distribution matview reconciliation skipped: {exc}")

    # A ModelVersion row already exists for this version (written when the model
    # was created or updated). Publishing records the applied DDL against that
    # same snapshot instead of inserting a duplicate — a duplicate violates
    # uq_model_version, and the resulting rollback would discard the status
    # change above, leaving physical tables deployed but metadata still 'draft'.
    note = payload.change_note or "Published"
    existing_version = (
        db.query(ModelVersion)
        .filter(
            ModelVersion.entity_id == entity.id,
            ModelVersion.version == entity.version,
        )
        .one_or_none()
    )
    if existing_version is None:
        db.add(
            ModelVersion(
                entity_id=entity.id, version=entity.version,
                snapshot=entity_to_dict(entity),
                change_note=note,
                applied_ddl=plan.sql, created_by=principal.username,
            )
        )
    else:
        existing_version.applied_ddl = plan.sql
        if note not in (existing_version.change_note or ""):
            existing_version.change_note = " · ".join(
                filter(None, [existing_version.change_note, note])
            )
    db.add(
        AuditEvent(
            actor=principal.username, actor_roles=principal.roles,
            action="model_publish", entity_name=entity.name, tier="meta",
            detail={"statements": result["executed"],
                    "destructive": plan.destructive},
        )
    )
    return {
        "entity": entity.name,
        "status": entity.status,
        "statements_executed": result["executed"],
        "warnings": plan.warnings,
        "reference_constraints_added": reference_added,
        "distribution_matview": distribution_matview,
        "tiers": {
            "landing": f"mdm_landing.{entity.name}",
            "staging": f"mdm_staging.{entity.name}",
            "live": f"mdm.{entity.name}",
            "history": f"mdm_history.{entity.name}",
            "distribution": f"{settings.SCHEMA_PUBLISH}.{entity.name}",
        },
    }


@router.delete("/{entity_name}")
def delete_entity(
    entity: Entity = Depends(get_entity),
    db: Session = Depends(get_db),
    principal: User = Depends(require_permission("model:drop")),
    drop_tables: bool = Query(False, description="Also DROP the physical tables."),
    confirm: bool = Query(False, description="Required when drop_tables is true."),
):
    if drop_tables and not confirm:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="drop_tables requires confirm=true — this destroys all data.",
        )
    dropped = []
    if drop_tables and entity.is_deployed:
        plan = build_drop_plan(entity.name, cascade=True)
        with get_ddl_engine().begin() as conn:
            apply_plan(conn, plan, allow_destructive=True)
        dropped = plan.statements
    name = entity.name
    # Retire any active workflow tasks BEFORE the entity's metadata and tables
    # disappear (MAJOR-1). Otherwise they dangle in the inbox / admin workflow
    # views pointing at a staging table that no longer exists. This rides the same
    # ORM transaction as the entity deletion, so both commit atomically.
    terminated = workflow.terminate_tasks_for_entity(
        db, name, actor=principal.username, reason="entity deleted"
    )
    db.delete(entity)
    db.add(
        AuditEvent(actor=principal.username, actor_roles=principal.roles,
                   action="model_delete", entity_name=name, tier="meta",
                   detail={"dropped_tables": bool(dropped),
                           "workflow_tasks_terminated": terminated})
    )
    return {"entity": name, "deleted": True, "tables_dropped": dropped,
            "workflow_tasks_terminated": terminated}


# ------------------------------------------------------------- import / export
@router.get("/export/all")
def export_all(
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("model:read")),
    fmt: str = Query("json", pattern="^(json|yaml|yml)$"),
    include_runtime: bool = False,
):
    entities = db.query(Entity).order_by(Entity.name).all()
    content = export_models(entities, fmt=fmt, include_runtime=include_runtime)
    ext = "yaml" if fmt in ("yaml", "yml") else "json"
    media = "application/x-yaml" if ext == "yaml" else "application/json"
    return Response(
        content=content,
        media_type=media,
        headers={
            "Content-Disposition": f'attachment; filename="mdm-models.{ext}"'
        },
    )


@router.get("/{entity_name}/export")
def export_one(
    entity: Entity = Depends(get_entity),
    _: User = Depends(require_permission("model:read")),
    fmt: str = Query("json", pattern="^(json|yaml|yml)$"),
):
    content = export_models([entity], fmt=fmt)
    ext = "yaml" if fmt in ("yaml", "yml") else "json"
    return Response(
        content=content,
        media_type="application/x-yaml" if ext == "yaml" else "application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{entity.name}.{ext}"'
        },
    )


@router.post("/import")
async def import_models(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    principal: User = Depends(require_permission("model:write")),
    dry_run: bool = Query(True, description="Preview the diff without writing."),
    replace: bool = Query(False, description="Remove attributes absent from the file."),
):
    """Import entity definitions from a JSON or YAML file.

    Defaults to a dry run — you see the diff before anything is written.
    Importing never changes physical tables; publish separately.
    """
    raw = (await file.read()).decode("utf-8", errors="replace")
    try:
        doc = parse_document(raw)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    defs, errors = validate_document(doc)
    blocking = [e for e in errors if "warning" not in e.lower()]
    if blocking:
        raise HTTPException(
            status_code=422,
            detail={"message": "Import document failed validation.",
                    "errors": blocking},
        )

    diff = diff_against_existing(db, defs)
    if dry_run:
        return {
            "dry_run": True, "filename": file.filename,
            "entities": [d["name"] for d in defs],
            "diff": diff, "warnings": [e for e in errors if "warning" in e.lower()],
        }

    results = apply_import(
        db, [dict(d, attributes=list(d["attributes"])) for d in defs],
        actor=principal.username, replace=replace,
    )
    db.add(
        AuditEvent(actor=principal.username, actor_roles=principal.roles,
                   action="model_import", tier="meta",
                   detail={"filename": file.filename, "results": results})
    )
    return {
        "dry_run": False, "filename": file.filename, "results": results,
        "diff": diff,
        "next_step": "Publish each entity to apply the DDL to the database.",
    }


@router.get("/{entity_name}/versions")
def list_versions(
    entity: Entity = Depends(get_entity),
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("model:read")),
    limit: int = Query(50, le=200),
):
    rows = (
        db.query(ModelVersion)
        .filter(ModelVersion.entity_id == entity.id)
        .order_by(ModelVersion.version.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "version": r.version, "change_note": r.change_note,
            "created_at": r.created_at, "created_by": r.created_by,
            "has_ddl": bool(r.applied_ddl),
        }
        for r in rows
    ]


# ------------------------------------------------------------------- cluster
@router.get("/cluster/privileges", tags=["administration"])
def cluster_privileges(_: User = Depends(require_admin)):
    """Preflight check: can we actually create the objects we need?"""
    return bootstrap_svc.check_privileges()


@router.post("/cluster/bootstrap", tags=["administration"])
def cluster_bootstrap(
    _: User = Depends(require_admin),
    create_database: bool = Query(False),
):
    try:
        return bootstrap_svc.bootstrap(create_db=create_database)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
