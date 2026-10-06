"""Tests for the Phase 5A user model + migration DDL — Phase 5A."""
from __future__ import annotations

from sqlalchemy import create_engine, inspect, text

from app.db.models import APP_USER_ROLES, AppUser, Base


def test_app_user_table_exists_and_has_expected_columns() -> None:
    eng = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(eng)
    inspector = inspect(eng)
    assert "app_users" in inspector.get_table_names()
    cols = {c["name"]: c for c in inspector.get_columns("app_users")}
    for name in (
        "id",
        "email",
        "password_hash",
        "display_name",
        "role",
        "is_active",
        "created_at",
        "updated_at",
        "last_login_at",
    ):
        assert name in cols, f"missing column {name}"


def test_email_is_unique() -> None:
    eng = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(eng)
    uniq = {tuple(c["column_names"]) for c in inspector_uniques(eng, "app_users")}
    assert ("email",) in uniq


def test_role_indexed() -> None:
    eng = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(eng)
    indexes = {i["name"] for i in inspector_indexes(eng, "app_users")}
    assert any("role" in i for i in indexes)


def test_role_check_constraint_present_in_sqlite_metadata() -> None:
    """The role CHECK constraint lives in the SQLAlchemy metadata."""
    table = AppUser.__table__
    check_constraints = [c.name for c in table.constraints if hasattr(c, "name") and c.name]
    assert "ck_app_users_role" in check_constraints


def test_app_user_repr_never_includes_password_hash() -> None:
    user = AppUser(
        id=1,
        email="x@example.com",
        password_hash="$argon2id$v=19$m=8,t=1,p=1$AAAA$BBBB",
        display_name="X",
        role="VIEWER",
        is_active=True,
    )
    assert "$argon2id$" not in repr(user)
    assert "BBBB" not in repr(user)


def test_app_user_roles_constant() -> None:
    assert APP_USER_ROLES == ("ADMIN", "ANALYST", "VIEWER")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def inspector_uniques(eng, table_name):
    from sqlalchemy import inspect

    return inspect(eng).get_unique_constraints(table_name)


def inspector_indexes(eng, table_name):
    from sqlalchemy import inspect

    return inspect(eng).get_indexes(table_name)


# ---------------------------------------------------------------------------
# Migration SQL smoke check (pure string inspection; no Postgres needed)
# ---------------------------------------------------------------------------


def test_migration_file_declares_expected_revision() -> None:
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0002_app_users.py"
    spec = importlib.util.spec_from_file_location("mig_0002", str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    assert mod.revision == "0002_app_users"
    assert mod.down_revision == "0001_cost_cache"


def test_migration_ddl_contains_expected_invariants() -> None:
    """The DDL must enforce: unique email, role check, indexes."""
    from pathlib import Path

    text_ = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0002_app_users.py"
    src = text_.read_text()
    assert "uq_app_users_email" in src
    assert "UNIQUE" in src
    assert "ADMIN" in src and "ANALYST" in src and "VIEWER" in src
    assert "ix_app_users_role" in src
    # Idempotent DDL.
    assert "IF NOT EXISTS" in src
