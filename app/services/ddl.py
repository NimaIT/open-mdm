"""DDL generation: turn an entity definition into physical tables.

Each published entity materialises four tables:

  landing  mdm_landing.<entity>    raw append-only inbound writes (jsonb)
  staging  mdm_staging.<entity>    typed, validated, awaiting steward review
  live     mdm.<entity>            golden records
  history  mdm_history.<entity>    prior versions of golden records

Generation is deterministic and diffable: publishing an updated model produces
an additive migration by default, and anything potentially destructive is
reported for explicit confirmation rather than silently applied.
"""
import hashlib
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.config import settings
from app.services.identifiers import (
    IdentifierError,
    is_safe_widening,
    pg_type,
    qualified,
    quote_ident,
    quote_literal,
    validate_column_name,
    validate_ident,
)

LANDING = "landing"
STAGING = "staging"
LIVE = "live"
HISTORY = "history"
TIERS = (LANDING, STAGING, LIVE, HISTORY)

SCHEMA_FOR = {
    LANDING: settings.SCHEMA_LANDING,
    STAGING: settings.SCHEMA_STAGING,
    LIVE: settings.SCHEMA_LIVE,
    HISTORY: settings.SCHEMA_HISTORY,
}


@dataclass
class ColumnDef:
    name: str
    data_type: str
    length: Optional[int] = None
    precision: Optional[int] = None
    scale: Optional[int] = None
    required: bool = False
    unique: bool = False
    default: Optional[str] = None
    indexed: bool = False
    # Reference / lookup relationship (DM-2/DM-3/DM-5): the parent entity whose
    # golden-record mdm_id this column points at, and the parent attribute used
    # to resolve inbound human-readable values.
    ref_entity: Optional[str] = None
    ref_attribute: Optional[str] = None
    # Enumerated allow-list (DM-4): rendered as a live-tier CHECK constraint.
    enum_values: Optional[Sequence] = None

    def sql_type(self) -> str:
        return pg_type(self.data_type, self.length, self.precision, self.scale)

    @property
    def is_reference(self) -> bool:
        return (self.data_type or "").lower() == "reference"


@dataclass
class DDLPlan:
    """A reviewable set of statements, with risk classification."""

    statements: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    destructive: List[str] = field(default_factory=list)

    def add(self, sql: str) -> None:
        self.statements.append(sql)

    @property
    def is_destructive(self) -> bool:
        return bool(self.destructive)

    @property
    def sql(self) -> str:
        return ";\n\n".join(self.statements) + (";" if self.statements else "")

    def to_dict(self) -> Dict:
        return {
            "statements": self.statements,
            "sql": self.sql,
            "warnings": self.warnings,
            "destructive": self.destructive,
            "is_destructive": self.is_destructive,
            "statement_count": len(self.statements),
        }


def columns_from_entity(entity) -> List[ColumnDef]:
    """Project entity metadata into business column definitions."""
    cols: List[ColumnDef] = []
    for attr in sorted(entity.attributes, key=lambda a: a.position):
        validate_column_name(attr.name)
        validation = attr.validation or {}
        enum_values = validation.get("enum") or validation.get("allowed_values")
        cols.append(
            ColumnDef(
                name=attr.name.lower(),
                data_type=attr.data_type,
                length=attr.length,
                precision=attr.numeric_precision,
                scale=attr.numeric_scale,
                required=attr.is_required,
                unique=attr.is_unique,
                default=attr.default_value,
                indexed=attr.is_indexed or attr.is_match_key or attr.is_business_key,
                ref_entity=attr.ref_entity,
                ref_attribute=attr.ref_attribute,
                enum_values=list(enum_values) if enum_values else None,
            )
        )
    if not cols:
        raise IdentifierError("Entity must define at least one attribute.")
    return cols


# ------------------------------------------------------------ CREATE builders
def _enum_check_sql(col: ColumnDef) -> str:
    """Render an enum allow-list as a CHECK constraint fragment (DM-4).

    Values come from metadata and are baked into the DDL, so they cannot be
    bind parameters — they go through quote_literal, never raw interpolation.
    """
    literals = ", ".join(quote_literal(v) for v in col.enum_values)
    return f"CHECK ({quote_ident(col.name)} IN ({literals}))"


def _business_column_sql(
    col: ColumnDef, *, enforce_not_null: bool, emit_enum_check: bool = False
) -> str:
    parts = [f"  {quote_ident(col.name)} {col.sql_type()}"]
    if enforce_not_null and col.required:
        parts.append("NOT NULL")
    # Enum CHECK constraints live on the golden tier only. Landing is jsonb and
    # staging deliberately stores invalid rows, so neither may carry it. Emitted
    # inline here (so a re-run of the idempotent CREATE stays a no-op); an edit
    # to the allow-list on an already-published column is reconciled separately
    # by reconcile_enum_constraints, which finds the CHECK by column, not name.
    if emit_enum_check and col.enum_values:
        parts.append(_enum_check_sql(col))
    if col.default is not None and col.default != "":
        # Defaults are metadata-controlled and validated upstream; render as a
        # literal only for simple scalars, otherwise skip.
        parts.append(f"DEFAULT {_render_default(col)}")
    return " ".join(parts)


