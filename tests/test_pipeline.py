"""The four-tier pipeline: landing -> staging -> review -> live -> history.

This is the product's core contract, so these tests are deliberately
behavioural: they assert what an operator would observe, not internals.
"""
import pytest
from sqlalchemy import text

from app.config import settings
from app.db import get_engine
from app.services.identifiers import qualified
from app.services.pipeline import (
    OP_DELETE,
    OP_INSERT,
    OP_UPDATE,
    OP_UPSERT,
    PipelineError,
    SegregationOfDutiesError,
    apply_staging_to_live,
    edit_staging,
    promote_landing_to_staging,
    reject_staging,
    write_to_landing,
)
from tests.conftest import requires_db

pytestmark = requires_db

GOOD = {"code": " ab-1 ", "label": "Widget One", "amount": "10.50",
        "category": "alpha"}
BAD = {"code": "bad-1", "label": "", "amount": "-5", "category": "nope"}


def _land(db, ent, payload, *, operation=OP_INSERT, **kw):
    with get_engine().begin() as c:
        return write_to_landing(c, ent, operation=operation, payload=payload, **kw)


def _promote(db, ent, actor="system"):
    with get_engine().begin() as c:
        return promote_landing_to_staging(db, c, ent, actor=actor)


def _approve(db, ent, sid, actor="steward_b", **kw):
    with get_engine().begin() as c:
        return apply_staging_to_live(
            db, c, ent, sid, actor=actor, actor_roles=["steward"], **kw
        )


def _live_rows(ent, *, include_deleted=False):
    t = qualified(settings.SCHEMA_LIVE, ent.name)
    where = "" if include_deleted else "where not mdm_is_deleted"
    with get_engine().connect() as c:
        return c.execute(text(f"select * from {t} {where}")).mappings().all()


def _staging_row(ent, sid):
    t = qualified(settings.SCHEMA_STAGING, ent.name)
    with get_engine().connect() as c:
        return c.execute(
            text(f"select * from {t} where mdm_staging_id=:i"), {"i": sid}
        ).mappings().first()


class TestLanding:
    def test_accepts_and_returns_id(self, entity_factory, db):
        ent = entity_factory()
        r = _land(db, ent, GOOD, submitted_by="svc")
        assert r["landing_id"] > 0
        assert r["deduplicated"] is False

    def test_never_rejects_garbage(self, entity_factory, db):
        """Losing an inbound message is worse than storing a bad one."""
        ent = entity_factory()
        r = _land(db, ent, {"totally": "unknown", "nested": {"a": [1, 2]}})
        assert r["landing_id"] > 0

    def test_idempotency_key_deduplicates(self, entity_factory, db):
        ent = entity_factory()
        first = _land(db, ent, GOOD, idempotency_key="k1")
        second = _land(db, ent, {"code": "other"}, idempotency_key="k1")
        assert second["deduplicated"] is True
        assert second["landing_id"] == first["landing_id"]

    def test_rejects_unknown_operation(self, entity_factory, db):
        ent = entity_factory()
        with pytest.raises(PipelineError):
            _land(db, ent, GOOD, operation="TRUNCATE")


class TestPromotion:
    def test_valid_row_becomes_valid_staging(self, entity_factory, db):
        ent = entity_factory()
        _land(db, ent, GOOD)
        res = _promote(db, ent)
        assert res["promoted"] == 1
        row = _staging_row(ent, res["results"][0]["staging_id"])
        assert row["mdm_is_valid"] is True
        assert row["code"] == "AB-1"          # normalisation applied
        assert row["mdm_status"] == "pending_review"

    def test_invalid_row_is_still_staged_with_errors(self, entity_factory, db):
        """The steward must be able to see and repair bad data."""
        ent = entity_factory()
        _land(db, ent, BAD)
        res = _promote(db, ent)
        assert res["promoted"] == 1
        row = _staging_row(ent, res["results"][0]["staging_id"])
        assert row["mdm_is_valid"] is False
        codes = {e["code"] for e in row["mdm_errors"]}
        assert {"required", "min", "enum"} <= codes

    def test_landing_marked_promoted(self, entity_factory, db):
        ent = entity_factory()
        lid = _land(db, ent, GOOD)["landing_id"]
        _promote(db, ent)
        t = qualified(settings.SCHEMA_LANDING, ent.name)
        with get_engine().connect() as c:
            status = c.execute(
                text(f"select mdm_status from {t} where mdm_landing_id=:i"),
                {"i": lid},
            ).scalar()
        assert status == "promoted"

    def test_records_supplied_fields(self, entity_factory, db):
        ent = entity_factory()
        _land(db, ent, {"code": "x1", "label": "L"})
        res = _promote(db, ent)
        row = _staging_row(ent, res["results"][0]["staging_id"])
        assert set(row["mdm_supplied_fields"]) == {"code", "label"}

    def test_batch_is_tracked(self, entity_factory, db):
        ent = entity_factory()
        _land(db, ent, GOOD)
        _land(db, ent, BAD)
        res = _promote(db, ent)
        assert res["rows_in"] == 2 and res["failed"] == 0
        assert res["batch_id"]


