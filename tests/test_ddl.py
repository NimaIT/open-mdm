"""DDL generation, migration diffing and destructive-change guards.

Runs against a real cluster: the point of these tests is that the generated
SQL actually executes and produces the intended physical shape.
"""
import pytest
from sqlalchemy import text

from app.config import settings
from app.db import get_ddl_engine
from app.services.ddl import (
    DDLPlan,
    apply_plan,
    build_alter_plan,
    build_create_plan,
    build_drop_plan,
    reflect_columns,
    table_exists,
)
from tests.conftest import requires_db

pytestmark = requires_db

TIERS = {
    "landing": settings.SCHEMA_LANDING,
    "staging": settings.SCHEMA_STAGING,
    "live": settings.SCHEMA_LIVE,
    "history": settings.SCHEMA_HISTORY,
}


class TestCreate:
    def test_creates_all_four_tiers(self, entity_factory):
        ent = entity_factory()
        with get_ddl_engine().connect() as c:
            for schema in TIERS.values():
                assert table_exists(c, schema, ent.name), schema

    def test_landing_is_schemaless_jsonb(self, entity_factory):
        """Landing must accept anything — capture first, validate later."""
        ent = entity_factory()
        with get_ddl_engine().connect() as c:
            cols = reflect_columns(c, settings.SCHEMA_LANDING, ent.name)
        assert cols["mdm_payload"]["data_type"] == "jsonb"
        # Business columns do NOT exist on landing.
        assert "code" not in cols

    def test_live_enforces_not_null_but_staging_does_not(self, entity_factory):
        """Staging must be able to hold invalid rows for steward repair."""
        ent = entity_factory()
        with get_ddl_engine().connect() as c:
            live = reflect_columns(c, settings.SCHEMA_LIVE, ent.name)
            staging = reflect_columns(c, settings.SCHEMA_STAGING, ent.name)
        assert live["code"]["nullable"] is False
        assert staging["code"]["nullable"] is True

    def test_history_has_no_not_null_on_business_columns(self, entity_factory):
        ent = entity_factory()
        with get_ddl_engine().connect() as c:
            hist = reflect_columns(c, settings.SCHEMA_HISTORY, ent.name)
        assert hist["code"]["nullable"] is True
        assert "mdm_valid_from" in hist and "mdm_valid_to" in hist

    def test_types_render_correctly(self, entity_factory):
        ent = entity_factory()
        with get_ddl_engine().connect() as c:
            live = reflect_columns(c, settings.SCHEMA_LIVE, ent.name)
        assert live["code"]["length"] == 40
        assert live["amount"]["data_type"] == "numeric"
        assert live["amount"]["precision"] == 12
        assert live["active"]["data_type"] == "boolean"

    def test_unique_index_excludes_soft_deleted(self, entity_factory):
        """A soft-deleted row must not block reuse of its business key."""
        ent = entity_factory()
        with get_ddl_engine().connect() as c:
            defn = c.execute(
                text("select indexdef from pg_indexes where schemaname=:s "
                     "and tablename=:t and indexname=:i"),
                {"s": settings.SCHEMA_LIVE, "t": ent.name,
                 "i": f"uq_{ent.name}_code"},
            ).scalar()
        assert defn and "mdm_is_deleted = false" in defn

    def test_idempotency_index_present_on_landing(self, entity_factory):
        ent = entity_factory()
        with get_ddl_engine().connect() as c:
            names = [r[0] for r in c.execute(
                text("select indexname from pg_indexes where schemaname=:s "
                     "and tablename=:t"),
                {"s": settings.SCHEMA_LANDING, "t": ent.name})]
        assert f"uq_{ent.name}_landing_idem" in names

    def test_rerun_is_idempotent(self, entity_factory):
        ent = entity_factory()
        with get_ddl_engine().begin() as c:
            apply_plan(c, build_create_plan(ent))  # must not raise

    def test_warns_without_match_key(self, entity_factory, make_attr):
        ent = entity_factory(
            attributes=[make_attr("only_col", "string", length=10)],
            publish=False,
        )
        plan = build_create_plan(ent)
        assert any("match key" in w.lower() for w in plan.warnings)
        assert any("business key" in w.lower() for w in plan.warnings)


