"""Data steward workflow: the review queue and approval actions."""
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.deps import (
    client_ip,
    get_current_principal,
    get_deployed_entity,
    require_entity_permission,
    require_permission,
)
from app.config import settings
from app.db import get_db, get_engine
from app.models import Entity, PromotionBatch, User, WorkflowEvent, WorkflowTask
from app.schemas.models import (
    BulkReview,
    RejectDecision,
    RequestChangesDecision,
    ReviewDecision,
    StagingEdit,
)
from app.services import notifications, workflow
from app.services.auth import (
    can_access_entity,
    effective_permissions,
    permissions_for,
)
from app.services.identifiers import qualified, quote_ident
from app.services.pipeline import (
    PipelineError,
    SegregationOfDutiesError,
    apply_staging_to_live,
    edit_staging,
    promote_landing_to_staging,
    reject_staging,
    run_post_commit_hooks,
)
from app.services.references import reresolve_broken_references


def _require_review_comment(note: Optional[str]) -> None:
    """Enforce the mandatory review-comment policy (GC-4).

    When ``REQUIRE_REVIEW_COMMENTS`` is on, an approval/rejection decision must
    carry a non-empty comment so the workflow history records *why*.
    """
    if settings.REQUIRE_REVIEW_COMMENTS and not (note and note.strip()):
        raise HTTPException(
            status_code=422,
            detail="A review comment is required for this decision "
            "(REQUIRE_REVIEW_COMMENTS is enabled).",
        )


def _can_review_task(principal: User, entity_name: str,
                     domain: Optional[str]) -> bool:
    """Whether a principal may see/act on a change request in ``domain``.

    Honours W2 domain conferral: staging:read must be held either globally or via
    a domain_roles grant for this domain, and the restriction layer must not
    exclude the entity."""
    return (
        "staging:read" in effective_permissions(principal, domain)
        and can_access_entity(principal, entity_name, "read", entity_domain=domain)
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
    principal: User = Depends(get_current_principal),
    limit: int = Query(25, le=200),
):
    """Cross-entity work summary — the steward's landing page.

    Honours W2 domain conferral: a reviewer whose ``staging:read`` comes only
    from a ``domain_roles`` grant sees the queues for entities in that domain
    (not a global 403). Entities the principal cannot review are omitted.
    """
    # Preserve the "no staging:read anywhere ⇒ 403" invariant, while allowing a
    # reviewer whose grant is domain-scoped. Aggregate global roles ∪ every
    # domain_roles grant; if that union can't read staging at all, refuse.
    _droles = getattr(principal, "domain_roles", None) or {}
    _agg_roles = list(principal.roles or []) + [
        r for rs in _droles.values() for r in (rs or [])
    ]
    if "staging:read" not in permissions_for(_agg_roles):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permission 'staging:read' is required.",
        )
    entities = db.query(Entity).filter(Entity.status.in_(["published", "modified"])).all()
    entities = [e for e in entities if _can_review_task(principal, e.name, e.domain)]
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
    principal: User = Depends(require_entity_permission("staging:read", "read")),
    status_filter: str = Query("pending_review", alias="status"),
    only_invalid: bool = False,
    limit: int = Query(50, le=500),
    offset: int = Query(0, ge=0),
):
    """The review queue for one entity."""
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
    principal: User = Depends(require_entity_permission("staging:read", "read")),
):
    """Incoming record beside the current golden values — the review view."""
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
    request: Request,
    entity: Entity = Depends(get_deployed_entity),
    db: Session = Depends(get_db),
    principal: User = Depends(require_entity_permission("staging:edit", "edit")),
):
    """Steward polish: correct values on a staged record and re-validate."""
    try:
        with get_engine().begin() as conn:
            return edit_staging(
                db, conn, entity, staging_id, updates=payload.updates,
                actor=principal.username, actor_roles=principal.roles,
                ip_address=client_ip(request),
            )
    except PipelineError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{entity_name}/staging/{staging_id}/approve")
