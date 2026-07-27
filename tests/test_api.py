"""End-to-end HTTP tests through the real ASGI app.

These assert the contract an integration developer actually consumes, including
the status codes that matter (401 vs 403 vs 409 vs 202).
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import ApiKey, Entity, Role, User, UserSource
from app.services.auth import create_access_token, hash_password, issue_api_key
from tests.conftest import requires_db

pytestmark = requires_db
API = settings.API_PREFIX


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _token(username, roles):
    return create_access_token(username=username, roles=roles, source="local")


@pytest.fixture
def principals(db):
    """Persist the accounts the routes will look up, then clean them away."""
    made = []
    specs = [
        ("api_admin", [Role.ADMIN.value]),
        ("api_steward", [Role.STEWARD.value]),
        ("api_reader", [Role.READER.value]),
        ("api_norole", []),
    ]
    out = {}
    for username, roles in specs:
        u = db.query(User).filter(User.username == username).one_or_none()
        if u is None:
            u = User(username=username, display_name=username,
                     source=UserSource.LOCAL.value,
                     password_hash=hash_password("pw"), roles=roles,
                     created_by="pytest")
            db.add(u)
            made.append(username)
        else:
            u.roles = roles
        out[username] = {"Authorization": f"Bearer {_token(username, roles)}"}
    db.commit()
    yield out
    from app.db import session_scope
    db.rollback()   # release locks before deleting from another session
    with session_scope() as s:
        for username in made:
            u = s.query(User).filter(User.username == username).one_or_none()
            if u:
                s.delete(u)


class TestHealth:
    def test_health_is_public(self, client):
        r = client.get("/health")
        assert r.status_code == 200 and r.json()["status"] == "ok"

    def test_ready_reports_database(self, client):
        r = client.get("/ready")
        assert r.status_code == 200
        assert r.json()["database"]["connected"] is True

    def test_openapi_served(self, client):
        assert client.get("/api/openapi.json").status_code == 200


class TestAuthentication:
    def test_unauthenticated_is_401(self, client):
        assert client.get(f"{API}/models").status_code == 401

    def test_bad_token_is_401(self, client):
        r = client.get(f"{API}/models",
                       headers={"Authorization": "Bearer not.a.jwt"})
        assert r.status_code == 401

    def test_valid_token_works(self, client, principals):
        assert client.get(f"{API}/models",
                          headers=principals["api_admin"]).status_code == 200

    def test_token_for_deleted_account_is_401(self, client):
        """A user removed from the directory must lose access immediately."""
        headers = {"Authorization": f"Bearer {_token('ghost_user', ['admin'])}"}
        assert client.get(f"{API}/models", headers=headers).status_code == 401

    def test_me_reports_roles_and_permissions(self, client, principals):
        r = client.get(f"{API}/auth/me", headers=principals["api_steward"])
        body = r.json()
        assert body["is_steward"] is True and body["is_admin"] is False
        assert "staging:approve" in body["permissions"]

    def test_login_rejects_bad_credentials(self, client):
        r = client.post(f"{API}/auth/login",
                        json={"username": "api_admin", "password": "wrong"})
        assert r.status_code == 401


class TestRoleEnforcement:
    def test_reader_cannot_create_model(self, client, principals):
        r = client.post(f"{API}/models", headers=principals["api_reader"],
                        json={"name": "nope", "attributes": []})
        assert r.status_code == 403

    def test_steward_cannot_publish(self, client, principals, entity_factory):
        ent = entity_factory(publish=False)
        r = client.post(f"{API}/models/{ent.name}/publish",
                        headers=principals["api_steward"], json={})
        assert r.status_code == 403

    def test_steward_cannot_manage_users(self, client, principals):
        assert client.get(f"{API}/admin/users",
                          headers=principals["api_steward"]).status_code == 403

    def test_roleless_user_is_403_everywhere(self, client, principals):
        h = principals["api_norole"]
        for path in ("/models", "/admin/users", "/stewardship/queue"):
            assert client.get(f"{API}{path}", headers=h).status_code == 403

    def test_admin_can_read_audit(self, client, principals):
        assert client.get(f"{API}/admin/audit",
                          headers=principals["api_admin"]).status_code == 200


class TestModelRoutes:
    def test_create_validates_identifier(self, client, principals):
        r = client.post(f"{API}/models", headers=principals["api_admin"],
                        json={"name": "Bad Name!", "attributes": [
                            {"name": "x", "data_type": "string"}]})
        assert r.status_code == 422

    def test_create_rejects_reserved_column(self, client, principals):
        r = client.post(f"{API}/models", headers=principals["api_admin"],
                        json={"name": f"t_{uuid.uuid4().hex[:8]}",
                              "attributes": [
                                  {"name": "mdm_id", "data_type": "string"}]})
        assert r.status_code == 422

    def test_create_rejects_unknown_type(self, client, principals):
        r = client.post(f"{API}/models", headers=principals["api_admin"],
                        json={"name": f"t_{uuid.uuid4().hex[:8]}",
                              "attributes": [
                                  {"name": "x", "data_type": "bogus"}]})
        assert r.status_code == 422

    def test_create_requires_attributes(self, client, principals):
        r = client.post(f"{API}/models", headers=principals["api_admin"],
                        json={"name": f"t_{uuid.uuid4().hex[:8]}",
                              "attributes": []})
        assert r.status_code == 422

    def test_duplicate_name_is_409(self, client, principals, entity_factory):
        ent = entity_factory(publish=False)
        r = client.post(f"{API}/models", headers=principals["api_admin"],
                        json={"name": ent.name, "attributes": [
                            {"name": "x", "data_type": "string"}]})
        assert r.status_code == 409

    def test_unknown_entity_is_404(self, client, principals):
        assert client.get(f"{API}/models/does_not_exist",
                          headers=principals["api_admin"]).status_code == 404

    def test_ddl_preview_does_not_execute(self, client, principals,
                                          entity_factory):
        ent = entity_factory(publish=False)
        r = client.get(f"{API}/models/{ent.name}/ddl",
                       headers=principals["api_admin"])
        assert r.status_code == 200
        body = r.json()
        assert body["mode"] == "create" and body["statement_count"] > 0
        # Still a draft: nothing was applied.
        detail = client.get(f"{API}/models/{ent.name}",
                            headers=principals["api_admin"]).json()
        assert detail["status"] == "draft"

    def test_publish_persists_status(self, client, principals, entity_factory):
        """Regression: a duplicate ModelVersion insert rolled the status back,
        leaving tables deployed but metadata still 'draft'."""
        ent = entity_factory(publish=False)
        r = client.post(f"{API}/models/{ent.name}/publish",
                        headers=principals["api_admin"],
                        json={"change_note": "first"})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "published"
        detail = client.get(f"{API}/models/{ent.name}",
                            headers=principals["api_admin"]).json()
        assert detail["status"] == "published"
        assert detail["published_version"] == detail["version"]

    def test_publish_is_repeatable(self, client, principals, entity_factory):
        ent = entity_factory(publish=False)
        h = principals["api_admin"]
        assert client.post(f"{API}/models/{ent.name}/publish", headers=h,
                           json={}).status_code == 200
        assert client.post(f"{API}/models/{ent.name}/publish", headers=h,
                           json={}).status_code == 200

    def test_export_round_trips(self, client, principals, entity_factory):
        ent = entity_factory(publish=False)
        h = principals["api_admin"]
        j = client.get(f"{API}/models/{ent.name}/export?fmt=json", headers=h)
        y = client.get(f"{API}/models/{ent.name}/export?fmt=yaml", headers=h)
        assert j.status_code == 200 and y.status_code == 200
        assert "attachment" in j.headers["content-disposition"]

    def test_import_dry_run_writes_nothing(self, client, principals):
        name = f"t_{uuid.uuid4().hex[:8]}"
        doc = (
            '{"mdm_schema_version":"1.0","entities":[{"name":"%s",'
            '"attributes":[{"name":"code","data_type":"string","length":10,'
            '"is_business_key":true}]}]}' % name
        )
        r = client.post(
            f"{API}/models/import?dry_run=true", headers=principals["api_admin"],
            files={"file": ("m.json", doc, "application/json")},
        )
        assert r.status_code == 200 and r.json()["dry_run"] is True
        assert client.get(f"{API}/models/{name}",
                          headers=principals["api_admin"]).status_code == 404

    def test_import_rejects_malicious_identifiers(self, client, principals):
        doc = ('{"entities":[{"name":"x; DROP TABLE y --","attributes":'
               '[{"name":"a","data_type":"string"}]}]}')
        r = client.post(
            f"{API}/models/import?dry_run=true", headers=principals["api_admin"],
            files={"file": ("m.json", doc, "application/json")},
        )
        assert r.status_code == 422


class TestDataRoutes:
    def test_write_to_unpublished_entity_is_409(self, client, principals,
                                                 entity_factory):
        ent = entity_factory(publish=False)
        r = client.post(f"{API}/data/{ent.name}",
                        headers=principals["api_admin"], json={"code": "x"})
        assert r.status_code == 409

    def test_post_is_accepted_not_created(self, client, principals,
                                          entity_factory):
        """202: the change is queued for review, not yet a golden record."""
        ent = entity_factory()
        r = client.post(f"{API}/data/{ent.name}",
                        headers=principals["api_admin"],
                        json={"code": "a1", "label": "L", "amount": "1",
                              "category": "alpha"})
        assert r.status_code == 202
        body = r.json()
        assert body["accepted"] is True
        assert body["status"] == "pending_review"
        assert body["validation_passed"] is True

    def test_write_does_not_touch_live(self, client, principals,
                                       entity_factory):
        ent = entity_factory()
        client.post(f"{API}/data/{ent.name}", headers=principals["api_admin"],
                    json={"code": "a2", "label": "L", "amount": "1",
                          "category": "alpha"})
        listing = client.get(f"{API}/data/{ent.name}",
                             headers=principals["api_admin"]).json()
        assert listing["meta"]["total"] == 0

    def test_invalid_payload_is_still_accepted(self, client, principals,
                                               entity_factory):
        ent = entity_factory()
        r = client.post(f"{API}/data/{ent.name}",
                        headers=principals["api_admin"],
                        json={"code": "a3", "label": "", "category": "nope"})
        assert r.status_code == 202
        assert r.json()["validation_passed"] is False
        assert r.json()["validation_errors"] >= 2

    def test_idempotency_key_dedupes(self, client, principals, entity_factory):
        ent = entity_factory()
        h = principals["api_admin"]
        payload = {"code": "a4", "label": "L", "amount": "1",
                   "category": "alpha"}
        first = client.post(f"{API}/data/{ent.name}?idempotency_key=abc",
                            headers=h, json=payload).json()
        second = client.post(f"{API}/data/{ent.name}?idempotency_key=abc",
                             headers=h, json=payload).json()
        assert second["deduplicated"] is True
        assert second["landing_id"] == first["landing_id"]

    def test_reader_cannot_write(self, client, principals, entity_factory):
        ent = entity_factory()
        r = client.post(f"{API}/data/{ent.name}",
                        headers=principals["api_reader"], json={"code": "x"})
        assert r.status_code == 403

    def test_statistics_covers_all_tiers(self, client, principals,
                                        entity_factory):
        ent = entity_factory()
        r = client.get(f"{API}/data/{ent.name}/statistics",
                       headers=principals["api_admin"])
        stats = r.json()["statistics"]
        assert set(stats) >= {"live_active", "live_deleted", "history_versions",
                              "landing_pending", "staging_by_status"}

    def test_csv_export(self, client, principals, entity_factory):
        ent = entity_factory()
        r = client.get(f"{API}/data/{ent.name}/export-csv",
                       headers=principals["api_admin"])
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/csv")

    def test_bulk_respects_row_limit(self, client, principals, entity_factory,
                                     monkeypatch):
        ent = entity_factory()
        monkeypatch.setattr(settings, "MAX_BULK_ROWS", 2)
        r = client.post(f"{API}/data/{ent.name}/bulk",
                        headers=principals["api_admin"],
                        json={"records": [{"code": f"c{i}"} for i in range(5)],
                              "operation": "INSERT"})
        assert r.status_code == 413

    def test_unknown_record_is_404(self, client, principals, entity_factory):
        ent = entity_factory()
        r = client.get(f"{API}/data/{ent.name}/{uuid.uuid4()}",
                       headers=principals["api_admin"])
        assert r.status_code == 404

    def test_malformed_record_id_is_422(self, client, principals,
                                        entity_factory):
        ent = entity_factory()
        r = client.get(f"{API}/data/{ent.name}/not-a-uuid",
                       headers=principals["api_admin"])
        assert r.status_code == 422


class TestOptionsRoute:
    """GET /data/{entity}/options — the FK/reference dropdown source (UI-3)."""

    def _seed(self, client, headers, ent, code, label):
        """Land + directly apply a valid record so it reaches the golden tier."""
        r = client.post(
            f"{API}/data/{ent.name}?direct=true",
            headers=headers,
            json={"code": code, "label": label, "amount": "1",
                  "category": "alpha"},
        )
        assert r.status_code == 202
        assert r.json()["applied"] is True, r.json()

    def test_options_returns_id_and_label(self, client, principals,
                                          entity_factory):
        ent = entity_factory()
        h = principals["api_admin"]
        self._seed(client, h, ent, "acme", "Acme Corp")
        r = client.get(f"{API}/data/{ent.name}/options", headers=h)
        assert r.status_code == 200
        opts = r.json()
        assert isinstance(opts, list) and len(opts) == 1
        assert set(opts[0]) == {"mdm_id", "label"}
        # Label is the business-key value (code), not the uuid.
        assert opts[0]["label"] == "ACME"  # normalization trims + uppercases code
        uuid.UUID(opts[0]["mdm_id"])  # a real uuid

    def test_options_query_filters_label(self, client, principals,
                                         entity_factory):
        ent = entity_factory()
        h = principals["api_admin"]
        self._seed(client, h, ent, "alpha", "One")
        self._seed(client, h, ent, "bravo", "Two")
        r = client.get(f"{API}/data/{ent.name}/options?q=alph", headers=h)
        assert r.status_code == 200
        labels = [o["label"] for o in r.json()]
        assert labels == ["ALPHA"]

    def test_options_requires_read_access(self, client, principals,
                                          entity_factory):
        ent = entity_factory()
        r = client.get(f"{API}/data/{ent.name}/options",
                       headers=principals["api_norole"])
        assert r.status_code == 403

    def test_options_excludes_deleted(self, client, principals, entity_factory):
        ent = entity_factory()
        h = principals["api_admin"]
        self._seed(client, h, ent, "keepme", "Keep")
        opts = client.get(f"{API}/data/{ent.name}/options", headers=h).json()
        # Directly delete the record and confirm it drops out of options.
        rid = opts[0]["mdm_id"]
        d = client.delete(f"{API}/data/{ent.name}/{rid}?direct=true", headers=h)
        assert d.status_code == 202
        opts2 = client.get(f"{API}/data/{ent.name}/options", headers=h).json()
        assert all(o["mdm_id"] != rid for o in opts2)


class TestServiceKeyRoutes:
    @pytest.fixture
    def key(self, db):
        record, raw = issue_api_key(db, name="pytest-key",
                                    source_system="pytest", actor="pytest")
        db.commit()
        yield raw
        from app.db import session_scope
        db.rollback()   # release locks before deleting from another session
        with session_scope() as s:
            k = s.query(ApiKey).filter(ApiKey.id == record.id).one_or_none()
            if k:
                s.delete(k)

    def test_key_can_write(self, client, key, entity_factory):
        ent = entity_factory()
        r = client.post(f"{API}/data/{ent.name}", headers={"X-API-Key": key},
                        json={"code": "k1", "label": "L", "amount": "1",
                              "category": "alpha"})
        assert r.status_code == 202

    def test_key_cannot_approve(self, client, key, entity_factory):
        """The maker-checker guarantee, enforced at the HTTP boundary."""
        ent = entity_factory()
        r = client.post(
            f"{API}/stewardship/{ent.name}/staging/1/approve",
            headers={"X-API-Key": key}, json={},
        )
        assert r.status_code == 403

    def test_key_cannot_publish(self, client, key, entity_factory):
        ent = entity_factory(publish=False)
        r = client.post(f"{API}/models/{ent.name}/publish",
                        headers={"X-API-Key": key}, json={})
        assert r.status_code == 403

    def test_invalid_key_is_401(self, client, entity_factory):
        ent = entity_factory()
        r = client.post(f"{API}/data/{ent.name}",
                        headers={"X-API-Key": "mdm_bogus"}, json={"code": "x"})
        assert r.status_code == 401


class TestStewardshipRoutes:
    def _stage(self, client, headers, ent, payload):
        return client.post(f"{API}/data/{ent.name}", headers=headers,
                           json=payload).json()["staging_id"]

    def test_queue_lists_pending(self, client, principals, entity_factory):
        ent = entity_factory()
        self._stage(client, principals["api_admin"], ent,
                    {"code": "q1", "label": "L", "amount": "1",
                     "category": "alpha"})
        r = client.get(f"{API}/stewardship/{ent.name}/queue",
                       headers=principals["api_steward"])
        assert r.status_code == 200 and r.json()["meta"]["total"] == 1

    def test_detail_exposes_supplied_fields_and_gate(self, client, principals,
                                                     entity_factory):
        ent = entity_factory()
        sid = self._stage(client, principals["api_admin"], ent,
                          {"code": "q2", "label": "L", "amount": "1",
                           "category": "alpha"})
        r = client.get(f"{API}/stewardship/{ent.name}/staging/{sid}",
                       headers=principals["api_steward"])
        body = r.json()
        assert body["can_approve"] is True
        assert set(body["staging"]["mdm_supplied_fields"]) == {
            "code", "label", "amount", "category"}

    def test_invalid_record_cannot_be_approved_over_http(
        self, client, principals, entity_factory
    ):
        ent = entity_factory()
        sid = self._stage(client, principals["api_admin"], ent,
                          {"code": "q3", "label": "", "category": "bad"})
        r = client.post(
            f"{API}/stewardship/{ent.name}/staging/{sid}/approve",
            headers=principals["api_steward"], json={"note": "approving"},
        )
        assert r.status_code == 400
        assert "validation errors" in r.json()["detail"]

    def test_segregation_of_duties_is_403(self, client, principals,
                                          entity_factory):
        ent = entity_factory()
        # api_steward submits, then tries to approve their own submission.
        sid = self._stage(client, principals["api_steward"], ent,
                          {"code": "q4", "label": "L", "amount": "1",
                           "category": "alpha"})
        r = client.post(
            f"{API}/stewardship/{ent.name}/staging/{sid}/approve",
            headers=principals["api_steward"], json={"note": "approving"},
        )
        assert r.status_code == 403
        assert "segregation of duties" in r.json()["detail"].lower()

    def test_approve_creates_golden_record(self, client, principals,
                                           entity_factory):
        ent = entity_factory()
        sid = self._stage(client, principals["api_admin"], ent,
                          {"code": "q5", "label": "L", "amount": "1",
                           "category": "alpha"})
        r = client.post(
            f"{API}/stewardship/{ent.name}/staging/{sid}/approve",
            headers=principals["api_steward"], json={"note": "ok"},
        )
        assert r.status_code == 200 and r.json()["change_type"] == "insert"
        listing = client.get(f"{API}/data/{ent.name}",
                             headers=principals["api_reader"]).json()
        assert listing["meta"]["total"] == 1

    def test_reject_requires_reason(self, client, principals, entity_factory):
        ent = entity_factory()
        sid = self._stage(client, principals["api_admin"], ent,
                          {"code": "q6", "label": "L", "amount": "1",
                           "category": "alpha"})
        r = client.post(f"{API}/stewardship/{ent.name}/staging/{sid}/reject",
                        headers=principals["api_steward"], json={})
        assert r.status_code == 422

    def test_bulk_approve_isolates_failures(self, client, principals,
                                            entity_factory):
        ent = entity_factory()
        good = self._stage(client, principals["api_admin"], ent,
                           {"code": "q7", "label": "L", "amount": "1",
                            "category": "alpha"})
        bad = self._stage(client, principals["api_admin"], ent,
                          {"code": "q8", "label": "", "category": "nope"})
        r = client.post(f"{API}/stewardship/{ent.name}/staging/bulk-approve",
                        headers=principals["api_steward"],
                        json={"staging_ids": [good, bad], "note": "bulk ok"})
        body = r.json()
        assert body["approved"] == 1 and body["failed"] == 1
