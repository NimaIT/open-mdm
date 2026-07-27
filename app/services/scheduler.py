"""In-process background scheduler and downstream-distribution jobs (W5).

A deliberately small, dependency-free scheduler (stdlib threads only — no
APScheduler). A single daemon thread wakes on a short tick and runs each job
when its interval has elapsed. It exists to drive two jobs:

  * ``refresh_views``  — refresh every published entity's ``mdm_pub`` matview.
  * ``run_retention``  — prune aged landing / history rows (never golden data).

Design guarantees:
  * **Opt-in.** Nothing starts unless ``SCHEDULER_ENABLED`` is set; the FastAPI
    lifespan is the only place the module singleton is started/stopped.
  * **Clean start/stop.** ``start()`` is guarded against a double-start; the
    worker is a daemon thread driven by a ``threading.Event`` so ``stop()`` sets
    the event and joins with a timeout — no busy-wait, no leaked thread.
  * **Per-job exception isolation.** Each job invocation is wrapped, so a failing
    refresh or retention run is recorded and the loop continues; it can never
    kill the scheduler thread or stop the other jobs.

The job callables (``refresh_views`` / ``run_retention``) are plain module
functions with no thread dependency, so they can be invoked directly by the
admin endpoints and by tests for deterministic behaviour.
"""
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional

from sqlalchemy import text

from app.config import settings
from app.db import get_ddl_engine, get_engine, session_scope
from app.models import Domain, Entity
from app.services.identifiers import qualified, quote_ident, validate_ident

log = logging.getLogger(__name__)


# --------------------------------------------------------------- entity lookup
def _published_names(db, entity_name: Optional[str] = None) -> List[str]:
    """Names of deployed (published/modified) entities, optionally one by name."""
    q = db.query(Entity)
    if entity_name:
        q = q.filter(Entity.name == entity_name.lower())
    return [e.name for e in q.order_by(Entity.name).all() if e.is_deployed]


# ------------------------------------------------------- job: refresh matviews
def _refresh_one(engine, name: str) -> Dict:
    """Refresh one matview, preferring CONCURRENTLY, falling back to a full
    refresh (e.g. when the view has never been populated or lacks the unique
    index a concurrent refresh needs)."""
    mv = qualified(settings.SCHEMA_PUBLISH, validate_ident(name))
    try:
        with engine.begin() as conn:
            conn.execute(text(f"REFRESH MATERIALIZED VIEW CONCURRENTLY {mv}"))
        return {"ok": True, "mode": "concurrent"}
    except Exception as exc:  # noqa: BLE001 — fall back to a non-concurrent refresh
        try:
            with engine.begin() as conn:
                conn.execute(text(f"REFRESH MATERIALIZED VIEW {mv}"))
            return {"ok": True, "mode": "full", "note": exc.__class__.__name__}
        except Exception as exc2:  # noqa: BLE001 — report, never raise
            return {"ok": False, "error": f"{exc2.__class__.__name__}: {exc2}"}


def refresh_views(entity_name: Optional[str] = None) -> Dict:
    """Refresh the distribution matview for every published entity (or one).

    Uses the DDL engine because ``REFRESH MATERIALIZED VIEW`` requires ownership
    / elevated privilege. Safe to call directly (admin endpoint, tests)."""
    with session_scope() as db:
        names = _published_names(db, entity_name)
    engine = get_ddl_engine()
    results = {name: _refresh_one(engine, name) for name in names}
    return {
        "refreshed": results,
        "count": len(results),
        "ran_at": datetime.now(timezone.utc).isoformat(),
    }


# ------------------------------------------------------------- job: retention
def _resolve_retention_days(entity: Entity, domains: Dict[str, Domain]) -> Optional[int]:
    """Entity's own retention_days, else its domain default, else None."""
    if entity.retention_days is not None:
        return entity.retention_days
    if entity.domain:
        dom = domains.get(entity.domain)
        if dom is not None and dom.retention_days is not None:
            return dom.retention_days
    return None


