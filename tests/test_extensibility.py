"""Workstream 6 — Extensibility (EX-1 hooks, EX-2 transforms, EX-3 mappings).

Behavioural tests driven through the pipeline service, mirroring test_pipeline.
Every hook/transform registration is scoped and torn down (autouse fixture) so
the process-wide registries never leak between cases.
"""
import uuid

import pytest
from sqlalchemy import text

from app.config import settings
from app.db import get_engine
from app.models import AuditEvent, FieldMapping
from app.services import hooks, transforms
from app.services.identifiers import qualified
from app.services.pipeline import (
    OP_INSERT,
    PipelineError,
    apply_staging_to_live,
    promote_landing_to_staging,
    run_post_commit_hooks,
    write_to_landing,
)
from tests.conftest import requires_db

pytestmark = requires_db


# --------------------------------------------------------------- helpers
def _land(ent, payload, *, operation=OP_INSERT, **kw):
    with get_engine().begin() as c:
        return write_to_landing(c, ent, operation=operation, payload=payload, **kw)


def _promote(db, ent, actor="system"):
    with get_engine().begin() as c:
        return promote_landing_to_staging(db, c, ent, actor=actor)


def _approve(db, ent, sid, actor="steward_b", **kw):
    # Mirror the endpoints: apply in one transaction, then fire post_commit hooks
    # in a FRESH transaction only AFTER that apply has committed (FIX 3).
    with get_engine().begin() as c:
        res = apply_staging_to_live(
            db, c, ent, sid, actor=actor, actor_roles=["steward"], **kw
        )
    run_post_commit_hooks(ent, res, actor=actor, db=db)
    return res


def _staging_row(ent, sid):
    t = qualified(settings.SCHEMA_STAGING, ent.name)
    with get_engine().connect() as c:
        return c.execute(
            text(f"select * from {t} where mdm_staging_id=:i"), {"i": sid}
        ).mappings().first()


def _live_rows(ent):
    t = qualified(settings.SCHEMA_LIVE, ent.name)
    with get_engine().connect() as c:
        return c.execute(
            text(f"select * from {t} where not mdm_is_deleted")
        ).mappings().all()


def _landing_row(ent, landing_id):
    t = qualified(settings.SCHEMA_LANDING, ent.name)
    with get_engine().connect() as c:
        return c.execute(
            text(f"select * from {t} where mdm_landing_id=:i"), {"i": landing_id}
        ).mappings().first()


@pytest.fixture(autouse=True)
def _clean_registries():
    """Keep the process-wide hook and transform registries clean per test."""
    hooks.clear_hooks()
    transforms.reset_transforms()
    yield
    hooks.clear_hooks()
    transforms.reset_transforms()


@pytest.fixture
def field_mapping(db):
    created = []

    def _make(**kw):
        m = FieldMapping(created_by="pytest", updated_by="pytest", **kw)
        db.add(m)
        db.flush()
        created.append(m.id)
        return m

    yield _make
    for mid in created:
        try:
            db.query(FieldMapping).filter(FieldMapping.id == mid).delete()
        except Exception:
            pass
    db.commit()


def _txt(name, **kw):
    from tests.conftest import _attr

    kw.setdefault("length", 80)
    return _attr(name, "string", **kw)


