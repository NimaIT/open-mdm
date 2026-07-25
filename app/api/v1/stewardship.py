"""Data steward workflow: the review queue and approval actions."""
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.deps import (
    check_entity_access,
    get_current_principal,
    get_deployed_entity,
    require_permission,
)
from app.config import settings
from app.db import get_db, get_engine
from app.models import Entity, PromotionBatch, User
from app.schemas.models import BulkReview, RejectDecision, ReviewDecision, StagingEdit
from app.services.identifiers import qualified, quote_ident
from app.services.pipeline import (
    PipelineError,
    SegregationOfDutiesError,
    apply_staging_to_live,
    edit_staging,
    promote_landing_to_staging,
    reject_staging,
)

router = APIRouter(prefix="/stewardship", tags=["stewardship"])

STAGING_META = [
    "mdm_staging_id", "mdm_landing_id", "mdm_operation", "mdm_target_id",
    "mdm_match_key", "mdm_source_system", "mdm_status", "mdm_errors",
    "mdm_is_valid", "mdm_change_type", "mdm_submitted_by", "mdm_submitted_at",
    "mdm_edited_by", "mdm_edited_at", "mdm_reviewed_by", "mdm_reviewed_at",
    "mdm_review_note", "mdm_supplied_fields",
]


def _staging_columns(entity: Entity) -> str:
    return ", ".join(
        quote_ident(c) for c in STAGING_META + [a.name for a in entity.attributes]
    )


@router.get("/queue")
def global_queue(
    db: Session = Depends(get_db),
    principal: User = Depends(require_permission("staging:read")),
    limit: int = Query(25, le=200),
):
    """Cross-entity work summary — the steward's landing page."""
    entities = db.query(Entity).filter(Entity.status.in_(["published", "modified"])).all()
    summary = []
    with get_engine().connect() as conn:
        for e in entities:
            t = qualified(settings.SCHEMA_STAGING, e.name)
            try:
                counts = conn.execute(
                    text(f"select mdm_status, count(*) from {t} group by 1")
                ).all()
                invalid = conn.execute(
                    text(f"select count(*) from {t} where not mdm_is_valid and "
                         "mdm_status in ('pending_review','changes_requested')")
                ).scalar()
            except Exception:
                continue
            by = {r[0]: r[1] for r in counts}
            pending = by.get("pending_review", 0) + by.get("changes_requested", 0)
            if pending or invalid:
                summary.append(
                    {
                        "entity": e.name,
                        "display_name": e.display_name,
                        "pending_review": pending,
                        "invalid": invalid,
                        "by_status": by,
                    }
                )
    return {
        "queues": sorted(summary, key=lambda s: -s["pending_review"]),
        "total_pending": sum(s["pending_review"] for s in summary),
    }


@router.get("/{entity_name}/queue")
def entity_queue(
    entity: Entity = Depends(get_deployed_entity),
    principal: User = Depends(require_permission("staging:read")),
    status_filter: str = Query("pending_review", alias="status"),
    only_invalid: bool = False,
    limit: int = Query(50, le=500),
    offset: int = Query(0, ge=0),
):
    """The review queue for one entity."""
    check_entity_access(entity, principal, "read")
    t = qualified(settings.SCHEMA_STAGING, entity.name)
    clauses, params = [], {"lim": limit, "off": offset}
    if status_filter and status_filter != "all":
        clauses.append("mdm_status = :st")
        params["st"] = status_filter
    if only_invalid:
        clauses.append("mdm_is_valid = false")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    with get_engine().connect() as conn:
        total = conn.execute(
            text(f"select count(*) from {t} {where}"),
            {k: v for k, v in params.items() if k not in ("lim", "off")},
        ).scalar()
        rows = conn.execute(
            text(f"select {_staging_columns(entity)} from {t} {where} "
                 "order by mdm_submitted_at asc limit :lim offset :off"),
            params,
        ).mappings().all()

    return {
        "entity": entity.name,
        "meta": {"total": total, "limit": limit, "offset": offset,
                 "has_more": offset + len(rows) < total},
        "data": [dict(r) for r in rows],
    }