def approve_record(
    staging_id: int,
    request: Request,
    payload: ReviewDecision = Body(default=ReviewDecision()),
    entity: Entity = Depends(get_deployed_entity),
    db: Session = Depends(get_db),
    principal: User = Depends(require_entity_permission("staging:approve", "approve")),
):
    """Approve a staged change and apply it to the golden record."""
    _require_review_comment(payload.note)
    try:
        with get_engine().begin() as conn:
            result = apply_staging_to_live(
                db, conn, entity, staging_id, actor=principal.username,
                actor_roles=principal.roles, review_note=payload.note,
                ip_address=client_ip(request),
            )
    except SegregationOfDutiesError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail=str(exc)) from exc
    except PipelineError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # EX-1: post_commit hooks fire AFTER the apply transaction above committed, so
    # a chaining hook sees the committed golden record. Swallowed + isolated.
    run_post_commit_hooks(entity, result, actor=principal.username, db=db)
    # Notify the submitter AFTER the decision committed (never blocks approval).
    notifications.safe_enqueue(
        db, event=notifications.EVENT_APPROVED, entity=entity,
        staging_id=staging_id, actor=principal.username, comment=payload.note,
        change_type=result.get("change_type"), record_id=result.get("mdm_id"),
    )
    return result


@router.post("/{entity_name}/staging/{staging_id}/reject")
def reject_record(
    staging_id: int,
    payload: RejectDecision,
    request: Request,
    entity: Entity = Depends(get_deployed_entity),
    db: Session = Depends(get_db),
    principal: User = Depends(require_entity_permission("staging:reject", "reject")),
):
    """Reject a staged change — terminal, fully discarded, zero golden impact."""
    try:
        with get_engine().begin() as conn:
            result = reject_staging(
                db, conn, entity, staging_id, actor=principal.username,
                reason=payload.reason, actor_roles=principal.roles,
                ip_address=client_ip(request),
            )
    except PipelineError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    notifications.safe_enqueue(
        db, event=notifications.EVENT_REJECTED, entity=entity,
        staging_id=staging_id, actor=principal.username, comment=payload.reason,
    )
    return result


@router.post("/{entity_name}/staging/{staging_id}/request-changes")
def request_changes_record(
    staging_id: int,
    payload: RequestChangesDecision,
    request: Request,
    entity: Entity = Depends(get_deployed_entity),
    db: Session = Depends(get_db),
    principal: User = Depends(require_entity_permission("staging:reject", "reject")),
):
    """Send a change request back to its submitter with a mandatory comment (GC-1).

    The staging row stays editable; the submitter's next edit returns it to
    ``pending_review``."""
    try:
        with get_engine().begin() as conn:
            result = workflow.request_changes(
                db, conn, entity, staging_id, actor=principal.username,
                comment=payload.comment, actor_roles=principal.roles,
                ip_address=client_ip(request),
            )
    except workflow.WorkflowError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    notifications.safe_enqueue(
        db, event=notifications.EVENT_CHANGES_REQUESTED, entity=entity,
        staging_id=staging_id, actor=principal.username, comment=payload.comment,
    )
    return result


@router.post("/{entity_name}/staging/{staging_id}/claim")
def claim_record(
    staging_id: int,
    request: Request,
    entity: Entity = Depends(get_deployed_entity),
    db: Session = Depends(get_db),
    principal: User = Depends(require_entity_permission("staging:edit", "edit")),
):
    """Take ownership of a change request so other reviewers see it as claimed."""
    try:
        return workflow.claim(
            db, entity, staging_id, actor=principal.username,
            actor_roles=principal.roles, ip_address=client_ip(request),
        )
    except workflow.WorkflowConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail=str(exc)) from exc
    except workflow.WorkflowError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{entity_name}/staging/{staging_id}/release")
def release_record(
    staging_id: int,
    request: Request,
    entity: Entity = Depends(get_deployed_entity),
    db: Session = Depends(get_db),
    principal: User = Depends(require_entity_permission("staging:edit", "edit")),
):
    """Give up ownership of a change request you previously claimed."""
    try:
        return workflow.release(
            db, entity, staging_id, actor=principal.username,
            actor_roles=principal.roles, ip_address=client_ip(request),
        )
    except workflow.WorkflowConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail=str(exc)) from exc
    except workflow.WorkflowError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{entity_name}/staging/{staging_id}/workflow")
