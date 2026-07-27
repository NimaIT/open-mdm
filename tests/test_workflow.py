"""Workstream 3 — governed change management (workflow).

Exercises the WorkflowTask / WorkflowEvent governance layer end-to-end:
auto-created tasks, the changes_requested round-trip, mandatory review comments,
claim/release ownership, the per-user inbox, admin reassign/terminate, the full
workflow history chain, and the database-level append-only guarantee (AO-2).
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.config import settings
from app.db import get_engine, session_scope
from app.main import app
from app.models import Role, User, UserSource, WorkflowEvent, WorkflowTask
from app.services import workflow
from app.services.auth import create_access_token, hash_password
from app.services.identifiers import qualified
from app.services.pipeline import promote_landing_to_staging, write_to_landing
from tests.conftest import requires_db

pytestmark = requires_db
API = settings.API_PREFIX

VALID = {"code": "w1", "label": "L", "amount": "1", "category": "alpha"}


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def mkuser(db):
    made = []
    out = {}

    def _make(name, roles, domain_roles=None):
        u = db.query(User).filter(User.username == name).one_or_none()
        if u is None:
            u = User(username=name, display_name=name,
                     source=UserSource.LOCAL.value,
                     password_hash=hash_password("pw"), roles=roles,
                     domain_roles=domain_roles or {}, created_by="pytest")
            db.add(u)
            made.append(name)
        else:
            u.roles = roles
            u.domain_roles = domain_roles or {}
        db.commit()
        token = create_access_token(username=name, roles=roles, source="local")
        out[name] = {"Authorization": f"Bearer {token}"}
        return out[name]

    yield _make
    db.rollback()
    with session_scope() as s:
        for name in made:
            u = s.query(User).filter(User.username == name).one_or_none()
            if u:
                s.delete(u)


def _stage(client, headers, ent, payload=None):
    """Submit a record through the normal write path and return its staging_id."""
    body = client.post(f"{API}/data/{ent.name}", headers=headers,
                       json=payload or VALID).json()
    return body["staging_id"]


# ------------------------------------------------------------- GC-1 auto-create
class TestTaskAutoCreated:
    def test_promote_creates_task_and_submit_event(self, entity_factory, db):
        ent = entity_factory()
        with get_engine().begin() as c:
            write_to_landing(c, ent, operation="INSERT", payload=VALID,
                             submitted_by="alice")
        with get_engine().begin() as c:
            res = promote_landing_to_staging(db, c, ent, actor="alice",
                                             rationale="onboarding batch")
        sid = res["results"][0]["staging_id"]
        task = workflow.get_task(db, ent.name, sid)
        assert task is not None
        assert task.status == "pending_review"
        assert task.submit_rationale == "onboarding batch"
        events = (db.query(WorkflowEvent)
                  .filter(WorkflowEvent.task_id == task.id).all())
        assert [e.step for e in events] == ["submit"]
        assert events[0].to_status == "pending_review"

    def test_http_write_opens_a_task(self, client, mkuser, entity_factory):
        h = mkuser("wf_sub1", [Role.POWER_USER.value])
        ent = entity_factory()
        sid = _stage(client, h, ent)
        r = client.get(f"{API}/stewardship/{ent.name}/staging/{sid}/workflow",
                       headers=h)
        assert r.status_code == 200
        body = r.json()
        assert body["task"]["status"] == "pending_review"
        assert body["history"][0]["step"] == "submit"


# ------------------------------------------- GC-1 changes_requested round-trip
class TestChangesRequestedRoundTrip:
    def test_pending_to_changes_requested_to_pending(self, client, mkuser,
                                                     entity_factory):
        sub = mkuser("wf_sub2", [Role.POWER_USER.value])
        rev = mkuser("wf_rev2", [Role.ADMIN.value])
        ent = entity_factory()
        sid = _stage(client, sub, ent)

        # Reviewer sends it back with a mandatory comment.
        r = client.post(
            f"{API}/stewardship/{ent.name}/staging/{sid}/request-changes",
            headers=rev, json={"comment": "Please fix the label."})
        assert r.status_code == 200
        assert r.json()["status"] == "changes_requested"

        detail = client.get(f"{API}/stewardship/{ent.name}/staging/{sid}",
                            headers=rev).json()
        assert detail["staging"]["mdm_status"] == "changes_requested"

        # The submitter's edit resubmits it -> back to pending_review.
        r2 = client.patch(f"{API}/stewardship/{ent.name}/staging/{sid}",
                          headers=sub, json={"updates": {"label": "Fixed"}})
        assert r2.status_code == 200
        detail2 = client.get(f"{API}/stewardship/{ent.name}/staging/{sid}",
                             headers=rev).json()
        assert detail2["staging"]["mdm_status"] == "pending_review"


# ------------------------------------------------- GC-4 mandatory review comments
class TestMandatoryComments:
    def test_approve_without_comment_is_422(self, client, mkuser, entity_factory):
        sub = mkuser("wf_sub3", [Role.POWER_USER.value])
        appr = mkuser("wf_appr3", [Role.ADMIN.value])
        ent = entity_factory()
        sid = _stage(client, sub, ent)
        r = client.post(f"{API}/stewardship/{ent.name}/staging/{sid}/approve",
                        headers=appr, json={})
        assert r.status_code == 422

    def test_approve_with_comment_succeeds(self, client, mkuser, entity_factory):
        sub = mkuser("wf_sub3b", [Role.POWER_USER.value])
        appr = mkuser("wf_appr3b", [Role.ADMIN.value])
        ent = entity_factory()
        sid = _stage(client, sub, ent)
        r = client.post(f"{API}/stewardship/{ent.name}/staging/{sid}/approve",
                        headers=appr, json={"note": "looks good"})
        assert r.status_code == 200

    def test_reject_without_reason_is_422(self, client, mkuser, entity_factory):
        sub = mkuser("wf_sub4", [Role.POWER_USER.value])
        rev = mkuser("wf_rev4", [Role.ADMIN.value])
        ent = entity_factory()
        sid = _stage(client, sub, ent)
        r = client.post(f"{API}/stewardship/{ent.name}/staging/{sid}/reject",
                        headers=rev, json={})
        assert r.status_code == 422

    def test_request_changes_without_comment_is_422(self, client, mkuser,
                                                    entity_factory):
        sub = mkuser("wf_sub5", [Role.POWER_USER.value])
        rev = mkuser("wf_rev5", [Role.ADMIN.value])
        ent = entity_factory()
        sid = _stage(client, sub, ent)
        r = client.post(
            f"{API}/stewardship/{ent.name}/staging/{sid}/request-changes",
            headers=rev, json={})
        assert r.status_code == 422


# ----------------------------------------------------- GC-7 claim / release
class TestClaimRelease:
    def test_claim_then_double_claim_conflicts(self, client, mkuser,
                                               entity_factory):
        sub = mkuser("wf_sub6", [Role.POWER_USER.value])
        a = mkuser("wf_stew_a", [Role.STEWARD.value])
        b = mkuser("wf_stew_b", [Role.STEWARD.value])
        ent = entity_factory()
        sid = _stage(client, sub, ent)

        r1 = client.post(f"{API}/stewardship/{ent.name}/staging/{sid}/claim",
                         headers=a)
        assert r1.status_code == 200 and r1.json()["claimed_by"] == "wf_stew_a"

        r2 = client.post(f"{API}/stewardship/{ent.name}/staging/{sid}/claim",
                         headers=b)
        assert r2.status_code == 409

        # A non-owner cannot release it, the owner can.
        assert client.post(
            f"{API}/stewardship/{ent.name}/staging/{sid}/release",
            headers=b).status_code == 409
        assert client.post(
            f"{API}/stewardship/{ent.name}/staging/{sid}/release",
            headers=a).status_code == 200


# --------------------------------------------------------------- GC-7 inbox
class TestInbox:
    def test_inbox_lists_unassigned_and_counts(self, client, mkuser,
                                               entity_factory):
        sub = mkuser("wf_sub7", [Role.POWER_USER.value])
        rev = mkuser("wf_rev7", [Role.STEWARD.value])
        ent = entity_factory()
        sid = _stage(client, sub, ent)

        inbox = client.get(f"{API}/stewardship/inbox", headers=rev).json()
        ids = [t["staging_id"] for t in inbox["unassigned"]
               if t["entity_name"] == ent.name]
        assert sid in ids
        assert inbox["counts"]["total_pending"] >= 1
        assert inbox["counts"]["unassigned"] >= 1

        counts = client.get(f"{API}/stewardship/inbox/counts", headers=rev).json()
        assert set(counts) == {"assigned_to_me", "unassigned",
                               "changes_requested", "total_pending"}

    def test_claimed_task_moves_to_assigned_to_me(self, client, mkuser,
                                                 entity_factory):
        sub = mkuser("wf_sub7b", [Role.POWER_USER.value])
        rev = mkuser("wf_rev7b", [Role.STEWARD.value])
        ent = entity_factory()
        sid = _stage(client, sub, ent)
        client.post(f"{API}/stewardship/{ent.name}/staging/{sid}/claim", headers=rev)
        inbox = client.get(f"{API}/stewardship/inbox", headers=rev).json()
        mine = [t["staging_id"] for t in inbox["assigned_to_me"]
                if t["entity_name"] == ent.name]
        assert sid in mine


# ----------------------------------------------------- GC-6 admin: reassign
class TestAdminReassign:
    def _task_id(self, client, headers, ent, sid):
        return client.get(
            f"{API}/stewardship/{ent.name}/staging/{sid}/workflow",
            headers=headers).json()["task"]["task_id"]

    def test_reassign_sets_assignee_and_event(self, client, mkuser,
                                              entity_factory):
        sub = mkuser("wf_sub8", [Role.POWER_USER.value])
        admin = mkuser("wf_admin8", [Role.ADMIN.value])
        ent = entity_factory()
        sid = _stage(client, sub, ent)
        tid = self._task_id(client, admin, ent, sid)

        r = client.post(f"{API}/admin/workflows/{tid}/reassign",
                        headers=admin, json={"assignee": "wf_target"})
        assert r.status_code == 200 and r.json()["assigned_to"] == "wf_target"

        listing = client.get(f"{API}/admin/workflows?entity_name={ent.name}",
                             headers=admin).json()
        assert any(t["assigned_to"] == "wf_target" for t in listing["data"])

    def test_non_admin_cannot_list_workflows(self, client, mkuser, entity_factory):
        rev = mkuser("wf_stew9", [Role.STEWARD.value])
        assert client.get(f"{API}/admin/workflows",
                          headers=rev).status_code == 403


# --------------------------------------------- GC-6/GC-3 admin: terminate
class TestAdminTerminate:
    def _task_id(self, client, headers, ent, sid):
        return client.get(
            f"{API}/stewardship/{ent.name}/staging/{sid}/workflow",
            headers=headers).json()["task"]["task_id"]

    def test_terminate_is_terminal_with_zero_golden_impact(self, client, mkuser,
                                                          entity_factory):
        sub = mkuser("wf_sub10", [Role.POWER_USER.value])
        admin = mkuser("wf_admin10", [Role.ADMIN.value])
        ent = entity_factory()
        sid = _stage(client, sub, ent)
        tid = self._task_id(client, admin, ent, sid)

        r = client.post(f"{API}/admin/workflows/{tid}/terminate",
                        headers=admin, json={"reason": "stale request"})
        assert r.status_code == 200 and r.json()["status"] == "terminated"

        # A terminated task cannot then be approved.
        r2 = client.post(f"{API}/stewardship/{ent.name}/staging/{sid}/approve",
                         headers=admin, json={"note": "try anyway"})
        assert r2.status_code == 400

        # Nothing reached the golden tier.
        listing = client.get(f"{API}/data/{ent.name}", headers=admin).json()
        assert listing["meta"]["total"] == 0

        # It is excluded from active work queues / inbox.
        inbox = client.get(f"{API}/stewardship/inbox", headers=admin).json()
        assert not any(
            t["entity_name"] == ent.name and t["staging_id"] == sid
            for t in inbox["unassigned"] + inbox["assigned_to_me"]
        )

    def test_terminate_requires_reason(self, client, mkuser, entity_factory):
        sub = mkuser("wf_sub10b", [Role.POWER_USER.value])
        admin = mkuser("wf_admin10b", [Role.ADMIN.value])
        ent = entity_factory()
        sid = _stage(client, sub, ent)
        tid = self._task_id(client, admin, ent, sid)
        r = client.post(f"{API}/admin/workflows/{tid}/terminate",
                        headers=admin, json={})
        assert r.status_code == 422


# ----------------------------------------------- GC-5 full workflow history
class TestWorkflowHistory:
    def test_full_coherent_chain(self, client, mkuser, entity_factory):
        sub = mkuser("wf_sub11", [Role.POWER_USER.value])
        rev = mkuser("wf_rev11", [Role.ADMIN.value])
        ent = entity_factory()
        sid = _stage(client, sub, ent)

        client.post(f"{API}/stewardship/{ent.name}/staging/{sid}/request-changes",
                    headers=rev, json={"comment": "tweak it"})
        client.patch(f"{API}/stewardship/{ent.name}/staging/{sid}",
                     headers=sub, json={"updates": {"label": "Fixed"}})
        client.post(f"{API}/stewardship/{ent.name}/staging/{sid}/approve",
                    headers=rev, json={"note": "approved now"})

        hist = client.get(f"{API}/stewardship/{ent.name}/staging/{sid}/workflow",
                         headers=rev).json()
        steps = [e["step"] for e in hist["history"]]
        assert steps == ["submit", "request_changes", "edit", "approve"]
        # One coherent chain: strictly increasing seq, matching comments recorded.
        assert [e["seq"] for e in hist["history"]] == [1, 2, 3, 4]
        by_step = {e["step"]: e for e in hist["history"]}
        assert by_step["request_changes"]["comment"] == "tweak it"
        assert by_step["approve"]["comment"] == "approved now"
        assert by_step["approve"]["to_status"] == "applied"


# --------------------------------------------- AO-2 append-only DB enforcement
class TestAppendOnlyTriggers:
    def test_workflow_event_cannot_be_updated_or_deleted(self, client, mkuser,
                                                        entity_factory):
        sub = mkuser("wf_sub12", [Role.POWER_USER.value])
        ent = entity_factory()
        _stage(client, sub, ent)  # commits a submit event

        with session_scope() as s:
            eid = s.execute(
                text("select id from mdm_meta.workflow_event limit 1")
            ).scalar()
        assert eid is not None

        with pytest.raises(Exception):
            with get_engine().begin() as c:
                c.execute(text("update mdm_meta.workflow_event set comment='x' "
                               "where id = :i"), {"i": str(eid)})
        with pytest.raises(Exception):
            with get_engine().begin() as c:
                c.execute(text("delete from mdm_meta.workflow_event where id = :i"),
                          {"i": str(eid)})

    def test_audit_event_cannot_be_updated_or_deleted(self, client, mkuser,
                                                     entity_factory):
        sub = mkuser("wf_sub13", [Role.POWER_USER.value])
        ent = entity_factory()
        _stage(client, sub, ent)  # commits an api_insert audit event

        with session_scope() as s:
            aid = s.execute(
                text("select id from mdm_meta.audit_event "
                     "where entity_name = :n limit 1"), {"n": ent.name}
            ).scalar()
        assert aid is not None

        with pytest.raises(Exception):
            with get_engine().begin() as c:
                c.execute(text("update mdm_meta.audit_event set actor='x' "
                               "where id = :i"), {"i": str(aid)})
        with pytest.raises(Exception):
            with get_engine().begin() as c:
                c.execute(text("delete from mdm_meta.audit_event where id = :i"),
                          {"i": str(aid)})


# -------------------------------------- MAJOR-1 entity delete cleans up tasks
class TestEntityDeleteCleansUpTasks:
    def test_delete_entity_terminates_active_tasks(self, client, mkuser,
                                                   entity_factory):
        sub = mkuser("wf_del_sub", [Role.POWER_USER.value])
        admin = mkuser("wf_del_admin", [Role.ADMIN.value])
        ent = entity_factory()
        sid = _stage(client, sub, ent)

        # Sanity: the task is active and visible to admin + steward inbox.
        wf = client.get(f"{API}/admin/workflows?entity_name={ent.name}",
                        headers=admin).json()
        assert any(t["staging_id"] == sid for t in wf["data"])

        # Dropping the entity (and its tables) must clean up its workflow tasks.
        r = client.delete(
            f"{API}/models/{ent.name}?drop_tables=true&confirm=true",
            headers=admin)
        assert r.status_code == 200, r.text
        assert r.json()["workflow_tasks_terminated"] >= 1

        # No longer listed anywhere, and querying raises no error.
        wf2 = client.get(f"{API}/admin/workflows", headers=admin).json()
        assert not any(t["entity_name"] == ent.name for t in wf2["data"])
        inbox = client.get(f"{API}/stewardship/inbox", headers=admin).json()
        assert not any(
            t["entity_name"] == ent.name
            for t in inbox["unassigned"] + inbox["assigned_to_me"]
        )

    def test_orphan_task_hidden_even_without_cleanup(self, client, mkuser,
                                                     entity_factory, db):
        """Defence-in-depth: a task whose entity vanished is filtered out of the
        admin workflow list even if it was never terminated."""
        admin = mkuser("wf_orphan_admin", [Role.ADMIN.value])
        ghost = f"t_ghost_{uuid.uuid4().hex[:8]}"
        # An active task for an entity that does not exist in metadata.
        task = WorkflowTask(entity_name=ghost, staging_id=1,
                            status="pending_review")
        db.add(task)
        db.commit()
        try:
            wf = client.get(f"{API}/admin/workflows", headers=admin).json()
            assert not any(t["entity_name"] == ghost for t in wf["data"])
        finally:
            # The task has no events, so it can simply be deleted (workflow_event
            # is append-only, but workflow_task is not).
            with session_scope() as s:
                s.query(WorkflowTask).filter(
                    WorkflowTask.entity_name == ghost
                ).delete(synchronize_session=False)


# ---------------------------------- MAJOR-3 double-decision / lock serialisation
class TestDoubleDecisionRefused:
    def test_second_approve_of_applied_row_is_refused(self, client, mkuser,
                                                      entity_factory):
        sub = mkuser("wf_dbl_sub", [Role.POWER_USER.value])
        appr = mkuser("wf_dbl_appr", [Role.ADMIN.value])
        ent = entity_factory()
        sid = _stage(client, sub, ent)

        r1 = client.post(f"{API}/stewardship/{ent.name}/staging/{sid}/approve",
                         headers=appr, json={"note": "ok"})
        assert r1.status_code == 200
        # A second approve of the now-applied row is refused (the lock + status
        # recheck simulate the lost double-approve race deterministically).
        r2 = client.post(f"{API}/stewardship/{ent.name}/staging/{sid}/approve",
                         headers=appr, json={"note": "again"})
        assert r2.status_code == 400
        # Exactly one golden record — no double write.
        listing = client.get(f"{API}/data/{ent.name}", headers=appr).json()
        assert listing["meta"]["total"] == 1


# --------------------------------------- MINOR-5 maker-checker loop enforced
class TestMakerCheckerLoop:
    def test_changes_requested_cannot_be_approved_until_edited(
        self, client, mkuser, entity_factory
    ):
        sub = mkuser("wf_mc_sub", [Role.POWER_USER.value])
        rev = mkuser("wf_mc_rev", [Role.ADMIN.value])
        ent = entity_factory()
        sid = _stage(client, sub, ent)

        client.post(
            f"{API}/stewardship/{ent.name}/staging/{sid}/request-changes",
            headers=rev, json={"comment": "please fix"})

        # Approving directly from changes_requested is refused.
        r = client.post(f"{API}/stewardship/{ent.name}/staging/{sid}/approve",
                        headers=rev, json={"note": "trying anyway"})
        assert r.status_code == 400
        # Nothing reached the golden tier yet.
        assert client.get(f"{API}/data/{ent.name}",
                          headers=rev).json()["meta"]["total"] == 0

        # An edit resubmits it -> pending_review, and now approval works.
        client.patch(f"{API}/stewardship/{ent.name}/staging/{sid}",
                     headers=sub, json={"updates": {"label": "Fixed"}})
        r2 = client.post(f"{API}/stewardship/{ent.name}/staging/{sid}/approve",
                         headers=rev, json={"note": "ok now"})
        assert r2.status_code == 200
        assert client.get(f"{API}/data/{ent.name}",
                          headers=rev).json()["meta"]["total"] == 1


# ------------------------------------- MINOR-4 seq collision -> conflict not 500
class TestSeqCollisionHandled:
    def test_duplicate_seq_becomes_conflict_not_500(self, entity_factory, db,
                                                    monkeypatch):
        ent = entity_factory()
        with get_engine().begin() as c:
            write_to_landing(c, ent, operation="INSERT", payload=VALID,
                             submitted_by="alice")
        with get_engine().begin() as c:
            res = promote_landing_to_staging(db, c, ent, actor="alice")
        sid = res["results"][0]["staging_id"]
        task = workflow.get_task(db, ent.name, sid)

        # Force every append to fight over the same seq (simulates concurrent
        # claim/approve racing on the UniqueConstraint(task_id, seq)).
        monkeypatch.setattr(workflow, "_next_seq", lambda _db, _tid: 999)
        workflow._append_event_guarded(
            db, task, step=workflow.STEP_CLAIM, actor="a",
            from_status=task.status, to_status=task.status)
        # The duplicate is surfaced as a clean conflict, never an unhandled 500.
        with pytest.raises(workflow.WorkflowConflict):
            workflow._append_event_guarded(
                db, task, step=workflow.STEP_CLAIM, actor="b",
                from_status="pending_review", to_status="pending_review")
        # A WorkflowConflict is also a WorkflowError, so a handler that only
        # catches the latter still returns a 4xx rather than 500.
        assert issubclass(workflow.WorkflowConflict, workflow.WorkflowError)


# --------------------------------- MAJOR-2 governance / tier atomicity
class TestGovernanceAtomicity:
    def test_approve_leaves_task_and_staging_consistent(self, client, mkuser,
                                                        entity_factory):
        sub = mkuser("wf_atom_sub", [Role.POWER_USER.value])
        appr = mkuser("wf_atom_appr", [Role.ADMIN.value])
        ent = entity_factory()
        sid = _stage(client, sub, ent)

        r = client.post(f"{API}/stewardship/{ent.name}/staging/{sid}/approve",
                        headers=appr, json={"note": "ok"})
        assert r.status_code == 200

        staging_t = qualified(settings.SCHEMA_STAGING, ent.name)
        with get_engine().connect() as c:
            st = c.execute(
                text(f"select mdm_status from {staging_t} "
                     "where mdm_staging_id = :i"), {"i": sid}
            ).scalar()
        with session_scope() as s:
            task = (s.query(WorkflowTask)
                    .filter(WorkflowTask.entity_name == ent.name,
                            WorkflowTask.staging_id == sid).one())
            events = (s.query(WorkflowEvent)
                      .filter(WorkflowEvent.task_id == task.id).all())
            task_status = task.status

        # Task status and staging status agree (they were written on one conn).
        assert st == "applied"
        assert task_status == "applied"
        # The approve landed on the same immutable chain.
        assert any(e.step == "approve" and e.to_status == "applied"
                   for e in events)