class TestAlter:
    def test_additive_column_is_applied(self, entity_factory, make_attr, db):
        ent = entity_factory()
        ent.attributes.append(make_attr("extra", "string", length=25, position=9))
        db.flush()
        with get_ddl_engine().connect() as c:
            plan = build_alter_plan(c, ent)
        assert not plan.is_destructive
        with get_ddl_engine().begin() as c:
            apply_plan(c, plan)
        with get_ddl_engine().connect() as c:
            for schema in (settings.SCHEMA_STAGING, settings.SCHEMA_LIVE,
                           settings.SCHEMA_HISTORY):
                assert "extra" in reflect_columns(c, schema, ent.name)

    def test_widening_is_applied(self, entity_factory, db):
        """Regression: length was previously ignored, so widenings were skipped."""
        ent = entity_factory()
        next(a for a in ent.attributes if a.name == "label").length = 400
        db.flush()
        with get_ddl_engine().connect() as c:
            plan = build_alter_plan(c, ent)
        assert not plan.is_destructive
        with get_ddl_engine().begin() as c:
            apply_plan(c, plan)
        with get_ddl_engine().connect() as c:
            assert reflect_columns(
                c, settings.SCHEMA_LIVE, ent.name)["label"]["length"] == 400

    def test_narrowing_is_flagged_destructive(self, entity_factory, db):
        """Regression: varchar(120)->varchar(20) silently truncates."""
        ent = entity_factory()
        next(a for a in ent.attributes if a.name == "label").length = 20
        db.flush()
        with get_ddl_engine().connect() as c:
            plan = build_alter_plan(c, ent)
        assert plan.is_destructive
        assert any("label" in d for d in plan.destructive)

    def test_removed_column_is_flagged_destructive(self, entity_factory, db):
        ent = entity_factory()
        ent.attributes = [a for a in ent.attributes if a.name != "category"]
        db.flush()
        with get_ddl_engine().connect() as c:
            plan = build_alter_plan(c, ent)
        assert plan.is_destructive
        assert any("category" in d for d in plan.destructive)

    def test_apply_refuses_destructive_without_confirmation(
        self, entity_factory, db
    ):
        ent = entity_factory()
        ent.attributes = [a for a in ent.attributes if a.name != "category"]
        db.flush()
        with get_ddl_engine().connect() as c:
            plan = build_alter_plan(c, ent)
        with pytest.raises(PermissionError):
            with get_ddl_engine().begin() as c:
                apply_plan(c, plan)

    def test_no_change_yields_empty_plan(self, entity_factory):
        ent = entity_factory()
        with get_ddl_engine().connect() as c:
            plan = build_alter_plan(c, ent)
        # Index creation statements are IF NOT EXISTS, so a no-op plan may
        # contain them, but there must be no ALTERs and nothing destructive.
        assert not plan.is_destructive
        assert not [s for s in plan.statements if "ALTER TABLE" in s]

    def test_recreates_a_missing_tier(self, entity_factory):
        """Operational reality: someone drops a table by hand."""
        ent = entity_factory()
        with get_ddl_engine().begin() as c:
            c.execute(text(
                f'DROP TABLE "{settings.SCHEMA_HISTORY}"."{ent.name}"'))
        with get_ddl_engine().connect() as c:
            plan = build_alter_plan(c, ent)
        with get_ddl_engine().begin() as c:
            apply_plan(c, plan)
        with get_ddl_engine().connect() as c:
            assert table_exists(c, settings.SCHEMA_HISTORY, ent.name)


class TestDrop:
    def test_drop_plan_is_always_destructive(self):
        plan = build_drop_plan("some_entity")
        assert plan.is_destructive
        assert len(plan.destructive) == 4


class TestPlanSerialisation:
    def test_to_dict_shape(self):
        plan = DDLPlan()
        plan.add("SELECT 1")
        plan.warnings.append("w")
        d = plan.to_dict()
        assert d["statement_count"] == 1
        assert d["sql"].endswith(";")
        assert d["is_destructive"] is False
