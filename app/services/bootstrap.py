"""Cluster provisioning: databases, schemas, grants and metadata tables.

Everything here uses the elevated DDL connection and is designed to be
idempotent — running it twice is a no-op.
"""
import logging
from typing import Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError

from app.config import settings
from app.db import Base, get_ddl_engine, get_engine, get_maintenance_engine
from app.services.identifiers import qualified, quote_ident

log = logging.getLogger(__name__)

# Roles that need to exist for least-privilege operation.
REQUIRED_PRIVILEGES = ("CREATE", "USAGE")


def database_exists(name: str) -> bool:
    with get_maintenance_engine().connect() as conn:
        return bool(
            conn.execute(
                text("select 1 from pg_database where datname = :n"), {"n": name}
            ).scalar()
        )


def create_database(name: str, owner: Optional[str] = None) -> Dict:
    """CREATE DATABASE — must run outside a transaction, hence AUTOCOMMIT.

    Requires the CREATEDB privilege; we report a precise, actionable error
    rather than surfacing a raw driver exception.
    """
    if database_exists(name):
        return {"created": False, "reason": "already_exists", "database": name}

    stmt = f"CREATE DATABASE {quote_ident(name)}"
    if owner:
        stmt += f" OWNER {quote_ident(owner)}"
    try:
        with get_maintenance_engine().connect() as conn:
            conn.execute(text(stmt))
    except ProgrammingError as exc:
        if "permission denied" in str(exc).lower():
            user = settings.PG_DDL_USER or settings.PGUSER
            raise PermissionError(
                f"Role '{user}' lacks CREATEDB. Grant it with: "
                f"ALTER ROLE {quote_ident(user)} CREATEDB;"
            ) from exc
        raise
    return {"created": True, "database": name, "sql": stmt}


def check_privileges() -> Dict:
    """Preflight: verify the DDL role can actually do what we need.

    Returns a structured report so the UI can tell an operator exactly which
    GRANT is missing instead of failing later mid-publish.
    """
    report: Dict = {"ok": True, "checks": [], "database": settings.PGDATABASE}
    try:
        with get_ddl_engine().connect() as conn:
            ddl_user = conn.execute(text("select current_user")).scalar()
            report["ddl_user"] = ddl_user

            can_create_db = conn.execute(
                text("select rolcreatedb from pg_roles where rolname = current_user")
            ).scalar()
            report["checks"].append(
                {
                    "name": "createdb",
                    "ok": bool(can_create_db),
                    "detail": "Required only to provision new databases.",
                    "remedy": None
                    if can_create_db
                    else f"ALTER ROLE {quote_ident(str(ddl_user))} CREATEDB;",
                }
            )

            can_create_schema = conn.execute(
                text("select has_database_privilege(current_user, :db, 'CREATE')"),
                {"db": settings.PGDATABASE},
            ).scalar()
            ok = bool(can_create_schema)
            report["checks"].append(
                {
                    "name": "create_schema",
                    "ok": ok,
                    "detail": "Required to create MDM schemas and tables.",
                    "remedy": None
                    if ok
                    else f"GRANT CREATE ON DATABASE {quote_ident(settings.PGDATABASE)} "
                    f"TO {quote_ident(str(ddl_user))};",
                }
            )
            if not ok:
                report["ok"] = False

            for schema in settings.all_schemas:
                exists = conn.execute(
                    text("select 1 from information_schema.schemata where schema_name=:s"),
                    {"s": schema},
                ).scalar()
                report["checks"].append(
                    {"name": f"schema:{schema}", "ok": bool(exists),
                     "detail": "present" if exists else "will be created"}
                )
    except SQLAlchemyError as exc:
        report["ok"] = False
        report["error"] = str(exc)
    return report


def create_schemas(schemas: Optional[List[str]] = None) -> List[str]:
    """Create the MDM schemas and grant the runtime role least-privilege DML."""
    schemas = schemas or settings.all_schemas
    runtime_user = settings.PGUSER
    created: List[str] = []
    with get_ddl_engine().begin() as conn:
        for schema in schemas:
            q = quote_ident(schema)
            conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {q}"))
            created.append(schema)
            if settings.PG_DDL_USER and runtime_user != settings.PG_DDL_USER:
                ru = quote_ident(runtime_user)
                conn.execute(text(f"GRANT USAGE ON SCHEMA {q} TO {ru}"))
                conn.execute(
                    text(
                        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES "
                        f"IN SCHEMA {q} TO {ru}"
                    )
                )
                conn.execute(
                    text(
                        f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {q} TO {ru}"
                    )
                )
                # Ensure future generated tables are reachable too.
                conn.execute(
                    text(
                        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {q} "
                        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {ru}"
                    )
                )
                conn.execute(
                    text(
                        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {q} "
                        f"GRANT USAGE, SELECT ON SEQUENCES TO {ru}"
                    )
                )
    return created


