"""Master-data write and read API.

The write verbs never touch the golden tables directly — every mutation is
deposited into the landing tier and flows through staging review. This is the
contract the whole application is built around.
"""
import csv
import io
import logging
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
    client_ip,
    get_deployed_entity,
    get_current_principal,
    require_entity_permission,
)
from app.config import settings
from app.db import get_db, get_engine
from app.models import AuditEvent, Entity, PromotionBatch, User
from app.schemas.models import BulkWrite
from app.services import notifications
from app.services.auth import effective_permissions
from app.services.identifiers import qualified, quote_ident
from app.services.pipeline import (
    OP_DELETE,
    OP_INSERT,
    OP_UPDATE,
    OP_UPSERT,
    SegregationOfDutiesError,
    apply_staging_to_live,
    promote_landing_to_staging,
    run_post_commit_hooks,
    write_to_landing,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/data", tags=["master data"])


def _auto_promote(
    db: Session, entity: Entity, actor: str, *,
    rationales: Optional[Dict[int, str]] = None,
    ip_address: Optional[str] = None,
) -> Optional[Dict]:
    """Immediately validate new landing rows into staging when configured."""
    if not settings.AUTO_PROMOTE_LANDING:
        return None
    with get_engine().begin() as conn:
        return promote_landing_to_staging(
            db, conn, entity, actor=actor, rationales=rationales,
            ip_address=ip_address,
        )


def _require_direct(principal: User, entity: Entity) -> None:
    """Guard the power-user bypass. Requesting a direct write without the
    data:write_direct permission is a 403 — this is what confines the bypass to
    power_user / admin and keeps service keys off it entirely. The permission may
    be held globally or conferred by a domain_roles grant for this entity's
    domain (effective_permissions), consistent with the entity-scoped gate."""
    if "data:write_direct" not in effective_permissions(principal, entity.domain):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Direct edits require the 'data:write_direct' permission "
                "(power_user or admin). Your write will still be accepted for "
                "steward review without ?direct=true."
            ),
        )


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
    direct: bool = False,
    rationale: Optional[str] = None,
    ip_address: Optional[str] = None,
) -> Dict:
    if direct:
        _require_direct(principal, entity)

    if settings.REQUIRE_SUBMIT_RATIONALE and not (rationale and rationale.strip()):
        raise HTTPException(
            status_code=422,
            detail="A submission rationale is required "
            "(REQUIRE_SUBMIT_RATIONALE is enabled).",
        )

    src = source_system or (
        principal.display_name if principal.source == "service" else None
    )
    applied: Optional[Dict] = None
    staged: Optional[Dict] = None
    apply_error: Optional[str] = None

    # -------------------------------------------------- tier 1: capture (always)
    # The inbound payload is ALWAYS committed to landing first, in its own
    # transaction, on both the normal and the direct path. Landing never rejects
    # and never loses a message: whatever happens downstream (validation,
    # promotion, or a failed direct apply) the captured row and its audit trail
    # survive.
    with get_engine().begin() as conn:
        landing = write_to_landing(
            conn, entity,
            operation=operation, payload=payload, target_id=target_id,
            source_system=src, submitted_by=principal.username,
            idempotency_key=idempotency_key,
        )

    # -------------------------------------------------- tier 1 -> 2: promotion
    rationales = (
        {landing["landing_id"]: rationale} if rationale else None
    )
    if not landing.get("deduplicated"):
        if direct:
            # Promote this row on its own, regardless of AUTO_PROMOTE_LANDING, so
            # the direct path always has a staging row to try to apply. Committed
            # in its own transaction so the staging row persists even if the
            # subsequent apply fails.
            staged = _promote_one(db, entity, landing["landing_id"],
                                  principal.username, rationale=rationale,
                                  ip_address=ip_address)
        else:
            promotion = _auto_promote(db, entity, principal.username,
                                      rationales=rationales, ip_address=ip_address)
            if promotion:
                for r in promotion.get("results", []):
                    if r.get("landing_id") == landing["landing_id"]:
                        staged = r
                        break

    # -------------------------------------------------- tier 2 -> 3: direct apply
    # Only on the direct path, and only for a VALID staged row. Runs in a SEPARATE
    # transaction from the landing capture and the promotion above: if the apply
    # fails for ANY reason (SoD, a DB constraint, a broken reference surfacing at
    # write time) the landing + staging rows remain persisted, the staging row is
    # left in its normal pending/error state for a steward, and the caller still
    # gets a 202 (captured) — never a 500 that discards the capture.
    if direct and staged and staged.get("is_valid") and staged.get("staging_id"):
        try:
            with get_engine().begin() as conn:
                applied = apply_staging_to_live(
                    db, conn, entity, staged["staging_id"],
                    actor=principal.username, actor_roles=principal.roles,
                    review_note="Direct edit (power-user auto-approve)",
                    enforce_sod=False, ip_address=ip_address,
                )
            # post_commit hooks fire AFTER the direct apply committed above.
            run_post_commit_hooks(entity, applied, actor=principal.username, db=db)
        except SegregationOfDutiesError:  # pragma: no cover
            # SoD is deliberately disabled on this path; never fatal.
            applied = None
        except Exception as exc:  # noqa: BLE001 - capture must survive any failure
            # The landing + staging rows are already committed; do not re-raise.
            log.warning(
                "Direct apply failed for %s staging_id=%s; row captured and left "
                "for steward review: %s",
                entity.name, staged.get("staging_id"), exc,
            )
            apply_error = str(exc)
            applied = None

    # A record that entered staging for review (and was not auto-applied on the
    # direct path) fires a 'submitted' notification to the domain's approvers,
    # AFTER landing + promotion committed above (never blocks the write path).
    if staged and staged.get("staging_id") and not applied:
        notifications.safe_enqueue(
            db, event=notifications.EVENT_SUBMITTED, entity=entity,
            staging_id=staged["staging_id"], actor=principal.username,
            change_type=staged.get("change_type"),
        )

    db.add(
        AuditEvent(
            actor=principal.username, actor_roles=principal.roles,
            action=f"api_{operation.lower()}" + ("_direct" if direct else ""),
            entity_name=entity.name,
            record_id=str((applied or {}).get("mdm_id") or target_id or "") or None,
            tier="live" if applied else "landing",
            detail={"landing_id": landing["landing_id"],
                    "staging_id": (staged or {}).get("staging_id"),
                    "direct": direct, "applied": bool(applied),
                    "apply_error": apply_error},
            after_value=payload,
            success=apply_error is None,
        )
    )
    result = {
        "accepted": True,
        "entity": entity.name,
        "operation": operation,
        "landing_id": landing["landing_id"],
        "deduplicated": landing.get("deduplicated", False),
        "staging_id": (staged or {}).get("staging_id"),
        "validation_passed": (staged or {}).get("is_valid"),
        "validation_errors": (staged or {}).get("errors", 0),
        "change_type": (applied or staged or {}).get("change_type"),
        "direct": direct,
        "applied": bool(applied),
        "mdm_id": (applied or {}).get("mdm_id"),
        "status": _write_status(entity, direct, applied, staged, apply_error),
        "message": _write_message(entity, direct, applied, staged, apply_error),
    }
    if apply_error:
        result["apply_error"] = apply_error
    return result


