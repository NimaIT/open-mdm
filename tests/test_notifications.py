"""Workstream 4 — Notifications.

Exercises the notification outbox / delivery queue end-to-end: notifications
enqueued at workflow transitions with deep-links, per-domain template overrides,
built-in defaults, recipient resolution by role, the pluggable transports
(network-free ``outbox`` vs a monkeypatched ``smtp``), and the critical safety
guarantee that a notification failure can never break a workflow transition.
"""
import smtplib
import uuid

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.db import session_scope
from app.main import app
from app.models import Notification, NotificationTemplate, Role, User, UserSource
from app.services import notifications
from app.services.auth import create_access_token, hash_password
from tests.conftest import requires_db

pytestmark = requires_db
API = settings.API_PREFIX

VALID = {"code": "n1", "label": "L", "amount": "1", "category": "alpha"}


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def mkuser(db):
    """Create a local user (with optional email) and return its auth headers."""
    made = []

    def _make(name, roles, *, email=None):
        u = db.query(User).filter(User.username == name).one_or_none()
        if u is None:
            u = User(username=name, display_name=name,
                     source=UserSource.LOCAL.value, email=email,
                     password_hash=hash_password("pw"), roles=roles,
                     created_by="pytest")
            db.add(u)
            made.append(name)
        else:
            u.roles = roles
            u.email = email
        db.commit()
        token = create_access_token(username=name, roles=roles, source="local")
        return {"Authorization": f"Bearer {token}"}

    yield _make
    db.rollback()
    with session_scope() as s:
        for name in made:
            u = s.query(User).filter(User.username == name).one_or_none()
            if u:
                s.delete(u)


@pytest.fixture
def template_factory(db):
    """Create notification templates and drop them on teardown (they hold a
    unique (domain, event) constraint, so they must not leak between runs)."""
    made = []

    def _make(*, domain, event, subject, body, recipients=None,
              from_address=None, enabled=True):
        t = NotificationTemplate(
            domain=domain, event=event, subject=subject, body=body,
            recipients=recipients or [], from_address=from_address,
            enabled=enabled, created_by="pytest", updated_by="pytest",
        )
        db.add(t)
        db.commit()
        made.append(t.id)
        return t

    yield _make
    db.rollback()
    with session_scope() as s:
        for tid in made:
            t = s.query(NotificationTemplate).filter(
                NotificationTemplate.id == tid).one_or_none()
            if t:
                s.delete(t)


def _submit(client, headers, ent, payload=None):
    body = client.post(f"{API}/data/{ent.name}", headers=headers,
                       json=payload or VALID).json()
    return body["staging_id"]


def _notifs(entity_name, event=None, status=None):
    with session_scope() as s:
        q = s.query(Notification).filter(Notification.entity_name == entity_name)
        if event:
            q = q.filter(Notification.event == event)
        if status:
            q = q.filter(Notification.status == status)
        return [notifications.to_dict(n)
                for n in q.order_by(Notification.created_at).all()]


# --------------------------------------------------------------- NT-1 submit
class TestSubmitNotification:
    def test_submit_enqueues_queued_to_domain_approvers(self, client, mkuser,
                                                        entity_factory):
        sub = mkuser("nt_sub1", [Role.POWER_USER.value], email="sub1@x.com")
        # An approver with an email is the resolved recipient for 'submitted'.
        mkuser("nt_appr1", [Role.STEWARD.value], email="appr1@x.com")
        ent = entity_factory()
        sid = _submit(client, sub, ent)

        rows = _notifs(ent.name, event="submitted")
        assert len(rows) == 1
        n = rows[0]
        assert n["status"] == "queued"
        assert n["staging_id"] == sid
        assert "appr1@x.com" in n["to_addresses"]
        # The submitter (no approve permission) is not notified of the submission.
        assert "sub1@x.com" not in n["to_addresses"]

    def test_deep_link_contains_base_url_entity_and_staging_id(
        self, client, mkuser, entity_factory
    ):
        sub = mkuser("nt_sub2", [Role.POWER_USER.value], email="sub2@x.com")
        mkuser("nt_appr2", [Role.STEWARD.value], email="appr2@x.com")
        ent = entity_factory()
        sid = _submit(client, sub, ent)
        n = _notifs(ent.name, event="submitted")[0]
        assert n["deep_link"] == (
            f"{settings.APP_BASE_URL}/review/{ent.name}?staging={sid}"
        )
        assert settings.APP_BASE_URL in n["deep_link"]
        assert ent.name in n["deep_link"]
        assert str(sid) in n["deep_link"]

    def test_default_template_used_when_none_configured(self, client, mkuser,
                                                       entity_factory):
        sub = mkuser("nt_sub3", [Role.POWER_USER.value], email="sub3@x.com")
        mkuser("nt_appr3", [Role.STEWARD.value], email="appr3@x.com")
        ent = entity_factory()
        _submit(client, sub, ent)
        n = _notifs(ent.name, event="submitted")[0]
        # The built-in default renders the entity name and staging id into the
        # subject; no {placeholders} leak through.
        assert "Review needed" in n["subject"]
        assert ent.name in n["subject"]
        assert "{" not in n["subject"]


