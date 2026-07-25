"""Master-data write and read API.

The write verbs never touch the golden tables directly — every mutation is
deposited into the landing tier and flows through staging review. This is the
contract the whole application is built around.
"""
import csv
import io
import uuid
from typing import Any, Dict, List, Optional

from fastapi import (
    APIRouter,
    Body,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.deps import (
    check_entity_access,
    get_deployed_entity,
    get_current_principal,
    require_permission,
)
from app.config import settings
from app.db import get_db, get_engine
from app.models import AuditEvent, Entity, PromotionBatch, User
from app.schemas.models import BulkWrite
from app.services.identifiers import qualified, quote_ident
from app.services.pipeline import (
    OP_DELETE,
    OP_INSERT,
    OP_UPDATE,
    OP_UPSERT,
    promote_landing_to_staging,
    write_to_landing,
)

router = APIRouter(prefix="/data", tags=["master data"])


def _auto_promote(db: Session, entity: Entity, actor: str) -> Optional[Dict]:
    """Immediately validate new landing rows into staging when configured."""
    if not settings.AUTO_PROMOTE_LANDING:
        return None
    with get_engine().begin() as conn:
        return promote_landing_to_staging(db, conn, entity, actor=actor)


def _write(
    db: Session,
    entity: Entity,
    principal: User,
    *,
    operation: str,
    payload: Dict[str, Any],
    target_id: Optional[str],
    idempotency_key: Optional[str],
    source_system: Optional[str],
) -> Dict:
    check_entity_access(entity, principal, "write")
    with get_engine().begin() as conn:
        landing = write_to_landing(
            conn, entity,
            operation=operation, payload=payload, target_id=target_id,
            source_system=source_system
            or (principal.display_name if principal.source == "service" else None),
            submitted_by=principal.username,
            idempotency_key=idempotency_key,
        )
    promotion = None
    if not landing.get("deduplicated"):
        promotion = _auto_promote(db, entity, principal.username)

    staged = None
    if promotion:
        for r in promotion.get("results", []):
            if r.get("landing_id") == landing["landing_id"]:
                staged = r
                break

    db.add(
        AuditEvent(
            actor=principal.username, actor_roles=principal.roles,
            action=f"api_{operation.lower()}", entity_name=entity.name,
            record_id=str(target_id) if target_id else None, tier="landing",
            detail={"landing_id": landing["landing_id"],
                    "staging_id": (staged or {}).get("staging_id")},
            after_value=payload,
        )
    )
    return {
        "accepted": True,
        "entity": entity.name,
        "operation": operation,
        "landing_id": landing["landing_id"],
        "deduplicated": landing.get("deduplicated", False),
        "staging_id": (staged or {}).get("staging_id"),
        "validation_passed": (staged or {}).get("is_valid"),
        "validation_errors": (staged or {}).get("errors", 0),
        "change_type": (staged or {}).get("change_type"),
        "status": "pending_review" if entity.requires_approval else "auto",
        "message": (
            "Change accepted into the landing tier and staged for steward review."
            if entity.requires_approval
            else "Change accepted into the landing tier."
        ),
    }


# ------------------------------------------------------------------ write verbs
@router.post("/{entity_name}", status_code=status.HTTP_202_ACCEPTED)
def create_record(
    payload: Dict[str, Any] = Body(...),
    entity: Entity = Depends(get_deployed_entity),
    db: Session = Depends(get_db),
    principal: User = Depends(require_permission("data:write")),
    idempotency_key: Optional[str] = Query(None),
    source_system: Optional[str] = Query(None),
):
    """Submit a new master-data record for review (never writes live directly)."""
    return _write(db, entity, principal, operation=OP_INSERT, payload=payload,
                  target_id=None, idempotency_key=idempotency_key,
                  source_system=source_system)


@router.put("/{entity_name}/{record_id}", status_code=status.HTTP_202_ACCEPTED)
def replace_record(
    record_id: str,
    payload: Dict[str, Any] = Body(...),
    entity: Entity = Depends(get_deployed_entity),
    db: Session = Depends(get_db),
    principal: User = Depends(require_permission("data:write")),
    idempotency_key: Optional[str] = Query(None),
    source_system: Optional[str] = Query(None),
):
    """Full update of an existing golden record, by mdm_id."""
    return _write(db, entity, principal, operation=OP_UPDATE, payload=payload,
                  target_id=record_id, idempotency_key=idempotency_key,
                  source_system=source_system)


@router.patch("/{entity_name}/{record_id}", status_code=status.HTTP_202_ACCEPTED)
def patch_record(
    record_id: str,
    payload: Dict[str, Any] = Body(...),
    entity: Entity = Depends(get_deployed_entity),
    db: Session = Depends(get_db),
    principal: User = Depends(require_permission("data:write")),
    idempotency_key: Optional[str] = Query(None),
    source_system: Optional[str] = Query(None),
):
    """Partial update — only the supplied fields are changed."""
    return _write(db, entity, principal, operation=OP_UPDATE, payload=payload,
                  target_id=record_id, idempotency_key=idempotency_key,
                  source_system=source_system)


@router.put("/{entity_name}", status_code=status.HTTP_202_ACCEPTED)
def upsert_record(
    payload: Dict[str, Any] = Body(...),
    entity: Entity = Depends(get_deployed_entity),
    db: Session = Depends(get_db),
    principal: User = Depends(require_permission("data:write")),
    idempotency_key: Optional[str] = Query(None),
    source_system: Optional[str] = Query(None),
):
    """Upsert by business key — the usual integration entry point."""
    return _write(db, entity, principal, operation=OP_UPSERT, payload=payload,
                  target_id=None, idempotency_key=idempotency_key,
                  source_system=source_system)


@router.delete("/{entity_name}/{record_id}", status_code=status.HTTP_202_ACCEPTED)
def delete_record(
    record_id: str,
    entity: Entity = Depends(get_deployed_entity),
    db: Session = Depends(get_db),
    principal: User = Depends(require_permission("data:write")),
    source_system: Optional[str] = Query(None),
):
    """Request deletion of a golden record (subject to steward approval)."""
    return _write(db, entity, principal, operation=OP_DELETE, payload={},
                  target_id=record_id, idempotency_key=None,
                  source_system=source_system)


@router.post("/{entity_name}/bulk", status_code=status.HTTP_202_ACCEPTED)
def bulk_write(
    payload: BulkWrite,
    entity: Entity = Depends(get_deployed_entity),
    db: Session = Depends(get_db),
    principal: User = Depends(require_permission("data:write")),
):
    """Submit many records in one batch, tracked by a shared batch id."""
    check_entity_access(entity, principal, "write")
    if len(payload.records) > settings.MAX_BULK_ROWS:
        raise HTTPException(
            status_code=413,
            detail=f"Batch exceeds the {settings.MAX_BULK_ROWS}-row limit.",
        )
    if payload.operation not in (OP_INSERT, OP_UPDATE, OP_UPSERT, OP_DELETE):
        raise HTTPException(status_code=422,
                            detail=f"Unsupported operation '{payload.operation}'.")

    batch_id = uuid.uuid4()
    ids: List[int] = []
    with get_engine().begin() as conn:
        for rec in payload.records:
            r = write_to_landing(
                conn, entity, operation=payload.operation, payload=rec,
                target_id=rec.get("mdm_id"), source_system=payload.source_system,
                submitted_by=principal.username, batch_id=batch_id,
            )
            ids.append(r["landing_id"])

    promotion = _auto_promote(db, entity, principal.username)
    valid = sum(1 for r in (promotion or {}).get("results", []) if r.get("is_valid"))
    invalid = len((promotion or {}).get("results", [])) - valid
    return {
        "accepted": True, "entity": entity.name, "batch_id": str(batch_id),
        "rows_received": len(ids), "landing_ids": ids[:100],
        "staged_valid": valid, "staged_invalid": invalid,
        "message": "Batch landed. Invalid rows are staged with errors for steward review.",
    }


@router.post("/{entity_name}/import-csv", status_code=status.HTTP_202_ACCEPTED)
async def import_csv(
    file: UploadFile = File(...),
    entity: Entity = Depends(get_deployed_entity),
    db: Session = Depends(get_db),
    principal: User = Depends(require_permission("data:write")),
    operation: str = Query(OP_UPSERT),
):
    """Bulk load from CSV — a very common MDM onboarding path."""
    check_entity_access(entity, principal, "write")
    raw = (await file.read()).decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(raw))
    if not reader.fieldnames:
        raise HTTPException(status_code=422, detail="CSV has no header row.")

    known = {a.name for a in entity.attributes}
    unknown = [f for f in reader.fieldnames if f and f not in known and f != "mdm_id"]

    batch_id = uuid.uuid4()
    count = 0
    with get_engine().begin() as conn:
        for row in reader:
            if count >= settings.MAX_BULK_ROWS:
                break
            clean = {k: v for k, v in row.items() if k in known and v != ""}
            if not clean:
                continue
            write_to_landing(
                conn, entity, operation=operation, payload=clean,
                target_id=row.get("mdm_id") or None,
                source_system=f"csv:{file.filename}",
                submitted_by=principal.username, batch_id=batch_id,
            )
            count += 1

    promotion = _auto_promote(db, entity, principal.username)
    return {
        "accepted": True, "filename": file.filename, "rows_loaded": count,
        "batch_id": str(batch_id),
        "ignored_columns": unknown,
        "staged": (promotion or {}).get("promoted", 0),
        "message": "CSV landed and staged for review.",
    }