def _promote_one(db: Session, entity: Entity, landing_id: int,
                 actor: str, *, rationale: Optional[str] = None,
                 ip_address: Optional[str] = None) -> Optional[Dict]:
    """Promote a single landing row to staging in its own transaction.

    Used by the direct path so the staging row is durably committed before the
    apply attempt — an apply failure then cannot roll the staging row away.
    """
    with get_engine().begin() as conn:
        promotion = promote_landing_to_staging(
            db, conn, entity, landing_ids=[landing_id], actor=actor,
            rationales={landing_id: rationale} if rationale else None,
            ip_address=ip_address,
        )
    for r in promotion.get("results", []):
        if r.get("landing_id") == landing_id:
            return r
    return None


def _write_status(entity, direct, applied, staged, apply_error=None) -> str:
    if applied:
        return "applied"
    if direct and apply_error:
        # Captured and staged, but the direct auto-apply failed — a steward can
        # now review/retry it. Not a live write, not an outright rejection.
        return "captured_pending_review"
    if direct and staged and not staged.get("is_valid"):
        return "pending_review"  # invalid: cannot be forced to live
    return "pending_review" if entity.requires_approval else "auto"


def _write_message(entity, direct, applied, staged, apply_error=None) -> str:
    if applied:
        return (
            "Direct edit applied straight to the golden record "
            "(authorised power-user auto-approve)."
        )
    if direct and apply_error:
        return (
            "Change captured into the landing tier and staged, but the direct "
            "auto-apply could not complete — it was left in staging for steward "
            "review rather than lost."
        )
    if direct and staged and not staged.get("is_valid"):
        return (
            "Direct edit requested but the record has validation errors; it was "
            "held in staging for steward review rather than applied to live."
        )
    if entity.requires_approval:
        return "Change accepted into the landing tier and staged for steward review."
    return "Change accepted into the landing tier."