def _render_default(col: ColumnDef) -> str:
    raw = str(col.default)
    lowered = raw.strip().lower()
    if lowered in {"now()", "current_timestamp", "current_date", "gen_random_uuid()"}:
        return lowered
    if col.data_type in {"integer", "bigint", "decimal", "float"}:
        float(raw)  # raises ValueError on junk
        return raw
    if col.data_type == "boolean":
        if lowered not in {"true", "false"}:
            raise IdentifierError(f"Invalid boolean default '{raw}'")
        return lowered
    return "'" + raw.replace("'", "''") + "'"


def build_landing_ddl(entity_name: str, cols: Sequence[ColumnDef]) -> List[str]:
    """Landing is intentionally permissive: it must never reject an inbound write."""
    t = qualified(settings.SCHEMA_LANDING, entity_name)
    stmts = [
        f"""CREATE TABLE IF NOT EXISTS {t} (
  mdm_landing_id      bigserial PRIMARY KEY,
  mdm_operation       varchar(10)  NOT NULL CHECK (mdm_operation IN ('INSERT','UPDATE','DELETE','UPSERT')),
  mdm_payload         jsonb        NOT NULL,
  mdm_target_id       varchar(64),
  mdm_source_system   varchar(100),
  mdm_batch_id        uuid,
  mdm_idempotency_key varchar(255),
  mdm_submitted_by    varchar(255),
  mdm_received_at     timestamptz  NOT NULL DEFAULT now(),
  mdm_status          varchar(20)  NOT NULL DEFAULT 'pending'
                      CHECK (mdm_status IN ('pending','promoted','rejected','error')),
  mdm_errors          jsonb,
  mdm_processed_at    timestamptz
)""",
        f"CREATE INDEX IF NOT EXISTS {quote_ident('ix_' + entity_name + '_landing_status')} "
        f"ON {t} (mdm_status, mdm_received_at)",
        f"CREATE INDEX IF NOT EXISTS {quote_ident('ix_' + entity_name + '_landing_batch')} "
        f"ON {t} (mdm_batch_id)",
        f"CREATE UNIQUE INDEX IF NOT EXISTS {quote_ident('uq_' + entity_name + '_landing_idem')} "
        f"ON {t} (mdm_idempotency_key) WHERE mdm_idempotency_key IS NOT NULL",
    ]
    return stmts


def build_staging_ddl(entity_name: str, cols: Sequence[ColumnDef]) -> List[str]:
    """Staging is typed but *not* NOT-NULL constrained — invalid rows must be
    storable so a steward can see and fix them."""
    t = qualified(settings.SCHEMA_STAGING, entity_name)
    body = ",\n".join(_business_column_sql(c, enforce_not_null=False) for c in cols)
    stmts = [
        f"""CREATE TABLE IF NOT EXISTS {t} (
  mdm_staging_id      bigserial PRIMARY KEY,
  mdm_landing_id      bigint,
  mdm_operation       varchar(10)  NOT NULL,
  mdm_target_id       varchar(64),
  mdm_match_key       text,
  mdm_source_system   varchar(100),
  mdm_batch_id        uuid,
  mdm_status          varchar(20)  NOT NULL DEFAULT 'pending_review'
                      CHECK (mdm_status IN ('pending_review','changes_requested','approved','rejected','applied','error')),
  mdm_errors          jsonb        NOT NULL DEFAULT '[]'::jsonb,
  mdm_is_valid        boolean      NOT NULL DEFAULT false,
  mdm_change_type     varchar(20),
  mdm_submitted_by    varchar(255),
  mdm_submitted_at    timestamptz  NOT NULL DEFAULT now(),
  mdm_edited_by       varchar(255),
  mdm_edited_at       timestamptz,
  mdm_reviewed_by     varchar(255),
  mdm_reviewed_at     timestamptz,
  mdm_review_note     text,
  -- Which business fields the caller actually supplied. Required to
  -- distinguish "not sent" from "sent as the column default", so a PATCH
  -- cannot silently overwrite untouched golden-record fields.
  mdm_supplied_fields jsonb        NOT NULL DEFAULT '[]'::jsonb,
{body}
)""",
        f"CREATE INDEX IF NOT EXISTS {quote_ident('ix_' + entity_name + '_staging_status')} "
        f"ON {t} (mdm_status, mdm_submitted_at)",
        f"CREATE INDEX IF NOT EXISTS {quote_ident('ix_' + entity_name + '_staging_match')} "
        f"ON {t} (mdm_match_key)",
    ]
    return stmts


