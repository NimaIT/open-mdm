"""Type coercion, validation and normalisation of inbound values.

This is the gate between the schemaless landing tier and the typed staging
tier. It never raises on bad data — it returns the coerced row plus a list of
structured errors, because a steward has to be able to *see* invalid data in
order to fix it.
"""
import json
import re
import uuid
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from app.services.logging_config import stream_logger
from app.services.transforms import apply_transforms

# Validation is part of ingestion (landing -> staging) -> "integration" stream.
log = stream_logger("integration")

TRUTHY = {"true", "t", "yes", "y", "1", "on"}
FALSY = {"false", "f", "no", "n", "0", "off"}

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
URL_RE = re.compile(r"^https?://[^\s]+$", re.IGNORECASE)

DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d", "%d-%b-%Y", "%Y%m%d")
DATETIME_FORMATS = (
    "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M",
)


class ValidationError(dict):
    """Structured, serialisable error record."""

    def __init__(self, field: str, code: str, message: str, value: Any = None):
        super().__init__(field=field, code=code, message=message,
                         value=None if value is None else str(value)[:500])


# ------------------------------------------------------------- normalisation
def normalize_value(value: Any, rules: Optional[List[str]]) -> Any:
    if value is None or not rules:
        return value
    if not isinstance(value, str):
        return value
    out = value
    for rule in rules:
        r = (rule or "").lower()
        if r == "trim":
            out = out.strip()
        elif r == "lower":
            out = out.lower()
        elif r == "upper":
            out = out.upper()
        elif r == "title":
            out = out.title()
        elif r == "collapse_whitespace":
            out = re.sub(r"\s+", " ", out)
        elif r == "strip_punctuation":
            out = re.sub(r"[^\w\s]", "", out)
        elif r == "digits_only":
            out = re.sub(r"\D", "", out)
        elif r == "nullify_empty" and out.strip() == "":
            return None
    return out


# ----------------------------------------------------------------- coercion
def coerce_value(value: Any, data_type: str) -> Tuple[Any, Optional[str]]:
    """Coerce a raw value to the target logical type.

    Returns (coerced, error_message). Empty strings become NULL.
    """
    if value is None:
        return None, None
    if isinstance(value, str) and value.strip() == "":
        return None, None

    dt = (data_type or "").lower()
    try:
        if dt in {"string", "text", "url", "enum"}:
            if isinstance(value, (dict, list)):
                return json.dumps(value), None
            return str(value), None

        if dt == "email":
            s = str(value).strip()
            if not EMAIL_RE.match(s):
                return s, f"'{s}' is not a valid email address"
            return s, None

        if dt in {"integer", "bigint"}:
            if isinstance(value, bool):
                return int(value), None
            if isinstance(value, float) and not float(value).is_integer():
                return None, f"'{value}' is not a whole number"
            iv = int(str(value).strip().replace(",", "")) if isinstance(value, str) else int(value)
            limit = 2_147_483_647 if dt == "integer" else 9_223_372_036_854_775_807
            if abs(iv) > limit:
                return None, f"{iv} exceeds the range of {dt}"
            return iv, None

        if dt == "decimal":
            return Decimal(str(value).strip().replace(",", "")), None

        if dt == "float":
            return float(str(value).strip().replace(",", "")), None

        if dt == "boolean":
            if isinstance(value, bool):
                return value, None
            s = str(value).strip().lower()
            if s in TRUTHY:
                return True, None
            if s in FALSY:
                return False, None
            return None, f"'{value}' is not a recognised boolean"

        if dt == "date":
            if isinstance(value, datetime):
                return value.date(), None
            if isinstance(value, date):
                return value, None
            s = str(value).strip()
            for fmt in DATE_FORMATS:
                try:
                    return datetime.strptime(s, fmt).date(), None
                except ValueError:
                    continue
            try:
                return datetime.fromisoformat(s).date(), None
            except ValueError:
                return None, f"'{s}' is not a recognised date"

        if dt == "timestamp":
            if isinstance(value, datetime):
                return value, None
            s = str(value).strip().replace("Z", "+00:00")
            for fmt in DATETIME_FORMATS:
                try:
                    return datetime.strptime(s, fmt), None
                except ValueError:
                    continue
            try:
                return datetime.fromisoformat(s), None
            except ValueError:
                return None, f"'{s}' is not a recognised timestamp"

        if dt == "uuid":
            return uuid.UUID(str(value).strip()), None

        if dt == "json":
            if isinstance(value, (dict, list)):
                return value, None
            return json.loads(str(value)), None

        return str(value), None
    except (ValueError, TypeError, InvalidOperation, json.JSONDecodeError) as exc:
        return None, f"cannot convert '{value}' to {dt}: {exc}"


