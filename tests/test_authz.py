"""Authorization: the permission matrix, API keys and route enforcement."""
import pytest

from app.models import Role, User, UserSource
from app.services.auth import (
    AuthError,
    PERMISSIONS,
    authenticate_api_key,
    can_access_entity,
    create_access_token,
    decode_access_token,
    has_permission,
    hash_password,
    issue_api_key,
    permissions_for,
    verify_password,
)
from tests.conftest import requires_db


class TestPasswordHashing:
    def test_round_trip(self):
        h = hash_password("s3cret-pw")
        assert h != "s3cret-pw"
        assert verify_password("s3cret-pw", h) is True
        assert verify_password("wrong", h) is False

    def test_empty_hash_rejected(self):
        assert verify_password("anything", "") is False

    def test_malformed_hash_does_not_raise(self):
        assert verify_password("x", "not-a-bcrypt-hash") is False


class TestTokens:
    def test_round_trip(self):
        t = create_access_token(username="jane", roles=["steward"], source="ldap")
        claims = decode_access_token(t)
        assert claims["sub"] == "jane"
        assert claims["roles"] == ["steward"]

    def test_tampered_token_rejected(self):
        t = create_access_token(username="jane", roles=["steward"], source="ldap")
        with pytest.raises(AuthError):
            decode_access_token(t[:-4] + "AAAA")

    def test_garbage_rejected(self):
        with pytest.raises(AuthError):
            decode_access_token("not.a.jwt")


class TestPermissionMatrix:
    def test_admin_is_superset_of_steward(self):
        assert PERMISSIONS[Role.STEWARD.value] <= PERMISSIONS[Role.ADMIN.value]

    def test_only_admin_publishes_ddl(self):
        assert has_permission(["admin"], "model:publish")
        for role in ("steward", "reader", "service"):
            assert not has_permission([role], "model:publish"), role

    def test_service_cannot_approve(self):
        """A machine account must never be able to self-approve its own writes —
        that would defeat the entire maker-checker design."""
        assert not has_permission(["service"], "staging:approve")
        assert not has_permission(["service"], "staging:edit")

    def test_service_can_write(self):
        assert has_permission(["service"], "data:write")

    def test_reader_is_read_only(self):
        perms = PERMISSIONS[Role.READER.value]
        assert not any(
            p.split(":")[1] in {"write", "approve", "reject", "publish", "drop",
                                "edit", "manage"}
            for p in perms
        )

    def test_steward_can_review_but_not_manage_users(self):
        assert has_permission(["steward"], "staging:approve")
        assert not has_permission(["steward"], "user:manage")

    def test_unknown_role_grants_nothing(self):
        assert permissions_for(["wizard"]) == set()

    def test_roles_combine(self):
        combined = permissions_for(["reader", "steward"])
        assert combined == PERMISSIONS["reader"] | PERMISSIONS["steward"]

    def test_empty_roles(self):
        assert permissions_for([]) == set()
        assert permissions_for(None) == set()


class TestEntityScoping:
    def _svc(self, allowed):
        return User(username="svc", source=UserSource.SERVICE.value,
                    roles=[Role.SERVICE.value],
                    entity_permissions={e: ["write"] for e in allowed})

    def test_scoped_key_reaches_only_its_entities(self):
        u = self._svc(["customer"])
        assert can_access_entity(u, "customer", "write") is True
        assert can_access_entity(u, "vendor", "write") is False

    def test_unscoped_user_reaches_everything(self):
        u = User(username="jane", source=UserSource.LDAP.value,
                 roles=["steward"], entity_permissions={})
        assert can_access_entity(u, "anything", "write") is True

    def test_wrong_action_denied(self):
        u = self._svc(["customer"])
        assert can_access_entity(u, "customer", "approve") is False


@requires_db
class TestApiKeys:
    def test_issue_returns_plaintext_once(self, db):
        record, raw = issue_api_key(db, name="test-key", actor="pytest")
        assert raw.startswith("mdm_")
        # Only the hash is persisted.
        assert record.key_hash != raw
        assert raw[:12] == record.key_prefix
        db.delete(record)

    def test_authenticates_and_yields_service_principal(self, db):
        record, raw = issue_api_key(db, name="k2", source_system="SAP",
                                    actor="pytest")
        db.flush()
        principal, found = authenticate_api_key(db, raw)
        assert found.id == record.id
        assert principal.roles == [Role.SERVICE.value]
        assert not has_permission(principal.roles, "staging:approve")
        db.delete(record)

    def test_rejects_wrong_key(self, db):
        record, raw = issue_api_key(db, name="k3", actor="pytest")
        db.flush()
        with pytest.raises(AuthError):
            authenticate_api_key(db, raw[:-6] + "zzzzzz")
        db.delete(record)

    def test_rejects_malformed(self, db):
        for bad in ("", "abc", "bearer xyz"):
            with pytest.raises(AuthError):
                authenticate_api_key(db, bad)

    def test_rejects_revoked(self, db):
        record, raw = issue_api_key(db, name="k4", actor="pytest")
        record.is_active = False
        db.flush()
        with pytest.raises(AuthError):
            authenticate_api_key(db, raw)
        db.delete(record)

    def test_rejects_expired(self, db):
        from datetime import datetime, timedelta

        record, raw = issue_api_key(
            db, name="k5", expires_at=datetime.utcnow() - timedelta(days=1),
            actor="pytest",
        )
        db.flush()
        with pytest.raises(AuthError, match="expired"):
            authenticate_api_key(db, raw)
        db.delete(record)

    def test_scoped_key_carries_entity_permissions(self, db):
        record, raw = issue_api_key(db, name="k6", allowed_entities=["customer"],
                                    actor="pytest")
        db.flush()
        principal, _ = authenticate_api_key(db, raw)
        assert can_access_entity(principal, "customer", "write") is True
        assert can_access_entity(principal, "vendor", "write") is False
        db.delete(record)