class TestApproval:
    def test_insert_creates_golden_record(self, entity_factory, db):
        ent = entity_factory()
        _land(db, ent, GOOD, submitted_by="svc")
        sid = _promote(db, ent)["results"][0]["staging_id"]
        out = _approve(db, ent, sid)
        assert out["change_type"] == "insert"
        rows = _live_rows(ent)
        assert len(rows) == 1 and rows[0]["code"] == "AB-1"
        assert _staging_row(ent, sid)["mdm_status"] == "applied"

    def test_invalid_record_cannot_be_approved(self, entity_factory, db):
        ent = entity_factory()
        _land(db, ent, BAD)
        sid = _promote(db, ent)["results"][0]["staging_id"]
        with pytest.raises(PipelineError, match="validation errors"):
            _approve(db, ent, sid)
        assert _live_rows(ent) == []

    def test_cannot_approve_twice(self, entity_factory, db):
        ent = entity_factory()
        _land(db, ent, GOOD, submitted_by="svc")
        sid = _promote(db, ent)["results"][0]["staging_id"]
        _approve(db, ent, sid)
        with pytest.raises(PipelineError, match="already"):
            _approve(db, ent, sid)

    def test_missing_record_raises(self, entity_factory, db):
        ent = entity_factory()
        with pytest.raises(PipelineError, match="not found"):
            _approve(db, ent, 999999)


class TestSegregationOfDuties:
    def test_submitter_cannot_approve_own_record(self, entity_factory, db):
        ent = entity_factory()
        _land(db, ent, GOOD, submitted_by="alice")
        sid = _promote(db, ent)["results"][0]["staging_id"]
        with pytest.raises(SegregationOfDutiesError):
            _approve(db, ent, sid, actor="alice")

    def test_editor_cannot_approve_own_edit(self, entity_factory, db):
        ent = entity_factory()
        _land(db, ent, BAD, submitted_by="svc")
        sid = _promote(db, ent)["results"][0]["staging_id"]
        with get_engine().begin() as c:
            edit_staging(db, c, ent, sid,
                         updates={"label": "Fixed", "amount": "1",
                                  "category": "beta"},
                         actor="bob", actor_roles=["steward"])
        with pytest.raises(SegregationOfDutiesError):
            _approve(db, ent, sid, actor="bob")
        # A different steward may approve it.
        assert _approve(db, ent, sid, actor="carol")["change_type"] == "insert"

    def test_can_be_disabled_explicitly(self, entity_factory, db):
        ent = entity_factory()
        _land(db, ent, GOOD, submitted_by="alice")
        sid = _promote(db, ent)["results"][0]["staging_id"]
        out = _approve(db, ent, sid, actor="alice", enforce_sod=False)
        assert out["change_type"] == "insert"