# --------------------------------------------------------------- constraints
def check_constraints(name: str, value: Any, attr) -> List[ValidationError]:
    errors: List[ValidationError] = []
    rules = attr.validation or {}

    if value is None:
        if attr.is_required:
            errors.append(ValidationError(name, "required", f"'{name}' is required"))
        return errors

    if isinstance(value, str):
        max_len = rules.get("max_length") or attr.length
        if max_len and len(value) > int(max_len):
            errors.append(
                ValidationError(
                    name, "max_length",
                    f"'{name}' is {len(value)} chars; maximum is {max_len}", value,
                )
            )
        if rules.get("min_length") and len(value) < int(rules["min_length"]):
            errors.append(
                ValidationError(
                    name, "min_length",
                    f"'{name}' is shorter than the minimum {rules['min_length']}", value,
                )
            )
        pattern = rules.get("regex") or rules.get("pattern")
        if pattern:
            try:
                if not re.match(pattern, value):
                    errors.append(
                        ValidationError(
                            name, "regex",
                            f"'{name}' does not match the required pattern", value,
                        )
                    )
            except re.error:
                errors.append(
                    ValidationError(
                        name, "bad_pattern",
                        f"Configured regex for '{name}' is invalid", pattern,
                    )
                )

    allowed = rules.get("enum") or rules.get("allowed_values")
    if allowed:
        if str(value) not in {str(a) for a in allowed}:
            errors.append(
                ValidationError(
                    name, "enum",
                    f"'{name}' must be one of: {', '.join(map(str, allowed))}", value,
                )
            )

    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        if rules.get("min") is not None and value < Decimal(str(rules["min"])):
            errors.append(
                ValidationError(name, "min", f"'{name}' is below minimum {rules['min']}", value)
            )
        if rules.get("max") is not None and value > Decimal(str(rules["max"])):
            errors.append(
                ValidationError(name, "max", f"'{name}' exceeds maximum {rules['max']}", value)
            )

    if attr.data_type == "url" and not URL_RE.match(str(value)):
        errors.append(ValidationError(name, "url", f"'{name}' is not a valid URL", value))

    return errors


def validate_record(payload: Dict[str, Any], entity, *, partial: bool = False) -> Dict:
    """Coerce, normalise and validate one inbound record.

    ``partial`` (PATCH semantics) suppresses required-field checks for absent
    keys, so a partial update isn't rejected for omitting untouched fields.
    """
    coerced: Dict[str, Any] = {}
    errors: List[ValidationError] = []
    known = {a.name: a for a in entity.attributes}

    for key in payload:
        if key not in known and not key.startswith("mdm_"):
            errors.append(
                ValidationError(key, "unknown_field",
                                f"'{key}' is not defined on entity '{entity.name}'")
            )

    for name, attr in known.items():
        present = name in payload
        if partial and not present:
            continue
        raw = payload.get(name)
        raw = normalize_value(raw, attr.normalization)
        value, err = coerce_value(raw, attr.data_type)
        if err:
            errors.append(ValidationError(name, "type", err, raw))
            coerced[name] = None
            continue
        if value is None and attr.default_value and not present:
            value, _ = coerce_value(attr.default_value, attr.data_type)
        # Custom transforms (EX-2) run AFTER normalisation and coercion, in order.
        # A transform failure (bad value or unknown name) becomes a structured
        # validation error — the row is held invalid, never a crash.
        transforms = getattr(attr, "transforms", None)
        if transforms:
            value, terr = apply_transforms(value, transforms, field=name)
            if terr:
                errors.append(
                    ValidationError(name, terr["code"], terr["message"], value)
                )
                coerced[name] = None
                continue
            # A transform may emit a value of the wrong logical type (e.g. a map
            # that yields text for a numeric column). Re-coerce so that becomes a
            # per-row structured validation error (row held invalid in staging),
            # mirroring the normal coercion path — never a whole-row INSERT crash.
            value, cerr = coerce_value(value, attr.data_type)
            if cerr:
                errors.append(ValidationError(name, "type", cerr, value))
                coerced[name] = None
                continue
        coerced[name] = value
        errors.extend(check_constraints(name, value, attr))

    if errors:
        # Field/code only — never the raw values (may be PII) at this level.
        log.debug(
            "record failed validation entity=%s errors=%s",
            getattr(entity, "name", None), len(errors),
            extra={"event": "record_invalid",
                   "entity": getattr(entity, "name", None),
                   "error_count": len(errors),
                   "error_codes": sorted({e.get("code") for e in errors})},
        )

    return {"values": coerced, "errors": errors, "is_valid": not errors}


def build_match_key(values: Dict[str, Any], entity) -> Optional[str]:
    """Deterministic match key from the configured match attributes.

    Normalisation is applied so that 'Acme Corp.' and 'acme corp' collide.
    """
    keys = entity.match_keys or entity.business_key
    if not keys:
        return None
    parts: List[str] = []
    for attr in keys:
        v = values.get(attr.name)
        if v is None:
            return None
        s = str(v).strip().lower()
        s = re.sub(r"[^\w\s]", "", s)
        s = re.sub(r"\s+", " ", s)
        parts.append(s)
    return "|".join(parts) if parts else None