# ------------------------------------------------------------------ write verbs
@router.post("/{entity_name}", status_code=status.HTTP_202_ACCEPTED)
def create_record(
    request: Request,
    payload: Dict[str, Any] = Body(...),
    entity: Entity = Depends(get_deployed_entity),
    db: Session = Depends(get_db),
    principal: User = Depends(require_entity_permission("data:write", "write")),
    idempotency_key: Optional[str] = Query(None),
    source_system: Optional[str] = Query(None),
    rationale: Optional[str] = Query(
        None, description="Why this change is being submitted — recorded on the "
        "workflow task and its submit event (GC-1)."),
    direct: bool = Query(
        False,
        description="Power-user bypass: auto-approve a valid write straight to "
        "the golden tier. Requires data:write_direct (power_user/admin).",
    ),
):
    """Submit a new master-data record for review, or (with ?direct=true and the
    right permission) apply it straight to the golden record."""
    return _write(db, entity, principal, operation=OP_INSERT, payload=payload,
                  target_id=None, idempotency_key=idempotency_key,
                  source_system=source_system, direct=direct,
                  rationale=rationale, ip_address=client_ip(request))


@router.put("/{entity_name}/{record_id}", status_code=status.HTTP_202_ACCEPTED)
def replace_record(
    record_id: str,
    request: Request,
    payload: Dict[str, Any] = Body(...),
    entity: Entity = Depends(get_deployed_entity),
    db: Session = Depends(get_db),
    principal: User = Depends(require_entity_permission("data:write", "write")),
    idempotency_key: Optional[str] = Query(None),
    source_system: Optional[str] = Query(None),
    rationale: Optional[str] = Query(None, description="Submission rationale (GC-1)."),
    direct: bool = Query(False, description="Power-user direct-to-live bypass."),
):
    """Full update of an existing golden record, by mdm_id."""
    return _write(db, entity, principal, operation=OP_UPDATE, payload=payload,
                  target_id=record_id, idempotency_key=idempotency_key,
                  source_system=source_system, direct=direct,
                  rationale=rationale, ip_address=client_ip(request))


@router.patch("/{entity_name}/{record_id}", status_code=status.HTTP_202_ACCEPTED)
def patch_record(
    record_id: str,
    request: Request,
    payload: Dict[str, Any] = Body(...),
    entity: Entity = Depends(get_deployed_entity),
    db: Session = Depends(get_db),
    principal: User = Depends(require_entity_permission("data:write", "write")),
    idempotency_key: Optional[str] = Query(None),
    source_system: Optional[str] = Query(None),
    rationale: Optional[str] = Query(None, description="Submission rationale (GC-1)."),
    direct: bool = Query(False, description="Power-user direct-to-live bypass."),
):
    """Partial update — only the supplied fields are changed."""
    return _write(db, entity, principal, operation=OP_UPDATE, payload=payload,
                  target_id=record_id, idempotency_key=idempotency_key,
                  source_system=source_system, direct=direct,
                  rationale=rationale, ip_address=client_ip(request))


@router.put("/{entity_name}", status_code=status.HTTP_202_ACCEPTED)
def upsert_record(
    request: Request,
    payload: Dict[str, Any] = Body(...),
    entity: Entity = Depends(get_deployed_entity),
    db: Session = Depends(get_db),
    principal: User = Depends(require_entity_permission("data:write", "write")),
    idempotency_key: Optional[str] = Query(None),
    source_system: Optional[str] = Query(None),
    rationale: Optional[str] = Query(None, description="Submission rationale (GC-1)."),
    direct: bool = Query(False, description="Power-user direct-to-live bypass."),
):
    """Upsert by business key — the usual integration entry point."""
    return _write(db, entity, principal, operation=OP_UPSERT, payload=payload,
                  target_id=None, idempotency_key=idempotency_key,
                  source_system=source_system, direct=direct,
                  rationale=rationale, ip_address=client_ip(request))


