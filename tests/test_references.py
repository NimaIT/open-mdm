"""Relationships & reference data: DM-2/3/4/5 and DQ-2/DQ-5.

These are behavioural, against a real cluster: the value of reference-data
resolution and foreign keys is what actually lands in the physical tables.
"""
import pytest
from sqlalchemy import text

from app.config import settings
from app.db import get_ddl_engine, get_engine
from app.services.ddl import (
    _enum_check_name,
    _fk_constraint_name,
    build_history_ddl,
    build_landing_ddl,
    build_live_ddl,
    build_reference_fk_statements,
    build_staging_ddl,
    columns_from_entity,
    reconcile_enum_constraints,
    reconcile_reference_constraints,
    reflect_columns,
)
from app.services.identifiers import SUPPORTED_TYPES, is_safe_widening, pg_type
from app.services.pipeline import (
    OP_INSERT,
    apply_staging_to_live,
    promote_landing_to_staging,
    write_to_landing,
)
from app.services.references import reresolve_broken_references
from tests.conftest import requires_db

pytestmark = requires_db


# --------------------------------------------------------------- helpers
def _land(ent, payload, *, operation=OP_INSERT, **kw):
    with get_engine().begin() as c:
        return write_to_landing(c, ent, operation=operation, payload=payload, **kw)


def _promote(db, ent, actor="system"):
    with get_engine().begin() as c:
        return promote_landing_to_staging(db, c, ent, actor=actor)


def _approve(db, ent, sid, actor="steward_b"):
    with get_engine().begin() as c:
        return apply_staging_to_live(
            db, c, ent, sid, actor=actor, actor_roles=["steward"]
        )


def _staging_row(ent, sid):
    from app.services.identifiers import qualified

    t = qualified(settings.SCHEMA_STAGING, ent.name)
    with get_engine().connect() as c:
        return c.execute(
            text(f"select * from {t} where mdm_staging_id=:i"), {"i": sid}
        ).mappings().first()


def _parent_mdm_id(ent, code):
    from app.services.identifiers import qualified

    t = qualified(settings.SCHEMA_LIVE, ent.name)
    with get_engine().connect() as c:
        return c.execute(
            text(f"select mdm_id from {t} where code=:c"), {"c": code}
        ).scalar()


def _fk_names(schema, table):
    with get_ddl_engine().connect() as c:
        return [
            r[0]
            for r in c.execute(
                text(
                    "select c.conname from pg_constraint c "
                    "join pg_class t on t.oid = c.conrelid "
                    "join pg_namespace n on n.oid = t.relnamespace "
                    "where n.nspname=:s and t.relname=:t and c.contype='f'"
                ),
                {"s": schema, "t": table},
            )
        ]


def _fk_target(schema, table, conname):
    """The table a named FK currently references (relname), or None."""
    with get_ddl_engine().connect() as c:
        return c.execute(
            text(
                "select cl.relname from pg_constraint c "
                "join pg_class t on t.oid = c.conrelid "
                "join pg_namespace n on n.oid = t.relnamespace "
                "join pg_class cl on cl.oid = c.confrelid "
                "where n.nspname=:s and t.relname=:t and c.conname=:c "
                "and c.contype='f'"
            ),
            {"s": schema, "t": table, "c": conname},
        ).scalar()


def _check_names_on_col(schema, table, col):
    """Names of single-column CHECK constraints on one column."""
    with get_ddl_engine().connect() as c:
        return [
            r[0]
            for r in c.execute(
                text(
                    "select c.conname from pg_constraint c "
                    "join pg_class t on t.oid = c.conrelid "
                    "join pg_namespace n on n.oid = t.relnamespace "
                    "join pg_attribute a on a.attrelid = t.oid "
                    "and a.attnum = any(c.conkey) "
                    "where n.nspname=:s and t.relname=:t and c.contype='c' "
                    "and cardinality(c.conkey)=1 and a.attname=:col"
                ),
                {"s": schema, "t": table, "col": col},
            )
        ]


