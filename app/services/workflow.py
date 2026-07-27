"""Governed change-management workflow (Workstream 3).

The generated staging row holds the *data* of a change request; the
``WorkflowTask`` / ``WorkflowEvent`` tables hold its *governance state* — status,
assignment, submission rationale and the immutable decision chain.

There is exactly one task per staging row. The staging row's ``mdm_status`` is
kept in sync with the task status so the existing queue queries keep working,
but the authoritative workflow state lives here.

Design notes:
  * Tasks/events are fixed ``mdm_meta`` tables, so they use the ORM ``Session``.
  * The generated staging table is accessed with Core ``text()`` + bind params.
  * ``WorkflowEvent`` is append-only; every transition appends exactly one event
    with actor, comment, from/to status and ip_address (AO-2).
"""
import json
import logging
import uuid
from datetime import datetime
from typing import List, Optional

from sqlalchemy import func, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Entity, WorkflowEvent, WorkflowTask
from app.services.identifiers import qualified

log = logging.getLogger(__name__)

# ---- task statuses
# NB: 'draft' and 'approved' were named in an early sketch of the state machine
# but are unreachable — a submitted row goes straight to pending_review, and an
# approval applies immediately and lands on 'applied'. They are deliberately
# omitted so the defined statuses match what the machine can actually reach.
PENDING_REVIEW = "pending_review"
CHANGES_REQUESTED = "changes_requested"
REJECTED = "rejected"
APPLIED = "applied"
TERMINATED = "terminated"

# Active statuses appear in work queues / inboxes; terminal ones never do (GC-3).
ACTIVE_STATUSES = (PENDING_REVIEW, CHANGES_REQUESTED)
TERMINAL_STATUSES = (REJECTED, APPLIED, TERMINATED)

# ---- workflow steps (WorkflowEvent.step)
STEP_SUBMIT = "submit"
STEP_CLAIM = "claim"
STEP_RELEASE = "release"
STEP_EDIT = "edit"
STEP_REQUEST_CHANGES = "request_changes"
STEP_APPROVE = "approve"
STEP_REJECT = "reject"
STEP_REASSIGN = "reassign"
STEP_TERMINATE = "terminate"
STEP_APPLY = "apply"

# Map an authoritative task status onto a value the generated staging table's
# CHECK constraint allows. 'terminated' has no staging equivalent, so a
# force-closed task lands the staging row on the terminal 'rejected' value while
# the task itself records the true 'terminated' status.
_STAGING_STATUS_FOR = {
    PENDING_REVIEW: "pending_review",
    CHANGES_REQUESTED: "changes_requested",
    REJECTED: "rejected",
    APPLIED: "applied",
    TERMINATED: "rejected",
}


class WorkflowError(RuntimeError):
    pass


class WorkflowConflict(WorkflowError):
    """A concurrent-ownership / concurrency conflict.

    Surfaces as HTTP 409 where a route handles it explicitly; it also subclasses
    WorkflowError so handlers that only catch the latter still return a clean 4xx
    (never an unhandled 500) when a concurrent seq collision is converted here."""


# --------------------------------------------------------------- event append
def _next_seq(db: Session, task_id) -> int:
    current = (
        db.query(func.coalesce(func.max(WorkflowEvent.seq), 0))
        .filter(WorkflowEvent.task_id == task_id)
        .scalar()
    )
    return int(current or 0) + 1


def append_event(
    db: Session,
    task: WorkflowTask,
    *,
    step: str,
    actor: Optional[str],
    actor_roles: Optional[List[str]] = None,
    comment: Optional[str] = None,
    from_status: Optional[str] = None,
    to_status: Optional[str] = None,
    ip_address: Optional[str] = None,
) -> WorkflowEvent:
    """Append one immutable event to a task's decision chain."""
    event = WorkflowEvent(
        task_id=task.id,
        seq=_next_seq(db, task.id),
        step=step,
        actor=actor,
        actor_roles=actor_roles or [],
        comment=comment,
        from_status=from_status,
        to_status=to_status,
        ip_address=ip_address,
    )
    db.add(event)
    db.flush()
    return event


def _append_event_guarded(db: Session, task: WorkflowTask, **kw) -> WorkflowEvent:
    """Append on the ORM session, turning a duplicate-seq collision into a clean
    409 rather than an unhandled 500 (MINOR-4).

    Pure task-only transitions (claim/release/reassign) compute ``seq`` without a
    row lock; if a concurrent decision (approve/claim) inserts the same seq first,
    the UniqueConstraint(task_id, seq) fires. We roll back and surface it as a
    WorkflowConflict so the caller retries rather than 500-ing."""
    try:
        return append_event(db, task, **kw)
    except IntegrityError as exc:
        db.rollback()
        raise WorkflowConflict(
            "This change request was updated concurrently — please retry."
        ) from exc