# ===================================================================== EX-1
class TestHooks:
    def test_pre_stage_mutates_values_into_staging(self, entity_factory, db):
        ent = entity_factory()

        def force_amount(ctx):
            ctx.values["amount"] = 999

        hooks.register_hook("pre_stage", force_amount, entity=ent.name)
        _land(ent, {"code": "hk1", "label": "L"})
        promo = _promote(db, ent)
        row = _staging_row(ent, promo["results"][0]["staging_id"])
        # The pre_stage hook's value landed in staging (and the row is valid).
        assert row["amount"] == 999
        assert row["mdm_is_valid"] is True

    def test_pre_stage_exception_becomes_row_error_not_500(self, entity_factory, db):
        ent = entity_factory()

        def boom(ctx):
            raise ValueError("cleansing exploded")

        hooks.register_hook("pre_stage", boom, entity=ent.name)
        _land(ent, {"code": "hk2", "label": "L"})
        result = _promote(db, ent)  # must NOT raise
        assert result["failed"] == 0  # row promoted, just held invalid
        sid = result["results"][0]["staging_id"]
        row = _staging_row(ent, sid)
        assert row["mdm_is_valid"] is False
        codes = {e["code"] for e in (row["mdm_errors"] or [])}
        assert "hook_error" in codes

    def test_pre_commit_aborts_apply_cleanly(self, entity_factory, db):
        ent = entity_factory()
        r = _land(ent, {"code": "hk3", "label": "L", "amount": "5",
                        "category": "alpha"})
        promo = _promote(db, ent)
        sid = promo["results"][0]["staging_id"]

        def abort(ctx):
            raise RuntimeError("downstream says no")

        hooks.register_hook("pre_commit", abort, entity=ent.name)
        with pytest.raises(PipelineError):
            _approve(db, ent, sid, review_note="ok")

        # Golden NOT written; staging left pending, never half-applied.
        assert _live_rows(ent) == []
        row = _staging_row(ent, sid)
        assert row["mdm_status"] == "pending_review"

    def test_post_commit_runs_after_successful_approve(self, entity_factory, db):
        ent = entity_factory()
        seen = []

        def chain(ctx):
            seen.append(ctx.mdm_id)

        hooks.register_hook("post_commit", chain, entity=ent.name)
        _land(ent, {"code": "hk4", "label": "L", "amount": "5", "category": "alpha"})
        promo = _promote(db, ent)
        sid = promo["results"][0]["staging_id"]
        res = _approve(db, ent, sid, review_note="ok")

        live = _live_rows(ent)
        assert len(live) == 1
        assert seen == [res["mdm_id"]]  # ran after the golden write, saw its id

    def test_throwing_post_commit_does_not_break_approve(self, entity_factory, db):
        ent = entity_factory()

        def bad_chain(ctx):
            raise RuntimeError("downstream refresh failed")

        hooks.register_hook("post_commit", bad_chain, entity=ent.name)
        _land(ent, {"code": "hk5", "label": "L", "amount": "5", "category": "alpha"})
        promo = _promote(db, ent)
        sid = promo["results"][0]["staging_id"]
        res = _approve(db, ent, sid, review_note="ok")  # must NOT raise

        # Golden still written; the swallowed failure is recorded on the result.
        assert len(_live_rows(ent)) == 1
        assert res["post_commit_errors"][0]["hook"] == "bad_chain"

    # ---- FIX 1: pre_stage SQL failure must not poison the whole batch
    def test_pre_stage_sql_failure_does_not_poison_batch(self, entity_factory, db):
        ent = entity_factory()

        def maybe_boom(ctx):
            # Run real SQL on the live promotion connection first...
            ctx.conn.execute(text("select 1"))
            # ...then fail at the DB for one row. Without savepoint isolation this
            # aborts the batch transaction and poisons every following INSERT.
            if ctx.values.get("code") == "BAD":
                ctx.conn.execute(text("select 1 / 0"))

        hooks.register_hook("pre_stage", maybe_boom, entity=ent.name)
        _land(ent, {"code": "good1", "label": "L"})
        _land(ent, {"code": "bad", "label": "L"})
        _land(ent, {"code": "good2", "label": "L"})
        result = _promote(db, ent)  # must NOT raise, batch not poisoned

        assert result["failed"] == 0
        by_code = {}
        for res in result["results"]:
            row = _staging_row(ent, res["staging_id"])
            by_code[row["code"]] = row
        # The good rows promoted valid despite the bad row's SQL failure.
        assert by_code["GOOD1"]["mdm_is_valid"] is True
        assert by_code["GOOD2"]["mdm_is_valid"] is True
        # The offending row is held invalid with a hook_error — not a batch abort.
        assert by_code["BAD"]["mdm_is_valid"] is False
        codes = {e["code"] for e in (by_code["BAD"]["mdm_errors"] or [])}
        assert "hook_error" in codes

    # ---- FIX 3: post_commit genuinely runs AFTER the apply commits
    def test_post_commit_reads_committed_golden_via_fresh_conn(
        self, entity_factory, db
    ):
        ent = entity_factory()
        seen = {}

        def reader(ctx):
            t = qualified(settings.SCHEMA_LIVE, ent.name)
            seen["code"] = ctx.conn.execute(
                text(f"select code from {t} where mdm_id = cast(:i as uuid)"),
                {"i": ctx.mdm_id},
            ).scalar()

        hooks.register_hook("post_commit", reader, entity=ent.name)
        _land(ent, {"code": "pc1", "label": "L", "amount": "5", "category": "alpha"})
        promo = _promote(db, ent)
        sid = promo["results"][0]["staging_id"]
        res = _approve(db, ent, sid, review_note="ok")

        # The hook ran on a FRESH connection after commit and SAW the golden row.
        assert seen["code"] == "PC1"
        assert res.get("post_commit_errors") is None

    # ---- FIX 2: a post_commit hook's db write is rolled back if it raises
    def test_post_commit_db_write_rolled_back_on_raise(self, entity_factory, db):
        ent = entity_factory()
        marker = f"pcmark_{uuid.uuid4().hex}"

        def writer(ctx):
            ctx.db.add(AuditEvent(actor=marker, action="hook_write",
                                  entity_name=ent.name, tier="live"))
            ctx.db.flush()
            raise RuntimeError("boom after a db write")

        hooks.register_hook("post_commit", writer, entity=ent.name)
        _land(ent, {"code": "pc2", "label": "L", "amount": "5", "category": "alpha"})
        promo = _promote(db, ent)
        sid = promo["results"][0]["staging_id"]
        res = _approve(db, ent, sid, review_note="ok")  # must NOT raise

        # Approve still succeeded and the golden record was written.
        assert len(_live_rows(ent)) == 1
        assert res["post_commit_errors"][0]["hook"] == "writer"
        # The hook's Session write was rolled back to the savepoint — nothing
        # persisted even after the surrounding session commits.
        db.commit()
        with get_engine().connect() as c:
            n = c.execute(
                text("select count(*) from mdm_meta.audit_event where actor = :a"),
                {"a": marker},
            ).scalar()
        assert n == 0

    # ---- FIX 4: a pre_commit hook injecting an unknown key must not 500
    def test_pre_commit_unknown_key_is_ignored_not_500(self, entity_factory, db):
        ent = entity_factory()

        def inject(ctx):
            ctx.values["totally_unknown_col"] = "x"

        hooks.register_hook("pre_commit", inject, entity=ent.name)
        _land(ent, {"code": "pc3", "label": "L", "amount": "5", "category": "alpha"})
        promo = _promote(db, ent)
        sid = promo["results"][0]["staging_id"]
        res = _approve(db, ent, sid, review_note="ok")  # must NOT raise / 500

        assert res["change_type"] == "insert"
        assert len(_live_rows(ent)) == 1