def _index_names(schema, table):
    with get_ddl_engine().connect() as c:
        return [
            r[0]
            for r in c.execute(
                text(
                    "select indexname from pg_indexes "
                    "where schemaname=:s and tablename=:t"
                ),
                {"s": schema, "t": table},
            )
        ]


def _try_insert_live(ent, values, *, uuid_cols=()):
    """Attempt an insert into the live tier; return True on success."""
    from app.services.identifiers import qualified, quote_ident

    t = qualified(settings.SCHEMA_LIVE, ent.name)
    cols = ", ".join(quote_ident(k) for k in values)
    binds = ", ".join(
        (f"cast(:{k} as uuid)" if k in uuid_cols else f":{k}") for k in values
    )
    try:
        with get_ddl_engine().begin() as c:
            c.execute(text(f"insert into {t} ({cols}) values ({binds})"), values)
        return True
    except Exception:
        return False


def _make_parent(entity_factory, make_attr, **kw):
    return entity_factory(
        attributes=[
            make_attr("code", "string", length=40, is_required=True,
                      is_unique=True, is_business_key=True, position=0),
            make_attr("name", "string", length=120, position=1),
        ],
        **kw,
    )


def _make_child(entity_factory, make_attr, parent_name, *, ref_attribute=None, **kw):
    return entity_factory(
        attributes=[
            make_attr("code", "string", length=40, is_required=True,
                      is_unique=True, is_business_key=True, position=0),
            make_attr("parent_ref", "reference", ref_entity=parent_name,
                      ref_attribute=ref_attribute, position=1),
        ],
        **kw,
    )


# --------------------------------------------------------------- type system
class TestReferenceType:
    def test_reference_is_supported_and_maps_to_uuid(self):
        assert "reference" in SUPPORTED_TYPES
        assert pg_type("reference") == "uuid"

    def test_reference_and_uuid_are_mutually_widenable(self):
        assert is_safe_widening("reference", None, "uuid", None) is True
        assert is_safe_widening("uuid", None, "reference", None) is True

    def test_reference_column_is_physically_uuid(self, entity_factory, make_attr):
        parent = _make_parent(entity_factory, make_attr)
        child = _make_child(entity_factory, make_attr, parent.name)
        with get_ddl_engine().connect() as c:
            live = reflect_columns(c, settings.SCHEMA_LIVE, child.name)
        assert live["parent_ref"]["data_type"] == "uuid"


# --------------------------------------------------------------- DDL placement
class TestConstraintPlacement:
    def test_fk_is_a_live_tier_construct_only(self, entity_factory, make_attr):
        parent = _make_parent(entity_factory, make_attr)
        child = _make_child(entity_factory, make_attr, parent.name)
        cols = columns_from_entity(child)

        fks = build_reference_fk_statements(child.name, cols)
        assert fks, "expected a FK statement for the reference attribute"
        stmt = fks[0]
        assert "FOREIGN KEY" in stmt
        assert f'"{settings.SCHEMA_LIVE}"."{parent.name}"' in stmt
        assert "(mdm_id)" in stmt
        assert "ON DELETE RESTRICT" in stmt

        # No foreign keys anywhere but the live tier, and the live CREATE itself
        # emits the FK as a separate ALTER, never inline.
        for builder in (build_landing_ddl, build_staging_ddl, build_history_ddl,
                        build_live_ddl):
            sql = " ".join(builder(child.name, cols))
            assert "FOREIGN KEY" not in sql

    def test_enum_check_is_live_only(self, entity_factory):
        ent = entity_factory()  # default attrs include category enum(alpha,beta)
        cols = columns_from_entity(ent)

        live_sql = " ".join(build_live_ddl(ent.name, cols))
        assert '"category" IN' in live_sql
        assert "'alpha'" in live_sql and "'beta'" in live_sql

        for builder in (build_landing_ddl, build_staging_ddl, build_history_ddl):
            sql = " ".join(builder(ent.name, cols))
            assert '"category" IN' not in sql

    def test_enum_check_enforced_on_live_table(self, entity_factory):
        """The generated CHECK actually rejects an out-of-list value."""
        from app.services.identifiers import qualified

        ent = entity_factory()
        t = qualified(settings.SCHEMA_LIVE, ent.name)
        with pytest.raises(Exception):
            with get_ddl_engine().begin() as c:
                c.execute(
                    text(f"insert into {t} (code, label, category) "
                         "values ('x', 'y', 'not_a_valid_value')")
                )


