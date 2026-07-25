"""Alembic environment.

Internal metadata tables are normally created with SQLAlchemy's create_all
(additive and idempotent). Alembic is wired up here for the case where a future
release needs a genuine migration. Generated master-data tables are NOT managed
by Alembic — they are created and altered by the DDL engine when a model is
published.
"""
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config import settings
from app.db import Base
import app.models  # noqa: F401  (registers the mappers)

config = context.config
config.set_main_option("sqlalchemy.url", settings.ddl_dsn)
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata
MANAGED_SCHEMA = settings.SCHEMA_META


def include_object(obj, name, type_, reflected, compare_to):
    """Only ever touch the internal metadata schema."""
    if type_ == "table":
        return getattr(obj, "schema", None) == MANAGED_SCHEMA
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        include_schemas=True,
        include_object=include_object,
        version_table_schema=MANAGED_SCHEMA,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_schemas=True,
            include_object=include_object,
            version_table_schema=MANAGED_SCHEMA,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