# ----------------------------------------- atomic (Core) governance writers
# These write WorkflowTask/WorkflowEvent through the SAME Core ``conn`` — and
# therefore the same transaction — as the tier write (staging status / golden
# record). That closes the split-brain window where a crash could leave a staging
# row 'applied' while its task was still 'pending_review' (MAJOR-2). They mirror
# the ORM helpers above but never touch the request's ORM Session.
def _task_table() -> str:
    return qualified(settings.SCHEMA_META, "workflow_task")


def _event_table() -> str:
    return qualified(settings.SCHEMA_META, "workflow_event")


def load_task_core(conn: Connection, entity_name: str, staging_id: int, *,
                   for_update: bool = False):
    """Return the ``{id, status}`` mapping of a staging row's task on ``conn``
    (or None). ``for_update`` locks the task row so seq assignment serialises."""
    lock = " for update" if for_update else ""
    return conn.execute(
        text(f"select id, status from {_task_table()} "
             f"where entity_name = :e and staging_id = :s{lock}"),
        {"e": entity_name, "s": staging_id},
    ).mappings().first()


def ensure_task_core(conn: Connection, entity: Entity, staging_id: int, *,
                     for_update: bool = True):
    """Load (and optionally lock) a staging row's task on ``conn``, creating a
    minimal one if absent (defensive fallback for pre-workflow staging rows).

    Returns ``(task_id, current_status)``. Creating the task on the same ``conn``
    (not the ORM session) is essential: a WorkflowEvent appended on ``conn`` would
    otherwise fail the FK to a task row still invisible in another transaction."""
    row = load_task_core(conn, entity.name, staging_id, for_update=for_update)
    if row is not None:
        return row["id"], row["status"]
    new_id = uuid.uuid4()
    conn.execute(
        text(f"""insert into {_task_table()}
                 (id, entity_name, staging_id, domain, status, priority)
                 values (:id, :e, :s, :d, :st, 0)"""),
        {"id": str(new_id), "e": entity.name, "s": staging_id,
         "d": entity.domain, "st": PENDING_REVIEW},
    )
    return new_id, PENDING_REVIEW


def advance_task_core(conn: Connection, task_id, to_status: str) -> None:
    conn.execute(
        text(f"update {_task_table()} set status = :st, updated_at = now() "
             "where id = :id"),
        {"st": to_status, "id": str(task_id)},
    )


def append_event_core(
    conn: Connection, task_id, *, step: str, actor: Optional[str],
    comment: Optional[str] = None, from_status: Optional[str] = None,
    to_status: Optional[str] = None, ip_address: Optional[str] = None,
    actor_roles: Optional[List[str]] = None,
) -> int:
    """Append one immutable event on ``conn``. ``seq`` is computed here; callers
    needing race-safety must hold the parent task row locked (ensure_task_core)."""
    seq = conn.execute(
        text(f"select coalesce(max(seq), 0) + 1 from {_event_table()} "
             "where task_id = :tid"),
        {"tid": str(task_id)},
    ).scalar()
    conn.execute(
        text(f"""insert into {_event_table()}
                 (id, task_id, seq, step, actor, actor_roles, comment,
                  from_status, to_status, ip_address)
                 values (:id, :tid, :seq, :step, :actor, cast(:roles as jsonb),
                         :comment, :fs, :ts, :ip)"""),
        {"id": str(uuid.uuid4()), "tid": str(task_id), "seq": int(seq),
         "step": step, "actor": actor, "roles": json.dumps(actor_roles or []),
         "comment": comment, "fs": from_status, "ts": to_status, "ip": ip_address},
    )
    return int(seq)


def record_decision_core(
    conn: Connection, entity: Entity, staging_id: int, *, step: str,
    to_status: str, actor: str, actor_roles: Optional[List[str]] = None,
    comment: Optional[str] = None, ip_address: Optional[str] = None,
) -> dict:
    """Advance a staging row's task and append its event on the SAME ``conn`` as
    the tier write — the atomic replacement for the old ORM ``sync_after_decision``
    (GC-5/MAJOR-2). The task row is locked FOR UPDATE so seq assignment is safe."""
    task_id, from_status = ensure_task_core(conn, entity, staging_id, for_update=True)
    advance_task_core(conn, task_id, to_status)
    append_event_core(
        conn, task_id, step=step, actor=actor, actor_roles=actor_roles,
        comment=comment, from_status=from_status, to_status=to_status,
        ip_address=ip_address,
    )
    return {"task_id": str(task_id), "from_status": from_status,
            "to_status": to_status}


