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
from app.services.identifiers import quote_ident

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
