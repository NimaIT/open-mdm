"""LDAP / Active Directory authentication.

Uses ldap3's MOCK_SYNC strategy with the offline AD schema, so the real code
paths (search filter construction, bind, group resolution, role mapping) are
exercised without a live directory.
"""
import pytest
from ldap3 import MOCK_SYNC, OFFLINE_AD_2012_R2, Connection, Server

from app.config import settings
from app.services import ldap_auth as LA

SVC_DN = "cn=svc,dc=corp,dc=com"
JANE_DN = "cn=Jane Doe,ou=Users,dc=corp,dc=com"
ADMIN_DN = "cn=Al Admin,ou=Users,dc=corp,dc=com"
ADMIN_GROUP = "CN=MDM_Admins,OU=Groups,DC=corp,DC=com"
STEWARD_GROUP = "CN=MDM_Stewards,OU=Groups,DC=corp,DC=com"
READER_GROUP = "CN=MDM_Readers,OU=Groups,DC=corp,DC=com"

MAPPINGS = [
    (ADMIN_GROUP, "admin"),
    (STEWARD_GROUP, "steward"),
    (READER_GROUP, "reader"),
]


@pytest.fixture
def directory(monkeypatch):
    """A mock AD directory with a service account and two users."""
    monkeypatch.setattr(settings, "LDAP_BASE_DN", "dc=corp,dc=com")
    monkeypatch.setattr(settings, "LDAP_USER_SEARCH_BASE", "dc=corp,dc=com")
    monkeypatch.setattr(settings, "LDAP_GROUP_SEARCH_BASE", "dc=corp,dc=com")

    server = Server("mock_ad", get_info=OFFLINE_AD_2012_R2)
    conn = Connection(server, user=SVC_DN, password="svcpw",
                      client_strategy=MOCK_SYNC)
    conn.strategy.add_entry(SVC_DN, {
        "sAMAccountName": "svc", "userPassword": "svcpw", "objectClass": "user"})
    conn.strategy.add_entry(JANE_DN, {
        "sAMAccountName": "jdoe", "userPassword": "Passw0rd!",
        "mail": "jane@corp.com", "displayName": "Jane Doe",
        "objectClass": "user", "memberOf": [STEWARD_GROUP]})
    conn.strategy.add_entry(ADMIN_DN, {
        "sAMAccountName": "aadmin", "userPassword": "AdminPw1",
        "mail": "al@corp.com", "displayName": "Al Admin",
        "objectClass": "user", "memberOf": [ADMIN_GROUP, READER_GROUP]})
    conn.bind()
    return conn


