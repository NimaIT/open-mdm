"""Workstream 5 — downstream distribution (mdm_pub matviews) and the scheduler.

Distribution and retention are exercised end-to-end against real PostgreSQL. The
scheduler's job callables are invoked directly for determinism; the thread
machinery is tested separately with a tiny in-memory job (no DB, no real waits).
"""
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.config import settings
from app.db import get_ddl_engine, get_engine, session_scope
from app.main import app
from app.models import Domain, Role, User, UserSource
from app.services.auth import create_access_token, hash_password
from app.services.ddl import reconcile_matview
from app.services.identifiers import qualified
from app.services.pipeline import (
    OP_INSERT,
    apply_staging_to_live,
    promote_landing_to_staging,
    write_to_landing,
)
from app.services.scheduler import (
    Job,
    Scheduler,
    distribution_status,
    refresh_views,
    run_retention,
)
from tests.conftest import requires_db

pytestmark = requires_db
API = settings.API_PREFIX

GOOD = {"code": "c1", "label": "Widget One", "amount": "10.50", "category": "alpha"}
GOOD2 = {"code": "c2", "label": "Widget Two", "amount": "20.00", "category": "beta"}


# ------------------------------------------------------------------- helpers
def _pub_rows(name):
    mv = qualified(settings.SCHEMA_PUBLISH, name)
    with get_engine().connect() as c:
        return c.execute(text(f"select * from {mv}")).mappings().all()


def _matview_exists(name):
    with get_engine().connect() as c:
        return bool(
            c.execute(
                text(
                    "select 1 from pg_matviews "
                    "where schemaname=:s and matviewname=:n"
                ),
                {"s": settings.SCHEMA_PUBLISH, "n": name},
            ).scalar()
        )


def _make_matview(ent):
    """Create the entity's matview the way the publish path does."""
    with get_ddl_engine().begin() as c:
        return reconcile_matview(c, ent)


def _land(ent, payload, **kw):
    with get_engine().begin() as c:
        return write_to_landing(c, ent, operation=OP_INSERT, payload=payload, **kw)


def _promote(db, ent, actor="system"):
    with get_engine().begin() as c:
        return promote_landing_to_staging(db, c, ent, actor=actor)


def _approve(db, ent, sid, actor="steward_b"):
    with get_engine().begin() as c:
        return apply_staging_to_live(
            db, c, ent, sid, actor=actor, actor_roles=["steward"]
        )


def _insert_golden(db, ent, payload):
    _land(ent, payload, submitted_by="svc")
    sid = _promote(db, ent)["results"][0]["staging_id"]
    _approve(db, ent, sid)
    return sid


def _count_live(ent):
    t = qualified(settings.SCHEMA_LIVE, ent.name)
    with get_engine().connect() as c:
        return c.execute(text(f"select count(*) from {t}")).scalar()


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def admin_headers(db):
    u = db.query(User).filter(User.username == "dist_admin").one_or_none()
    if u is None:
        u = User(username="dist_admin", display_name="dist_admin",
                 source=UserSource.LOCAL.value, password_hash=hash_password("pw"),
                 roles=[Role.ADMIN.value], created_by="pytest")
        db.add(u)
    else:
        u.roles = [Role.ADMIN.value]
    db.commit()
    token = create_access_token(username="dist_admin", roles=[Role.ADMIN.value],
                                source="local")
    yield {"Authorization": f"Bearer {token}"}
    db.rollback()
    with session_scope() as s:
        uu = s.query(User).filter(User.username == "dist_admin").one_or_none()
        if uu:
            s.delete(uu)


