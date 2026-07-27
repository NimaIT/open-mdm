"""Safe SQL identifier handling and the type system.

Every physical object name in this application originates from user-supplied
metadata, so identifiers are validated against a strict allow-list *and*
quoted. Values are always passed as bind parameters, never interpolated.
"""
import re
from typing import Dict, Optional

# Snake case, starts with a letter. Deliberately strict.
IDENT_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")

# Postgres reserved words that must never be used bare as identifiers.
RESERVED = {
    "all", "analyse", "analyze", "and", "any", "array", "as", "asc", "authorization",
    "between", "binary", "both", "case", "cast", "check", "collate", "column",
    "constraint", "create", "cross", "current_date", "current_role", "current_time",
    "current_timestamp", "current_user", "default", "deferrable", "desc", "distinct",
    "do", "else", "end", "except", "false", "for", "foreign", "freeze", "from", "full",
    "grant", "group", "having", "ilike", "in", "initially", "inner", "intersect",
    "into", "is", "isnull", "join", "leading", "left", "like", "limit", "localtime",
    "localtimestamp", "natural", "not", "notnull", "null", "offset", "on", "only",
    "or", "order", "outer", "overlaps", "placing", "primary", "references", "returning",
    "right", "select", "session_user", "similar", "some", "symmetric", "table", "then",
    "to", "trailing", "true", "union", "unique", "user", "using", "verbose", "when",
    "where", "window", "with",
}

# Reserved column names used by the platform's own tier bookkeeping.
SYSTEM_COLUMNS = {
    "mdm_id", "mdm_version", "mdm_created_at", "mdm_updated_at", "mdm_created_by",
    "mdm_updated_by", "mdm_is_deleted", "mdm_deleted_at", "mdm_source_system",
    "mdm_landing_id", "mdm_staging_id", "mdm_batch_id", "mdm_valid_from",
    "mdm_valid_to", "mdm_operation", "mdm_status", "mdm_errors", "mdm_payload",
    "mdm_received_at", "mdm_reviewed_by", "mdm_reviewed_at", "mdm_review_note",
    "mdm_submitted_by", "mdm_match_key", "mdm_target_id", "mdm_idempotency_key",
    "mdm_history_id", "mdm_change_type", "mdm_edited_by",
}


class IdentifierError(ValueError):
    """Raised when a proposed identifier is unsafe or invalid."""


def validate_ident(name: str, *, kind: str = "identifier") -> str:
    if not isinstance(name, str) or not name:
        raise IdentifierError(f"{kind} must be a non-empty string")
    lowered = name.lower()
    if not IDENT_RE.match(lowered):
        raise IdentifierError(
            f"Invalid {kind} '{name}': must be lower snake_case, start with a letter, "
            "max 63 chars, and contain only [a-z0-9_]."
        )
    if lowered in RESERVED:
        raise IdentifierError(
            f"Invalid {kind} '{name}': '{lowered}' is a reserved SQL keyword."
        )
    return lowered


def validate_column_name(name: str) -> str:
    lowered = validate_ident(name, kind="column name")
    if lowered in SYSTEM_COLUMNS or lowered.startswith("mdm_"):
        raise IdentifierError(
            f"Column '{name}' conflicts with a platform-reserved name. "
            "The 'mdm_' prefix is reserved."
        )
    return lowered


def quote_ident(name: str) -> str:
    """Quote an identifier for safe interpolation.

    Validation happens first; the quoting doubles any embedded quotes as a
    defence-in-depth measure.
    """
    if not isinstance(name, str) or not name:
        raise IdentifierError("identifier must be a non-empty string")
    escaped = name.replace('"', '""')
    return f'"{escaped}"'


def quote_literal(value) -> str:
    """Render a value as a safe single-quoted SQL string literal.

    Doubles any embedded single quotes. Used for the fixed allow-list of an
    enum CHECK constraint, where the permitted values come from metadata and
    cannot be passed as bind parameters (they are baked into the DDL). Never
    interpolate a raw value into SQL without this.
    """
    return "'" + str(value).replace("'", "''") + "'"