def _prune_one(engine, name: str, days: int, cutoff: datetime) -> Dict:
    """DELETE aged landing + history rows for one entity.

    Golden records (``mdm``) and the append-only audit / workflow tables are
    NEVER touched — only landing and history, which carry no immutability trigger
    and are safe to prune. All timestamps are passed as bind parameters.
    """
    name = validate_ident(name, kind="entity name")
    landing_t = qualified(settings.SCHEMA_LANDING, name)
    history_t = qualified(settings.SCHEMA_HISTORY, name)
    result: Dict = {
        "retention_days": days,
        "cutoff": cutoff.isoformat(),
        "landing": 0,
        "history": 0,
    }
    try:
        with engine.begin() as conn:
            r1 = conn.execute(
                text(f"DELETE FROM {landing_t} WHERE mdm_received_at < :c"),
                {"c": cutoff},
            )
            result["landing"] = r1.rowcount or 0
            r2 = conn.execute(
                text(f"DELETE FROM {history_t} WHERE mdm_valid_to < :c"),
                {"c": cutoff},
            )
            result["history"] = r2.rowcount or 0
    except Exception as exc:  # noqa: BLE001 — isolate one entity's failure
        result["error"] = f"{exc.__class__.__name__}: {exc}"
    return result


def run_retention(entity_name: Optional[str] = None, now: Optional[datetime] = None) -> Dict:
    """Prune aged landing / history rows for each entity with a retention policy.

    An entity's ``retention_days`` wins; otherwise its domain default applies.
    Entities with neither are skipped. Runs on the runtime (DML) engine — DELETE
    on landing / history only. Safe to call directly (admin endpoint, tests)."""
    now = now or datetime.now(timezone.utc)
    results: Dict = {}
    specs: List[tuple] = []
    with session_scope() as db:
        domains = {d.name: d for d in db.query(Domain).all()}
        entities = {e.name: e for e in db.query(Entity).all()}
        for name in _published_names(db, entity_name):
            days = _resolve_retention_days(entities[name], domains)
            if days is None:
                results[name] = {"skipped": "no retention_days configured"}
                continue
            specs.append((name, days))
    engine = get_engine()
    for name, days in specs:
        cutoff = now - timedelta(days=days)
        results[name] = _prune_one(engine, name, days, cutoff)
    return {
        "retention": results,
        "ran_at": now.isoformat(),
    }


# ----------------------------------------------------- distribution status (read)
def distribution_status() -> List[Dict]:
    """Per-entity distribution surface: matview name, existence, population."""
    with session_scope() as db:
        names = _published_names(db)
    populated: Dict[str, bool] = {}
    try:
        with get_engine().connect() as conn:
            rows = conn.execute(
                text(
                    "select matviewname, ispopulated from pg_matviews "
                    "where schemaname = :s"
                ),
                {"s": settings.SCHEMA_PUBLISH},
            ).all()
        populated = {r[0]: r[1] for r in rows}
    except Exception as exc:  # noqa: BLE001 — status is best-effort
        log.warning("Could not read pg_matviews: %s", exc)
    out: List[Dict] = []
    for name in names:
        out.append(
            {
                "entity": name,
                "matview": f"{settings.SCHEMA_PUBLISH}.{name}",
                "exists": name in populated,
                "populated": bool(populated.get(name, False)),
            }
        )
    return out


# --------------------------------------------------------------- the scheduler
class Job:
    """One periodic unit of work: run ``func`` every ``interval`` seconds."""

    def __init__(self, name: str, interval: float, func: Callable[[], object]):
        self.name = name
        self.interval = float(interval)
        self.func = func