def create_task_on_submit_core(
    conn: Connection, entity: Entity, staging_id: int, *,
    submitted_by: Optional[str], submit_rationale: Optional[str],
    change_type: Optional[str], actor_roles: Optional[List[str]] = None,
    ip_address: Optional[str] = None, status: str = PENDING_REVIEW,
):
    """Open the governed-change workflow for a freshly-promoted staging row, on the
    SAME ``conn`` as the staging INSERT.

    Doing this on ``conn`` (not the ORM session) has two payoffs:
      * promotion is atomic — the staging row and its governance task/submit event
        commit together, so a crash can't leave a staging row with no task; and
      * the task is committed *with* the promotion, so a later decision running on
        a separate connection can see it (without this, ensure_task_core on that
        connection would block on the uncommitted unique key).

    Idempotent per (entity, staging_id): an existing task is returned unchanged."""
    existing = load_task_core(conn, entity.name, staging_id)
    if existing is not None:
        return existing["id"]
    task_id = uuid.uuid4()
    conn.execute(
        text(f"""insert into {_task_table()}
                 (id, entity_name, staging_id, domain, status, priority,
                  submitted_by, submit_rationale, change_type)
                 values (:id, :e, :s, :d, :st, 0, :by, :ra, :ct)"""),
        {"id": str(task_id), "e": entity.name, "s": staging_id,
         "d": entity.domain, "st": status, "by": submitted_by,
         "ra": submit_rationale, "ct": change_type},
    )
    append_event_core(
        conn, task_id, step=STEP_SUBMIT, actor=submitted_by,
        actor_roles=actor_roles, comment=submit_rationale, from_status=None,
        to_status=status, ip_address=ip_address,
    )
    return task_id


# ------------------------------------------------------------------- lookup
def get_task(db: Session, entity_name: str, staging_id: int) -> Optional[WorkflowTask]:
    return (
        db.query(WorkflowTask)
        .filter(
            WorkflowTask.entity_name == entity_name,
            WorkflowTask.staging_id == staging_id,
        )
        .one_or_none()
    )


def ensure_task(
    db: Session,
    entity: Entity,
    staging_id: int,
    *,
    status: str = PENDING_REVIEW,
    submitted_by: Optional[str] = None,
    change_type: Optional[str] = None,
) -> WorkflowTask:
    """Fetch the task for a staging row, creating a minimal one if absent.

    Promotion always creates the task up front; this is a defensive fallback for
    staging rows created before the workflow existed, so a transition never
    fails merely because a task row is missing.
    """
    task = get_task(db, entity.name, staging_id)
    if task is None:
        task = WorkflowTask(
            entity_name=entity.name,
            staging_id=staging_id,
            domain=entity.domain,
            status=status,
            submitted_by=submitted_by,
            change_type=change_type,
        )
        db.add(task)
        db.flush()
    return task


# ------------------------------------------------------------------- submit
def create_task_on_submit(
    db: Session,
    entity: Entity,
    staging_id: int,
    *,
    submitted_by: Optional[str],
    submit_rationale: Optional[str],
    change_type: Optional[str],
    actor_roles: Optional[List[str]] = None,
    ip_address: Optional[str] = None,
    status: str = PENDING_REVIEW,
) -> WorkflowTask:
    """Create the WorkflowTask for a freshly-promoted staging row + submit event.

    Idempotent per (entity, staging_id): if a task already exists (e.g. a
    re-promotion) it is returned unchanged rather than duplicated.
    """
    existing = get_task(db, entity.name, staging_id)
    if existing is not None:
        return existing
    task = WorkflowTask(
        entity_name=entity.name,
        staging_id=staging_id,
        domain=entity.domain,
        status=status,
        submitted_by=submitted_by,
        submit_rationale=submit_rationale,
        change_type=change_type,
    )
    db.add(task)
    db.flush()
    append_event(
        db, task, step=STEP_SUBMIT, actor=submitted_by, actor_roles=actor_roles,
        comment=submit_rationale, from_status=None, to_status=status,
        ip_address=ip_address,
    )
    return task


