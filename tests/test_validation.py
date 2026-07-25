"""Coercion, normalisation and constraint validation."""
from decimal import Decimal

import pytest

from app.services.validation import (
    build_match_key,
    check_constraints,
    coerce_value,
    normalize_value,
    validate_record,
)


class _Attr:
    def __init__(self, name, data_type="string", **kw):
        self.name = name
        self.data_type = data_type
        self.length = kw.get("length")
        self.is_required = kw.get("is_required", False)
        self.is_unique = kw.get("is_unique", False)
        self.is_business_key = kw.get("is_business_key", False)
        self.is_match_key = kw.get("is_match_key", False)
        self.default_value = kw.get("default_value")
        self.validation = kw.get("validation", {})
        self.normalization = kw.get("normalization", [])


class _Entity:
    name = "thing"

    def __init__(self, attrs):
        self.attributes = attrs

    @property
    def match_keys(self):
        return [a for a in self.attributes if a.is_match_key]

    @property
    def business_key(self):
        return [a for a in self.attributes if a.is_business_key]


class TestCoercion:
    @pytest.mark.parametrize(
        "raw,logical,expected",
        [
            ("42", "integer", 42),
            (42, "integer", 42),
            ("1,234", "integer", 1234),
            ("1,234.56", "decimal", Decimal("1234.56")),
            ("3.5", "float", 3.5),
            ("yes", "boolean", True),
            ("N", "boolean", False),
            ("true", "boolean", True),
            (True, "boolean", True),
            ("2024-03-15", "date", None),   # checked below by type
            ("hello", "string", "hello"),
            ('{"a":1}', "json", {"a": 1}),
            ({"a": 1}, "json", {"a": 1}),
        ],
    )
    def test_coerces(self, raw, logical, expected):
        value, err = coerce_value(raw, logical)
        assert err is None
        if expected is not None:
            assert value == expected

    def test_multiple_date_formats(self):
        for raw in ("2024-03-15", "15/03/2024", "2024/03/15", "20240315"):
            value, err = coerce_value(raw, "date")
            assert err is None, raw
            assert (value.year, value.month, value.day) == (2024, 3, 15), raw

    @pytest.mark.parametrize(
        "raw,logical",
        [
            ("abc", "integer"),
            ("3.7", "integer"),          # not a whole number
            ("not-a-date", "date"),
            ("maybe", "boolean"),
            ("{bad json", "json"),
            ("nope", "uuid"),
        ],
    )
    def test_reports_errors(self, raw, logical):
        value, err = coerce_value(raw, logical)
        assert err is not None
        assert value is None

    def test_invalid_email_keeps_value_and_errors(self):
        # The steward needs to see what was actually sent in order to fix it.
        value, err = coerce_value("bad@", "email")
        assert value == "bad@"
        assert err is not None

    def test_empty_string_becomes_null(self):
        assert coerce_value("", "string") == (None, None)
        assert coerce_value("   ", "integer") == (None, None)

    def test_none_passes_through(self):
        assert coerce_value(None, "integer") == (None, None)

    def test_integer_overflow_detected(self):
        value, err = coerce_value("99999999999", "integer")
        assert err is not None and "range" in err


class TestNormalisation:
    @pytest.mark.parametrize(
        "raw,rules,expected",
        [
            ("  x  ", ["trim"], "x"),
            ("abc", ["upper"], "ABC"),
            ("ABC", ["lower"], "abc"),
            ("a   b", ["collapse_whitespace"], "a b"),
            ("a.b,c", ["strip_punctuation"], "abc"),
            ("+61 400 000", ["digits_only"], "61400000"),
            ("  acme co ", ["trim", "upper"], "ACME CO"),
            ("   ", ["trim", "nullify_empty"], None),
        ],
    )
    def test_rules(self, raw, rules, expected):
        assert normalize_value(raw, rules) == expected

    def test_leaves_non_strings_alone(self):
        assert normalize_value(42, ["trim", "upper"]) == 42