# --------------------------------------------------------------- DQ-2 resolve
class TestReferenceResolution:
    def _seed_parent(self, db, entity_factory, make_attr):
        parent = _make_parent(entity_factory, make_attr)
        _land(parent, {"code": "US", "name": "United States"}, submitted_by="svc")
        sid = _promote(db, parent)["results"][0]["staging_id"]
        _approve(db, parent, sid)
        return parent

    def test_name_resolves_to_parent_mdm_id(self, entity_factory, make_attr, db):
        parent = self._seed_parent(db, entity_factory, make_attr)
        parent_id = _parent_mdm_id(parent, "US")
        child = _make_child(entity_factory, make_attr, parent.name)

        _land(child, {"code": "C1", "parent_ref": "US"}, submitted_by="svc")
        sid = _promote(db, child)["results"][0]["staging_id"]
        row = _staging_row(child, sid)

        assert row["mdm_is_valid"] is True, row["mdm_errors"]
        assert str(row["parent_ref"]) == str(parent_id)

    def test_existing_uuid_is_kept(self, entity_factory, make_attr, db):
        parent = self._seed_parent(db, entity_factory, make_attr)
        parent_id = _parent_mdm_id(parent, "US")
        child = _make_child(entity_factory, make_attr, parent.name)

        _land(child, {"code": "C2", "parent_ref": str(parent_id)}, submitted_by="svc")
        sid = _promote(db, child)["results"][0]["staging_id"]
        row = _staging_row(child, sid)

        assert row["mdm_is_valid"] is True, row["mdm_errors"]
        assert str(row["parent_ref"]) == str(parent_id)

    def test_resolved_child_can_be_approved_to_live(
        self, entity_factory, make_attr, db
    ):
        parent = self._seed_parent(db, entity_factory, make_attr)
        parent_id = _parent_mdm_id(parent, "US")
        child = _make_child(entity_factory, make_attr, parent.name)

        _land(child, {"code": "C3", "parent_ref": "US"}, submitted_by="svc")
        sid = _promote(db, child)["results"][0]["staging_id"]
        out = _approve(db, child, sid)
        assert out["change_type"] == "insert"

        from app.services.identifiers import qualified

        t = qualified(settings.SCHEMA_LIVE, child.name)
        with get_engine().connect() as c:
            stored = c.execute(
                text(f"select parent_ref from {t} where code='C3'")
            ).scalar()
        assert str(stored) == str(parent_id)