# ------------------------------------------------- entity-lifecycle cleanup
def terminate_tasks_for_entity(
    db: Session,
    entity_name: str,
    *,
    actor: str = "system",
    reason: str = "entity deleted",
) -> int:
    """Force-close every still-active task for an entity being deleted (MAJOR-1).

    When an entity is dropped its staging table and metadata disappear, but its
    ``workflow_task`` rows do not — leaving active tasks dangling in the inbox and
    admin workflow views, pointing at a staging table that no longer exists
    (opening one would error). We terminate each active task (an UPDATE, which the
    AO-2 append-only trigger permits) and append a terminate event. Events are
    never deleted, so the immutable decision chain stays intact (GC-3).

    Runs entirely on the ORM ``db`` session, so it commits atomically with the
    entity-metadata deletion in the same request transaction.
    """
    tasks = (
        db.query(WorkflowTask)
        .filter(
            WorkflowTask.entity_name == entity_name,
            WorkflowTask.status.in_(list(ACTIVE_STATUSES)),
        )
        .all()
    )
    for task in tasks:
        from_status = task.status
        task.status = TERMINATED
        append_event(
            db, task, step=STEP_TERMINATE, actor=actor, actor_roles=["system"],
            comment=reason, from_status=from_status, to_status=TERMINATED,
        )
    return len(tasks)


# --------------------------------------------- transitions owned by this module
def _set_staging_status(conn: Connection, entity: Entity, staging_id: int,
                        task_status: str) -> None:
    staging_t = qualified(settings.SCHEMA_STAGING, entity.name)
    conn.execute(
        text(f"update {staging_t} set mdm_status=:s where mdm_staging_id=:id"),
        {"s": _STAGING_STATUS_FOR[task_status], "id": staging_id},
    )


def request_changes(
    db: Session,
    conn: Connection,
    entity: Entity,
    staging_id: int,
    *,
    actor: str,
    comment: str,
    actor_roles: Optional[List[str]] = None,
    ip_address: Optional[str] = None,
) -> dict:
    """Send a change request back to its submitter with a mandatory comment.

    The staging row stays editable; the editor's next edit (or resubmit) returns
    it to ``pending_review`` (GC-1). This is what makes ``changes_requested``
    reachable.
    """
    # Lock the staging row first, then the task row — the same order approve /
    # reject use, so concurrent decisions on one record serialise rather than
    # deadlock. The task + event ride the SAME conn as the staging write (MAJOR-2).
    staging_t = qualified(settings.SCHEMA_STAGING, entity.name)
    locked = conn.execute(
        text(f"select mdm_status from {staging_t} "
             "where mdm_staging_id = :id for update"),
        {"id": staging_id},
    ).scalar()
    if locked is None:
        raise WorkflowError(f"Staging record {staging_id} not found.")
    task_id, from_status = ensure_task_core(conn, entity, staging_id, for_update=True)
    if from_status in TERMINAL_STATUSES:
        raise WorkflowError(
            f"Change request is already '{from_status}' and cannot be sent back."
        )
    _set_staging_status(conn, entity, staging_id, CHANGES_REQUESTED)
    advance_task_core(conn, task_id, CHANGES_REQUESTED)
    append_event_core(
        conn, task_id, step=STEP_REQUEST_CHANGES, actor=actor,
        actor_roles=actor_roles, comment=comment, from_status=from_status,
        to_status=CHANGES_REQUESTED, ip_address=ip_address,
    )
    return {"staging_id": staging_id, "status": CHANGES_REQUESTED,
            "task_id": str(task_id), "comment": comment}


def claim(
    db: Session,
    entity: Entity,
    staging_id: int,
    *,
    actor: str,
    actor_roles: Optional[List[str]] = None,
    ip_address: Optional[str] = None,
) -> dict:
    """Take ownership of a task. A task already claimed by someone else -> 409."""
    task = ensure_task(db, entity, staging_id)
    if task.status in TERMINAL_STATUSES:
        raise WorkflowError(f"Change request is '{task.status}' — nothing to claim.")
    if task.claimed_by and task.claimed_by != actor:
        raise WorkflowConflict(
            f"Already claimed by '{task.claimed_by}'. Ask them to release it first."
        )
    task.claimed_by = actor
    task.claimed_at = datetime.utcnow()
    task.assigned_to = actor
    _append_event_guarded(
        db, task, step=STEP_CLAIM, actor=actor, actor_roles=actor_roles,
        from_status=task.status, to_status=task.status, ip_address=ip_address,
    )
    return {"staging_id": staging_id, "task_id": str(task.id),
            "claimed_by": actor, "status": task.status}