def _enum_check_add_sql(entity_name: str, col: ColumnDef) -> str:
    """A named ``ADD CONSTRAINT ... CHECK`` for an enum allow-list (live tier).

    Named deterministically so re-publish can find, drop and re-add it when the
    allow-list changes. Values are baked into the DDL via ``quote_literal``.
    """
    live = qualified(settings.SCHEMA_LIVE, entity_name)
    cname = _enum_check_name(entity_name, col.name)
    return (
        f"ALTER TABLE {live} ADD CONSTRAINT {quote_ident(cname)} "
        f"{_enum_check_sql(col)}"
    )


def _association_ref_cols(cols: Sequence[ColumnDef]) -> List[str]:
    """The reference column names of an association (junction) entity."""
    return [c.name for c in cols if c.is_reference and c.ref_entity]


def _association_unique_index_sql(entity_name: str, ref_cols: Sequence[str]) -> str:
    """One composite partial UNIQUE index across an association's reference
    columns (DM-3): prevents duplicate many-to-many edges while allowing a
    soft-deleted edge to be recreated."""
    idxname = constraint_name("uqa", entity_name, "_".join(ref_cols))
    t = qualified(settings.SCHEMA_LIVE, entity_name)
    cols_sql = ", ".join(quote_ident(c) for c in ref_cols)
    return (
        f"CREATE UNIQUE INDEX IF NOT EXISTS {quote_ident(idxname)} "
        f"ON {t} ({cols_sql}) WHERE mdm_is_deleted = false"
    )


def build_live_ddl(
    entity_name: str, cols: Sequence[ColumnDef], *, kind: str = "master"
) -> List[str]:
    """Live carries the real constraints — this is the golden record."""
    t = qualified(settings.SCHEMA_LIVE, entity_name)
    body = ",\n".join(
        _business_column_sql(c, enforce_not_null=True, emit_enum_check=True)
        for c in cols
    )
    stmts = [
        f"""CREATE TABLE IF NOT EXISTS {t} (
  mdm_id            uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
  mdm_version       integer      NOT NULL DEFAULT 1,
  mdm_source_system varchar(100),
  mdm_is_deleted    boolean      NOT NULL DEFAULT false,
  mdm_deleted_at    timestamptz,
  mdm_created_at    timestamptz  NOT NULL DEFAULT now(),
  mdm_created_by    varchar(255),
  mdm_updated_at    timestamptz  NOT NULL DEFAULT now(),
  mdm_updated_by    varchar(255),
  mdm_approved_by   varchar(255),
  mdm_staging_id    bigint,
{body}
)"""
    ]
    for col in cols:
        if col.unique:
            # Partial unique index so soft-deleted rows don't block reuse.
            stmts.append(
                f"CREATE UNIQUE INDEX IF NOT EXISTS "
                f"{quote_ident('uq_' + entity_name + '_' + col.name)} "
                f"ON {t} ({quote_ident(col.name)}) WHERE mdm_is_deleted = false"
            )
        elif col.indexed:
            stmts.append(
                f"CREATE INDEX IF NOT EXISTS "
                f"{quote_ident('ix_' + entity_name + '_' + col.name)} "
                f"ON {t} ({quote_ident(col.name)})"
            )
    stmts.append(
        f"CREATE INDEX IF NOT EXISTS {quote_ident('ix_' + entity_name + '_live_active')} "
        f"ON {t} (mdm_is_deleted, mdm_updated_at)"
    )
    # An association (junction) entity gets ONE composite partial UNIQUE index
    # across its reference columns, so a many-to-many edge cannot be duplicated.
    if (kind or "master").lower() == "association":
        ref_cols = _association_ref_cols(cols)
        if len(ref_cols) >= 2:
            stmts.append(_association_unique_index_sql(entity_name, ref_cols))
    return stmts


def build_history_ddl(entity_name: str, cols: Sequence[ColumnDef]) -> List[str]:
    """History mirrors live with no constraints — it stores what *was* true."""
    t = qualified(settings.SCHEMA_HISTORY, entity_name)
    body = ",\n".join(_business_column_sql(c, enforce_not_null=False) for c in cols)
    return [
        f"""CREATE TABLE IF NOT EXISTS {t} (
  mdm_history_id    bigserial    PRIMARY KEY,
  mdm_id            uuid         NOT NULL,
  mdm_version       integer      NOT NULL,
  mdm_change_type   varchar(20)  NOT NULL,
  mdm_valid_from    timestamptz  NOT NULL,
  mdm_valid_to      timestamptz  NOT NULL DEFAULT now(),
  mdm_changed_by    varchar(255),
  mdm_source_system varchar(100),
  mdm_is_deleted    boolean      NOT NULL DEFAULT false,
  mdm_staging_id    bigint,
{body}
)""",
        f"CREATE INDEX IF NOT EXISTS {quote_ident('ix_' + entity_name + '_hist_record')} "
        f"ON {qualified(settings.SCHEMA_HISTORY, entity_name)} (mdm_id, mdm_version DESC)",
    ]