# ===================================================================== EX-2
class TestTransforms:
    def test_named_transforms_applied_in_staging(self, entity_factory, db):
        attrs = [
            _txt("code", length=40, is_required=True, is_unique=True,
                 is_business_key=True, position=0),
            _txt("u", transforms=["upper"], position=1),
            _txt("c", transforms=[{"fn": "map",
                                   "mapping": {"US": "United States"}}], position=2),
            _txt("d", transforms=[{"fn": "default", "value": "DEF"}], position=3),
        ]
        ent = entity_factory(attributes=attrs)
        _land(ent, {"code": "tf1", "u": "hello", "c": "US"})  # d omitted
        promo = _promote(db, ent)
        row = _staging_row(ent, promo["results"][0]["staging_id"])
        assert row["u"] == "HELLO"          # upper
        assert row["c"] == "United States"  # map substitution
        assert row["d"] == "DEF"            # conditional default on empty
        assert row["mdm_is_valid"] is True

    def test_unknown_transform_is_a_field_error(self, entity_factory, db):
        attrs = [
            _txt("code", length=40, is_required=True, is_unique=True,
                 is_business_key=True, position=0),
            _txt("u", transforms=["frobnicate"], position=1),
        ]
        ent = entity_factory(attributes=attrs)
        _land(ent, {"code": "tf2", "u": "x"})
        promo = _promote(db, ent)
        row = _staging_row(ent, promo["results"][0]["staging_id"])
        assert row["mdm_is_valid"] is False
        err = [e for e in (row["mdm_errors"] or []) if e["field"] == "u"]
        assert err and err[0]["code"] == "unknown_transform"

    def test_transform_output_recoerced_to_column_type(self, entity_factory, db):
        # A transform that emits text for a numeric column must yield a per-row
        # validation error (invalid staging row), not a whole-batch INSERT crash.
        from tests.conftest import _attr

        attrs = [
            _txt("code", length=40, is_required=True, is_unique=True,
                 is_business_key=True, position=0),
            _attr("amount", "decimal", numeric_precision=12, numeric_scale=2,
                  transforms=[{"fn": "map", "mapping": {"5": "five"}}], position=1),
        ]
        ent = entity_factory(attributes=attrs)
        _land(ent, {"code": "tf3", "amount": "5"})
        promo = _promote(db, ent)  # must not crash
        row = _staging_row(ent, promo["results"][0]["staging_id"])
        assert row["mdm_is_valid"] is False
        err = [e for e in (row["mdm_errors"] or []) if e["field"] == "amount"]
        assert err and err[0]["code"] == "type"
        assert row["amount"] is None