# --------------------------------------------------------------- DQ-5 held/unblock
class TestBrokenReferenceAndReresolution:
    def test_missing_parent_holds_child_invalid(
        self, entity_factory, make_attr, db
    ):
        parent = _make_parent(entity_factory, make_attr)  # published, but empty
        child = _make_child(entity_factory, make_attr, parent.name)

        _land(child, {"code": "C9", "parent_ref": "GHOST"}, submitted_by="svc")
        sid = _promote(db, child)["results"][0]["staging_id"]
        row = _staging_row(child, sid)

        assert row["mdm_is_valid"] is False
        assert any(e["code"] == "broken_reference" for e in row["mdm_errors"])
        assert row["parent_ref"] is None  # never store the raw human value

    def test_reresolution_unblocks_child_when_parent_arrives(
        self, entity_factory, make_attr, db
    ):
        parent = _make_parent(entity_factory, make_attr)
        child = _make_child(entity_factory, make_attr, parent.name)

        # Child arrives first — held invalid.
        _land(child, {"code": "C10", "parent_ref": "DE"}, submitted_by="svc")
        sid = _promote(db, child)["results"][0]["staging_id"]
        assert _staging_row(child, sid)["mdm_is_valid"] is False

        # Parent arrives later.
        _land(parent, {"code": "DE", "name": "Germany"}, submitted_by="svc")
        p_sid = _promote(db, parent)["results"][0]["staging_id"]
        _approve(db, parent, p_sid)
        parent_id = _parent_mdm_id(parent, "DE")

        # Explicit re-resolution flips the waiting child valid.
        with get_engine().begin() as c:
            res = reresolve_broken_references(db, c, child)
        assert res["unblocked"] == 1

        row = _staging_row(child, sid)
        assert row["mdm_is_valid"] is True, row["mdm_errors"]
        assert str(row["parent_ref"]) == str(parent_id)
        assert not any(e["code"] == "broken_reference" for e in row["mdm_errors"])

    def test_promotion_auto_reresolves_waiting_children(
        self, entity_factory, make_attr, db
    ):
        parent = _make_parent(entity_factory, make_attr)
        child = _make_child(entity_factory, make_attr, parent.name)

        _land(child, {"code": "C11", "parent_ref": "FR"}, submitted_by="svc")
        sid = _promote(db, child)["results"][0]["staging_id"]
        assert _staging_row(child, sid)["mdm_is_valid"] is False

        # Create the parent record.
        _land(parent, {"code": "FR", "name": "France"}, submitted_by="svc")
        p_sid = _promote(db, parent)["results"][0]["staging_id"]
        _approve(db, parent, p_sid)

        # A fresh promotion pass on the child entity re-resolves automatically
        # (even with no new landing rows).
        res = _promote(db, child)
        assert res["reresolved"]["unblocked"] == 1
        assert _staging_row(child, sid)["mdm_is_valid"] is True


# --------------------------------------------------------------- DM-3 junction
class TestAssociationEntity:
    def test_junction_publishes_with_two_foreign_keys(
        self, entity_factory, make_attr, db
    ):
        left = _make_parent(entity_factory, make_attr)
        right = _make_parent(entity_factory, make_attr)
        junction = entity_factory(
            attributes=[
                make_attr("left_ref", "reference", ref_entity=left.name,
                          is_required=True, position=0),
                make_attr("right_ref", "reference", ref_entity=right.name,
                          is_required=True, position=1),
            ],
            kind="association",
        )

        # Wire up the FKs the way the publish path does.
        with get_ddl_engine().begin() as c:
            recon = reconcile_reference_constraints(
                c, junction, db.query(type(junction)).all()
            )
        assert len(recon["added"]) == 2, recon

        fks = _fk_names(settings.SCHEMA_LIVE, junction.name)
        assert _fk_constraint_name(junction.name, "left_ref") in fks
        assert _fk_constraint_name(junction.name, "right_ref") in fks

    def test_reconcile_is_idempotent(self, entity_factory, make_attr, db):
        parent = _make_parent(entity_factory, make_attr)
        child = _make_child(entity_factory, make_attr, parent.name)

        Entity = type(parent)
        with get_ddl_engine().begin() as c:
            first = reconcile_reference_constraints(c, child, db.query(Entity).all())
        assert first["added"] == [_fk_constraint_name(child.name, "parent_ref")]
        # Second run adds nothing (constraint already present).
        with get_ddl_engine().begin() as c:
            second = reconcile_reference_constraints(c, child, db.query(Entity).all())
        assert second["added"] == []

    def test_association_composite_unique_index(self, entity_factory, make_attr):
        from app.services.ddl import constraint_name

        left = _make_parent(entity_factory, make_attr)
        right = _make_parent(entity_factory, make_attr)
        assoc = entity_factory(
            attributes=[
                make_attr("left_ref", "reference", ref_entity=left.name,
                          is_required=True, position=0),
                make_attr("right_ref", "reference", ref_entity=right.name,
                          is_required=True, position=1),
            ],
            kind="association",
        )
        expected = constraint_name(
            "uqa", assoc.name, "left_ref_right_ref"
        )
        assert expected in _index_names(settings.SCHEMA_LIVE, assoc.name)

        import uuid as _u

        a, b = str(_u.uuid4()), str(_u.uuid4())
        assert _try_insert_live(
            assoc, {"left_ref": a, "right_ref": b},
            uuid_cols=("left_ref", "right_ref"),
        )
        # A duplicate many-to-many edge is rejected by the composite index.
        assert not _try_insert_live(
            assoc, {"left_ref": a, "right_ref": b},
            uuid_cols=("left_ref", "right_ref"),
        )

    def test_non_association_has_no_composite_index(self, entity_factory, make_attr):
        parent = _make_parent(entity_factory, make_attr)
        child = _make_child(entity_factory, make_attr, parent.name)
        assert not any(
            n.startswith("uqa_") for n in _index_names(settings.SCHEMA_LIVE, child.name)
        )


