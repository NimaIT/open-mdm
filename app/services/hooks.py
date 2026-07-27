"""Custom processing logic at pipeline commit events (EX-1).

Three hook points let operators inject cleansing, defaulting and downstream
chaining without editing core pipeline code:

  * ``pre_stage``   — during ingestion, BEFORE the staging insert. May mutate the
                      coerced ``values`` dict and may append structured errors.
  * ``pre_commit``  — BEFORE ``apply_staging_to_live`` writes the golden record.
                      May mutate ``values`` or ABORT the apply.
  * ``post_commit`` — AFTER the golden write. For chaining subsequent processing
                      (e.g. enqueue a downstream refresh).

A hook receives a :class:`HookContext` (attribute *and* item access) carrying the
fields relevant to its phase: ``event, entity, operation, values, errors, actor,
staging_id, mdm_id, result, conn, db``.

Failure isolation is the critical contract (see the run_* functions):
  * ``pre_stage``  exceptions  -> structured row error (row invalid), never a 500.
  * ``pre_commit`` exceptions  -> clean abort (PipelineError), golden NOT written.
  * ``post_commit`` exceptions -> caught, logged, SWALLOWED, recorded in result;
                                  they never roll back the committed golden write.

Operators register hooks from modules named in ``settings.HOOK_MODULES`` — these
are imported at startup (:func:`load_hook_modules`); an import failure is logged,
not fatal.
"""
import importlib
import logging
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from app.config import settings
from app.services.logging_config import stream_logger

# Operator-supplied hooks are "custom logic" -> "custom" stream (AO-1).
log = stream_logger("custom")

PRE_STAGE = "pre_stage"
PRE_COMMIT = "pre_commit"
POST_COMMIT = "post_commit"
EVENTS = (PRE_STAGE, PRE_COMMIT, POST_COMMIT)


@dataclass
class HookContext:
    """The argument passed to every hook. Supports attribute and item access."""

    event: str
    entity: str
    operation: Optional[str] = None
    values: Optional[Dict[str, Any]] = None
    errors: Optional[List[Any]] = None
    actor: Optional[str] = None
    staging_id: Optional[int] = None
    mdm_id: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    conn: Any = None
    db: Any = None

    def __getitem__(self, key):
        return getattr(self, key)

    def get(self, key, default=None):
        return getattr(self, key, default)


@dataclass
class _Registration:
    name: str
    func: Callable[[HookContext], Any]
    entity: Optional[str] = None


_REGISTRY: Dict[str, List[_Registration]] = {e: [] for e in EVENTS}
# Registration is startup-only, but guard registry mutation against concurrent
# iteration (a hot request reading while a module registers) so neither sees a
# half-mutated list.
_LOCK = threading.Lock()


# ------------------------------------------------------------------ registry
def register_hook(
    event: str,
    func: Callable[[HookContext], Any],
    *,
    entity: Optional[str] = None,
    name: Optional[str] = None,
) -> _Registration:
    """Register ``func`` at ``event``. ``entity=None`` runs it for every entity;
    a name scopes it to that entity only."""
    if event not in EVENTS:
        raise ValueError(f"Unknown hook event '{event}'. Valid: {', '.join(EVENTS)}")
    reg = _Registration(name=name or getattr(func, "__name__", "hook"),
                        func=func, entity=entity)
    with _LOCK:
        _REGISTRY[event].append(reg)
    return reg


def hook(event: str, entity: Optional[str] = None, name: Optional[str] = None):
    """Decorator form of :func:`register_hook`."""

    def _deco(func):
        register_hook(event, func, entity=entity, name=name)
        return func

    return _deco


def clear_hooks(event: Optional[str] = None) -> None:
    """Remove registered hooks. Tests call this to avoid cross-test leakage."""
    with _LOCK:
        if event is None:
            for e in EVENTS:
                _REGISTRY[e].clear()
        elif event in _REGISTRY:
            _REGISTRY[event].clear()


def _matching(event: str, entity_name: Optional[str]) -> List[_Registration]:
    # Snapshot under the lock so a concurrent registration can't mutate the list
    # mid-iteration.
    with _LOCK:
        return [r for r in _REGISTRY[event]
                if r.entity is None or r.entity == entity_name]


def has_hooks(event: str, entity_name: Optional[str] = None) -> bool:
    return bool(_matching(event, entity_name))


# ------------------------------------------------------------- module loading
def load_hook_modules(modules: Optional[List[str]] = None) -> Dict[str, List]:
    """Import each module path so its hook/transform registrations take effect.

    Import failures are logged and collected, never raised — a bad operator
    module must not stop the application from starting.
    """
    modules = list(settings.HOOK_MODULES if modules is None else modules)
    loaded: List[str] = []
    failed: List[Dict[str, str]] = []
    for path in modules:
        try:
            importlib.import_module(path)
            loaded.append(path)
            log.info("Loaded hook module '%s'", path)
        except Exception as exc:  # never fatal
            log.warning("Could not load hook module '%s': %s", path, exc)
            failed.append({"module": path, "error": str(exc)})
    return {"loaded": loaded, "failed": failed}