@router.get("/{entity_name}/staging/{staging_id}")
def staging_detail(
    staging_id: int,
    entity: Entity = Depends(get_deployed_entity),
    principal: User = Depends(require_permission("staging:read")),
):
    """Incoming record beside the current golden values — the review view."""
    check_entity_access(entity, principal, "read")
    st = qualified(settings.SCHEMA_STAGING, entity.name)
    live = qualified(settings.SCHEMA_LIVE, entity.name)
    with get_engine().connect() as conn:
        row = conn.execute(
            text(f"select {_staging_columns(entity)} from {st} where mdm_staging_id=:i"),
            {"i": staging_id},
        ).mappings().first()
        if row is None:
            raise HTTPException(status_code=404,
                                detail=f"Staging record {staging_id} not found.")
        current = None
        if row["mdm_target_id"]:
            cols = ", ".join(
                quote_ident(c) for c in
                ["mdm_id", "mdm_version", "mdm_updated_at", "mdm_updated_by"]
                + [a.name for a in entity.attributes]
            )
            current = conn.execute(
                text(f"select {cols} from {live} where mdm_id = cast(:i as uuid)"),
                {"i": row["mdm_target_id"]},
            ).mappings().first()

    incoming = dict(row)
    diff = {}
    if current:
        supplied = incoming.get("mdm_supplied_fields") or []
        for a in entity.attributes:
            new, old = incoming.get(a.name), current[a.name]
            if a.name in supplied and str(new) != str(old):
                diff[a.name] = {"current": old, "incoming": new}

    return {
        "entity": entity.name,
        "staging": incoming,
        "current_golden_record": dict(current) if current else None,
        "diff": diff,
        "can_approve": bool(row["mdm_is_valid"])
        and row["mdm_status"] in ("pending_review", "changes_requested"),
        "blocked_reason": None if row["mdm_is_valid"]
        else "Record has unresolved validation errors — edit it to fix them first.",
    }


@router.patch("/{entity_name}/staging/{staging_id}")
def edit_staging_record(
    staging_id: int,
    payload: StagingEdit,
    entity: Entity = Depends(get_deployed_entity),
    db: Session = Depends(get_db),
    principal: User = Depends(require_permission("staging:edit")),
):
    """Steward polish: correct values on a staged record and re-validate."""
    check_entity_access(entity, principal, "write")
    try:
        with get_engine().begin() as conn:
            return edit_staging(
                db, conn, entity, staging_id, updates=payload.updates,
                actor=principal.username, actor_roles=principal.roles,
            )
    except PipelineError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{entity_name}/staging/{staging_id}/approve")
def approve_record(
    staging_id: int,
    payload: ReviewDecision = Body(default=ReviewDecision()),
    entity: Entity = Depends(get_deployed_entity),
    db: Session = Depends(get_db),
    principal: User = Depends(require_permission("staging:approve")),
):
    """Approve a staged change and apply it to the golden record."""
    check_entity_access(entity, principal, "write")
    try:
        with get_engine().begin() as conn:
            return apply_staging_to_live(
                db, conn, entity, staging_id, actor=principal.username,
                actor_roles=principal.roles, review_note=payload.note,
            )
    except SegregationOfDutiesError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail=str(exc)) from exc
    except PipelineError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{entity_name}/staging/{staging_id}/reject")
def reject_record(
    staging_id: int,
    payload: RejectDecision,
    entity: Entity = Depends(get_deployed_entity),
    db: Session = Depends(get_db),
    principal: User = Depends(require_permission("staging:reject")),
):
    check_entity_access(entity, principal, "write")
    try:
        with get_engine().begin() as conn:
            return reject_staging(
                db, conn, entity, staging_id, actor=principal.username,
                reason=payload.reason, actor_roles=principal.roles,
            )
    except PipelineError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{entity_name}/staging/bulk-approve")
