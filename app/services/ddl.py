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

    def sql_type(self) -> str:
        return pg_type(self.data_type, self.length, self.precision, self.scale)


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
            )
        )
    if not cols:
        raise IdentifierError("Entity must define at least one attribute.")
    return cols


# ------------------------------------------------------------ CREATE builders
def _business_column_sql(col: ColumnDef, *, enforce_not_null: bool) -> str:
    parts = [f"  {quote_ident(col.name)} {col.sql_type()}"]
    if enforce_not_null and col.required:
        parts.append("NOT NULL")
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


def build_live_ddl(entity_name: str, cols: Sequence[ColumnDef]) -> List[str]:
    """Live carries the real constraints — this is the golden record."""
    t = qualified(settings.SCHEMA_LIVE, entity_name)
    body = ",\n".join(_business_column_sql(c, enforce_not_null=True) for c in cols)
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
        + build_live_ddl(name, cols)
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
            builder = {
                LANDING: build_landing_ddl,
                STAGING: build_staging_ddl,
                LIVE: build_live_ddl,
                HISTORY: build_history_ddl,
            }[tier]
            for stmt in builder(name, cols):
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