# ------------------------------------------------------------------- read verbs
def _select_columns(entity: Entity) -> str:
    cols = ["mdm_id", "mdm_version", "mdm_created_at", "mdm_updated_at",
            "mdm_created_by", "mdm_updated_by", "mdm_is_deleted",
            "mdm_source_system"]
    cols += [a.name for a in entity.attributes]
    return ", ".join(quote_ident(c) for c in cols)


@router.get("/{entity_name}")
def list_records(
    request: Request,
    entity: Entity = Depends(get_deployed_entity),
    principal: User = Depends(require_permission("data:read")),
    limit: int = Query(50, le=1000),
    offset: int = Query(0, ge=0),
    q: Optional[str] = Query(None, description="Free-text search across text fields."),
    include_deleted: bool = False,
    sort: Optional[str] = Query(None),
    order: str = Query("asc", pattern="^(asc|desc)$"),
):
    """Query golden records with filtering, search, sorting and pagination.

    Any entity attribute can be used as an exact-match filter via a query
    parameter of the same name.
    """
    check_entity_access(entity, principal, "read")
    t = qualified(settings.SCHEMA_LIVE, entity.name)
    attr_names = {a.name: a for a in entity.attributes}

    clauses: List[str] = []
    params: Dict[str, Any] = {"lim": limit, "off": offset}
    if not include_deleted:
        clauses.append("mdm_is_deleted = false")

    reserved = {"limit", "offset", "q", "include_deleted", "sort", "order"}
    for key, value in request.query_params.items():
        if key in reserved or key not in attr_names:
            continue
        clauses.append(f"{quote_ident(key)}::text = :f_{key}")
        params[f"f_{key}"] = value

    if q:
        text_cols = [
            a.name for a in entity.attributes
            if a.data_type in ("string", "text", "email", "url", "enum")
        ]
        if text_cols:
            ors = " OR ".join(
                f"{quote_ident(c)} ILIKE :q" for c in text_cols
            )
            clauses.append(f"({ors})")
            params["q"] = f"%{q}%"

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    order_by = "mdm_updated_at DESC"
    if sort and sort in attr_names:
        order_by = f"{quote_ident(sort)} {'DESC' if order == 'desc' else 'ASC'}"
    elif sort and sort.startswith("mdm_"):
        order_by = f"{quote_ident(sort)} {'DESC' if order == 'desc' else 'ASC'}"

    with get_engine().connect() as conn:
        total = conn.execute(
            text(f"SELECT count(*) FROM {t} {where}"),
            {k: v for k, v in params.items() if k not in ("lim", "off")},
        ).scalar()
        rows = conn.execute(
            text(
                f"SELECT {_select_columns(entity)} FROM {t} {where} "
                f"ORDER BY {order_by} LIMIT :lim OFFSET :off"
            ),
            params,
        ).mappings().all()

    return {
        "meta": {"total": total, "limit": limit, "offset": offset,
                 "has_more": offset + len(rows) < total},
        "data": [dict(r) for r in rows],
    }