@router.delete("/{entity_name}/{record_id}", status_code=status.HTTP_202_ACCEPTED)
def delete_record(
    record_id: str,
    request: Request,
    entity: Entity = Depends(get_deployed_entity),
    db: Session = Depends(get_db),
    principal: User = Depends(require_entity_permission("data:write", "write")),
    source_system: Optional[str] = Query(None),
    rationale: Optional[str] = Query(None, description="Submission rationale (GC-1)."),
    direct: bool = Query(False, description="Power-user direct-to-live bypass."),
):
    """Request deletion of a golden record (subject to steward approval)."""
    return _write(db, entity, principal, operation=OP_DELETE, payload={},
                  target_id=record_id, idempotency_key=None,
                  source_system=source_system, direct=direct,
                  rationale=rationale, ip_address=client_ip(request))


@router.post("/{entity_name}/bulk", status_code=status.HTTP_202_ACCEPTED)
def bulk_write(
    payload: BulkWrite,
    entity: Entity = Depends(get_deployed_entity),
    db: Session = Depends(get_db),
    principal: User = Depends(require_entity_permission("data:write", "write")),
):
    """Submit many records in one batch, tracked by a shared batch id."""
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
    principal: User = Depends(require_entity_permission("data:write", "write")),
    operation: str = Query(OP_UPSERT),
):
    """Bulk load from CSV — a very common MDM onboarding path."""
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
    principal: User = Depends(require_entity_permission("data:read", "read")),
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
    principal: User = Depends(require_entity_permission("data:read", "read")),
    include_deleted: bool = False,
):
    """Stream golden records as CSV."""
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
    principal: User = Depends(require_entity_permission("data:read", "read")),
):
    """Counts across all four tiers — the operational pulse of an entity."""
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


def _label_attrs(entity: Entity) -> List[str]:
    """Columns used to build a human label for a reference/FK dropdown option.

    Business-key attribute(s) first (the natural identity a steward recognises);
    failing that, the first text-ish attribute. Empty means fall back to mdm_id.
    """
    label = [a.name for a in entity.attributes if a.is_business_key]
    if label:
        return label
    text_attr = next(
        (a.name for a in entity.attributes
         if a.data_type in ("string", "text", "email", "url", "enum")),
        None,
    )
    return [text_attr] if text_attr else []


@router.get("/{entity_name}/options")
def list_options(
    entity: Entity = Depends(get_deployed_entity),
    principal: User = Depends(require_entity_permission("data:read", "read")),
    q: Optional[str] = Query(None, description="Type-ahead filter over the label."),
    limit: int = Query(20, le=100),
):
    """Reference/FK dropdown source: ``[{mdm_id, label}]`` for this entity.

    ``label`` is the business-key value(s) (or the first text attribute), so a
    steward picking a parent record sees a recognisable value rather than a UUID.
    Respects read access exactly like the list endpoint (require_entity_permission).
    """
    t = qualified(settings.SCHEMA_LIVE, entity.name)
    label_attrs = _label_attrs(entity)
    label_cols = [quote_ident(c) for c in label_attrs]

    clauses: List[str] = ["mdm_is_deleted = false"]
    params: Dict[str, Any] = {"lim": limit}
    if q and label_attrs:
        ors = " OR ".join(f"{quote_ident(c)}::text ILIKE :q" for c in label_attrs)
        clauses.append(f"({ors})")
        params["q"] = f"%{q}%"
    where = "WHERE " + " AND ".join(clauses)

    select_cols = "mdm_id" + ("".join(", " + c for c in label_cols))
    order_by = label_cols[0] if label_cols else "mdm_updated_at"
    with get_engine().connect() as conn:
        rows = conn.execute(
            text(f"SELECT {select_cols} FROM {t} {where} "
                 f"ORDER BY {order_by} LIMIT :lim"),
            params,
        ).mappings().all()

    out: List[Dict[str, Any]] = []
    for r in rows:
        if label_attrs:
            label = " · ".join(
                str(r[c]) for c in label_attrs if r[c] is not None
            )
        else:
            label = ""
        out.append({"mdm_id": str(r["mdm_id"]),
                    "label": label or str(r["mdm_id"])})
    return out


@router.get("/{entity_name}/{record_id}")
def get_record(
    record_id: str,
    entity: Entity = Depends(get_deployed_entity),
    principal: User = Depends(require_entity_permission("data:read", "read")),
):
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
    principal: User = Depends(require_entity_permission("data:read", "read")),
    limit: int = Query(100, le=500),
):
    """Full version history (lineage) of one golden record."""
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