def staging_workflow_history(
    staging_id: int,
    entity: Entity = Depends(get_deployed_entity),
    db: Session = Depends(get_db),
    principal: User = Depends(require_entity_permission("staging:read", "read")),
):
    """The complete, ordered decision chain for one change request (GC-5).

    Returns every step — submit, claim, edit, request_changes, approve, reject,
    terminate — with actor, comment, from/to status and timestamp, as one
    coherent chain (never fragmented across record ids)."""
    task = workflow.get_task(db, entity.name, staging_id)
    if task is None:
        raise HTTPException(
            status_code=404,
            detail=f"No workflow task for staging record {staging_id}.",
        )
    events = (
        db.query(WorkflowEvent)
        .filter(WorkflowEvent.task_id == task.id)
        .order_by(WorkflowEvent.seq)
        .all()
    )
    return {
        "entity": entity.name,
        "task": workflow.task_to_dict(task),
        "history": [workflow.event_to_dict(e) for e in events],
    }


# ------------------------------------------------------------------- inbox
def _accessible_active_tasks(db: Session, principal: User) -> List[WorkflowTask]:
    """Active (non-terminal) tasks the principal is allowed to review (GC-7)."""
    tasks = (
        db.query(WorkflowTask)
        .filter(WorkflowTask.status.in_(list(workflow.ACTIVE_STATUSES)))
        .order_by(WorkflowTask.priority.desc(), WorkflowTask.created_at.asc())
        .all()
    )
    # Defence-in-depth (MAJOR-1): skip tasks whose entity has since been deleted —
    # their staging table is gone, so opening them would error. Entity cleanup
    # already terminates such tasks, but this guards any that slip through.
    existing = {name for (name,) in db.query(Entity.name).all()}
    return [
        t for t in tasks
        if t.entity_name in existing
        and _can_review_task(principal, t.entity_name, t.domain)
    ]


def _inbox_counts(principal: User, tasks: List[WorkflowTask]) -> Dict[str, int]:
    me = principal.username
    assigned = [t for t in tasks if me in (t.claimed_by, t.assigned_to)]
    unassigned = [t for t in tasks if not t.claimed_by and not t.assigned_to]
    changes = [t for t in tasks if t.status == workflow.CHANGES_REQUESTED]
    return {
        "assigned_to_me": len(assigned),
        "unassigned": len(unassigned),
        "changes_requested": len(changes),
        "total_pending": len(tasks),
    }


@router.get("/inbox")
def inbox(
    db: Session = Depends(get_db),
    principal: User = Depends(get_current_principal),
    limit: int = Query(100, le=500),
):
    """Per-user work inbox: tasks assigned to me plus the unassigned pool (GC-7).

    Domain-scoped: a reviewer only sees tasks for domains/entities they can
    access. Terminal (rejected/terminated/applied) tasks never appear."""
    tasks = _accessible_active_tasks(db, principal)
    me = principal.username
    now = datetime.now(timezone.utc)

    def _age(t: WorkflowTask) -> float:
        created = t.created_at
        if created is None:
            return 0.0
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return (now - created).total_seconds()

    assigned = [t for t in tasks if me in (t.claimed_by, t.assigned_to)]
    unassigned = [t for t in tasks if not t.claimed_by and not t.assigned_to]
    return {
        "counts": _inbox_counts(principal, tasks),
        "assigned_to_me": [
            workflow.task_to_dict(t, age_seconds=_age(t)) for t in assigned[:limit]
        ],
        "unassigned": [
            workflow.task_to_dict(t, age_seconds=_age(t)) for t in unassigned[:limit]
        ],
    }


@router.get("/inbox/counts")
def inbox_counts(
    db: Session = Depends(get_db),
    principal: User = Depends(get_current_principal),
):
    """Badge counts for the nav: assigned_to_me / unassigned / changes_requested /
    total_pending (GC-7)."""
    tasks = _accessible_active_tasks(db, principal)
    return _inbox_counts(principal, tasks)