def build_create_plan(entity) -> DDLPlan:
    """Full four-tier CREATE plan for a new entity."""
    name = validate_ident(entity.name, kind="entity name")
    cols = columns_from_entity(entity)
    plan = DDLPlan()
    for stmt in (
        build_landing_ddl(name, cols)
        + build_staging_ddl(name, cols)
        + build_live_ddl(name, cols, kind=getattr(entity, "kind", "master"))
        + build_history_ddl(name, cols)
    ):
        plan.add(stmt)
    if not entity.match_keys:
        plan.warnings.append(
            "No match key defined — duplicate detection against golden records "
            "will be skipped for this entity."
        )
    if not entity.business_key:
        plan.warnings.append(
            "No business key defined — updates must reference mdm_id explicitly."
        )
    return plan


# -------------------------------------------------------------- introspection
def reflect_columns(conn: Connection, schema: str, table: str) -> Dict[str, Dict]:
    rows = conn.execute(
        text(
            """
            select column_name, data_type, character_maximum_length,
                   numeric_precision, numeric_scale, is_nullable, column_default
            from information_schema.columns
            where table_schema = :s and table_name = :t
            order by ordinal_position
            """
        ),
        {"s": schema, "t": table},
    ).all()
    return {
        r[0]: {
            "data_type": r[1],
            "length": r[2],
            "precision": r[3],
            "scale": r[4],
            "nullable": r[5] == "YES",
            "default": r[6],
        }
        for r in rows
    }


def table_exists(conn: Connection, schema: str, table: str) -> bool:
    return bool(
        conn.execute(
            text(
                "select 1 from information_schema.tables "
                "where table_schema=:s and table_name=:t"
            ),
            {"s": schema, "t": table},
        ).scalar()
    )


# ---------------------------------------------------- constraint naming
def _stable_suffix(key: str, n: int = 8) -> str:
    """A short, process-stable hex digest of ``key``.

    Uses hashlib (not Python's per-process salted ``hash()``) so the same key
    yields the same suffix across runs — essential for looking a constraint
    back up by name after it was created in an earlier publish.
    """
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:n]


def constraint_name(prefix: str, entity_name: str, col_name: str) -> str:
    """Deterministic, collision-safe constraint/index identifier (<= 63 chars).

    A readable ``{prefix}_{entity}_{col}`` stem plus a short stable hash of the
    full ``{entity}_{col}`` key. Truncating the stem alone can collide when two
    long identifiers share a 63-char prefix, silently dropping one constraint;
    the hash suffix keeps distinct inputs distinct. Reconciliation recomputes
    the same name to find an existing constraint.
    """
    key = f"{entity_name}_{col_name}"
    suffix = "_" + _stable_suffix(key)
    stem = f"{prefix}_{key}"
    return stem[: 63 - len(suffix)] + suffix


def _fk_constraint_name(entity_name: str, col_name: str) -> str:
    """Deterministic, collision-safe FK name. Inputs are pre-validated idents."""
    return constraint_name("fk", entity_name, col_name)


def _enum_check_name(entity_name: str, col_name: str) -> str:
    """Deterministic, collision-safe enum CHECK name (live tier only)."""
    return constraint_name("cke", entity_name, col_name)


def build_reference_fk_statements(entity_name: str, cols: Sequence[ColumnDef]) -> List[str]:
    """FOREIGN KEY statements for an entity's reference attributes (DM-2/DM-3).

    Foreign keys are a golden-tier construct only: they reference the parent's
    live ``mdm_id`` with ON DELETE RESTRICT. Emitted as separate ALTER TABLE
    statements because the parent table may not exist when this entity is first
    published — see ``reconcile_reference_constraints`` for resilient wiring.
    """
    name = validate_ident(entity_name, kind="entity name")
    live = qualified(settings.SCHEMA_LIVE, name)
    stmts: List[str] = []
    for col in cols:
        if not col.is_reference or not col.ref_entity:
            continue
        ref = validate_ident(col.ref_entity, kind="ref_entity")
        parent = qualified(settings.SCHEMA_LIVE, ref)
        cname = _fk_constraint_name(name, col.name)
        stmts.append(
            f"ALTER TABLE {live} ADD CONSTRAINT {quote_ident(cname)} "
            f"FOREIGN KEY ({quote_ident(col.name)}) REFERENCES {parent} (mdm_id) "
            f"ON DELETE RESTRICT"
        )
    return stmts