def release(
    db: Session,
    entity: Entity,
    staging_id: int,
    *,
    actor: str,
    actor_roles: Optional[List[str]] = None,
    ip_address: Optional[str] = None,
) -> dict:
    """Give up ownership of a task previously claimed by this actor."""
    task = ensure_task(db, entity, staging_id)
    if not task.claimed_by:
        raise WorkflowError("Task is not currently claimed.")
    if task.claimed_by != actor:
        raise WorkflowConflict(
            f"Claimed by '{task.claimed_by}', not you — you cannot release it."
        )
    task.claimed_by = None
    task.claimed_at = None
    task.assigned_to = None
    _append_event_guarded(
        db, task, step=STEP_RELEASE, actor=actor, actor_roles=actor_roles,
        from_status=task.status, to_status=task.status, ip_address=ip_address,
    )
    return {"staging_id": staging_id, "task_id": str(task.id), "claimed_by": None,
            "status": task.status}


def reassign(
    db: Session,
    task: WorkflowTask,
    *,
    assignee: Optional[str],
    actor: str,
    actor_roles: Optional[List[str]] = None,
    comment: Optional[str] = None,
    ip_address: Optional[str] = None,
) -> dict:
    """Admin: (re)assign a task to a reviewer."""
    if task.status in TERMINAL_STATUSES:
        raise WorkflowError(f"Task is '{task.status}' and cannot be reassigned.")
    task.assigned_to = assignee
    _append_event_guarded(
        db, task, step=STEP_REASSIGN, actor=actor, actor_roles=actor_roles,
        comment=comment, from_status=task.status, to_status=task.status,
        ip_address=ip_address,
    )
    return {"task_id": str(task.id), "assigned_to": assignee, "status": task.status}


def terminate(
    db: Session,
    conn: Connection,
    entity: Entity,
    task: WorkflowTask,
    *,
    actor: str,
    reason: str,
    actor_roles: Optional[List[str]] = None,
    ip_address: Optional[str] = None,
) -> dict:
    """Admin: force-close a stuck task. Zero golden-record impact (GC-6/GC-3).

    The staging row is moved to a terminal status and the task to 'terminated';
    the golden tier is never touched.
    """
    from_status = task.status
    if from_status in TERMINAL_STATUSES:
        raise WorkflowError(f"Task is already '{from_status}'.")
    if entity is not None and conn is not None:
        # Staging status is written on ``conn``; keep the task advance + event on
        # the SAME transaction so they commit atomically with it (MAJOR-2). Do not
        # also mutate the ORM ``task`` here — that would split the write across two
        # transactions again.
        _set_staging_status(conn, entity, task.staging_id, TERMINATED)
        advance_task_core(conn, task.id, TERMINATED)
        append_event_core(
            conn, task.id, step=STEP_TERMINATE, actor=actor,
            actor_roles=actor_roles, comment=reason, from_status=from_status,
            to_status=TERMINATED, ip_address=ip_address,
        )
    else:
        # The entity/table was dropped out from under a stuck task: there is no
        # tier write, so the task advance + event ride the ORM session as a single
        # atomic transaction.
        task.status = TERMINATED
        append_event(
            db, task, step=STEP_TERMINATE, actor=actor, actor_roles=actor_roles,
            comment=reason, from_status=from_status, to_status=TERMINATED,
            ip_address=ip_address,
        )
    return {"task_id": str(task.id), "staging_id": task.staging_id,
            "status": TERMINATED, "reason": reason}


# ---------------------------------------------------------------- serialisation
def task_to_dict(task: WorkflowTask, *, age_seconds: Optional[float] = None) -> dict:
    out = {
        "task_id": str(task.id),
        "entity_name": task.entity_name,
        "staging_id": task.staging_id,
        "domain": task.domain,
        "status": task.status,
        "submitted_by": task.submitted_by,
        "submit_rationale": task.submit_rationale,
        "assigned_to": task.assigned_to,
        "claimed_by": task.claimed_by,
        "claimed_at": task.claimed_at,
        "priority": task.priority,
        "change_type": task.change_type,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    }
    if age_seconds is not None:
        out["age_seconds"] = age_seconds
    return out


def event_to_dict(event: WorkflowEvent) -> dict:
    return {
        "seq": event.seq,
        "step": event.step,
        "actor": event.actor,
        "actor_roles": event.actor_roles,
        "comment": event.comment,
        "from_status": event.from_status,
        "to_status": event.to_status,
        "occurred_at": event.occurred_at,
        "ip_address": event.ip_address,
    }
