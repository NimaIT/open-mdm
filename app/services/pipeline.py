"""The four-tier data pipeline.

    API write  ->  landing  ->  staging  ->  [steward review]  ->  live
                                                                    |
                                                                 history

Design rules that hold throughout:
  * The write API never touches the live tier. Ever.
  * Landing never rejects a payload — capture first, validate later.
  * Staging holds invalid rows deliberately, so a human can repair them.
  * Applying to live is transactional and always writes a history row.
"""
import json
import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_engine
from app.models import AuditEvent, Entity, PromotionBatch
from app.services import hooks, workflow
from app.services.identifiers import qualified, quote_ident
from app.services.logging_config import stream_logger
from app.services.mappings import apply_field_mappings, load_field_mappings
from app.services.references import reresolve_broken_references, resolve_references
from app.services.validation import build_match_key, validate_record

# Ingestion pipeline logs flow on the "integration" stream (AO-1).
log = stream_logger("integration")

OP_INSERT = "INSERT"
OP_UPDATE = "UPDATE"
OP_DELETE = "DELETE"
OP_UPSERT = "UPSERT"

STATUS_PENDING_REVIEW = "pending_review"
STATUS_CHANGES_REQUESTED = "changes_requested"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_APPLIED = "applied"
STATUS_ERROR = "error"


class PipelineError(RuntimeError):
    pass


class SegregationOfDutiesError(PermissionError):
    """A steward may not approve their own submission."""