def _constraint_exists(conn: Connection, schema: str, table: str, conname: str) -> bool:
    return bool(
        conn.execute(
            text(
                "select 1 from pg_constraint c "
                "join pg_class t on t.oid = c.conrelid "
                "join pg_namespace n on n.oid = t.relnamespace "
                "where n.nspname = :s and t.relname = :t and c.conname = :c"
            ),
            {"s": schema, "t": table, "c": conname},
        ).scalar()
    )


def _fk_target_table(
    conn: Connection, schema: str, table: str, conname: str
) -> Optional[str]:
    """The table a named foreign key currently references (its ``relname``).

    Used to detect a re-pointed reference: if the live FK still targets the old
    parent after ``ref_entity`` changed, it must be dropped and re-added.
    """
    return conn.execute(
        text(
            "select cl.relname from pg_constraint c "
            "join pg_class t on t.oid = c.conrelid "
            "join pg_namespace n on n.oid = t.relnamespace "
            "join pg_class cl on cl.oid = c.confrelid "
            "where n.nspname = :s and t.relname = :t and c.conname = :c "
            "and c.contype = 'f'"
        ),
        {"s": schema, "t": table, "c": conname},
    ).scalar()


def _enum_checks_by_column(
    conn: Connection, schema: str, table: str
) -> Dict[str, List[str]]:
    """Map each business column to the names of single-column CHECK constraints
    on it. The platform only ever generates enum allow-list CHECKs on business
    columns, so these are exactly the enum CHECKs to reconcile (system ``mdm_``
    columns are excluded)."""
    rows = conn.execute(
        text(
            "select c.conname, a.attname from pg_constraint c "
            "join pg_class t on t.oid = c.conrelid "
            "join pg_namespace n on n.oid = t.relnamespace "
            "join pg_attribute a on a.attrelid = t.oid and a.attnum = any(c.conkey) "
            "where n.nspname = :s and t.relname = :t and c.contype = 'c' "
            "and cardinality(c.conkey) = 1"
        ),
        {"s": schema, "t": table},
    ).all()
    out: Dict[str, List[str]] = {}
    for conname, attname in rows:
        if attname.startswith("mdm_"):
            continue
        out.setdefault(attname, []).append(conname)
    return out


def _add_fk_resilient(
    conn: Connection, child_name: str, col: ColumnDef, added: List[str],
    warnings: List[str],
) -> None:
    """Attempt to add one FK, isolating failure in a savepoint.

    A publish must never be aborted by a reference constraint: a missing parent
    table or existing data that violates the constraint becomes a warning, not
    a hard error.
    """
    live_schema = settings.SCHEMA_LIVE
    ref = validate_ident(col.ref_entity, kind="ref_entity")
    if not table_exists(conn, live_schema, ref):
        warnings.append(
            f"{child_name}.{col.name}: parent table {live_schema}.{ref} does not "
            "exist yet — FK deferred until it is published."
        )
        return
    cname = _fk_constraint_name(child_name, col.name)
    if _constraint_exists(conn, live_schema, child_name, cname):
        current_target = _fk_target_table(conn, live_schema, child_name, cname)
        if current_target == ref:
            return  # already points at the right parent
        # Re-pointed reference: the FK still targets the old parent. Drop the
        # stale constraint (in a savepoint) so it can be re-added below.
        child_live = qualified(live_schema, child_name)
        try:
            with conn.begin_nested():
                conn.execute(
                    text(
                        f"ALTER TABLE {child_live} "
                        f"DROP CONSTRAINT {quote_ident(cname)}"
                    )
                )
        except Exception as exc:  # noqa: BLE001 — best effort
            warnings.append(
                f"{child_name}.{col.name}: could not drop stale FK targeting "
                f"'{current_target}' ({exc.__class__.__name__})."
            )
            return
    stmt = build_reference_fk_statements(child_name, [col])
    if not stmt:
        return
    try:
        with conn.begin_nested():
            conn.execute(text(stmt[0]))
        added.append(cname)
    except Exception as exc:  # noqa: BLE001 — never fail a publish on this
        warnings.append(
            f"{child_name}.{col.name}: could not add FK to {live_schema}.{ref} "
            f"({exc.__class__.__name__}). Existing data may violate it."
        )