# ----------------------------------------- NT-1 submitter-facing transitions
class TestDecisionNotifications:
    def test_reject_notifies_submitter(self, client, mkuser, entity_factory):
        sub = mkuser("nt_sub4", [Role.POWER_USER.value], email="sub4@x.com")
        rev = mkuser("nt_rev4", [Role.ADMIN.value], email="rev4@x.com")
        ent = entity_factory()
        sid = _submit(client, sub, ent)
        r = client.post(f"{API}/stewardship/{ent.name}/staging/{sid}/reject",
                        headers=rev, json={"reason": "no good"})
        assert r.status_code == 200
        rows = _notifs(ent.name, event="rejected")
        assert len(rows) == 1
        assert rows[0]["to_addresses"] == ["sub4@x.com"]
        assert rows[0]["status"] == "queued"

    def test_changes_requested_notifies_submitter(self, client, mkuser,
                                                 entity_factory):
        sub = mkuser("nt_sub5", [Role.POWER_USER.value], email="sub5@x.com")
        rev = mkuser("nt_rev5", [Role.ADMIN.value], email="rev5@x.com")
        ent = entity_factory()
        sid = _submit(client, sub, ent)
        r = client.post(
            f"{API}/stewardship/{ent.name}/staging/{sid}/request-changes",
            headers=rev, json={"comment": "please fix the label"})
        assert r.status_code == 200
        rows = _notifs(ent.name, event="changes_requested")
        assert len(rows) == 1
        assert rows[0]["to_addresses"] == ["sub5@x.com"]
        assert "please fix the label" in rows[0]["body"]

    def test_approve_notifies_submitter(self, client, mkuser, entity_factory):
        sub = mkuser("nt_sub6", [Role.POWER_USER.value], email="sub6@x.com")
        appr = mkuser("nt_appr6", [Role.ADMIN.value], email="appr6@x.com")
        ent = entity_factory()
        sid = _submit(client, sub, ent)
        r = client.post(f"{API}/stewardship/{ent.name}/staging/{sid}/approve",
                        headers=appr, json={"note": "looks good"})
        assert r.status_code == 200
        rows = _notifs(ent.name, event="approved")
        assert len(rows) == 1
        assert rows[0]["to_addresses"] == ["sub6@x.com"]


# --------------------------------------------- NT-2 per-domain template override
class TestTemplateOverride:
    def test_domain_template_overrides_subject_and_recipients(
        self, client, mkuser, entity_factory, template_factory
    ):
        dom = f"notif_{uuid.uuid4().hex[:8]}"
        sub = mkuser("nt_sub7", [Role.POWER_USER.value], email="sub7@x.com")
        ent = entity_factory(domain=dom)
        template_factory(
            domain=dom, event="submitted",
            subject="CUSTOM REVIEW {entity}",
            body="Go review {entity} at {deep_link}",
            recipients=["team@finance.example"],
        )
        sid = _submit(client, sub, ent)
        rows = _notifs(ent.name, event="submitted")
        assert len(rows) == 1
        n = rows[0]
        assert n["subject"] == f"CUSTOM REVIEW {ent.name}"
        assert n["to_addresses"] == ["team@finance.example"]
        assert str(sid) in n["deep_link"]

    def test_disabled_template_skips(self, client, mkuser, entity_factory,
                                    template_factory):
        dom = f"notif_{uuid.uuid4().hex[:8]}"
        sub = mkuser("nt_sub8", [Role.POWER_USER.value], email="sub8@x.com")
        ent = entity_factory(domain=dom)
        template_factory(
            domain=dom, event="submitted", subject="x", body="y",
            recipients=["team@finance.example"], enabled=False,
        )
        _submit(client, sub, ent)
        rows = _notifs(ent.name, event="submitted")
        assert len(rows) == 1
        assert rows[0]["status"] == "skipped"


