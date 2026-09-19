"""Alembic environment.

Runs synchronously. The application uses an async engine, but migrations are a
one-at-a-time batch job with nothing to overlap, and psycopg 3 serves both from
the same `postgresql+psycopg://` URL - `create_engine` gives the sync flavour,
`create_async_engine` the async one. Keeping migrations sync avoids the async
event-loop boilerplate for no loss.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from app.core.config import get_settings
from app.db.base import Base

# Importing the models package is what registers the tables on Base.metadata.
# Without it autogenerate would cheerfully produce an empty migration.
import app.models  # noqa: F401  isort:skip

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ALEMBIC_DATABASE_URL lets the test suite point migrations at the test database
# without editing configuration.
database_url = os.environ.get("ALEMBIC_DATABASE_URL") or get_settings().database_url
config.set_main_option("sqlalchemy.url", database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
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
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