# --------------------------------------------------- MAJOR 3: constraint naming
class TestConstraintNaming:
    def test_names_are_bounded_and_deterministic(self):
        from app.services.ddl import constraint_name

        n1 = constraint_name("fk", "some_entity", "some_col")
        n2 = constraint_name("fk", "some_entity", "some_col")
        assert n1 == n2  # stable across calls (hashlib, not salted hash())
        assert n1.startswith("fk_some_entity_some_col")
        assert len(n1) <= 63

    def test_long_names_do_not_collide(self):
        from app.services.ddl import constraint_name

        e = "e" * 40
        # Two column names that share the first 40 chars — a plain truncation to
        # 63 would collapse them to the same constraint name, silently dropping
        # one. The hash suffix must keep them distinct.
        a = constraint_name("fk", e, "c" * 40 + "_aaaa")
        b = constraint_name("fk", e, "c" * 40 + "_bbbb")
        assert len(a) <= 63 and len(b) <= 63
        assert a != b

    def test_matches_hashlib_digest(self):
        import hashlib

        from app.services.ddl import constraint_name

        key = "acme_supplier"
        digest = hashlib.sha1(key.encode()).hexdigest()[:8]
        assert constraint_name("fk", "acme", "supplier").endswith("_" + digest)


# ------------------------------------------------ MAJOR 1: enum CHECK reconcile
class TestEnumConstraintReconcile:
    def _set_enum(self, db, ent, values):
        cat = next(a for a in ent.attributes if a.name == "category")
        cat.validation = {"enum": list(values)} if values else {}
        db.flush()
        return ent

    def test_added_enum_value_is_enforced_after_reconcile(
        self, entity_factory, db
    ):
        ent = entity_factory()  # category enum(alpha, beta)
        # 'gamma' is initially rejected.
        assert not _try_insert_live(
            ent, {"code": "E1", "label": "l", "category": "gamma"}
        )
        # Operator widens the allow-list and re-publishes (reconcile step).
        self._set_enum(db, ent, ["alpha", "beta", "gamma"])
        with get_ddl_engine().begin() as c:
            recon = reconcile_enum_constraints(c, ent)
        assert recon["changed"]
        assert _try_insert_live(
            ent, {"code": "E2", "label": "l", "category": "gamma"}
        )
        # And the reconciled CHECK carries the deterministic name.
        assert _enum_check_name(ent.name, "category") in _check_names_on_col(
            settings.SCHEMA_LIVE, ent.name, "category"
        )

    def test_removed_enum_drops_the_check(self, entity_factory, db):
        ent = entity_factory()
        assert not _try_insert_live(
            ent, {"code": "E3", "label": "l", "category": "zzz"}
        )
        self._set_enum(db, ent, None)  # enum removed entirely
        with get_ddl_engine().begin() as c:
            reconcile_enum_constraints(c, ent)
        assert _check_names_on_col(
            settings.SCHEMA_LIVE, ent.name, "category"
        ) == []
        assert _try_insert_live(
            ent, {"code": "E4", "label": "l", "category": "zzz"}
        )

    def test_reconcile_is_idempotent(self, entity_factory, db):
        ent = entity_factory()
        with get_ddl_engine().begin() as c:
            reconcile_enum_constraints(c, ent)
        with get_ddl_engine().begin() as c:
            reconcile_enum_constraints(c, ent)  # must not raise
        # Still exactly one CHECK on the column.
        assert len(
            _check_names_on_col(settings.SCHEMA_LIVE, ent.name, "category")
        ) == 1