# ---------------------------------------------------- NT-3 transports / dispatch
class TestTransports:
    def test_outbox_transport_makes_no_smtp_call(self, client, mkuser,
                                                entity_factory, monkeypatch, db):
        sub = mkuser("nt_sub9", [Role.POWER_USER.value], email="sub9@x.com")
        mkuser("nt_appr9", [Role.STEWARD.value], email="appr9@x.com")
        ent = entity_factory()
        _submit(client, sub, ent)
        nid = _notifs(ent.name, event="submitted")[0]["id"]

        calls = []
        monkeypatch.setattr(smtplib, "SMTP",
                            lambda *a, **k: calls.append((a, k)))
        monkeypatch.setattr(settings, "NOTIFICATION_TRANSPORT", "outbox")

        counts = notifications.dispatch(db, [nid])
        db.commit()
        assert calls == []  # no SMTP construction, no network
        assert counts["sent"] == 1
        assert _notifs(ent.name, event="submitted")[0]["status"] == "sent"

    def test_smtp_transport_sends_via_monkeypatched_smtp(
        self, client, mkuser, entity_factory, monkeypatch, db
    ):
        sub = mkuser("nt_sub10", [Role.POWER_USER.value], email="sub10@x.com")
        mkuser("nt_appr10", [Role.STEWARD.value], email="appr10@x.com")
        ent = entity_factory()
        _submit(client, sub, ent)
        nid = _notifs(ent.name, event="submitted")[0]["id"]

        sent = {}

        class FakeSMTP:
            def __init__(self, host, port, timeout=None):
                sent["host"] = host
                sent["port"] = port

            def ehlo(self):
                pass

            def starttls(self):
                sent["tls"] = True

            def login(self, u, p):
                sent["login"] = u

            def sendmail(self, frm, to, msg):
                sent["from"] = frm
                sent["to"] = to

            def quit(self):
                sent["quit"] = True

        monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
        monkeypatch.setattr(settings, "NOTIFICATION_TRANSPORT", "smtp")
        monkeypatch.setattr(settings, "SMTP_HOST", "smtp.internal.example")
        monkeypatch.setattr(settings, "SMTP_USE_TLS", True)
        monkeypatch.setattr(settings, "SMTP_USERNAME", None)

        counts = notifications.dispatch(db, [nid])
        db.commit()
        assert sent["host"] == "smtp.internal.example"  # fake, no real network
        assert sent.get("tls") is True
        assert counts["sent"] == 1
        row = _notifs(ent.name, event="submitted")[0]
        assert row["status"] == "sent"
        assert row["attempts"] == 1
        assert row["sent_at"] is not None


# ------------------------------------------- SAFETY: enqueue failure is inert
class TestNotificationFailureIsInert:
    def test_approve_succeeds_even_if_enqueue_raises(self, client, mkuser,
                                                    entity_factory, monkeypatch):
        sub = mkuser("nt_sub11", [Role.POWER_USER.value], email="sub11@x.com")
        appr = mkuser("nt_appr11", [Role.ADMIN.value], email="appr11@x.com")
        ent = entity_factory()
        sid = _submit(client, sub, ent)

        def boom(*a, **k):
            raise RuntimeError("notification subsystem is down")

        monkeypatch.setattr(notifications, "enqueue_notification", boom)

        # The approve still applies to the golden record and returns success.
        r = client.post(f"{API}/stewardship/{ent.name}/staging/{sid}/approve",
                        headers=appr, json={"note": "ok"})
        assert r.status_code == 200
        listing = client.get(f"{API}/data/{ent.name}", headers=appr).json()
        assert listing["meta"]["total"] == 1

    def test_reject_succeeds_even_if_enqueue_raises(self, client, mkuser,
                                                   entity_factory, monkeypatch):
        sub = mkuser("nt_sub12", [Role.POWER_USER.value], email="sub12@x.com")
        rev = mkuser("nt_rev12", [Role.ADMIN.value], email="rev12@x.com")
        ent = entity_factory()
        sid = _submit(client, sub, ent)

        monkeypatch.setattr(
            notifications, "enqueue_notification",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))

        r = client.post(f"{API}/stewardship/{ent.name}/staging/{sid}/reject",
                        headers=rev, json={"reason": "nope"})
        assert r.status_code == 200


# --------------------------------------------------------- admin outbox endpoints
class TestAdminOutboxEndpoints:
    def test_outbox_viewer_and_flush(self, client, mkuser, entity_factory):
        sub = mkuser("nt_sub13", [Role.POWER_USER.value], email="sub13@x.com")
        admin = mkuser("nt_admin13", [Role.ADMIN.value], email="admin13@x.com")
        ent = entity_factory()
        _submit(client, sub, ent)

        listing = client.get(
            f"{API}/admin/notifications?event=submitted&domain={ent.domain or ''}",
            headers=admin).json()
        assert "data" in listing and "meta" in listing

        # Flush dispatches queued rows; default 'outbox' transport marks them sent.
        flushed = client.post(f"{API}/admin/notifications/flush",
                              headers=admin).json()
        assert set(["sent", "failed", "skipped", "total"]).issubset(flushed.keys())

    def test_template_crud(self, client, mkuser):
        admin = mkuser("nt_admin14", [Role.ADMIN.value], email="admin14@x.com")
        dom = f"crud_{uuid.uuid4().hex[:8]}"
        created = client.post(
            f"{API}/admin/notification-templates", headers=admin,
            json={"domain": dom, "event": "approved", "subject": "Hi {entity}",
                  "body": "Body {deep_link}", "recipients": ["a@b.com"]})
        assert created.status_code == 201, created.text
        tid = created.json()["id"]
        got = client.get(f"{API}/admin/notification-templates/{tid}",
                         headers=admin)
        assert got.status_code == 200 and got.json()["domain"] == dom
        deleted = client.delete(f"{API}/admin/notification-templates/{tid}",
                               headers=admin)
        assert deleted.status_code == 200 and deleted.json()["deleted"] is True

    def test_non_admin_cannot_manage_templates(self, client, mkuser):
        rev = mkuser("nt_stew15", [Role.STEWARD.value], email="stew15@x.com")
        assert client.get(f"{API}/admin/notification-templates",
                          headers=rev).status_code == 403