class TestConstraints:
    def test_required(self):
        errs = check_constraints("f", None, _Attr("f", is_required=True))
        assert [e["code"] for e in errs] == ["required"]

    def test_optional_null_is_fine(self):
        assert check_constraints("f", None, _Attr("f")) == []

    def test_max_length(self):
        errs = check_constraints("f", "x" * 20, _Attr("f", length=10))
        assert errs[0]["code"] == "max_length"

    def test_regex(self):
        a = _Attr("f", validation={"regex": r"^[A-Z]{3}$"})
        assert check_constraints("f", "ABC", a) == []
        assert check_constraints("f", "abc", a)[0]["code"] == "regex"

    def test_bad_configured_regex_is_reported_not_raised(self):
        a = _Attr("f", validation={"regex": "([unclosed"})
        errs = check_constraints("f", "x", a)
        assert errs[0]["code"] == "bad_pattern"

    def test_enum(self):
        a = _Attr("f", validation={"enum": ["a", "b"]})
        assert check_constraints("f", "a", a) == []
        assert check_constraints("f", "z", a)[0]["code"] == "enum"

    def test_min_max_numeric(self):
        a = _Attr("n", "integer", validation={"min": 0, "max": 100})
        assert check_constraints("n", 50, a) == []
        assert check_constraints("n", -1, a)[0]["code"] == "min"
        assert check_constraints("n", 101, a)[0]["code"] == "max"

    def test_boolean_not_treated_as_number(self):
        # bool is a subclass of int in Python; min/max must not fire on it.
        a = _Attr("b", "boolean", validation={"min": 5})
        assert check_constraints("b", True, a) == []


class TestValidateRecord:
    def _entity(self):
        return _Entity([
            _Attr("code", "string", length=10, is_required=True,
                  is_business_key=True, normalization=["trim", "upper"]),
            _Attr("name", "string", length=20, is_required=True,
                  is_match_key=True, normalization=["trim", "collapse_whitespace"]),
            _Attr("email", "email"),
            _Attr("amount", "decimal", validation={"min": 0}),
            _Attr("tier", "enum", validation={"enum": ["gold", "silver"]}),
        ])

    def test_clean_record(self):
        out = validate_record(
            {"code": " ab-1 ", "name": "Acme   Co", "email": "a@b.com",
             "amount": "10", "tier": "gold"},
            self._entity(),
        )
        assert out["is_valid"]
        assert out["values"]["code"] == "AB-1"
        assert out["values"]["name"] == "Acme Co"

    def test_collects_all_errors_at_once(self):
        out = validate_record(
            {"code": "x" * 30, "name": "", "email": "bad", "amount": "-1",
             "tier": "bronze", "unknown_field": 1},
            self._entity(),
        )
        codes = {e["code"] for e in out["errors"]}
        assert not out["is_valid"]
        # A steward should see every problem in one pass, not one at a time.
        assert {"max_length", "required", "type", "min", "enum",
                "unknown_field"} <= codes

    def test_partial_suppresses_required(self):
        out = validate_record({"email": "a@b.com"}, self._entity(), partial=True)
        assert out["is_valid"]
        assert list(out["values"]) == ["email"]

    def test_default_applied_when_absent(self):
        ent = _Entity([_Attr("flag", "boolean", default_value="true")])
        out = validate_record({}, ent)
        assert out["values"]["flag"] is True


class TestMatchKey:
    def test_normalises_for_comparison(self):
        ent = _Entity([_Attr("name", is_match_key=True)])
        assert build_match_key({"name": "Acme Corp."}, ent) == "acme corp"
        assert build_match_key({"name": "  ACME   corp  "}, ent) == "acme corp"

    def test_composite(self):
        ent = _Entity([
            _Attr("a", is_match_key=True), _Attr("b", is_match_key=True)
        ])
        assert build_match_key({"a": "X", "b": "Y"}, ent) == "x|y"

    def test_none_when_component_missing(self):
        ent = _Entity([_Attr("a", is_match_key=True)])
        assert build_match_key({"a": None}, ent) is None

    def test_falls_back_to_business_key(self):
        ent = _Entity([_Attr("code", is_business_key=True)])
        assert build_match_key({"code": "AB"}, ent) == "ab"