# ------------------------------------------------ MAJOR 2: FK re-point on change
class TestReferenceRepoint:
    def test_changing_ref_entity_repoints_the_fk(
        self, entity_factory, make_attr, db
    ):
        parent_a = _make_parent(entity_factory, make_attr)
        parent_b = _make_parent(entity_factory, make_attr)
        child = _make_child(entity_factory, make_attr, parent_a.name)
        Entity = type(child)

        with get_ddl_engine().begin() as c:
            reconcile_reference_constraints(c, child, db.query(Entity).all())
        cname = _fk_constraint_name(child.name, "parent_ref")
        assert _fk_target(settings.SCHEMA_LIVE, child.name, cname) == parent_a.name

        # Re-point the reference at a different parent and re-reconcile.
        ref_attr = next(a for a in child.attributes if a.name == "parent_ref")
        ref_attr.ref_entity = parent_b.name
        db.flush()
        with get_ddl_engine().begin() as c:
            recon = reconcile_reference_constraints(c, child, db.query(Entity).all())
        assert cname in recon["added"]  # dropped + re-added
        assert _fk_target(settings.SCHEMA_LIVE, child.name, cname) == parent_b.name


# --------------------------------------------- DQ-5: full lookup value preserved
class TestLookupValuePreservation:
    def test_broken_reference_preserves_full_value(
        self, entity_factory, make_attr, db
    ):
        parent = _make_parent(entity_factory, make_attr)  # empty
        child = _make_child(entity_factory, make_attr, parent.name)
        long_val = "X" * 300  # longer than the old 200-char truncation

        _land(child, {"code": "CLONG", "parent_ref": long_val}, submitted_by="svc")
        sid = _promote(db, child)["results"][0]["staging_id"]
        row = _staging_row(child, sid)

        err = next(e for e in row["mdm_errors"] if e["code"] == "broken_reference")
        assert err["value"] == long_val  # stored in full, not truncated


# ------------------------------------- cheap item: match key recomputed on unblock
class TestMatchKeyRecompute:
    def test_reresolution_recomputes_stale_match_key(
        self, entity_factory, make_attr, db
    ):
        from app.services.validation import build_match_key

        parent = _make_parent(entity_factory, make_attr)
        # A child whose reference IS the match key.
        child = entity_factory(
            attributes=[
                make_attr("code", "string", length=40, is_required=True,
                          is_unique=True, is_business_key=True, position=0),
                make_attr("parent_ref", "reference", ref_entity=parent.name,
                          is_match_key=True, position=1),
            ],
        )

        _land(child, {"code": "MK1", "parent_ref": "ZZ"}, submitted_by="svc")
        sid = _promote(db, child)["results"][0]["staging_id"]
        # Match key is unset while the reference is unresolved.
        assert _staging_row(child, sid)["mdm_match_key"] is None

        _land(parent, {"code": "ZZ", "name": "Zed"}, submitted_by="svc")
        p_sid = _promote(db, parent)["results"][0]["staging_id"]
        _approve(db, parent, p_sid)
        parent_id = _parent_mdm_id(parent, "ZZ")

        with get_engine().begin() as c:
            reresolve_broken_references(db, c, child)

        row = _staging_row(child, sid)
        assert row["mdm_is_valid"] is True
        expected = build_match_key({"parent_ref": parent_id}, child)
        assert row["mdm_match_key"] == expected
        assert expected is not None