class TestEditStaging:
    def test_repair_makes_record_valid(self, entity_factory, db):
        ent = entity_factory()
        _land(db, ent, BAD)
        sid = _promote(db, ent)["results"][0]["staging_id"]
        with get_engine().begin() as c:
            out = edit_staging(db, c, ent, sid,
                               updates={"label": "Repaired", "amount": "5",
                                        "category": "alpha"},
                               actor="bob", actor_roles=["steward"])
        assert out["is_valid"] is True

    def test_edit_adds_to_supplied_fields(self, entity_factory, db):
        """Otherwise the steward's repair is discarded on apply."""
        ent = entity_factory()
        _land(db, ent, {"code": "c1", "label": "L"})
        sid = _promote(db, ent)["results"][0]["staging_id"]
        with get_engine().begin() as c:
            edit_staging(db, c, ent, sid, updates={"amount": "7.25"},
                         actor="bob", actor_roles=["steward"])
        assert "amount" in _staging_row(ent, sid)["mdm_supplied_fields"]

    def test_rejects_unknown_field(self, entity_factory, db):
        ent = entity_factory()
        _land(db, ent, GOOD)
        sid = _promote(db, ent)["results"][0]["staging_id"]
        with pytest.raises(PipelineError, match="Unknown field"):
            with get_engine().begin() as c:
                edit_staging(db, c, ent, sid, updates={"nope": 1}, actor="bob")

    def test_cannot_edit_applied_record(self, entity_factory, db):
        ent = entity_factory()
        _land(db, ent, GOOD, submitted_by="svc")
        sid = _promote(db, ent)["results"][0]["staging_id"]
        _approve(db, ent, sid)
        with pytest.raises(PipelineError, match="already been applied"):
            with get_engine().begin() as c:
                edit_staging(db, c, ent, sid, updates={"label": "x"}, actor="bob")


class TestUpdateSemantics:
    def _seed(self, entity_factory, db):
        ent = entity_factory()
        _land(db, ent, {"code": "c1", "label": "Original", "amount": "10.00",
                        "category": "alpha"}, submitted_by="svc")
        sid = _promote(db, ent)["results"][0]["staging_id"]
        _approve(db, ent, sid)
        return ent, _live_rows(ent)[0]

    def test_partial_update_preserves_untouched_fields(
        self, entity_factory, db
    ):
        """Regression: DDL defaults used to leak in and clobber curated data."""
        ent, before = self._seed(entity_factory, db)
        _land(db, ent, {"code": "c1", "amount": "99.99"},
              operation=OP_UPDATE, submitted_by="svc")
        sid = _promote(db, ent)["results"][-1]["staging_id"]
        out = _approve(db, ent, sid)
        after = _live_rows(ent)[0]
        assert out["change_type"] == "update"
        assert str(after["amount"]) == "99.99"
        assert after["label"] == "Original"      # untouched
        assert after["category"] == "alpha"      # untouched
        assert after["mdm_version"] == before["mdm_version"] + 1

    def test_update_writes_history(self, entity_factory, db):
        ent, _ = self._seed(entity_factory, db)
        _land(db, ent, {"code": "c1", "amount": "50.00"},
              operation=OP_UPDATE, submitted_by="svc")
        sid = _promote(db, ent)["results"][-1]["staging_id"]
        _approve(db, ent, sid)
        h = qualified(settings.SCHEMA_HISTORY, ent.name)
        with get_engine().connect() as c:
            rows = c.execute(
                text(f"select mdm_version, mdm_change_type, amount from {h} "
                     "order by mdm_history_id")
            ).all()
        assert rows and rows[0][1] == "update"
        assert str(rows[0][2]) == "10.00"        # the *prior* value

    def test_duplicate_insert_detected_on_business_key(
        self, entity_factory, db
    ):
        ent, _ = self._seed(entity_factory, db)
        _land(db, ent, {"code": "c1", "label": "Dupe", "amount": "1",
                        "category": "beta"}, operation=OP_INSERT)
        res = _promote(db, ent)["results"][-1]
        row = _staging_row(ent, res["staging_id"])
        assert row["mdm_is_valid"] is False
        assert any(e["code"] == "duplicate" for e in row["mdm_errors"])
        assert row["mdm_change_type"] == "update"   # resolved to the existing record

    def test_upsert_of_new_key_inserts(self, entity_factory, db):
        ent = entity_factory()
        _land(db, ent, {"code": "fresh", "label": "New", "amount": "1",
                        "category": "alpha"}, operation=OP_UPSERT,
              submitted_by="svc")
        sid = _promote(db, ent)["results"][0]["staging_id"]
        assert _approve(db, ent, sid)["change_type"] == "insert"

    def test_update_with_unknown_mdm_id_errors(self, entity_factory, db):
        ent = entity_factory()
        _land(db, ent, {"code": "c9", "label": "L"}, operation=OP_UPDATE,
              target_id="00000000-0000-0000-0000-000000000000")
        res = _promote(db, ent)["results"][0]
        row = _staging_row(ent, res["staging_id"])
        assert any(e["code"] == "not_found" for e in row["mdm_errors"])