class Scheduler:
    """A single-thread, tick-driven periodic runner (stdlib only).

    ``jobs`` and ``tick`` may be injected for testing; otherwise the jobs and
    intervals are built from settings on ``start()``.
    """

    def __init__(
        self,
        jobs: Optional[List[Job]] = None,
        tick: Optional[float] = None,
    ):
        self._jobs_override = jobs
        self._tick_override = tick
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.jobs: List[Job] = []
        self.tick: float = 1.0
        # name -> {started_at, finished_at, ok, result|error}
        self.last_runs: Dict[str, Dict] = {}

    # ---- lifecycle
    def _build_jobs(self) -> List[Job]:
        if self._jobs_override is not None:
            return list(self._jobs_override)
        jobs = [
            Job("refresh_views", settings.VIEW_REFRESH_INTERVAL_SECONDS,
                lambda: refresh_views()),
        ]
        if settings.RETENTION_ENABLED:
            jobs.append(
                Job("run_retention", settings.RETENTION_INTERVAL_SECONDS,
                    lambda: run_retention())
            )
        return jobs

    def start(self) -> bool:
        """Start the worker thread. Returns False if already running (no-op)."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self.jobs = self._build_jobs()
            min_interval = min((j.interval for j in self.jobs), default=1.0)
            self.tick = (
                self._tick_override
                if self._tick_override is not None
                else min(max(min_interval / 2.0, 0.05), 5.0)
            )
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="mdm-scheduler", daemon=True
            )
            self._thread.start()
            return True

    def stop(self, timeout: float = 5.0) -> bool:
        """Signal the worker to stop and join it. Returns True if it joined."""
        with self._lock:
            thread = self._thread
        if thread is None:
            return False
        self._stop.set()
        thread.join(timeout=timeout)
        joined = not thread.is_alive()
        with self._lock:
            if joined:
                self._thread = None
        return joined

    def is_running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    # ---- the loop
    def _run(self) -> None:
        monotonic = time.monotonic
        next_due = {j.name: monotonic() + j.interval for j in self.jobs}
        while not self._stop.is_set():
            now = monotonic()
            for job in self.jobs:
                if self._stop.is_set():
                    break
                if now >= next_due.get(job.name, 0.0):
                    self._run_job(job)
                    next_due[job.name] = monotonic() + job.interval
            # Sleep responsively: wait() returns immediately once stop is set.
            self._stop.wait(self.tick)

    def _run_job(self, job: Job) -> None:
        started = datetime.now(timezone.utc)
        record: Dict = {"started_at": started.isoformat(), "ok": True}
        try:
            record["result"] = job.func()
        except Exception as exc:  # noqa: BLE001 — a job must never kill the thread
            record["ok"] = False
            record["error"] = f"{exc.__class__.__name__}: {exc}"
            log.exception("Scheduler job '%s' failed", job.name)
        record["finished_at"] = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self.last_runs[job.name] = record

    def last_run_summary(self) -> Dict[str, Dict]:
        with self._lock:
            return {k: dict(v) for k, v in self.last_runs.items()}


# --------------------------------------------------------------- module singleton
_scheduler: Optional[Scheduler] = None
_singleton_lock = threading.Lock()


def get_scheduler() -> Scheduler:
    global _scheduler
    with _singleton_lock:
        if _scheduler is None:
            _scheduler = Scheduler()
        return _scheduler


def start_scheduler() -> bool:
    return get_scheduler().start()


def stop_scheduler(timeout: float = 5.0) -> bool:
    return get_scheduler().stop(timeout=timeout)


def scheduler_info() -> Dict:
    """Status report for the admin endpoint (GET /admin/scheduler)."""
    sched = get_scheduler()
    return {
        "enabled": settings.SCHEDULER_ENABLED,
        "running": sched.is_running(),
        "retention_enabled": settings.RETENTION_ENABLED,
        "intervals": {
            "view_refresh_seconds": settings.VIEW_REFRESH_INTERVAL_SECONDS,
            "retention_seconds": settings.RETENTION_INTERVAL_SECONDS,
        },
        "last_runs": sched.last_run_summary(),
    }