def qualified(schema: str, table: str) -> str:
    return f"{quote_ident(schema)}.{quote_ident(table)}"


# --------------------------------------------------------------- type system
# Logical type -> Postgres DDL fragment builder.
TYPE_MAP: Dict[str, Dict] = {
    "string":    {"pg": "varchar",     "sized": True,  "python": "str"},
    "text":      {"pg": "text",        "sized": False, "python": "str"},
    "integer":   {"pg": "integer",     "sized": False, "python": "int"},
    "bigint":    {"pg": "bigint",      "sized": False, "python": "int"},
    "decimal":   {"pg": "numeric",     "sized": False, "python": "Decimal"},
    "float":     {"pg": "double precision", "sized": False, "python": "float"},
    "boolean":   {"pg": "boolean",     "sized": False, "python": "bool"},
    "date":      {"pg": "date",        "sized": False, "python": "date"},
    "timestamp": {"pg": "timestamptz", "sized": False, "python": "datetime"},
    "uuid":      {"pg": "uuid",        "sized": False, "python": "UUID"},
    # A `reference` stores the resolved parent golden-record mdm_id (a uuid).
    # Physically identical to uuid; the logical name drives FK generation and
    # reference-data resolution in the pipeline.
    "reference": {"pg": "uuid",        "sized": False, "python": "UUID"},
    "json":      {"pg": "jsonb",       "sized": False, "python": "dict"},
    "email":     {"pg": "varchar",     "sized": True,  "python": "str", "default_length": 320},
    "url":       {"pg": "text",        "sized": False, "python": "str"},
    "enum":      {"pg": "varchar",     "sized": True,  "python": "str", "default_length": 100},
}

SUPPORTED_TYPES = tuple(TYPE_MAP)


def pg_type(
    data_type: str,
    length: Optional[int] = None,
    precision: Optional[int] = None,
    scale: Optional[int] = None,
) -> str:
    """Render the Postgres column type for a logical MDM type."""
    key = (data_type or "").lower()
    spec = TYPE_MAP.get(key)
    if spec is None:
        raise IdentifierError(
            f"Unsupported data type '{data_type}'. Supported: {', '.join(SUPPORTED_TYPES)}"
        )
    base = spec["pg"]
    if key == "decimal" and precision:
        return f"numeric({int(precision)},{int(scale or 0)})"
    if spec["sized"]:
        size = length or spec.get("default_length")
        if size:
            if not 1 <= int(size) <= 10_485_760:
                raise IdentifierError(f"Invalid length {size} for '{data_type}'.")
            return f"{base}({int(size)})"
        return "text" if base == "varchar" else base
    return base


def type_rank(data_type: str, length: Optional[int] = None) -> tuple:
    """Ordering used to decide whether a type change is a safe widening."""
    key = (data_type or "").lower()
    families = {
        "string": ("text", 1), "text": ("text", 2), "email": ("text", 1),
        "url": ("text", 2), "enum": ("text", 1),
        "integer": ("num", 1), "bigint": ("num", 2),
        "decimal": ("num", 3), "float": ("num", 4),
        "boolean": ("bool", 1), "date": ("time", 1), "timestamp": ("time", 2),
        "uuid": ("uuid", 1), "reference": ("uuid", 1), "json": ("json", 1),
    }
    family, rank = families.get(key, ("other", 0))
    return (family, rank, length or 0)


def is_safe_widening(
    old_type: str, old_len: Optional[int], new_type: str, new_len: Optional[int]
) -> bool:
    """True when ALTER TYPE cannot lose data."""
    of, orank, olen = type_rank(old_type, old_len)
    nf, nrank, nlen = type_rank(new_type, new_len)
    if of != nf:
        return False
    if nrank < orank:
        return False
    if of == "text":
        # varchar(n) -> text is safe; varchar(50) -> varchar(20) is not.
        if nrank > orank:
            return True
        return (nlen == 0) or (olen != 0 and nlen >= olen)
    return True