# ------------------------------------------------------------- invocation
@contextmanager
def _hook_savepoints(conn: Any, db: Any):
    """Isolate a single hook invocation in nested SAVEPOINTs on BOTH the Core
    connection and the ORM Session, so any DB work the hook does — via ``ctx.conn``
    *or* ``ctx.db`` — is unwound together if the hook raises, and committed
    together if it succeeds.

    Either leg may be ``None`` (that leg is skipped). The Session's pending state
    is flushed before its savepoint so a rollback unwinds only the hook's own
    writes, never work the surrounding pipeline already staged on the Session.
    No savepoint is opened unless there is a hook to run — the caller only enters
    this block from inside its per-hook loop, so the zero-hook path pays nothing.
    """
    conn_sp = conn.begin_nested() if conn is not None else None
    db_sp = None
    if db is not None:
        db.flush()
        db_sp = db.begin_nested()
    try:
        yield
    except Exception:
        if db_sp is not None:
            db_sp.rollback()
        if conn_sp is not None:
            conn_sp.rollback()
        raise
    else:
        if db_sp is not None:
            db_sp.commit()
        if conn_sp is not None:
            conn_sp.commit()


def run_pre_stage(
    entity_name: str,
    *,
    values: Dict[str, Any],
    errors: List[Any],
    operation: Optional[str] = None,
    actor: Optional[str] = None,
    db: Any = None,
    conn: Any = None,
) -> HookContext:
    """Run pre_stage hooks. A raising hook is converted into a structured error
    attached to the row (it becomes invalid in staging) — never a 500, never a
    poisoned transaction. Hooks mutate ``values``/``errors`` in place.

    Each hook is isolated in nested SAVEPOINTs on the connection AND the Session:
    a hook that runs SQL then raises is rolled back to the savepoint, so the outer
    promotion-batch transaction is never left aborted (which would otherwise
    poison every remaining row's staging INSERT with 'transaction is aborted')."""
    ctx = HookContext(event=PRE_STAGE, entity=entity_name, values=values,
                      errors=errors, operation=operation, actor=actor, db=db,
                      conn=conn)
    for reg in _matching(PRE_STAGE, entity_name):
        try:
            with _hook_savepoints(conn, db):
                reg.func(ctx)
        except Exception as exc:
            log.exception(
                "pre_stage hook '%s' failed on %s", reg.name, entity_name,
                extra={"event": "hook_failed", "hook": reg.name,
                       "hook_event": PRE_STAGE, "entity": entity_name},
            )
            errors.append({
                "field": "_hook", "code": "hook_error",
                "message": f"pre_stage hook '{reg.name}' failed: {exc}",
            })
    return ctx


def run_pre_commit(
    entity_name: str,
    *,
    values: Dict[str, Any],
    operation: Optional[str] = None,
    actor: Optional[str] = None,
    staging_id: Optional[int] = None,
    mdm_id: Optional[str] = None,
    db: Any = None,
    conn: Any = None,
) -> HookContext:
    """Run pre_commit hooks BEFORE the golden write. A raising hook ABORTS the
    apply cleanly by raising ``PipelineError`` — leaving staging un-applied and
    pending, never a half-written golden record.

    Each hook is isolated in nested SAVEPOINTs on the connection AND the Session,
    so a hook that touches either and then errors cannot leave the outer apply
    transaction poisoned before the abort."""
    from app.services.pipeline import PipelineError  # lazy: avoid import cycle

    ctx = HookContext(event=PRE_COMMIT, entity=entity_name, values=values,
                      operation=operation, actor=actor, staging_id=staging_id,
                      mdm_id=mdm_id, db=db, conn=conn, errors=[])
    for reg in _matching(PRE_COMMIT, entity_name):
        try:
            with _hook_savepoints(conn, db):
                reg.func(ctx)
        except PipelineError:
            raise
        except Exception as exc:
            log.exception(
                "pre_commit hook '%s' aborted apply on %s", reg.name, entity_name,
                extra={"event": "hook_failed", "hook": reg.name,
                       "hook_event": PRE_COMMIT, "entity": entity_name},
            )
            raise PipelineError(
                f"pre_commit hook '{reg.name}' aborted the change: {exc}"
            ) from exc
    return ctx


def run_post_commit(
    entity_name: str,
    *,
    values: Optional[Dict[str, Any]] = None,
    operation: Optional[str] = None,
    actor: Optional[str] = None,
    staging_id: Optional[int] = None,
    mdm_id: Optional[str] = None,
    result: Optional[Dict[str, Any]] = None,
    db: Any = None,
    conn: Any = None,
) -> List[Dict[str, str]]:
    """Run post_commit hooks AFTER the golden write has committed. Exceptions are
    caught, logged and SWALLOWED so they can never roll back or break the
    committed change; each failure is returned so the caller can record it.

    Each hook runs in nested SAVEPOINTs on the connection AND the Session: a hook
    whose DB work (via ``ctx.conn`` or ``ctx.db``) errors is rolled back to the
    savepoint, keeping the connection/session usable for the remaining hooks and
    the final commit while leaving no partial hook writes behind."""
    ctx = HookContext(event=POST_COMMIT, entity=entity_name, values=values,
                      operation=operation, actor=actor, staging_id=staging_id,
                      mdm_id=mdm_id, result=result, db=db, conn=conn, errors=[])
    hook_errors: List[Dict[str, str]] = []
    for reg in _matching(POST_COMMIT, entity_name):
        try:
            with _hook_savepoints(conn, db):
                reg.func(ctx)
        except Exception as exc:  # swallowed — must never break the golden write
            log.exception(
                "post_commit hook '%s' failed (swallowed) on %s",
                reg.name, entity_name,
                extra={"event": "hook_failed", "hook": reg.name,
                       "hook_event": POST_COMMIT, "entity": entity_name},
            )
            hook_errors.append({"hook": reg.name, "error": str(exc)})
    return hook_errors