def _json_default(value: Any):
    from decimal import Decimal

    if isinstance(value, (datetime,)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _dumps(obj) -> str:
    return json.dumps(obj, default=_json_default)


# ============================================================ tier 1: landing
def write_to_landing(
    conn: Connection,
    entity: Entity,
    *,
    operation: str,
    payload: Dict[str, Any],
    target_id: Optional[str] = None,
    source_system: Optional[str] = None,
    submitted_by: Optional[str] = None,
    batch_id: Optional[uuid.UUID] = None,
    idempotency_key: Optional[str] = None,
) -> Dict:
    """Append an inbound write intent to the landing tier.

    Deliberately permissive — the payload is stored as jsonb regardless of
    shape. An unparseable or invalid record must still be *captured*, because
    losing an inbound message is worse than storing a bad one.
    """
    if operation not in (OP_INSERT, OP_UPDATE, OP_DELETE, OP_UPSERT):
        raise PipelineError(f"Unsupported operation '{operation}'")

    t = qualified(settings.SCHEMA_LANDING, entity.name)

    if idempotency_key:
        existing = conn.execute(
            text(
                f"select mdm_landing_id, mdm_status from {t} "
                "where mdm_idempotency_key = :k"
            ),
            {"k": idempotency_key},
        ).first()
        if existing:
            return {
                "landing_id": existing[0],
                "status": existing[1],
                "deduplicated": True,
                "message": "Idempotency key already seen; original write returned.",
            }

    row = conn.execute(
        text(
            f"""INSERT INTO {t}
                (mdm_operation, mdm_payload, mdm_target_id, mdm_source_system,
                 mdm_batch_id, mdm_idempotency_key, mdm_submitted_by)
                VALUES (:op, cast(:payload as jsonb), :tid, :src, :batch, :idem, :by)
                RETURNING mdm_landing_id, mdm_received_at"""
        ),
        {
            "op": operation,
            "payload": _dumps(payload),
            "tid": str(target_id) if target_id else None,
            "src": source_system,
            "batch": str(batch_id) if batch_id else None,
            "idem": idempotency_key,
            "by": submitted_by,
        },
    ).one()
    return {
        "landing_id": row[0],
        "received_at": row[1],
        "status": "pending",
        "deduplicated": False,
    }


# ================================================= tier 1 -> 2: to staging
def promote_landing_to_staging(
    db: Session,
    conn: Connection,
    entity: Entity,
    *,
    landing_ids: Optional[Sequence[int]] = None,
    limit: int = 1000,
    actor: str = "system",
    rationale: Optional[str] = None,
    rationales: Optional[Dict[int, str]] = None,
    ip_address: Optional[str] = None,
) -> Dict:
    """Validate, type and de-duplicate landing rows into staging.

    Rows that fail validation are still written to staging, flagged invalid
    with their errors attached — that is the whole point of a staging tier.

    A ``WorkflowTask`` (status ``pending_review``) is created for every promoted
    row and a ``submit`` event opens its decision chain (GC-1/AO-2). A submission
    ``rationale`` may be attached per landing row (``rationales`` map) or applied
    to the whole run (``rationale``).
    """
    landing_t = qualified(settings.SCHEMA_LANDING, entity.name)
    staging_t = qualified(settings.SCHEMA_STAGING, entity.name)
    live_t = qualified(settings.SCHEMA_LIVE, entity.name)

    batch = PromotionBatch(
        entity_name=entity.name, stage="landing_to_staging", triggered_by=actor
    )
    db.add(batch)
    db.flush()

    log.info(
        "landing->staging promotion start entity=%s batch=%s",
        entity.name, batch.id,
        extra={"event": "promotion_start", "entity": entity.name,
               "batch_id": str(batch.id), "actor": actor},
    )

    where = "mdm_status = 'pending'"
    params: Dict[str, Any] = {"lim": limit}
    if landing_ids:
        where += " AND mdm_landing_id = ANY(:ids)"
        params["ids"] = list(landing_ids)

    rows = conn.execute(
        text(
            f"""select mdm_landing_id, mdm_operation, mdm_payload, mdm_target_id,
                       mdm_source_system, mdm_batch_id, mdm_submitted_by
                from {landing_t} where {where}
                order by mdm_landing_id limit :lim"""
        ),
        params,
    ).all()

    attr_names = [a.name for a in entity.attributes]
    ok = failed = 0
    results: List[Dict] = []
    # One parent business-key lookup cache for the whole batch (avoids an N+1
    # metadata query per row inside resolve_references).
    ref_column_cache: Dict[str, Any] = {}
    # EX-3: field mappings for this entity, loaded once per run. Applied per row
    # BEFORE validation, keyed by the landing row's source system.
    field_mappings = load_field_mappings(db, entity.name)

    for r in rows:
        landing_id, operation, payload, target_id, source, src_batch, submitted_by = r
        if isinstance(payload, str):
            payload = json.loads(payload)

        try:
            # UPDATE and DELETE are partial by nature: the caller identifies the
            # record and sends only what changes. Required-field checks must not
            # fire for fields the caller legitimately omitted.
            partial = operation in (OP_UPDATE, OP_DELETE)
            # EX-3: rename source fields to target attributes before validation.
            # The RAW landing payload is untouched (capture-first) — this only
            # shapes the copy that flows into staging.
            mapped_payload, map_errors = apply_field_mappings(
                entity, payload or {}, source_system=source, mappings=field_mappings,
            )
            outcome = validate_record(mapped_payload, entity, partial=partial)
            values, errors = outcome["values"], list(outcome["errors"])
            errors.extend(map_errors)

            # Resolve reference attributes (human-readable value -> parent
            # mdm_id). Unresolvable references attach a soft error and null the
            # column, so the row is held invalid in staging until the parent
            # exists (DQ-2 / DQ-5). Must run before match-key and target
            # resolution so those see the resolved values.
            resolve_references(
                db, conn, entity, values, errors,
                ref_column_cache=ref_column_cache,
            )

            # EX-1: pre_stage hooks cleanse/default the coerced values before the
            # staging insert. A hook exception becomes a structured row error (row
            # invalid) rather than a 500 — handled inside run_pre_stage.
            before_hook = dict(values)
            hooks.run_pre_stage(
                entity.name, values=values, errors=errors, operation=operation,
                actor=actor, db=db, conn=conn,
            )

            # Record exactly which business fields the caller sent, so the apply
            # step can distinguish an omitted field from one set to its default.
            # A field a pre_stage hook set/changed counts as supplied, so its
            # cleansed value is actually written through to the golden record.
            supplied_fields = [a for a in attr_names if a in (mapped_payload or {})]
            hook_touched = [a for a in attr_names
                            if values.get(a) != before_hook.get(a)]
            if hook_touched:
                supplied_fields = sorted(set(supplied_fields) | set(hook_touched))

            match_key = build_match_key(values, entity)

            # ---- resolve the target golden record
            resolved_id, change_type = _resolve_target(
                conn, entity, live_t, values, target_id, operation, errors
            )

            if operation == OP_DELETE:
                change_type = "delete"
                if resolved_id is None:
                    errors.append(
                        {
                            "field": "_record",
                            "code": "not_found",
                            "message": (
                                "DELETE could not locate a matching golden record "
                                "from the supplied identifier or business key."
                            ),
                        }
                    )

            is_valid = not errors
            cols = [c for c in attr_names if c in values]
            col_sql = ", ".join(quote_ident(c) for c in cols)
            val_sql = ", ".join(f":v_{c}" for c in cols)
            bind = {f"v_{c}": _adapt(values[c], entity, c) for c in cols}
            bind.update(
                lid=landing_id, op=operation, tid=resolved_id,
                mk=match_key, src=source, batch=str(batch.id),
                errs=_dumps(errors), valid=is_valid, ct=change_type,
                by=submitted_by, supplied=_dumps(supplied_fields),
            )
            prefix = f"{col_sql}, " if cols else ""
            vprefix = f"{val_sql}, " if cols else ""

            staging_id = conn.execute(
                text(
                    f"""INSERT INTO {staging_t}
                        ({prefix}mdm_landing_id, mdm_operation, mdm_target_id,
                         mdm_match_key, mdm_source_system, mdm_batch_id,
                         mdm_errors, mdm_is_valid, mdm_change_type, mdm_submitted_by,
                         mdm_supplied_fields)
                        VALUES ({vprefix}:lid, :op, :tid, :mk, :src,
                                cast(:batch as uuid), cast(:errs as jsonb), :valid,
                                :ct, :by, cast(:supplied as jsonb))
                        RETURNING mdm_staging_id"""
                ),
                bind,
            ).scalar()

            conn.execute(
                text(
                    f"update {landing_t} set mdm_status='promoted', "
                    "mdm_processed_at=now() where mdm_landing_id=:id"
                ),
                {"id": landing_id},
            )

            # Open the governed-change workflow for this staging row: one task +
            # a submit event carrying the submission rationale (GC-1). The task
            # is the authoritative governance state; the staging row is the data.
            submit_rationale = (rationales or {}).get(landing_id, rationale)
            # Create the task + submit event on the SAME conn as the staging
            # INSERT, so promotion is atomic and the task is committed with it —
            # a later decision on a separate connection can then see it (MAJOR-2).
            workflow.create_task_on_submit_core(
                conn, entity, staging_id,
                submitted_by=submitted_by,
                submit_rationale=submit_rationale,
                change_type=change_type,
                ip_address=ip_address,
            )
            ok += 1
            results.append(
                {
                    "landing_id": landing_id,
                    "staging_id": staging_id,
                    "is_valid": is_valid,
                    "change_type": change_type,
                    "errors": len(errors),
                }
            )
        except Exception as exc:  # per-row isolation
            log.exception("landing row %s failed", landing_id)
            conn.execute(
                text(
                    f"update {landing_t} set mdm_status='error', "
                    "mdm_errors=cast(:e as jsonb), mdm_processed_at=now() "
                    "where mdm_landing_id=:id"
                ),
                {"id": landing_id, "e": _dumps([{"code": "exception", "message": str(exc)}])},
            )
            failed += 1
            results.append({"landing_id": landing_id, "error": str(exc)})

    # DQ-5: newly-arrived parents may unblock children that were previously held
    # invalid on a broken reference. Re-resolve a bounded window automatically.
    try:
        reresolved = reresolve_broken_references(db, conn, entity, limit=500)
    except Exception:  # never let re-resolution abort a promotion run
        log.exception("re-resolution pass failed for %s", entity.name)
        reresolved = {"checked": 0, "unblocked": 0}

    batch.rows_in = len(rows)
    batch.rows_ok = ok
    batch.rows_failed = failed
    batch.status = "completed" if not failed else "completed_with_errors"
    batch.finished_at = datetime.utcnow()
    batch.detail = {"results": results[:200], "reresolved": reresolved}
    db.flush()

    log.info(
        "landing->staging promotion finished entity=%s batch=%s rows_in=%s ok=%s failed=%s",
        entity.name, batch.id, len(rows), ok, failed,
        extra={"event": "promotion_finished", "entity": entity.name,
               "batch_id": str(batch.id), "rows_in": len(rows),
               "rows_ok": ok, "rows_failed": failed,
               "reresolved_unblocked": reresolved.get("unblocked", 0)},
    )

    return {
        "batch_id": str(batch.id),
        "rows_in": len(rows),
        "promoted": ok,
        "failed": failed,
        "results": results,
        "reresolved": reresolved,
    }


def _resolve_target(
    conn: Connection,
    entity: Entity,
    live_t: str,
    values: Dict,
    target_id: Optional[str],
    operation: str,
    errors: List,
) -> tuple:
    """Locate the golden record this write applies to.

    Resolution order: explicit mdm_id -> business key -> deterministic match
    key. Returns (mdm_id or None, change_type).
    """
    if target_id:
        found = conn.execute(
            text(f"select mdm_id from {live_t} where mdm_id = cast(:id as uuid)"),
            {"id": str(target_id)},
        ).scalar()
        if found:
            return str(found), "update"
        errors.append(
            {
                "field": "mdm_id",
                "code": "not_found",
                "message": f"No golden record with mdm_id '{target_id}'.",
            }
        )
        return None, "insert"

    bkeys = entity.business_key
    if bkeys and all(values.get(a.name) is not None for a in bkeys):
        clause = " AND ".join(
            f"{quote_ident(a.name)} = :bk_{a.name}" for a in bkeys
        )
        params = {f"bk_{a.name}": _adapt(values[a.name], entity, a.name) for a in bkeys}
        found = conn.execute(
            text(
                f"select mdm_id from {live_t} where {clause} and mdm_is_deleted = false"
            ),
            params,
        ).scalar()
        if found:
            if operation == OP_INSERT:
                errors.append(
                    {
                        "field": bkeys[0].name,
                        "code": "duplicate",
                        "message": (
                            "A golden record with this business key already exists. "
                            "Use PUT/PATCH to update it, or approve as an update."
                        ),
                    }
                )
            return str(found), "update"
        return None, "insert"

    # Deterministic match-key fallback. Golden records store the constituent
    # columns rather than the composite key, so compare on those, applying the
    # same normalisation used to build the key.
    keys = entity.match_keys
    if keys and all(values.get(a.name) is not None for a in keys):
        clause = " AND ".join(
            f"lower(trim({quote_ident(a.name)}::text)) = :mk_{a.name}" for a in keys
        )
        params = {f"mk_{a.name}": str(values[a.name]).strip().lower() for a in keys}
        found = conn.execute(
            text(
                f"select mdm_id from {live_t} where {clause} "
                "and mdm_is_deleted = false"
            ),
            params,
        ).scalar()
        if found:
            if operation == OP_INSERT:
                errors.append(
                    {
                        "field": keys[0].name,
                        "code": "probable_duplicate",
                        "message": (
                            "A golden record matches on the configured match key. "
                            "Review before approving to avoid creating a duplicate."
                        ),
                    }
                )
            return str(found), "update"
    return None, "insert"


def _adapt(value, entity, col):
    """Adapt Python values for the driver (dict/list -> json string)."""
    if isinstance(value, (dict, list)):
        return _dumps(value)
    return value


# ================================================== tier 2 -> 3: to live
def apply_staging_to_live(
    db: Session,
    conn: Connection,
    entity: Entity,
    staging_id: int,
    *,
    actor: str,
    actor_roles: Optional[List[str]] = None,
    review_note: Optional[str] = None,
    enforce_sod: Optional[bool] = None,
    ip_address: Optional[str] = None,
) -> Dict:
    """Approve a staging row and apply it to the golden record.

    Writes a history row for every change. Runs inside the caller's
    transaction so a failure leaves nothing half-applied. Advances the
    WorkflowTask to ``applied`` and appends an ``approve`` event (GC-1/GC-5).
    """
    staging_t = qualified(settings.SCHEMA_STAGING, entity.name)
    live_t = qualified(settings.SCHEMA_LIVE, entity.name)
    hist_t = qualified(settings.SCHEMA_HISTORY, entity.name)

    # Lock the staging row for the whole decision (MAJOR-3): two concurrent
    # approvals of the same record now serialise — the second sees
    # 'applied'/'rejected' and is refused, so the golden record is never written
    # twice.
    row = conn.execute(
        text(f"select * from {staging_t} where mdm_staging_id = :id for update"),
        {"id": staging_id},
    ).mappings().first()
    if row is None:
        raise PipelineError(f"Staging record {staging_id} not found.")
    if row["mdm_status"] in (STATUS_APPLIED, STATUS_REJECTED):
        raise PipelineError(
            f"Staging record {staging_id} is already '{row['mdm_status']}'."
        )
    # Enforce the maker-checker loop (MINOR-5/GC-1): a row sent back for changes
    # must be edited (resubmitted to pending_review) before it can be approved.
    # Approving it directly would skip the resubmit half of the round-trip.
    if row["mdm_status"] == STATUS_CHANGES_REQUESTED:
        raise PipelineError(
            f"Staging record {staging_id} is awaiting requested changes and "
            "cannot be approved until it is edited and resubmitted for review."
        )
    if not row["mdm_is_valid"]:
        raise PipelineError(
            f"Staging record {staging_id} has unresolved validation errors and "
            "cannot be approved. Edit the record to fix them first."
        )

    sod = settings.ENFORCE_SEGREGATION_OF_DUTIES if enforce_sod is None else enforce_sod
    if sod:
        originator = row["mdm_edited_by"] or row["mdm_submitted_by"]
        if originator and originator == actor:
            raise SegregationOfDutiesError(
                f"Segregation of duties: '{actor}' submitted or last edited this "
                "record and therefore cannot approve it. Another steward must review."
            )

    attr_names = [a.name for a in entity.attributes]
    values = {c: row[c] for c in attr_names if c in row}
    operation = row["mdm_operation"]
    target_id = row["mdm_target_id"]

    # For an update we may only touch fields the caller actually supplied (or a
    # steward subsequently edited). Anything else must retain its golden value.
    supplied = row["mdm_supplied_fields"] or []
    if isinstance(supplied, str):
        supplied = json.loads(supplied)

    # EX-1: pre_commit hooks run BEFORE the golden write. They may mutate values
    # or abort. A hook that raises aborts the apply cleanly (PipelineError) with
    # the staging row left un-applied and pending — never a half-written record.
    before_hook = dict(values)
    hooks.run_pre_commit(
        entity.name, values=values, operation=operation, actor=actor,
        staging_id=staging_id, mdm_id=target_id, db=db, conn=conn,
    )
    # A hook may inject a key that is not a real attribute column; drop it so it
    # can never turn into an UndefinedColumn 500 on the golden write (mirrors the
    # attr_names filtering the pre_stage->staging path already applies). A hook
    # that legitimately sets a real business field on an UPDATE must actually be
    # written, so union any field it changed into supplied — otherwise only the
    # caller-supplied fields apply and the cleansed value is silently dropped.
    hook_changed = [c for c in attr_names if values.get(c) != before_hook.get(c)]
    values = {c: v for c, v in values.items() if c in attr_names}
    if hook_changed:
        supplied = sorted(set(supplied) | set(hook_changed))

    if operation == OP_DELETE:
        result = _apply_delete(conn, entity, live_t, hist_t, target_id, actor)
    elif target_id:
        result = _apply_update(
            conn, entity, live_t, hist_t, target_id, values, actor, staging_id,
            supplied_fields=supplied,
        )
    else:
        result = _apply_insert(conn, live_t, values, actor, staging_id, row)

    log.info(
        "apply-to-live entity=%s staging=%s change_type=%s mdm_id=%s",
        entity.name, staging_id, result.get("change_type"), result.get("mdm_id"),
        extra={"event": "apply_to_live", "entity": entity.name,
               "staging_id": staging_id, "change_type": result.get("change_type"),
               "mdm_id": result.get("mdm_id"), "actor": actor},
    )

    conn.execute(
        text(
            f"""update {staging_t}
                set mdm_status = :st, mdm_reviewed_by = :by, mdm_reviewed_at = now(),
                    mdm_review_note = :note
                where mdm_staging_id = :id"""
        ),
        {"st": STATUS_APPLIED, "by": actor, "note": review_note, "id": staging_id},
    )

    db.add(
        AuditEvent(
            actor=actor,
            actor_roles=actor_roles or [],
            action=f"approve_{result['change_type']}",
            entity_name=entity.name,
            record_id=str(result.get("mdm_id")),
            tier="live",
            detail={"staging_id": staging_id, "note": review_note},
            ip_address=ip_address,
            after_value={k: _json_default(v) if not isinstance(v, (str, int, float, bool, type(None))) else v
                         for k, v in values.items()},
        )
    )
    # Advance the task + append the approve event on the SAME conn/transaction as
    # the staging status and golden-record writes, so governance state and tier
    # state commit atomically — no crash window leaves them disagreeing (MAJOR-2).
    workflow.record_decision_core(
        conn, entity, staging_id, step=workflow.STEP_APPROVE,
        to_status=workflow.APPLIED, actor=actor, actor_roles=actor_roles,
        comment=review_note, ip_address=ip_address,
    )
    # EX-1: post_commit hooks are deliberately NOT run here. They must fire AFTER
    # the caller's apply transaction has COMMITTED, so a chaining hook can observe
    # the committed golden record and its external side effects aren't stranded if
    # the outer commit later fails. Each call site runs them via
    # ``run_post_commit_hooks`` once this transaction has committed.
    result["staging_id"] = staging_id
    result["operation"] = operation
    return result


def run_post_commit_hooks(
    entity: Entity,
    result: Optional[Dict],
    *,
    actor: str,
    db: Optional[Session] = None,
) -> List[Dict[str, str]]:
    """Run EX-1 post_commit hooks AFTER the apply transaction has committed.

    Opens a FRESH short transaction so a chaining hook sees the committed golden
    record (via ``ctx.conn``) and any DB work it does is savepoint-isolated on
    both the fresh connection and ``db``. Exceptions are swallowed and recorded on
    ``result['post_commit_errors']``; they can never undo the committed change.

    A no-op — **no connection is opened** — when the entity has no post_commit
    hooks, so the zero-hook path keeps its original cost and behaviour.
    """
    if not result or not hooks.has_hooks(hooks.POST_COMMIT, entity.name):
        return []
    with get_engine().begin() as conn2:
        errs = hooks.run_post_commit(
            entity.name,
            operation=result.get("operation"),
            actor=actor,
            staging_id=result.get("staging_id"),
            mdm_id=result.get("mdm_id"),
            result=result,
            db=db, conn=conn2,
        )
    if errs:
        result["post_commit_errors"] = errs
    return errs


def _apply_insert(conn, live_t, values, actor, staging_id, row) -> Dict:
    cols = list(values)
    col_sql = ", ".join(quote_ident(c) for c in cols)
    val_sql = ", ".join(f":v_{c}" for c in cols)
    bind = {f"v_{c}": _adapt(values[c], None, c) for c in cols}
    bind.update(by=actor, sid=staging_id, src=row["mdm_source_system"])
    new_id = conn.execute(
        text(
            f"""INSERT INTO {live_t} ({col_sql}, mdm_created_by, mdm_updated_by,
                                       mdm_approved_by, mdm_staging_id, mdm_source_system)
                VALUES ({val_sql}, :by, :by, :by, :sid, :src)
                RETURNING mdm_id"""
        ),
        bind,
    ).scalar()
    return {"change_type": "insert", "mdm_id": str(new_id), "version": 1}


def _apply_update(
    conn, entity, live_t, hist_t, target_id, values, actor, staging_id,
    *, supplied_fields=None,
) -> Dict:
    current = conn.execute(
        text(f"select * from {live_t} where mdm_id = cast(:id as uuid)"),
        {"id": str(target_id)},
    ).mappings().first()
    if current is None:
        raise PipelineError(f"Golden record {target_id} no longer exists.")

    _write_history(conn, entity, hist_t, current, "update", actor)

    # Only overwrite columns the caller actually supplied. Falling back to
    # "every non-null staging column" would let DDL defaults leak into the
    # golden record and silently clobber curated values.
    if supplied_fields:
        supplied = {k: values[k] for k in supplied_fields if k in values}
    else:
        supplied = {k: v for k, v in values.items() if v is not None}
    if not supplied:
        return {"change_type": "no_change", "mdm_id": str(target_id),
                "version": current["mdm_version"]}
    set_sql = ", ".join(f"{quote_ident(c)} = :v_{c}" for c in supplied)
    bind = {f"v_{c}": _adapt(v, None, c) for c, v in supplied.items()}
    bind.update(id=str(target_id), by=actor, sid=staging_id)
    new_version = conn.execute(
        text(
            f"""update {live_t}
                set {set_sql}, mdm_version = mdm_version + 1, mdm_updated_at = now(),
                    mdm_updated_by = :by, mdm_approved_by = :by, mdm_staging_id = :sid
                where mdm_id = cast(:id as uuid)
                RETURNING mdm_version"""
        ),
        bind,
    ).scalar()
    return {"change_type": "update", "mdm_id": str(target_id), "version": new_version,
            "fields_changed": sorted(supplied)}


def _apply_delete(conn, entity, live_t, hist_t, target_id, actor) -> Dict:
    current = conn.execute(
        text(f"select * from {live_t} where mdm_id = cast(:id as uuid)"),
        {"id": str(target_id)},
    ).mappings().first()
    if current is None:
        raise PipelineError(f"Golden record {target_id} not found.")
    _write_history(conn, entity, hist_t, current, "delete", actor)

    if entity.soft_delete and settings.SOFT_DELETE:
        conn.execute(
            text(
                f"""update {live_t}
                    set mdm_is_deleted = true, mdm_deleted_at = now(),
                        mdm_version = mdm_version + 1, mdm_updated_at = now(),
                        mdm_updated_by = :by, mdm_approved_by = :by
                    where mdm_id = cast(:id as uuid)"""
            ),
            {"id": str(target_id), "by": actor},
        )
        return {"change_type": "soft_delete", "mdm_id": str(target_id)}

    conn.execute(
        text(f"delete from {live_t} where mdm_id = cast(:id as uuid)"),
        {"id": str(target_id)},
    )
    return {"change_type": "hard_delete", "mdm_id": str(target_id)}


def _write_history(conn, entity, hist_t, current, change_type, actor) -> None:
    """Snapshot the pre-change state of a golden record."""
    attr_names = [a.name for a in entity.attributes]
    cols = [c for c in attr_names if c in current]
    col_sql = "".join(f", {quote_ident(c)}" for c in cols)
    val_sql = "".join(f", :v_{c}" for c in cols)
    bind = {f"v_{c}": _adapt(current[c], None, c) for c in cols}
    bind.update(
        id=str(current["mdm_id"]),
        ver=current["mdm_version"],
        ct=change_type,
        vf=current["mdm_updated_at"] or current["mdm_created_at"],
        by=actor,
        src=current.get("mdm_source_system"),
        deleted=current.get("mdm_is_deleted", False),
        sid=current.get("mdm_staging_id"),
    )
    conn.execute(
        text(
            f"""INSERT INTO {hist_t}
                (mdm_id, mdm_version, mdm_change_type, mdm_valid_from, mdm_changed_by,
                 mdm_source_system, mdm_is_deleted, mdm_staging_id{col_sql})
                VALUES (cast(:id as uuid), :ver, :ct, :vf, :by, :src, :deleted,
                        :sid{val_sql})"""
        ),
        bind,
    )


# ------------------------------------------------------------------ rejection
def reject_staging(
    db: Session, conn: Connection, entity: Entity, staging_id: int, *,
    actor: str, reason: str, actor_roles: Optional[List[str]] = None,
    ip_address: Optional[str] = None,
) -> Dict:
    staging_t = qualified(settings.SCHEMA_STAGING, entity.name)
    # A rejected change is fully discarded (GC-3): terminal, no golden impact.
    # It can never be applied later, so an already-terminal row is refused.
    # Lock the row for the decision (MAJOR-3) so a concurrent approve/reject of the
    # same record serialises rather than double-processing.
    current = conn.execute(
        text(f"select mdm_status from {staging_t} "
             "where mdm_staging_id = :id for update"),
        {"id": staging_id},
    ).scalar()
    if current is None or current in (STATUS_APPLIED, STATUS_REJECTED):
        raise PipelineError(
            f"Staging record {staging_id} not found or already applied/rejected."
        )
    conn.execute(
        text(
            f"""update {staging_t}
                set mdm_status='{STATUS_REJECTED}', mdm_reviewed_by=:by,
                    mdm_reviewed_at=now(), mdm_review_note=:note
                where mdm_staging_id=:id"""
        ),
        {"by": actor, "note": reason, "id": staging_id},
    )
    db.add(
        AuditEvent(
            actor=actor, actor_roles=actor_roles or [], action="reject",
            entity_name=entity.name, record_id=str(staging_id), tier="staging",
            detail={"reason": reason}, ip_address=ip_address,
        )
    )
    # Task advance + reject event share the conn with the staging write (MAJOR-2).
    workflow.record_decision_core(
        conn, entity, staging_id, step=workflow.STEP_REJECT,
        to_status=workflow.REJECTED, actor=actor, actor_roles=actor_roles,
        comment=reason, ip_address=ip_address,
    )
    return {"staging_id": staging_id, "status": STATUS_REJECTED, "reason": reason}


def edit_staging(
    db: Session, conn: Connection, entity: Entity, staging_id: int, *,
    updates: Dict[str, Any], actor: str, actor_roles: Optional[List[str]] = None,
    ip_address: Optional[str] = None,
) -> Dict:
    """Steward polish: re-validate the edited row and update its error state.

    An edit returns the change request to ``pending_review`` — this is the
    resubmit half of the ``changes_requested`` round-trip (GC-1)."""
    staging_t = qualified(settings.SCHEMA_STAGING, entity.name)
    row = conn.execute(
        text(f"select * from {staging_t} where mdm_staging_id=:id"), {"id": staging_id}
    ).mappings().first()
    if row is None:
        raise PipelineError(f"Staging record {staging_id} not found.")
    if row["mdm_status"] == STATUS_APPLIED:
        raise PipelineError("Cannot edit a record that has already been applied.")
    # A rejected/terminated change is fully discarded (GC-3) — editing it must not
    # resurrect it back into an approvable state.
    if row["mdm_status"] == STATUS_REJECTED:
        raise PipelineError(
            "Cannot edit a record that has been rejected or terminated."
        )

    attr_names = [a.name for a in entity.attributes]
    merged = {c: row[c] for c in attr_names if c in row}
    unknown = [k for k in updates if k not in attr_names]
    if unknown:
        raise PipelineError(f"Unknown field(s): {', '.join(unknown)}")
    merged.update(updates)

    outcome = validate_record(
        merged, entity, partial=(row["mdm_operation"] in (OP_UPDATE, OP_DELETE))
    )
    values, errors = outcome["values"], list(outcome["errors"])
    # Re-resolve references so a steward's correction (or a now-present parent)
    # is reflected before the row is re-validated for approval.
    resolve_references(db, conn, entity, values, errors)
    match_key = build_match_key(values, entity)

    # A steward's edits count as supplied — otherwise the repair they just made
    # would be discarded when the record is applied to the golden table.
    previously = row["mdm_supplied_fields"] or []
    if isinstance(previously, str):
        previously = json.loads(previously)
    supplied = sorted(set(previously) | set(updates))

    set_sql = ", ".join(f"{quote_ident(c)} = :v_{c}" for c in values)
    bind = {f"v_{c}": _adapt(v, None, c) for c, v in values.items()}
    bind.update(
        id=staging_id, by=actor, errs=_dumps(errors), valid=not errors, mk=match_key,
        st=STATUS_PENDING_REVIEW, supplied=_dumps(supplied),
    )
    conn.execute(
        text(
            f"""update {staging_t} set {set_sql}, mdm_errors=cast(:errs as jsonb),
                    mdm_is_valid=:valid, mdm_match_key=:mk, mdm_edited_by=:by,
                    mdm_edited_at=now(), mdm_status=:st,
                    mdm_supplied_fields=cast(:supplied as jsonb)
                where mdm_staging_id=:id"""
        ),
        bind,
    )
    db.add(
        AuditEvent(
            actor=actor, actor_roles=actor_roles or [], action="edit_staging",
            entity_name=entity.name, record_id=str(staging_id), tier="staging",
            detail={"fields": sorted(updates)}, ip_address=ip_address,
            before_value={k: _json_default(row[k]) for k in updates if k in row},
            after_value={k: _json_default(v) for k, v in updates.items()},
        )
    )
    # An edit resubmits the change: task returns to pending_review (GC-1). The
    # task advance + edit event share the conn with the staging write (MAJOR-2).
    workflow.record_decision_core(
        conn, entity, staging_id, step=workflow.STEP_EDIT,
        to_status=workflow.PENDING_REVIEW, actor=actor, actor_roles=actor_roles,
        comment=None, ip_address=ip_address,
    )
    return {
        "staging_id": staging_id,
        "is_valid": not errors,
        "errors": errors,
        "updated_fields": sorted(updates),
    }