@router.get("/{entity_name}/export-csv")
def export_csv(
    entity: Entity = Depends(get_deployed_entity),
    principal: User = Depends(require_permission("data:read")),
    include_deleted: bool = False,
):
    """Stream golden records as CSV."""
    check_entity_access(entity, principal, "read")
    t = qualified(settings.SCHEMA_LIVE, entity.name)
    where = "" if include_deleted else "WHERE mdm_is_deleted = false"
    with get_engine().connect() as conn:
        rows = conn.execute(
            text(f"SELECT {_select_columns(entity)} FROM {t} {where} ORDER BY mdm_id")
        ).mappings().all()

    buf = io.StringIO()
    if rows:
        writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        for r in rows:
            writer.writerow({k: ("" if v is None else v) for k, v in r.items()})
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{entity.name}.csv"'
        },
    )


@router.get("/{entity_name}/statistics")
def entity_statistics(
    entity: Entity = Depends(get_deployed_entity),
    principal: User = Depends(require_permission("data:read")),
):
    """Counts across all four tiers — the operational pulse of an entity."""
    check_entity_access(entity, principal, "read")
    live = qualified(settings.SCHEMA_LIVE, entity.name)
    staging = qualified(settings.SCHEMA_STAGING, entity.name)
    landing = qualified(settings.SCHEMA_LANDING, entity.name)
    hist = qualified(settings.SCHEMA_HISTORY, entity.name)
    with get_engine().connect() as conn:
        stats = {
            "live_active": conn.execute(
                text(f"select count(*) from {live} where not mdm_is_deleted")).scalar(),
            "live_deleted": conn.execute(
                text(f"select count(*) from {live} where mdm_is_deleted")).scalar(),
            "history_versions": conn.execute(
                text(f"select count(*) from {hist}")).scalar(),
            "landing_pending": conn.execute(
                text(f"select count(*) from {landing} where mdm_status='pending'")).scalar(),
            "landing_total": conn.execute(
                text(f"select count(*) from {landing}")).scalar(),
        }
        by_status = conn.execute(
            text(f"select mdm_status, count(*) from {staging} group by 1")
        ).all()
        stats["staging_by_status"] = {r[0]: r[1] for r in by_status}
        stats["staging_invalid"] = conn.execute(
            text(f"select count(*) from {staging} where not mdm_is_valid "
                 "and mdm_status not in ('applied','rejected')")
        ).scalar()
    return {"entity": entity.name, "statistics": stats}