def bulk_approve(
    payload: BulkReview,
    entity: Entity = Depends(get_deployed_entity),
    db: Session = Depends(get_db),
    principal: User = Depends(require_permission("staging:approve")),
):
    """Approve many records. Each is applied independently so one failure
    doesn't abort the rest."""
    check_entity_access(entity, principal, "write")
    applied, failed = [], []
    for sid in payload.staging_ids:
        try:
            with get_engine().begin() as conn:
                res = apply_staging_to_live(
                    db, conn, entity, sid, actor=principal.username,
                    actor_roles=principal.roles, review_note=payload.note,
                )
            applied.append(res)
        except (PipelineError, SegregationOfDutiesError) as exc:
            failed.append({"staging_id": sid, "error": str(exc)})
    return {
        "approved": len(applied), "failed": len(failed),
        "results": applied, "errors": failed,
    }


@router.post("/{entity_name}/staging/bulk-reject")
def bulk_reject(
    payload: BulkReview,
    entity: Entity = Depends(get_deployed_entity),
    db: Session = Depends(get_db),
    principal: User = Depends(require_permission("staging:reject")),
):
    check_entity_access(entity, principal, "write")
    done, failed = [], []
    for sid in payload.staging_ids:
        try:
            with get_engine().begin() as conn:
                done.append(
                    reject_staging(db, conn, entity, sid, actor=principal.username,
                                   reason=payload.note or "Bulk rejected",
                                   actor_roles=principal.roles)
                )
        except PipelineError as exc:
            failed.append({"staging_id": sid, "error": str(exc)})
    return {"rejected": len(done), "failed": len(failed), "errors": failed}


# --------------------------------------------------------------- pipeline ops
@router.post("/{entity_name}/promote")
def run_promotion(
    entity: Entity = Depends(get_deployed_entity),
    db: Session = Depends(get_db),
    principal: User = Depends(require_permission("pipeline:run")),
    limit: int = Query(1000, le=10000),
):
    """Manually run the landing -> staging pass (useful when auto-promote is off)."""
    with get_engine().begin() as conn:
        return promote_landing_to_staging(
            db, conn, entity, limit=limit, actor=principal.username
        )


@router.get("/{entity_name}/landing")
def landing_rows(
    entity: Entity = Depends(get_deployed_entity),
    principal: User = Depends(require_permission("staging:read")),
    status_filter: Optional[str] = Query(None, alias="status"),
    limit: int = Query(50, le=500),
    offset: int = Query(0, ge=0),
):
    """Inspect the raw landing tier — the audit trail of what was actually sent."""
    check_entity_access(entity, principal, "read")
    t = qualified(settings.SCHEMA_LANDING, entity.name)
    clauses, params = [], {"lim": limit, "off": offset}
    if status_filter:
        clauses.append("mdm_status = :st")
        params["st"] = status_filter
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with get_engine().connect() as conn:
        total = conn.execute(
            text(f"select count(*) from {t} {where}"),
            {k: v for k, v in params.items() if k not in ("lim", "off")},
        ).scalar()
        rows = conn.execute(
            text(f"select * from {t} {where} order by mdm_landing_id desc "
                 "limit :lim offset :off"),
            params,
        ).mappings().all()
    return {
        "entity": entity.name,
        "meta": {"total": total, "limit": limit, "offset": offset},
        "data": [dict(r) for r in rows],
    }


@router.get("/batches")
def list_batches(
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("staging:read")),
    entity_name: Optional[str] = None,
    limit: int = Query(50, le=200),
):
    q = db.query(PromotionBatch)
    if entity_name:
        q = q.filter(PromotionBatch.entity_name == entity_name)
    rows = q.order_by(PromotionBatch.started_at.desc()).limit(limit).all()
    return [
        {
            "id": str(r.id), "entity": r.entity_name, "stage": r.stage,
            "status": r.status, "rows_in": r.rows_in, "rows_ok": r.rows_ok,
            "rows_failed": r.rows_failed, "started_at": r.started_at,
            "finished_at": r.finished_at, "triggered_by": r.triggered_by,
            "error": r.error,
        }
        for r in rows
    ]