# ------------------------------------------------------------- DD-1 matviews
class TestDistributionMatview:
    def test_publish_creates_matview(self, client, admin_headers, entity_factory):
        ent = entity_factory(publish=False)
        r = client.post(f"{API}/models/{ent.name}/publish",
                        headers=admin_headers, json={})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["tiers"]["distribution"] == f"{settings.SCHEMA_PUBLISH}.{ent.name}"
        assert body["distribution_matview"] == [f"{settings.SCHEMA_PUBLISH}.{ent.name}"]
        assert _matview_exists(ent.name)

    def test_matview_exposes_active_golden_records(self, entity_factory, db):
        ent = entity_factory()
        _make_matview(ent)
        assert _pub_rows(ent.name) == []          # empty until data + refresh
        _insert_golden(db, ent, GOOD)
        refresh_views(entity_name=ent.name)
        rows = _pub_rows(ent.name)
        assert len(rows) == 1
        assert rows[0]["code"] == "C1"            # normalisation (trim+upper)
        # system columns are exposed
        assert rows[0]["mdm_id"] is not None
        assert "mdm_version" in rows[0] and "mdm_updated_at" in rows[0]

    def test_matview_excludes_soft_deleted(self, entity_factory, db):
        ent = entity_factory()
        _make_matview(ent)
        _insert_golden(db, ent, GOOD)
        _insert_golden(db, ent, GOOD2)
        # soft-delete the second golden record
        t = qualified(settings.SCHEMA_LIVE, ent.name)
        with get_engine().begin() as c:
            c.execute(
                text(f"update {t} set mdm_is_deleted=true where code=:code"),
                {"code": "C2"},
            )
        refresh_views(entity_name=ent.name)
        codes = {r["code"] for r in _pub_rows(ent.name)}
        assert codes == {"C1"}

    def test_republish_reflects_new_column(self, client, admin_headers,
                                           entity_factory, make_attr):
        ent = entity_factory()
        _make_matview(ent)
        # add a new attribute, then re-publish through the endpoint
        def _a(name, **kw):
            return {"name": name, "display_name": name.title(), **kw}

        payload = {
            "name": ent.name, "display_name": ent.display_name,
            "attributes": [
                _a("code", data_type="string", length=40, is_required=True,
                   is_unique=True, is_business_key=True),
                _a("label", data_type="string", length=120, is_required=True,
                   is_match_key=True),
                _a("amount", data_type="decimal", numeric_precision=12,
                   numeric_scale=2),
                _a("category", data_type="enum", length=20,
                   validation={"enum": ["alpha", "beta"]}),
                _a("active", data_type="boolean", default_value="true"),
                _a("region", data_type="string", length=30),
            ],
        }
        assert client.put(f"{API}/models/{ent.name}", headers=admin_headers,
                          json=payload).status_code == 200
        r = client.post(f"{API}/models/{ent.name}/publish",
                        headers=admin_headers, json={})
        assert r.status_code == 200, r.text
        # the matview now carries the new column (matviews are absent from
        # information_schema.columns, so read the column set off the result keys)
        mv = qualified(settings.SCHEMA_PUBLISH, ent.name)
        with get_engine().connect() as c:
            cols = list(c.execute(text(f"select * from {mv} limit 0")).keys())
        assert "region" in cols


# ------------------------------------------------------- DD-2 refresh job
class TestRefreshJob:
    def test_refresh_all_returns_per_entity_result(self, entity_factory, db):
        ent = entity_factory()
        _make_matview(ent)
        out = refresh_views()
        assert out["count"] >= 1
        assert out["refreshed"][ent.name]["ok"] is True

    def test_refresh_reflects_new_row_after_approval(self, entity_factory, db):
        ent = entity_factory()
        _make_matview(ent)
        refresh_views(entity_name=ent.name)
        assert _pub_rows(ent.name) == []
        _insert_golden(db, ent, GOOD)
        # not visible until a refresh
        assert _pub_rows(ent.name) == []
        refresh_views(entity_name=ent.name)
        assert len(_pub_rows(ent.name)) == 1


# ------------------------------------------------------- DD-2 retention job
class TestRetentionJob:
    def _backdate_landing(self, ent, landing_id, days):
        t = qualified(settings.SCHEMA_LANDING, ent.name)
        with get_engine().begin() as c:
            c.execute(
                text(
                    f"update {t} set mdm_received_at = now() - (:d || ' days')::interval "
                    f"where mdm_landing_id=:i"
                ),
                {"d": days, "i": landing_id},
            )

    def _insert_history(self, ent, code, days_old):
        t = qualified(settings.SCHEMA_HISTORY, ent.name)
        with get_engine().begin() as c:
            c.execute(
                text(
                    f"insert into {t} "
                    "(mdm_id, mdm_version, mdm_change_type, mdm_valid_from, "
                    " mdm_valid_to, code, label) "
                    "values (gen_random_uuid(), 1, 'update', "
                    "        now() - ((:d + 1) || ' days')::interval, "
                    "        now() - (:d || ' days')::interval, :code, 'x')"
                ),
                {"d": days_old, "code": code},
            )

    def _count_landing(self, ent):
        t = qualified(settings.SCHEMA_LANDING, ent.name)
        with get_engine().connect() as c:
            return c.execute(text(f"select count(*) from {t}")).scalar()

    def _count_history(self, ent):
        t = qualified(settings.SCHEMA_HISTORY, ent.name)
        with get_engine().connect() as c:
            return c.execute(text(f"select count(*) from {t}")).scalar()

    def test_prunes_old_landing_and_history_leaves_golden(self, entity_factory, db):
        ent = entity_factory(retention_days=30)
        # a golden record that must NOT be touched
        _insert_golden(db, ent, GOOD)
        live_before = _count_live(ent)
        # old + recent landing rows (still pending, unpromoted)
        old_lid = _land(ent, {"code": "old"})["landing_id"]
        recent_lid = _land(ent, {"code": "new"})["landing_id"]
        self._backdate_landing(ent, old_lid, 100)
        # old + recent history rows
        self._insert_history(ent, "HOLD", 100)
        self._insert_history(ent, "HNEW", 1)

        landing_before = self._count_landing(ent)
        history_before = self._count_history(ent)

        out = run_retention(entity_name=ent.name)
        res = out["retention"][ent.name]
        assert res["landing"] == 1 and res["history"] == 1

        # golden untouched, one old landing + one old history pruned
        assert _count_live(ent) == live_before
        assert self._count_landing(ent) == landing_before - 1
        assert self._count_history(ent) == history_before - 1
        # the recent rows survive
        with get_engine().connect() as c:
            lt = qualified(settings.SCHEMA_LANDING, ent.name)
            assert c.execute(
                text(f"select 1 from {lt} where mdm_landing_id=:i"),
                {"i": recent_lid},
            ).scalar() == 1

    def test_uses_entity_retention_days(self, entity_factory, db):
        ent = entity_factory(retention_days=5)
        old_lid = _land(ent, {"code": "old"})["landing_id"]
        recent_lid = _land(ent, {"code": "new"})["landing_id"]
        self._backdate_landing(ent, old_lid, 10)   # older than 5-day cutoff
        run_retention(entity_name=ent.name)
        assert self._count_landing(ent) == 1

    def test_falls_back_to_domain_retention_days(self, entity_factory, db):
        dname = f"d_{uuid.uuid4().hex[:8]}"
        dom = Domain(name=dname, display_name="D", retention_days=10,
                     created_by="pytest", updated_by="pytest")
        db.add(dom)
        db.commit()
        try:
            # entity_factory bypasses the domain-default inheritance done by the
            # HTTP create route, so retention_days stays None here — exactly what
            # exercises the scheduler's own entity->domain fallback.
            ent = entity_factory(domain=dname, retention_days=None)
            old_lid = _land(ent, {"code": "old"})["landing_id"]
            self._backdate_landing(ent, old_lid, 20)   # older than domain's 10d
            out = run_retention(entity_name=ent.name)
            assert out["retention"][ent.name]["landing"] == 1
            assert out["retention"][ent.name]["retention_days"] == 10
        finally:
            db.rollback()
            with session_scope() as s:
                d = s.query(Domain).filter(Domain.name == dname).one_or_none()
                if d:
                    s.delete(d)

    def test_skips_entity_without_retention(self, entity_factory, db):
        ent = entity_factory(retention_days=None)   # no domain, no policy
        old_lid = _land(ent, {"code": "old"})["landing_id"]
        self._backdate_landing(ent, old_lid, 100)
        out = run_retention(entity_name=ent.name)
        assert "skipped" in out["retention"][ent.name]
        assert self._count_landing(ent) == 1        # nothing deleted