class TestDelete:
    def _seed(self, entity_factory, db):
        ent = entity_factory()
        _land(db, ent, {"code": "d1", "label": "Doomed", "amount": "1",
                        "category": "alpha"}, submitted_by="svc")
        sid = _promote(db, ent)["results"][0]["staging_id"]
        _approve(db, ent, sid)
        return ent

    def test_delete_by_business_key_only(self, entity_factory, db):
        """Regression: DELETE used to demand a full payload."""
        ent = self._seed(entity_factory, db)
        _land(db, ent, {"code": "d1"}, operation=OP_DELETE, submitted_by="svc")
        res = _promote(db, ent)["results"][-1]
        row = _staging_row(ent, res["staging_id"])
        assert row["mdm_is_valid"] is True, row["mdm_errors"]
        out = _approve(db, ent, res["staging_id"])
        assert out["change_type"] == "soft_delete"

    def test_soft_delete_retains_row_and_writes_history(
        self, entity_factory, db
    ):
        ent = self._seed(entity_factory, db)
        _land(db, ent, {"code": "d1"}, operation=OP_DELETE, submitted_by="svc")
        sid = _promote(db, ent)["results"][-1]["staging_id"]
        _approve(db, ent, sid)
        assert _live_rows(ent) == []                       # hidden by default
        assert len(_live_rows(ent, include_deleted=True)) == 1
        h = qualified(settings.SCHEMA_HISTORY, ent.name)
        with get_engine().connect() as c:
            kinds = [r[0] for r in c.execute(
                text(f"select mdm_change_type from {h}"))]
        assert "delete" in kinds

    def test_hard_delete_when_soft_delete_disabled(self, entity_factory, db):
        ent = entity_factory(soft_delete=False)
        _land(db, ent, {"code": "h1", "label": "Gone", "amount": "1",
                        "category": "alpha"}, submitted_by="svc")
        sid = _promote(db, ent)["results"][0]["staging_id"]
        _approve(db, ent, sid)
        _land(db, ent, {"code": "h1"}, operation=OP_DELETE, submitted_by="svc")
        sid2 = _promote(db, ent)["results"][-1]["staging_id"]
        assert _approve(db, ent, sid2)["change_type"] == "hard_delete"
        assert _live_rows(ent, include_deleted=True) == []

    def test_delete_of_unknown_record_is_invalid(self, entity_factory, db):
        ent = entity_factory()
        _land(db, ent, {"code": "ghost"}, operation=OP_DELETE)
        res = _promote(db, ent)["results"][0]
        row = _staging_row(ent, res["staging_id"])
        assert row["mdm_is_valid"] is False
        assert any(e["code"] == "not_found" for e in row["mdm_errors"])


class TestReject:
    def test_reject_marks_status_and_keeps_live_untouched(
        self, entity_factory, db
    ):
        ent = entity_factory()
        _land(db, ent, GOOD)
        sid = _promote(db, ent)["results"][0]["staging_id"]
        with get_engine().begin() as c:
            out = reject_staging(db, c, ent, sid, actor="bob",
                                 reason="Not a real vendor",
                                 actor_roles=["steward"])
        assert out["status"] == "rejected"
        assert _staging_row(ent, sid)["mdm_review_note"] == "Not a real vendor"
        assert _live_rows(ent) == []

    def test_cannot_reject_applied_record(self, entity_factory, db):
        ent = entity_factory()
        _land(db, ent, GOOD, submitted_by="svc")
        sid = _promote(db, ent)["results"][0]["staging_id"]
        _approve(db, ent, sid)
        with pytest.raises(PipelineError):
            with get_engine().begin() as c:
                reject_staging(db, c, ent, sid, actor="bob", reason="too late")