def reconcile_reference_constraints(conn: Connection, entity, all_entities) -> Dict:
    """Wire up reference foreign keys around a just-published entity.

    Because a child may be published before its parent, FK creation is deferred
    to publish time and reconciled from both directions:
      (a) outbound — this entity's reference attrs whose parent now exists;
      (b) inbound — already-published children that reference THIS entity, now
          that its table exists.
    Missing targets and constraint-violating data degrade to warnings.
    """
    added: List[str] = []
    warnings: List[str] = []
    live_schema = settings.SCHEMA_LIVE
    name = validate_ident(entity.name, kind="entity name")

    # (a) outbound
    if table_exists(conn, live_schema, name):
        for col in columns_from_entity(entity):
            if col.is_reference and col.ref_entity:
                _add_fk_resilient(conn, name, col, added, warnings)

    # (b) inbound — other deployed entities that point here
    for child in all_entities:
        if child.name == entity.name or not getattr(child, "is_deployed", False):
            continue
        try:
            cname = validate_ident(child.name, kind="entity name")
            if not table_exists(conn, live_schema, cname):
                continue
            child_cols = columns_from_entity(child)
        except IdentifierError:
            continue
        for col in child_cols:
            if col.is_reference and col.ref_entity:
                try:
                    ref = validate_ident(col.ref_entity, kind="ref_entity")
                except IdentifierError:
                    continue
                if ref == name:
                    _add_fk_resilient(conn, cname, col, added, warnings)

    return {"added": added, "warnings": warnings}


def reconcile_enum_constraints(conn: Connection, entity) -> Dict:
    """Reconcile enum allow-list CHECK constraints on the LIVE tier (DM-4).

    A fresh publish emits the enum CHECK as part of the live DDL, but an
    operator who later adds, edits or removes ``validation.enum`` on an
    already-published column must have the live CHECK brought back in line.
    For each business column this drops any existing single-column CHECK and,
    when the metadata still defines an allow-list, re-adds it under the
    deterministic name — or leaves it dropped when the enum was removed.

    Best-effort like FK reconciliation: a CHECK that existing data would
    violate degrades to a warning inside a savepoint and never aborts publish.
    """
    changed: List[str] = []
    warnings: List[str] = []
    live_schema = settings.SCHEMA_LIVE
    name = validate_ident(entity.name, kind="entity name")
    if not table_exists(conn, live_schema, name):
        return {"changed": changed, "warnings": warnings}

    live = qualified(live_schema, name)
    existing = _enum_checks_by_column(conn, live_schema, name)

    for col in columns_from_entity(entity):
        current_names = existing.get(col.name, [])
        desired = bool(col.enum_values)
        if not desired and not current_names:
            continue  # no enum, nothing on the column — nothing to do
        cname = _enum_check_name(name, col.name)
        try:
            with conn.begin_nested():
                for existing_name in current_names:
                    conn.execute(
                        text(
                            f"ALTER TABLE {live} "
                            f"DROP CONSTRAINT {quote_ident(existing_name)}"
                        )
                    )
                if desired:
                    conn.execute(text(_enum_check_add_sql(name, col)))
            changed.append(cname if desired else f"-{col.name}")
        except Exception as exc:  # noqa: BLE001 — never fail a publish on this
            warnings.append(
                f"{name}.{col.name}: could not reconcile enum CHECK "
                f"({exc.__class__.__name__}). Existing data may violate it."
            )

    return {"changed": changed, "warnings": warnings}


# ------------------------------------------------ downstream distribution (DD-1)
# The columns exposed from the live golden tier on every distribution matview,
# in addition to the entity's business columns.
MATVIEW_SYSTEM_COLUMNS = (
    "mdm_id",
    "mdm_version",
    "mdm_source_system",
    "mdm_created_at",
    "mdm_updated_at",
)


def matview_index_name(entity_name: str) -> str:
    """Deterministic (<=63 char) name for a matview's UNIQUE index on mdm_id."""
    return constraint_name("uqm", entity_name, "mdm_id")


def build_matview_sql(entity_name: str, cols: Sequence[ColumnDef]) -> str:
    """CREATE MATERIALIZED VIEW mdm_pub.<entity> (DD-1).

    Selects the business columns plus a fixed set of golden-tier system columns
    from the live table, restricted to active (non-soft-deleted) golden records.
    Created WITH DATA so it is immediately queryable and eligible for a
    subsequent ``REFRESH ... CONCURRENTLY`` (which requires prior population).
    """
    name = validate_ident(entity_name, kind="entity name")
    mv = qualified(settings.SCHEMA_PUBLISH, name)
    live = qualified(settings.SCHEMA_LIVE, name)
    select_cols = ", ".join(
        [quote_ident(c.name) for c in cols] + list(MATVIEW_SYSTEM_COLUMNS)
    )
    return (
        f"CREATE MATERIALIZED VIEW {mv} AS\n"
        f"SELECT {select_cols}\n"
        f"FROM {live}\n"
        f"WHERE mdm_is_deleted = false\n"
        f"WITH DATA"
    )


