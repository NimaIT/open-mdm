"""Identifier safety and the type system.

These are the SQL-injection guardrails: every physical object name in the
product originates from user-supplied metadata.
"""
import pytest

from app.services.identifiers import (
    IdentifierError,
    is_safe_widening,
    pg_type,
    qualified,
    quote_ident,
    validate_column_name,
    validate_ident,
)


class TestValidateIdent:
    @pytest.mark.parametrize("name", ["customer", "customer_master", "a", "x9_y"])
    def test_accepts_snake_case(self, name):
        assert validate_ident(name) == name

    def test_lowercases(self):
        # Postgres folds unquoted identifiers, so normalise rather than reject.
        assert validate_ident("Customer") == "customer"

    @pytest.mark.parametrize(
        "name",
        [
            "1abc",                      # leading digit
            "has-dash",
            "has space",
            "select",                    # reserved word
            "table",
            "x" * 64,                    # too long
            "",
            "drop table x",
            'quote"inject',
            "semi;colon",
        ],
    )
    def test_rejects_unsafe(self, name):
        with pytest.raises(IdentifierError):
            validate_ident(name)

    def test_rejects_non_string(self):
        with pytest.raises(IdentifierError):
            validate_ident(None)


class TestColumnNames:
    def test_rejects_mdm_prefix(self):
        # The mdm_ namespace belongs to the platform's tier bookkeeping.
        for name in ("mdm_id", "mdm_version", "mdm_anything"):
            with pytest.raises(IdentifierError):
                validate_column_name(name)

    def test_accepts_normal(self):
        assert validate_column_name("legal_name") == "legal_name"


class TestQuoting:
    def test_wraps_in_double_quotes(self):
        assert quote_ident("customer") == '"customer"'

    def test_doubles_embedded_quotes(self):
        # Defence in depth: even if validation were bypassed, quoting holds.
        assert quote_ident('weird"name') == '"weird""name"'

    def test_qualified(self):
        assert qualified("mdm", "customer") == '"mdm"."customer"'


class TestPgType:
    @pytest.mark.parametrize(
        "logical,kwargs,expected",
        [
            ("string", {"length": 100}, "varchar(100)"),
            ("string", {}, "text"),
            ("text", {}, "text"),
            ("integer", {}, "integer"),
            ("bigint", {}, "bigint"),
            ("decimal", {"precision": 12, "scale": 2}, "numeric(12,2)"),
            ("boolean", {}, "boolean"),
            ("date", {}, "date"),
            ("timestamp", {}, "timestamptz"),
            ("uuid", {}, "uuid"),
            ("json", {}, "jsonb"),
            ("email", {}, "varchar(320)"),
        ],
    )
    def test_renders(self, logical, kwargs, expected):
        assert pg_type(logical, **kwargs) == expected

    def test_rejects_unknown(self):
        with pytest.raises(IdentifierError):
            pg_type("bogus_type")

    def test_rejects_absurd_length(self):
        with pytest.raises(IdentifierError):
            pg_type("string", length=99_999_999)


class TestSafeWidening:
    @pytest.mark.parametrize(
        "old,ol,new,nl",
        [
            ("string", 50, "string", 100),   # wider varchar
            ("string", 50, "text", None),    # varchar -> text
            ("integer", None, "bigint", None),
            ("integer", None, "decimal", None),
            ("date", None, "timestamp", None),
        ],
    )
    def test_safe(self, old, ol, new, nl):
        assert is_safe_widening(old, ol, new, nl) is True

    @pytest.mark.parametrize(
        "old,ol,new,nl",
        [
            ("string", 100, "string", 50),   # truncation
            ("text", None, "string", 50),
            ("bigint", None, "integer", None),
            ("string", 50, "integer", None),  # family change
            ("timestamp", None, "date", None),
            ("json", None, "string", 50),
        ],
    )
    def test_unsafe(self, old, ol, new, nl):
        assert is_safe_widening(old, ol, new, nl) is False