def create_metadata_tables() -> List[str]:
    """Create the fixed internal tables via SQLAlchemy metadata."""
    import app.models  # noqa: F401  (registers mappers)

    engine = get_ddl_engine()
    Base.metadata.create_all(bind=engine)
    # create_all() never alters an existing table, so additive columns on the
    # fixed metadata tables must be applied explicitly and idempotently for
    # databases provisioned before the column existed.
    with engine.begin() as conn:
        entity_t = qualified(settings.SCHEMA_META, "entity")
        conn.execute(
            text(
                f"ALTER TABLE {entity_t} ADD COLUMN IF NOT EXISTS "
                "kind varchar(20) NOT NULL DEFAULT 'master'"
            )
        )
        # Workstream 2 additive columns. create_all() never alters an existing
        # table, so databases provisioned before these columns existed need the
        # explicit, idempotent ALTERs below.
        user_t = qualified(settings.SCHEMA_META, "app_user")
        conn.execute(
            text(
                f"ALTER TABLE {user_t} ADD COLUMN IF NOT EXISTS "
                "domain_permissions jsonb NOT NULL DEFAULT '{}'::jsonb"
            )
        )
        conn.execute(
            text(
                f"ALTER TABLE {user_t} ADD COLUMN IF NOT EXISTS "
                "domain_roles jsonb NOT NULL DEFAULT '{}'::jsonb"
            )
        )
        apikey_t = qualified(settings.SCHEMA_META, "api_key")
        conn.execute(
            text(
                f"ALTER TABLE {apikey_t} ADD COLUMN IF NOT EXISTS "
                "elevated boolean NOT NULL DEFAULT false"
            )
        )
        conn.execute(
            text(
                f"ALTER TABLE {apikey_t} ADD COLUMN IF NOT EXISTS "
                "allowed_domains jsonb NOT NULL DEFAULT '[]'::jsonb"
            )
        )
        grp_t = qualified(settings.SCHEMA_META, "group_role_mapping")
        conn.execute(
            text(
                f"ALTER TABLE {grp_t} ADD COLUMN IF NOT EXISTS domain varchar(63)"
            )
        )
        # Workstream 6 (EX-2) additive column on the attribute metadata table.
        # create_all() creates the field_mapping table for fresh installs but
        # never alters this existing table, so add the column idempotently.
        attr_t = qualified(settings.SCHEMA_META, "attribute")
        conn.execute(
            text(
                f"ALTER TABLE {attr_t} ADD COLUMN IF NOT EXISTS "
                "transforms jsonb NOT NULL DEFAULT '[]'::jsonb"
            )
        )
        # Seed the built-in 'default' domain so every deployment has one to fall
        # back on. Idempotent via the unique constraint on domain.name.
        domain_t = qualified(settings.SCHEMA_META, "domain")
        conn.execute(
            text(
                f"INSERT INTO {domain_t} "
                "(id, name, display_name, description, requires_approval, "
                " default_soft_delete) "
                "VALUES (gen_random_uuid(), 'default', 'Default', "
                "'Built-in default governance domain.', true, true) "
                "ON CONFLICT (name) DO NOTHING"
            )
        )
    # AO-2: make the audit and workflow-event history append-only at the DATABASE
    # level. A BEFORE UPDATE OR DELETE trigger raises, so even the application
    # cannot rewrite the decision chain. Idempotent: the function is CREATE OR
    # REPLACE and each trigger is dropped-if-exists then recreated.
    install_append_only_triggers(engine)

    if settings.PG_DDL_USER and settings.PGUSER != settings.PG_DDL_USER:
        with engine.begin() as conn:
            q = quote_ident(settings.SCHEMA_META)
            ru = quote_ident(settings.PGUSER)
            conn.execute(
                text(
                    f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES "
                    f"IN SCHEMA {q} TO {ru}"
                )
            )
    return sorted(Base.metadata.tables)


APPEND_ONLY_TABLES = ("audit_event", "workflow_event")


def install_append_only_triggers(engine=None) -> None:
    """Install (idempotently) the BEFORE UPDATE OR DELETE guard on history tables.

    Any UPDATE or DELETE against ``mdm_meta.audit_event`` or
    ``mdm_meta.workflow_event`` raises, enforcing immutability in the database
    rather than by convention (AO-2).
    """
    engine = engine or get_ddl_engine()
    schema = quote_ident(settings.SCHEMA_META)
    fn = f"{schema}.mdm_forbid_mutation"
    with engine.begin() as conn:
        conn.execute(
            text(
                f"""
                CREATE OR REPLACE FUNCTION {fn}() RETURNS trigger AS $mdm$
                BEGIN
                    RAISE EXCEPTION
                        'append-only: % on %.% is not permitted (immutable audit history)',
                        TG_OP, TG_TABLE_SCHEMA, TG_TABLE_NAME;
                END;
                $mdm$ LANGUAGE plpgsql
                """
            )
        )
        for table in APPEND_ONLY_TABLES:
            t = qualified(settings.SCHEMA_META, table)
            trg = quote_ident(f"trg_{table}_append_only")
            conn.execute(text(f"DROP TRIGGER IF EXISTS {trg} ON {t}"))
            conn.execute(
                text(
                    f"CREATE TRIGGER {trg} BEFORE UPDATE OR DELETE ON {t} "
                    f"FOR EACH ROW EXECUTE FUNCTION {fn}()"
                )
            )


def bootstrap(create_db: bool = False) -> Dict:
    """Full idempotent provisioning run."""
    result: Dict = {}
    if create_db:
        result["database"] = create_database(
            settings.PGDATABASE, owner=settings.PG_DDL_USER or settings.PGUSER
        )
    result["schemas"] = create_schemas()
    result["metadata_tables"] = create_metadata_tables()
    result["privileges"] = check_privileges()
    return result