def build_matview_index_sql(entity_name: str) -> str:
    """UNIQUE index on mdm_id — required for ``REFRESH ... CONCURRENTLY``."""
    name = validate_ident(entity_name, kind="entity name")
    mv = qualified(settings.SCHEMA_PUBLISH, name)
    return (
        f"CREATE UNIQUE INDEX {quote_ident(matview_index_name(name))} "
        f"ON {mv} (mdm_id)"
    )


def drop_matview_sql(entity_name: str, *, cascade: bool = False) -> str:
    name = validate_ident(entity_name, kind="entity name")
    mv = qualified(settings.SCHEMA_PUBLISH, name)
    return f"DROP MATERIALIZED VIEW IF EXISTS {mv}" + (" CASCADE" if cascade else "")


def matview_grant_sql(entity_name: str) -> List[str]:
    """GRANT SELECT on the matview to the runtime role.

    Only meaningful when a distinct, least-privilege runtime role exists (the
    DDL role owns the object). ``GRANT ON ALL TABLES`` / default privileges do
    not reliably cover materialized views, so this is granted explicitly.
    """
    if not (settings.PG_DDL_USER and settings.PGUSER != settings.PG_DDL_USER):
        return []
    name = validate_ident(entity_name, kind="entity name")
    mv = qualified(settings.SCHEMA_PUBLISH, name)
    return [f"GRANT SELECT ON {mv} TO {quote_ident(settings.PGUSER)}"]


def reconcile_matview(conn: Connection, entity) -> Dict:
    """(Re)create the distribution materialized view for an entity (DD-1).

    A matview's column list is fixed at creation, so a re-publish with a changed
    column set must DROP + CREATE it (and its unique index). Best-effort, like FK
    and enum reconciliation: any failure is isolated in a savepoint and degraded
    to a warning so it can never abort a publish.
    """
    created: List[str] = []
    warnings: List[str] = []
    name = validate_ident(entity.name, kind="entity name")
    if not table_exists(conn, settings.SCHEMA_LIVE, name):
        warnings.append(
            f"{name}: live table {settings.SCHEMA_LIVE}.{name} does not exist — "
            "distribution matview skipped."
        )
        return {"created": created, "warnings": warnings}
    cols = columns_from_entity(entity)
    try:
        with conn.begin_nested():
            conn.execute(text(drop_matview_sql(name, cascade=True)))
            conn.execute(text(build_matview_sql(name, cols)))
            conn.execute(text(build_matview_index_sql(name)))
            for stmt in matview_grant_sql(name):
                conn.execute(text(stmt))
        created.append(f"{settings.SCHEMA_PUBLISH}.{name}")
    except Exception as exc:  # noqa: BLE001 — never fail a publish on this
        warnings.append(
            f"{name}: could not (re)create distribution matview "
            f"({exc.__class__.__name__}: {exc})."
        )
    return {"created": created, "warnings": warnings}