@router.post("/{entity_name}/staging/bulk-approve")
def bulk_approve(
    payload: BulkReview,
    request: Request,
    entity: Entity = Depends(get_deployed_entity),
    db: Session = Depends(get_db),
    principal: User = Depends(require_entity_permission("staging:approve", "approve")),
):
    """Approve many records. Each is applied independently so one failure
    doesn't abort the rest."""
    _require_review_comment(payload.note)
    ip = client_ip(request)
    applied, failed = [], []
    for sid in payload.staging_ids:
        try:
            with get_engine().begin() as conn:
                res = apply_staging_to_live(
                    db, conn, entity, sid, actor=principal.username,
                    actor_roles=principal.roles, review_note=payload.note,
                    ip_address=ip,
                )
            # post_commit hooks run per row AFTER that row's apply committed.
            run_post_commit_hooks(entity, res, actor=principal.username, db=db)
            applied.append(res)
            notifications.safe_enqueue(
                db, event=notifications.EVENT_APPROVED, entity=entity,
                staging_id=sid, actor=principal.username, comment=payload.note,
                change_type=res.get("change_type"), record_id=res.get("mdm_id"),
            )
        except (PipelineError, SegregationOfDutiesError) as exc:
            failed.append({"staging_id": sid, "error": str(exc)})
    return {
        "approved": len(applied), "failed": len(failed),
        "results": applied, "errors": failed,
    }


@router.post("/{entity_name}/staging/bulk-reject")
def bulk_reject(
    payload: BulkReview,
    request: Request,
    entity: Entity = Depends(get_deployed_entity),
    db: Session = Depends(get_db),
    principal: User = Depends(require_entity_permission("staging:reject", "reject")),
):
    ip = client_ip(request)
    done, failed = [], []
    for sid in payload.staging_ids:
        try:
            with get_engine().begin() as conn:
                done.append(
                    reject_staging(db, conn, entity, sid, actor=principal.username,
                                   reason=payload.note or "Bulk rejected",
                                   actor_roles=principal.roles, ip_address=ip)
                )
            notifications.safe_enqueue(
                db, event=notifications.EVENT_REJECTED, entity=entity,
                staging_id=sid, actor=principal.username,
                comment=payload.note or "Bulk rejected",
            )
        except PipelineError as exc:
            failed.append({"staging_id": sid, "error": str(exc)})
    return {"rejected": len(done), "failed": len(failed), "errors": failed}


# --------------------------------------------------------------- pipeline ops
@router.post("/{entity_name}/promote")
def run_promotion(
    request: Request,
    entity: Entity = Depends(get_deployed_entity),
    db: Session = Depends(get_db),
    principal: User = Depends(require_entity_permission("pipeline:run", "write")),
    limit: int = Query(1000, le=10000),
    rationale: Optional[str] = Query(
        None, description="Submission rationale attached to every task created "
        "in this promotion run (GC-1)."),
):
    """Manually run the landing -> staging pass (useful when auto-promote is off)."""
    with get_engine().begin() as conn:
        result = promote_landing_to_staging(
            db, conn, entity, limit=limit, actor=principal.username,
            rationale=rationale, ip_address=client_ip(request),
        )
    # One 'submitted' notification per freshly-staged row, AFTER promotion committed.
    for r in result.get("results", []):
        if r.get("staging_id"):
            notifications.safe_enqueue(
                db, event=notifications.EVENT_SUBMITTED, entity=entity,
                staging_id=r["staging_id"], actor=principal.username,
                change_type=r.get("change_type"),
            )
    return result


@router.post("/{entity_name}/reresolve")
def run_reresolution(
    entity: Entity = Depends(get_deployed_entity),
    db: Session = Depends(get_db),
    principal: User = Depends(require_entity_permission("pipeline:run", "write")),
    limit: int = Query(500, le=5000),
):
    """Re-resolve staging rows held invalid on a broken reference (DQ-5).

    Unblocks children whose parent has since been created, flipping them valid
    and updating the stored reference column. Returns how many were unblocked.
    """
    with get_engine().begin() as conn:
        return reresolve_broken_references(db, conn, entity, limit=limit)


@router.get("/{entity_name}/landing")
def landing_rows(
    entity: Entity = Depends(get_deployed_entity),
    principal: User = Depends(require_entity_permission("staging:read", "read")),
    status_filter: Optional[str] = Query(None, alias="status"),
    limit: int = Query(50, le=500),
    offset: int = Query(0, ge=0),
):
    """Inspect the raw landing tier — the audit trail of what was actually sent."""
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
