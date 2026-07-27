"""Workstream 2 — five-tier RBAC, domains and per-domain access.

Covers the new roles (editor / approver / power_user), the data:write_direct
power-user bypass, first-class Domain objects with lifecycle-default
inheritance, per-domain access precedence, and service-account elevation.

Backward-compat note: with both override maps empty, can_access_entity behaves
exactly as before Workstream 2 — that invariant is exercised in test_authz.py.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import ApiKey, Domain, Role, User, UserSource
from app.services import ldap_auth as LA
from app.services.auth import (
    PERMISSIONS,
    authenticate_api_key,
    can_access_entity,
    create_access_token,
    domain_roles_to_permissions,
    has_permission,
    issue_api_key,
)
from tests.conftest import requires_db

pytestmark = requires_db
API = settings.API_PREFIX


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def mkuser(db):
    """Persist local users with arbitrary roles; yield a {name: headers} map."""
    from app.services.auth import hash_password

    made = []
    out = {}

    def _make(name, roles, domain_roles=None):
        u = db.query(User).filter(User.username == name).one_or_none()
        if u is None:
            u = User(username=name, display_name=name,
                     source=UserSource.LOCAL.value,
                     password_hash=hash_password("pw"), roles=roles,
                     domain_roles=domain_roles or {},
                     created_by="pytest")
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

    from app.db import session_scope
    db.rollback()
    with session_scope() as s:
        for name in made:
            u = s.query(User).filter(User.username == name).one_or_none()
            if u:
                s.delete(u)


@pytest.fixture
def mkdomain(db):
    """Create governance domains and clean them up."""
    made = []

    def _make(name=None, **kw):
        name = name or f"d_{uuid.uuid4().hex[:8]}"
        d = Domain(name=name, display_name=kw.pop("display_name", name),
                   created_by="pytest", updated_by="pytest", **kw)
        db.add(d)
        db.commit()
        made.append(name)
        return d

    yield _make

    from app.db import session_scope
    db.rollback()
    with session_scope() as s:
        for name in made:
            d = s.query(Domain).filter(Domain.name == name).one_or_none()
            if d:
                s.delete(d)


# ---------------------------------------------------------------- AC-1 matrix
class TestFiveTierMatrix:
    def test_new_roles_exist(self):
        assert Role.EDITOR.value == "editor"
        assert Role.APPROVER.value == "approver"
        assert Role.POWER_USER.value == "power_user"

    def test_editor_can_write_but_not_approve(self):
        assert has_permission(["editor"], "data:write")
        assert has_permission(["editor"], "staging:edit")
        assert not has_permission(["editor"], "staging:approve")
        assert not has_permission(["editor"], "staging:reject")
        assert not has_permission(["editor"], "data:write_direct")

    def test_approver_can_decide_but_not_write(self):
        assert has_permission(["approver"], "staging:approve")
        assert has_permission(["approver"], "staging:reject")
        assert has_permission(["approver"], "staging:edit")
        assert not has_permission(["approver"], "data:write")
        assert not has_permission(["approver"], "data:write_direct")

    def test_power_user_is_editor_plus_direct(self):
        assert PERMISSIONS["editor"] <= PERMISSIONS["power_user"]
        assert has_permission(["power_user"], "data:write_direct")
        assert not has_permission(["power_user"], "staging:approve")

    def test_admin_gains_direct(self):
        assert has_permission(["admin"], "data:write_direct")

    def test_service_still_cannot_direct_or_approve(self):
        assert not has_permission(["service"], "data:write_direct")
        assert not has_permission(["service"], "staging:approve")

    def test_legacy_roles_unchanged(self):
        # steward is still the combined maker+checker.
        for p in ("data:write", "staging:approve", "staging:reject",
                  "staging:edit"):
            assert has_permission(["steward"], p)
        assert not has_permission(["steward"], "data:write_direct")


# ------------------------------------------------------- AC-3 access precedence
class TestPerDomainAccess:
    def _user(self, entity_perms=None, domain_perms=None,
              source=UserSource.LDAP.value):
        return User(username="u", source=source, roles=["editor"],
                    entity_permissions=entity_perms or {},
                    domain_permissions=domain_perms or {})

    def test_empty_maps_fall_through(self):
        u = self._user()
        assert can_access_entity(u, "anything", "write", entity_domain="x") is True

    def test_domain_grant_enforces_actions(self):
        u = self._user(domain_perms={"finance": ["read"]})
        assert can_access_entity(u, "gl", "read", entity_domain="finance") is True
        assert can_access_entity(u, "gl", "write", entity_domain="finance") is False

    def test_human_falls_through_unlisted_domain(self):
        u = self._user(domain_perms={"finance": ["read"]})
        # An entity outside the restricted domain is allowed (human fall-through).
        assert can_access_entity(u, "cust", "write", entity_domain="sales") is True

    def test_entity_override_beats_domain(self):
        u = self._user(entity_perms={"customer": ["read", "write"]},
                       domain_perms={"sales": ["read"]})
        # customer lives in sales, whose domain grant is read-only, but the more
        # specific entity override wins.
        assert can_access_entity(u, "customer", "write",
                                 entity_domain="sales") is True

    def test_service_confined_by_domain(self):
        u = self._user(domain_perms={"finance": ["write"]},
                       source=UserSource.SERVICE.value)
        assert can_access_entity(u, "gl", "write", entity_domain="finance") is True
        assert can_access_entity(u, "x", "write", entity_domain="sales") is False

    def test_domain_roles_translation(self):
        out = domain_roles_to_permissions({"fin": ["approver"], "hr": ["editor"]})
        assert out["fin"] == ["approve", "read"]
        assert out["hr"] == ["read", "write"]


# --------------------------------------------------- AC-3 LDAP domain mapping
class TestLdapDomainMapping:
    GROUP = "CN=Fin_Editors,OU=Groups,DC=corp,DC=com"

    def test_domain_scoped_grant_not_global(self):
        mappings = [(self.GROUP, "editor", "finance")]
        # Domain-scoped grants do not leak into the global roles list.
        assert LA.map_roles([self.GROUP], mappings) == []
        assert LA.map_domain_roles([self.GROUP], mappings) == {
            "finance": ["editor"]}

    def test_global_grant_still_global(self):
        mappings = [(self.GROUP, "editor", None)]
        assert LA.map_roles([self.GROUP], mappings) == ["editor"]
        assert LA.map_domain_roles([self.GROUP], mappings) == {}

    def test_two_tuple_mappings_still_work(self):
        # Backward compatibility: legacy 2-tuples are treated as global.
        assert LA.map_roles([self.GROUP], [(self.GROUP, "steward")]) == ["steward"]


# --------------------------------------------------- AC-1 power-user direct edit
class TestDirectEdit:
    VALID = {"code": "d1", "label": "L", "amount": "1", "category": "alpha"}
    INVALID = {"code": "d2", "label": "", "category": "nope"}

    def test_power_user_direct_lands_live(self, client, mkuser, entity_factory):
        h = mkuser("wp_power", [Role.POWER_USER.value])
        ent = entity_factory()
        r = client.post(f"{API}/data/{ent.name}?direct=true", headers=h,
                        json=self.VALID)
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["applied"] is True
        assert body["status"] == "applied"
        assert body["mdm_id"]
        # It is really in the golden tier now.
        listing = client.get(f"{API}/data/{ent.name}", headers=h).json()
        assert listing["meta"]["total"] == 1

    def test_invalid_direct_stays_in_staging(self, client, mkuser,
                                             entity_factory):
        h = mkuser("wp_power2", [Role.POWER_USER.value])
        ent = entity_factory()
        r = client.post(f"{API}/data/{ent.name}?direct=true", headers=h,
                        json=self.INVALID)
        assert r.status_code == 202
        body = r.json()
        assert body["applied"] is False
        assert body["validation_passed"] is False
        # Nothing reached live.
        listing = client.get(f"{API}/data/{ent.name}", headers=h).json()
        assert listing["meta"]["total"] == 0

    def test_direct_without_permission_is_403(self, client, mkuser,
                                              entity_factory):
        h = mkuser("wp_editor", [Role.EDITOR.value])
        ent = entity_factory()
        r = client.post(f"{API}/data/{ent.name}?direct=true", headers=h,
                        json=self.VALID)
        assert r.status_code == 403
        # But the same editor can still write via the normal workflow.
        r2 = client.post(f"{API}/data/{ent.name}", headers=h, json=self.VALID)
        assert r2.status_code == 202
        assert r2.json()["applied"] is False

    def test_editor_cannot_approve_over_http(self, client, mkuser,
                                             entity_factory):
        writer = mkuser("wp_ed_w", [Role.POWER_USER.value])
        editor = mkuser("wp_ed_a", [Role.EDITOR.value])
        ent = entity_factory()
        sid = client.post(f"{API}/data/{ent.name}", headers=writer,
                          json=self.VALID).json()["staging_id"]
        r = client.post(f"{API}/stewardship/{ent.name}/staging/{sid}/approve",
                        headers=editor, json={})
        assert r.status_code == 403

    def test_approver_cannot_write_over_http(self, client, mkuser,
                                             entity_factory):
        h = mkuser("wp_appr", [Role.APPROVER.value])
        ent = entity_factory()
        r = client.post(f"{API}/data/{ent.name}", headers=h, json=self.VALID)
        assert r.status_code == 403


# --------------------------------------------------- AC-4 service elevation
class TestServiceElevation:
    VALID = {"code": "s1", "label": "L", "amount": "1", "category": "alpha"}

    @pytest.fixture
    def key(self, db):
        keys = []

        def _make(**kw):
            record, raw = issue_api_key(db, name=f"k_{uuid.uuid4().hex[:6]}",
                                        actor="pytest", **kw)
            db.commit()
            keys.append(record.id)
            return raw

        yield _make
        from app.db import session_scope
        db.rollback()
        with session_scope() as s:
            for kid in keys:
                k = s.query(ApiKey).filter(ApiKey.id == kid).one_or_none()
                if k:
                    s.delete(k)

    def test_elevated_writes_across_listed_domains(self, db, key):
        raw = key(elevated=True, allowed_domains=["dom_a", "dom_b"])
        principal, _ = authenticate_api_key(db, raw)
        assert can_access_entity(principal, "e1", "write",
                                 entity_domain="dom_a") is True
        assert can_access_entity(principal, "e2", "write",
                                 entity_domain="dom_b") is True
        # A domain it was not granted is refused.
        assert can_access_entity(principal, "e3", "write",
                                 entity_domain="dom_c") is False

    def test_elevated_still_cannot_approve(self, db, key):
        raw = key(elevated=True, allowed_domains=["dom_a"])
        principal, _ = authenticate_api_key(db, raw)
        assert principal.roles == [Role.SERVICE.value]
        assert not has_permission(principal.roles, "staging:approve")
        assert not has_permission(principal.roles, "data:write_direct")

    def test_elevated_key_writes_over_http(self, client, key, entity_factory):
        ent = entity_factory(domain="dom_http")
        raw = key(elevated=True, allowed_domains=["dom_http"])
        r = client.post(f"{API}/data/{ent.name}", headers={"X-API-Key": raw},
                        json=self.VALID)
        assert r.status_code == 202

    def test_elevated_key_denied_outside_domain(self, client, key,
                                                entity_factory):
        ent = entity_factory(domain="dom_other")
        raw = key(elevated=True, allowed_domains=["dom_only"])
        r = client.post(f"{API}/data/{ent.name}", headers={"X-API-Key": raw},
                        json=self.VALID)
        assert r.status_code == 403

    def test_elevated_key_cannot_approve_over_http(self, client, key,
                                                   entity_factory):
        ent = entity_factory()
        raw = key(elevated=True)
        r = client.post(f"{API}/stewardship/{ent.name}/staging/1/approve",
                        headers={"X-API-Key": raw}, json={})
        assert r.status_code == 403

    def test_non_elevated_key_unchanged(self, db, key):
        raw = key(allowed_entities=["customer"])
        principal, _ = authenticate_api_key(db, raw)
        assert can_access_entity(principal, "customer", "write") is True
        assert can_access_entity(principal, "vendor", "write") is False


# ----------------------------------------------------------- DM-1 domain CRUD
class TestDomainCrud:
    def test_default_domain_seeded(self, client, mkuser):
        h = mkuser("dm_reader", [Role.READER.value])
        names = [d["name"] for d in client.get(f"{API}/domains",
                                               headers=h).json()]
        assert "default" in names

    def test_any_authenticated_can_list(self, client, mkuser):
        h = mkuser("dm_reader2", [Role.READER.value])
        assert client.get(f"{API}/domains", headers=h).status_code == 200

    def test_reader_cannot_create(self, client, mkuser):
        h = mkuser("dm_reader3", [Role.READER.value])
        r = client.post(f"{API}/domains", headers=h,
                        json={"name": f"d_{uuid.uuid4().hex[:8]}"})
        assert r.status_code == 403

    def test_admin_crud_roundtrip(self, client, mkuser):
        h = mkuser("dm_admin", [Role.ADMIN.value])
        name = f"d_{uuid.uuid4().hex[:8]}"
        try:
            c = client.post(f"{API}/domains", headers=h,
                            json={"name": name, "display_name": "Fin",
                                  "requires_approval": False,
                                  "retention_days": 30})
            assert c.status_code == 201, c.text
            assert c.json()["requires_approval"] is False

            g = client.get(f"{API}/domains/{name}", headers=h)
            assert g.status_code == 200 and g.json()["retention_days"] == 30

            u = client.put(f"{API}/domains/{name}", headers=h,
                           json={"name": name, "requires_approval": True,
                                 "retention_days": 90})
            assert u.status_code == 200 and u.json()["retention_days"] == 90

            d = client.delete(f"{API}/domains/{name}", headers=h)
            assert d.status_code == 200 and d.json()["deleted"] is True
        finally:
            client.delete(f"{API}/domains/{name}", headers=h)

    def test_duplicate_name_is_409(self, client, mkuser, mkdomain):
        h = mkuser("dm_admin2", [Role.ADMIN.value])
        dom = mkdomain()
        r = client.post(f"{API}/domains", headers=h, json={"name": dom.name})
        assert r.status_code == 409

    def test_invalid_name_is_422(self, client, mkuser):
        h = mkuser("dm_admin3", [Role.ADMIN.value])
        r = client.post(f"{API}/domains", headers=h, json={"name": "Bad Name!"})
        assert r.status_code == 422

    def test_default_cannot_be_deleted(self, client, mkuser):
        h = mkuser("dm_admin4", [Role.ADMIN.value])
        assert client.delete(f"{API}/domains/default",
                             headers=h).status_code == 409

    def test_delete_blocked_when_referenced(self, client, mkuser, mkdomain,
                                            entity_factory):
        h = mkuser("dm_admin5", [Role.ADMIN.value])
        dom = mkdomain()
        entity_factory(domain=dom.name)
        r = client.delete(f"{API}/domains/{dom.name}", headers=h)
        assert r.status_code == 409
        assert "referenced" in r.json()["detail"]


# ------------------------------------------------- DM-1 lifecycle inheritance
class TestDomainInheritance:
    def _attrs(self):
        return [{"name": "code", "data_type": "string", "length": 10,
                 "is_business_key": True}]

    def test_entity_inherits_domain_defaults(self, client, mkuser, mkdomain, db):
        h = mkuser("inh_admin", [Role.ADMIN.value])
        dom = mkdomain(requires_approval=False, retention_days=45,
                       default_soft_delete=False)
        name = f"t_{uuid.uuid4().hex[:8]}"
        try:
            r = client.post(f"{API}/models", headers=h,
                            json={"name": name, "domain": dom.name,
                                  "attributes": self._attrs()})
            assert r.status_code == 201, r.text
            body = r.json()
            # Unset lifecycle fields inherit from the domain.
            assert body["requires_approval"] is False
            assert body["soft_delete"] is False
        finally:
            client.delete(f"{API}/models/{name}?drop_tables=false", headers=h)

    def test_explicit_value_overrides_domain(self, client, mkuser, mkdomain):
        h = mkuser("inh_admin2", [Role.ADMIN.value])
        dom = mkdomain(requires_approval=False)
        name = f"t_{uuid.uuid4().hex[:8]}"
        try:
            r = client.post(f"{API}/models", headers=h,
                            json={"name": name, "domain": dom.name,
                                  "requires_approval": True,
                                  "attributes": self._attrs()})
            assert r.status_code == 201, r.text
            # Explicit True beats the domain's False default.
            assert r.json()["requires_approval"] is True
        finally:
            client.delete(f"{API}/models/{name}?drop_tables=false", headers=h)


# --------------------------------------------------------------- UI-2 context
class TestAuthMeContext:
    def test_me_exposes_direct_edit_flag(self, client, mkuser):
        h = mkuser("me_power", [Role.POWER_USER.value])
        body = client.get(f"{API}/auth/me", headers=h).json()
        assert body["can_direct_edit"] is True
        assert body["can_write"] is True
        assert body["can_approve"] is False
        assert "domain_permissions" in body
        assert "entity_permissions" in body
        assert "power_user" in body["roles"]

    def test_me_reader_flags(self, client, mkuser):
        h = mkuser("me_reader", [Role.READER.value])
        body = client.get(f"{API}/auth/me", headers=h).json()
        assert body["can_direct_edit"] is False
        assert body["can_write"] is False

    def test_me_exposes_domain_roles(self, client, mkuser):
        h = mkuser("me_domroles", [], domain_roles={"finance": ["editor"]})
        body = client.get(f"{API}/auth/me", headers=h).json()
        assert body["domain_roles"] == {"finance": ["editor"]}

    def test_me_flags_aggregate_domain_roles(self, client, mkuser):
        """UI hints (can_approve / can_write) must reflect capability held via a
        domain_roles grant, not only global roles — otherwise the SPA hides the
        approve/submit controls from a domain-only reviewer (W9 regression)."""
        appr = mkuser("me_dom_appr", [], domain_roles={"finance": ["approver"]})
        b = client.get(f"{API}/auth/me", headers=appr).json()
        assert b["can_approve"] is True
        assert "staging:approve" in b["permissions"]
        assert b["can_write"] is False  # approver confers no data:write

        edit = mkuser("me_dom_edit", [], domain_roles={"finance": ["editor"]})
        be = client.get(f"{API}/auth/me", headers=edit).json()
        assert be["can_write"] is True and be["can_approve"] is False

    def test_domain_reviewer_can_load_global_queue(self, client, mkuser):
        """A domain-only reviewer must be able to open the cross-entity queue
        (scoped to their domain); a truly role-less user is still 403 (W9)."""
        appr = mkuser("q_dom_appr", [], domain_roles={"finance": ["approver"]})
        assert client.get(f"{API}/stewardship/queue", headers=appr).status_code == 200
        roleless = mkuser("q_roleless", [])
        assert client.get(f"{API}/stewardship/queue", headers=roleless).status_code == 403


# ------------------------------- AC-3 domain CONFERRAL over HTTP (the real gap)
class TestDomainConferralHttp:
    """A domain_roles grant must GRANT capability within that domain and confer
    nothing elsewhere — exercised end-to-end through the ASGI app, which is the
    coverage the security review found missing (only unit-level checks existed)."""

    VALID = {"code": "cf1", "label": "L", "amount": "1", "category": "alpha"}

    def test_domain_editor_writes_only_in_its_domain(self, client, mkuser,
                                                     entity_factory):
        # Global roles are EMPTY — all capability comes from the domain grant.
        h = mkuser("conf_editor", [], domain_roles={"finance": ["editor"]})
        fin = entity_factory(domain="finance")
        other = entity_factory(domain="sales")

        # Can write inside the granted domain.
        r_ok = client.post(f"{API}/data/{fin.name}", headers=h, json=self.VALID)
        assert r_ok.status_code == 202, r_ok.text

        # Cannot write an entity in another domain.
        r_no = client.post(f"{API}/data/{other.name}", headers=h, json=self.VALID)
        assert r_no.status_code == 403, r_no.text

        # An editor cannot approve even inside their own domain (no staging:approve).
        sid = r_ok.json()["staging_id"]
        r_appr = client.post(
            f"{API}/stewardship/{fin.name}/staging/{sid}/approve",
            headers=h, json={})
        assert r_appr.status_code == 403

    def test_domain_approver_can_approve_end_to_end(self, client, mkuser,
                                                    entity_factory):
        # This is the exact case that used to 403 the approver out of their own
        # domain (approve routes check 'staging:approve', not 'approve').
        writer = mkuser("conf_w1", [Role.POWER_USER.value])   # global submitter
        appr = mkuser("conf_a1", [], domain_roles={"finance": ["approver"]})
        fin = entity_factory(domain="finance")

        sid = client.post(f"{API}/data/{fin.name}", headers=writer,
                          json=self.VALID).json()["staging_id"]
        assert sid is not None

        r = client.post(f"{API}/stewardship/{fin.name}/staging/{sid}/approve",
                        headers=appr, json={"note": "approved"})
        assert r.status_code == 200, r.text
        # It really reached the golden tier (approver has conferred data:read).
        listing = client.get(f"{API}/data/{fin.name}", headers=appr).json()
        assert listing["meta"]["total"] == 1

    def test_domain_approver_cannot_approve_other_domain(self, client, mkuser,
                                                         entity_factory):
        writer = mkuser("conf_w2", [Role.POWER_USER.value])
        appr = mkuser("conf_a2", [], domain_roles={"finance": ["approver"]})
        other = entity_factory(domain="sales")
        sid = client.post(f"{API}/data/{other.name}", headers=writer,
                          json=self.VALID).json()["staging_id"]
        r = client.post(f"{API}/stewardship/{other.name}/staging/{sid}/approve",
                        headers=appr, json={})
        assert r.status_code == 403

    def test_global_approver_not_weakened_by_domain_grant(self, client, mkuser,
                                                         entity_factory):
        # Regression guard: a GLOBAL approver who also holds a finance domain
        # grant must still approve in ANY domain (the union never subtracts).
        writer = mkuser("conf_w3", [Role.POWER_USER.value])
        appr = mkuser("conf_ga", [Role.APPROVER.value],
                      domain_roles={"finance": ["approver"]})
        other = entity_factory(domain="sales")
        sid = client.post(f"{API}/data/{other.name}", headers=writer,
                          json=self.VALID).json()["staging_id"]
        r = client.post(f"{API}/stewardship/{other.name}/staging/{sid}/approve",
                        headers=appr, json={"note": "approved"})
        assert r.status_code == 200, r.text


# -------------------------- power-user direct path: capture-first guarantee
class TestDirectCaptureFirst:
    """The direct (auto-approve) path must capture the inbound payload FIRST and
    never lose it if the apply step fails for any reason (a 500 that rolled back
    the landing write would violate 'landing never rejects')."""

    VALID = {"code": "cap1", "label": "L", "amount": "1", "category": "alpha"}

    def test_apply_failure_preserves_capture(self, client, mkuser,
                                             entity_factory, monkeypatch):
        h = mkuser("cap_power", [Role.POWER_USER.value])
        ent = entity_factory()

        def _boom(*a, **kw):
            raise RuntimeError("simulated apply-time failure")

        # Force the tier-2 -> tier-3 apply to blow up unexpectedly.
        monkeypatch.setattr("app.api.v1.data.apply_staging_to_live", _boom)

        r = client.post(f"{API}/data/{ent.name}?direct=true", headers=h,
                        json=self.VALID)
        # Not a 500 that discards the capture — a clean 202.
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["applied"] is False
        assert body["landing_id"] is not None
        assert body.get("apply_error")
        assert body["status"] == "captured_pending_review"

        # The landing row survived (capture preserved).
        landing = client.get(f"{API}/stewardship/{ent.name}/landing",
                             headers=h).json()
        assert landing["meta"]["total"] >= 1
        # The staging row survived too, left valid & pending for a steward.
        assert body["staging_id"] is not None
        assert body["validation_passed"] is True
        # Nothing reached the golden tier.
        listing = client.get(f"{API}/data/{ent.name}", headers=h).json()
        assert listing["meta"]["total"] == 0
