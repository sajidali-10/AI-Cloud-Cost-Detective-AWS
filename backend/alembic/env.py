"""Alembic environment — Phase 2.

This file is invoked by Alembic for every migration run.  It pulls
the SQLAlchemy URL from :func:`app.core.config.get_settings` so the
DSN is never duplicated in ``alembic.ini``.

The backend container's entrypoint cwd is ``/app`` (set by the
Dockerfile ``WORKDIR``), so the ``app`` package is importable here.
"""
from __future__ import annotations

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Make sure ``app`` is importable.  ``prepend_sys_path = .`` in
# alembic.ini already adds the directory containing alembic.ini
# (/app inside the container) to sys.path, so this is a belt-and-
# braces guard for ad-hoc local invocations.
_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

from app.core.config import get_settings  # noqa: E402

config = context.config

# Override sqlalchemy.url from settings — never trust the placeholder
# in alembic.ini.
config.set_main_option("sqlalchemy.url", get_settings().database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Phase 2 owns exactly one ORM (CostCache).  We deliberately do NOT
# point Alembic at the ORM metadata for autogenerate yet — every
# migration is reviewed and hand-written, so the autogenerate
# surface is intentionally null.
target_metadata = None


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL to stdout)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (connect to the DB)."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