# ===================================================================== EX-3
class TestFieldMappings:
    def test_mapping_renames_source_to_target_landing_keeps_raw(
        self, entity_factory, db, field_mapping
    ):
        ent = entity_factory()
        field_mapping(entity_name=ent.name, source_field="org",
                      target_field="label", enabled=True)
        sent = {"code": "map1", "org": "widget"}
        r = _land(ent, sent)
        promo = _promote(db, ent)
        row = _staging_row(ent, promo["results"][0]["staging_id"])
        # Source renamed to target on the way to staging.
        assert row["label"] == "widget"
        assert row["mdm_is_valid"] is True
        # Capture-first: the landing row still holds exactly what was sent.
        landing = _landing_row(ent, r["landing_id"])
        assert landing["mdm_payload"] == sent

    def test_mapping_with_transform_and_default(
        self, entity_factory, db, field_mapping
    ):
        ent = entity_factory()
        field_mapping(entity_name=ent.name, source_system="crm",
                      source_field="org", target_field="label",
                      transform="upper", default_value="unknown", enabled=True)
        _land(ent, {"code": "map2", "org": "acme corp"}, source_system="crm")
        _land(ent, {"code": "map3"}, source_system="crm")  # no org -> default
        promo = _promote(db, ent)
        by_code = {}
        for res in promo["results"]:
            row = _staging_row(ent, res["staging_id"])
            by_code[row["code"]] = row
        assert by_code["MAP2"]["label"] == "ACME CORP"   # transform applied
        assert by_code["MAP3"]["label"] == "UNKNOWN"      # default then transform

    def test_mapping_unknown_target_is_row_error_not_crash(
        self, entity_factory, db, field_mapping
    ):
        ent = entity_factory()
        field_mapping(entity_name=ent.name, source_field="org",
                      target_field="nope_not_here", enabled=True)
        _land(ent, {"code": "map4", "label": "L", "org": "x"})
        promo = _promote(db, ent)  # must not crash
        row = _staging_row(ent, promo["results"][0]["staging_id"])
        assert row["mdm_is_valid"] is False
        codes = {e["code"] for e in (row["mdm_errors"] or [])}
        assert "unknown_mapping_target" in codes
