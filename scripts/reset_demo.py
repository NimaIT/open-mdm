"""Clean slate for the demo: drop every generated entity + its tables and data.

Removes ALL master-data entities and their physical landing / staging / live /
history tables plus the mdm_pub materialized view, then clears the workflow,
notification, field-mapping and promotion metadata and any non-admin demo users
and API keys. The built-in ``default`` domain and the break-glass ``admin`` user
are preserved.

Append-only history tables (``audit_event`` / ``workflow_event``) carry a
BEFORE UPDATE OR DELETE trigger, so they are cleared with TRUNCATE (which does
not fire the trigger) rather than row DELETEs.

Idempotent and safe to re-run: table drops use IF EXISTS, so it completes even
if some objects are already gone.

    python -m scripts.reset_demo
"""
import sys
from typing import List

from sqlalchemy import text

from app.config import settings
from app.db import get_ddl_engine, session_scope
from app.models import Entity
from app.services.ddl import build_drop_plan
from app.services.identifiers import qualified


def _entity_names() -> List[str]:
    with session_scope() as db:
        return [e.name for e in db.query(Entity).order_by(Entity.name).all()]


def drop_entity_tables(names: List[str]) -> int:
    """Drop the four tiers + distribution matview for every entity (IF EXISTS)."""
    engine = get_ddl_engine()
    dropped = 0
    for name in names:
        try:
            plan = build_drop_plan(name, cascade=True)
            with engine.begin() as conn:
                for stmt in plan.statements:
                    conn.execute(text(stmt))
            dropped += 1
        except Exception as exc:  # noqa: BLE001 — best effort, keep going
            print(f"  ! could not drop tables for '{name}': {exc}")
    return dropped


def clear_metadata() -> dict:
    """Delete metadata + demo rows, preserving the default domain and admin user.

    ``audit_event`` and ``workflow_event`` are append-only (mutation trigger), so
    they are TRUNCATEd; ``workflow_task`` is truncated with them to satisfy the
    FK. Everything else is a plain DELETE on mutable tables.
    """
    m = settings.SCHEMA_META
    counts: dict = {}
    engine = get_ddl_engine()
    with engine.begin() as conn:
        # Append-only history + the tasks they reference: TRUNCATE (no trigger).
        conn.execute(
            text(
                f"TRUNCATE {qualified(m, 'workflow_event')}, "
                f"{qualified(m, 'workflow_task')}, "
                f"{qualified(m, 'audit_event')} RESTART IDENTITY"
            )
        )
        counts["workflow_and_audit"] = "truncated"

        for table in (
            "notification",
            "notification_template",
            "field_mapping",
            "promotion_batch",
            "model_version",
            "attribute",
            "entity",
        ):
            res = conn.execute(text(f"DELETE FROM {qualified(m, table)}"))
            counts[table] = res.rowcount

        # Demo users + API keys: keep only the break-glass admin.
        res = conn.execute(
            text(
                f"DELETE FROM {qualified(m, 'app_user')} "
                "WHERE username <> :admin"
            ),
            {"admin": settings.LOCAL_ADMIN_USERNAME},
        )
        counts["app_user"] = res.rowcount
        res = conn.execute(text(f"DELETE FROM {qualified(m, 'api_key')}"))
        counts["api_key"] = res.rowcount

        # Governance domains: keep only the built-in default.
        res = conn.execute(
            text(f"DELETE FROM {qualified(m, 'domain')} WHERE name <> 'default'")
        )
        counts["domain"] = res.rowcount
    return counts


def main() -> int:
    names = _entity_names()
    print(f"Resetting demo: {len(names)} entity/entities found: {names or '(none)'}")

    dropped = drop_entity_tables(names)
    print(f"Dropped physical tables (4 tiers + matview) for {dropped} entity/entities.")

    counts = clear_metadata()
    print("Cleared metadata:")
    for k, v in counts.items():
        print(f"  {k:22} {v}")

    print("\nClean slate ready. Kept: the 'default' domain and the "
          f"'{settings.LOCAL_ADMIN_USERNAME}' break-glass admin.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