def build_alter_plan(conn: Connection, entity) -> DDLPlan:
    """Diff the model against deployed reality and produce a migration.

    Additive changes (new columns, safe widenings) are applied automatically.
    Drops and narrowings are classified destructive and require explicit
    confirmation.
    """
    name = validate_ident(entity.name, kind="entity name")
    cols = columns_from_entity(entity)
    plan = DDLPlan()
    by_name = {c.name: c for c in cols}

    for tier in TIERS:
        schema = SCHEMA_FOR[tier]
        if not table_exists(conn, schema, name):
            # Tier missing entirely — create it fresh.
            if tier == LIVE:
                fresh = build_live_ddl(
                    name, cols, kind=getattr(entity, "kind", "master")
                )
            else:
                fresh = {
                    LANDING: build_landing_ddl,
                    STAGING: build_staging_ddl,
                    HISTORY: build_history_ddl,
                }[tier](name, cols)
            for stmt in fresh:
                plan.add(stmt)
            continue

        if tier == LANDING:
            continue  # landing is schemaless (jsonb payload) — nothing to migrate

        existing = reflect_columns(conn, schema, name)
        t = qualified(schema, name)
        enforce_nn = tier == LIVE

        # --- additions
        for col in cols:
            if col.name in existing:
                continue
            frag = f"ADD COLUMN {quote_ident(col.name)} {col.sql_type()}"
            if enforce_nn and col.required:
                if col.default:
                    frag += f" NOT NULL DEFAULT {_render_default(col)}"
                else:
                    plan.warnings.append(
                        f"{tier}.{name}.{col.name}: added as NULLABLE — a required "
                        "column cannot be added to a populated table without a "
                        "default. Backfill, then tighten the constraint."
                    )
            elif col.default:
                frag += f" DEFAULT {_render_default(col)}"
            # Enum allow-list CHECKs (DM-4) are reconciled separately on the live
            # tier by reconcile_enum_constraints — never emitted inline here, so
            # a re-publish that changes the allow-list can drop and re-add them.
            plan.add(f"ALTER TABLE {t} {frag}")

        # --- type changes
        for col in cols:
            cur = existing.get(col.name)
            if not cur:
                continue
            old_logical = _logical_from_pg(cur)
            if old_logical is None:
                continue
            # Compare the *rendered* Postgres types, not the logical names —
            # varchar(255) and varchar(500) share a logical type but are not the
            # same column, and treating them as equal silently skips both safe
            # widenings and unsafe narrowings.
            desired = pg_type(col.data_type, col.length, col.precision, col.scale)
            if desired == _render_pg(cur):
                continue
            safe = is_safe_widening(
                old_logical, cur["length"], col.data_type, col.length
            )
            stmt = (
                f"ALTER TABLE {t} ALTER COLUMN {quote_ident(col.name)} "
                f"TYPE {col.sql_type()}"
            )
            if safe:
                plan.add(stmt)
            else:
                plan.destructive.append(
                    f"{tier}.{name}.{col.name}: {_render_pg(cur)} -> "
                    f"{col.sql_type()} may truncate or fail. SQL: {stmt} USING "
                    f"{quote_ident(col.name)}::{col.sql_type()}"
                )

        # --- removals
        for existing_name in existing:
            if existing_name.startswith("mdm_") or existing_name in by_name:
                continue
            plan.destructive.append(
                f"{tier}.{name}.{existing_name}: column no longer in the model. "
                f"SQL: ALTER TABLE {t} DROP COLUMN {quote_ident(existing_name)}"
            )

        # --- indexes for newly flagged columns
        if tier == LIVE:
            for col in cols:
                if col.unique:
                    plan.add(
                        f"CREATE UNIQUE INDEX IF NOT EXISTS "
                        f"{quote_ident('uq_' + name + '_' + col.name)} ON {t} "
                        f"({quote_ident(col.name)}) WHERE mdm_is_deleted = false"
                    )
                elif col.indexed:
                    plan.add(
                        f"CREATE INDEX IF NOT EXISTS "
                        f"{quote_ident('ix_' + name + '_' + col.name)} ON {t} "
                        f"({quote_ident(col.name)})"
                    )
            # Composite uniqueness for an association's reference pair (DM-3).
            if (getattr(entity, "kind", "master") or "master").lower() == "association":
                ref_cols = _association_ref_cols(cols)
                if len(ref_cols) >= 2:
                    plan.add(_association_unique_index_sql(name, ref_cols))
    return plan


_PG_TO_LOGICAL = {
    "character varying": "string",
    "text": "text",
    "integer": "integer",
    "bigint": "bigint",
    "numeric": "decimal",
    "double precision": "float",
    "boolean": "boolean",
    "date": "date",
    "timestamp with time zone": "timestamp",
    "uuid": "uuid",
    "jsonb": "json",
}


def _logical_from_pg(meta: Dict) -> Optional[str]:
    return _PG_TO_LOGICAL.get(meta["data_type"])


def _render_pg(meta: Dict) -> str:
    dt = meta["data_type"]
    if dt == "character varying" and meta["length"]:
        return f"varchar({meta['length']})"
    if dt == "character varying":
        return "varchar"
    if dt == "numeric" and meta["precision"]:
        return f"numeric({meta['precision']},{meta['scale'] or 0})"
    return {"timestamp with time zone": "timestamptz"}.get(dt, dt)


def build_drop_plan(entity_name: str, *, cascade: bool = False) -> DDLPlan:
    name = validate_ident(entity_name, kind="entity name")
    plan = DDLPlan()
    suffix = " CASCADE" if cascade else ""
    # Drop the distribution matview first — it depends on the live table (DD-1).
    plan.add(drop_matview_sql(name, cascade=cascade))
    plan.destructive.append(
        f"{settings.SCHEMA_PUBLISH}.{name}: distribution matview will be dropped."
    )
    for tier in (HISTORY, LIVE, STAGING, LANDING):
        stmt = f"DROP TABLE IF EXISTS {qualified(SCHEMA_FOR[tier], name)}{suffix}"
        plan.add(stmt)
        plan.destructive.append(f"{tier}.{name}: table and all data will be dropped.")
    return plan


def apply_plan(conn: Connection, plan: DDLPlan, *, allow_destructive: bool = False) -> Dict:
    """Execute a plan inside the caller's transaction."""
    if plan.is_destructive and not allow_destructive:
        raise PermissionError(
            "Plan contains destructive changes. Re-issue with explicit "
            f"confirmation. Items: {plan.destructive}"
        )
    executed = []
    for stmt in plan.statements:
        conn.execute(text(stmt))
        executed.append(stmt)
    return {"executed": len(executed), "statements": executed}