class TestFilterEscaping:
    """LDAP filter injection must be neutralised (RFC 4515)."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("*", r"\2a"),
            ("(", r"\28"),
            (")", r"\29"),
            ("\\", r"\5c"),
            ("/", r"\2f"),
            ("\x00", r"\00"),
        ],
    )
    def test_escapes_metacharacters(self, raw, expected):
        assert LA._escape_filter(raw) == expected

    def test_escapes_injection_payload(self):
        out = LA._escape_filter("*)(objectClass=*")
        assert "*" not in out and "(" not in out and ")" not in out

    def test_leaves_ordinary_usernames_intact(self):
        assert LA._escape_filter("jdoe") == "jdoe"


class TestUserLookup:
    def test_finds_user(self, directory):
        entry = LA.find_user(directory, "jdoe")
        assert entry["dn"] == JANE_DN
        assert entry["attributes"]["mail"] == ["jane@corp.com"]

    def test_missing_user_returns_none(self, directory):
        assert LA.find_user(directory, "nobody") is None

    def test_wildcard_does_not_match_everyone(self, directory):
        """Unescaped, '*' would match the first user in the tree."""
        assert LA.find_user(directory, "*") is None


class TestPasswordVerification:
    """Passwords are verified by binding as the user, never by comparison."""

    def test_correct_password_binds(self, directory):
        server = Server("mock_ad", get_info=OFFLINE_AD_2012_R2)
        c = Connection(server, user=JANE_DN, password="Passw0rd!",
                       client_strategy=MOCK_SYNC)
        c.strategy.add_entry(JANE_DN, {
            "sAMAccountName": "jdoe", "userPassword": "Passw0rd!",
            "objectClass": "user"})
        assert c.bind() is True

    def test_wrong_password_fails(self, directory):
        server = Server("mock_ad", get_info=OFFLINE_AD_2012_R2)
        c = Connection(server, user=JANE_DN, password="wrong",
                       client_strategy=MOCK_SYNC)
        c.strategy.add_entry(JANE_DN, {
            "sAMAccountName": "jdoe", "userPassword": "Passw0rd!",
            "objectClass": "user"})
        assert c.bind() is False


class TestGroupResolution:
    def test_returns_direct_membership(self, directory):
        groups = LA.resolve_groups(directory, JANE_DN, [STEWARD_GROUP])
        assert STEWARD_GROUP in groups

    def test_degrades_gracefully_without_matching_rule(self, directory):
        """Non-AD servers lack LDAP_MATCHING_RULE_IN_CHAIN; memberOf must survive."""
        groups = LA.resolve_groups(directory, ADMIN_DN,
                                   [ADMIN_GROUP, READER_GROUP])
        assert ADMIN_GROUP in groups and READER_GROUP in groups


class TestRoleMapping:
    def test_maps_group_to_role(self):
        assert LA.map_roles([STEWARD_GROUP], MAPPINGS) == ["steward"]

    def test_multiple_roles(self):
        assert LA.map_roles([ADMIN_GROUP, READER_GROUP], MAPPINGS) == [
            "admin", "reader"]

    def test_unmapped_group_grants_nothing(self):
        assert LA.map_roles(["CN=Randoms,DC=corp,DC=com"], MAPPINGS) == []

    def test_dn_comparison_is_case_and_space_insensitive(self):
        """Directories are inconsistent about DN casing and spacing."""
        messy = "cn=mdm_admins, ou=groups ,dc=corp,dc=com"
        assert LA.map_roles([messy], MAPPINGS) == ["admin"]

    def test_no_groups_no_roles(self):
        assert LA.map_roles([], MAPPINGS) == []

    def test_falls_back_to_env_configuration(self, monkeypatch):
        monkeypatch.setattr(settings, "LDAP_ADMIN_GROUP_DN", ADMIN_GROUP)
        monkeypatch.setattr(settings, "LDAP_STEWARD_GROUP_DN", None)
        monkeypatch.setattr(settings, "LDAP_READER_GROUP_DN", None)
        assert LA.map_roles([ADMIN_GROUP], None) == ["admin"]


class TestAuthenticateGuards:
    def test_refuses_when_disabled(self, monkeypatch):
        monkeypatch.setattr(settings, "LDAP_ENABLED", False)
        with pytest.raises(LA.LDAPConfigurationError):
            LA.authenticate("jdoe", "pw")

    def test_requires_both_credentials(self, monkeypatch):
        monkeypatch.setattr(settings, "LDAP_ENABLED", True)
        with pytest.raises(LA.LDAPAuthenticationError):
            LA.authenticate("jdoe", "")

    def test_unreachable_server_raises_unavailable(self, monkeypatch):
        """The caller must distinguish this from bad credentials so it can
        fall back to the break-glass admin."""
        monkeypatch.setattr(settings, "LDAP_ENABLED", True)
        monkeypatch.setattr(settings, "LDAP_SERVER", "ldap://127.0.0.1:1")
        monkeypatch.setattr(settings, "LDAP_START_TLS", False)
        monkeypatch.setattr(settings, "LDAP_TIMEOUT_SECONDS", 2)
        with pytest.raises((LA.LDAPUnavailableError, LA.LDAPConfigurationError)):
            LA.authenticate("jdoe", "pw")


class TestConnectionTest:
    def test_reports_disabled(self, monkeypatch):
        monkeypatch.setattr(settings, "LDAP_ENABLED", False)
        out = LA.test_connection()
        assert out["ok"] is False and "disabled" in out["error"].lower()