# ------------------------------------------------------- admin endpoints
class TestAdminEndpoints:
    def test_endpoints_work(self, client, admin_headers, entity_factory, db):
        ent = entity_factory()
        _make_matview(ent)

        r = client.get(f"{API}/admin/distribution", headers=admin_headers)
        assert r.status_code == 200
        names = {e["entity"] for e in r.json()["entities"]}
        assert ent.name in names

        r = client.post(f"{API}/admin/distribution/refresh?entity={ent.name}",
                        headers=admin_headers)
        assert r.status_code == 200
        assert r.json()["refreshed"][ent.name]["ok"] is True

        r = client.post(f"{API}/admin/retention/run?entity={ent.name}",
                        headers=admin_headers)
        assert r.status_code == 200
        assert ent.name in r.json()["retention"]

        r = client.get(f"{API}/admin/scheduler", headers=admin_headers)
        assert r.status_code == 200
        body = r.json()
        assert body["enabled"] is False           # default OFF
        assert body["intervals"]["view_refresh_seconds"] == \
            settings.VIEW_REFRESH_INTERVAL_SECONDS

    def test_distribution_requires_auth(self, client):
        assert client.get(f"{API}/admin/distribution").status_code == 401


# --------------------------------------------------- scheduler thread machinery
class TestSchedulerThread:
    def test_start_runs_jobs_then_stops_cleanly(self):
        ping = []

        def boom():
            raise RuntimeError("intentional")

        sched = Scheduler(
            jobs=[Job("ping", 0.05, lambda: ping.append(1)),
                  Job("boom", 0.05, boom)],
            tick=0.02,
        )
        assert sched.start() is True
        assert sched.start() is False             # double-start guard
        assert sched.is_running() is True

        # wait (bounded) until the ping job has run at least once
        deadline = time.time() + 3.0
        while time.time() < deadline and not ping:
            time.sleep(0.02)
        assert ping, "scheduled job never ran"

        # a failing job must not kill the thread — ping keeps recording
        assert sched.is_running() is True

        joined = sched.stop(timeout=3.0)
        assert joined is True
        assert sched.is_running() is False
        # per-job isolation is recorded: boom failed, ping succeeded
        summary = sched.last_run_summary()
        assert summary["boom"]["ok"] is False
        assert summary["ping"]["ok"] is True

    def test_stop_when_not_started_is_false(self):
        sched = Scheduler(jobs=[Job("noop", 1.0, lambda: None)])
        assert sched.stop() is False

    def test_distribution_status_is_callable(self, entity_factory, db):
        ent = entity_factory()
        _make_matview(ent)
        rows = distribution_status()
        by_name = {r["entity"]: r for r in rows}
        assert by_name[ent.name]["exists"] is True
        assert by_name[ent.name]["populated"] is True