@router.get("/{entity_name}/{record_id}")
def get_record(
    record_id: str,
    entity: Entity = Depends(get_deployed_entity),
    principal: User = Depends(require_permission("data:read")),
):
    check_entity_access(entity, principal, "read")
    t = qualified(settings.SCHEMA_LIVE, entity.name)
    with get_engine().connect() as conn:
        try:
            row = conn.execute(
                text(f"SELECT {_select_columns(entity)} FROM {t} "
                     "WHERE mdm_id = cast(:id as uuid)"),
                {"id": record_id},
            ).mappings().first()
        except Exception as exc:
            raise HTTPException(status_code=422,
                                detail=f"Invalid record id: {exc}") from exc
    if row is None:
        raise HTTPException(status_code=404, detail=f"Record '{record_id}' not found.")
    return dict(row)


@router.get("/{entity_name}/{record_id}/history")
def record_history(
    record_id: str,
    entity: Entity = Depends(get_deployed_entity),
    principal: User = Depends(require_permission("data:read")),
    limit: int = Query(100, le=500),
):
    """Full version history (lineage) of one golden record."""
    check_entity_access(entity, principal, "read")
    hist = qualified(settings.SCHEMA_HISTORY, entity.name)
    cols = ", ".join(
        quote_ident(c) for c in
        ["mdm_history_id", "mdm_version", "mdm_change_type", "mdm_valid_from",
         "mdm_valid_to", "mdm_changed_by", "mdm_is_deleted"]
        + [a.name for a in entity.attributes]
    )
    with get_engine().connect() as conn:
        rows = conn.execute(
            text(f"SELECT {cols} FROM {hist} WHERE mdm_id = cast(:id as uuid) "
                 "ORDER BY mdm_version DESC LIMIT :lim"),
            {"id": record_id, "lim": limit},
        ).mappings().all()
    return {"record_id": record_id, "versions": [dict(r) for r in rows]}
