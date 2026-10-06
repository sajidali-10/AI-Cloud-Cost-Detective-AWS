"""Static DDL assertions for the Phase 5B migration.

Mirrors :mod:`tests.test_migration_users` style: pure string
inspection of the migration file so the assertions don't require a
live Postgres.  Live Postgres round-trip coverage lives in
``scripts/phase5b_verify.sh``.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0003_conversations.py"
    )
    spec = importlib.util.spec_from_file_location("mig_0003", str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod, path


def test_migration_revision_and_down_revision() -> None:
    mod, _ = _load_migration()
    assert mod.revision == "0003_conversations"
    assert mod.down_revision == "0002_app_users"


def test_migration_ddl_enforces_role_check() -> None:
    _, path = _load_migration()
    src = path.read_text()
    assert "USER" in src and "ASSISTANT" in src and "SYSTEM_EVENT" in src
    assert "ck_conversation_messages_role" in src


def test_migration_ddl_enforces_title_length_check() -> None:
    _, path = _load_migration()
    src = path.read_text()
    assert "ck_conversations_title_length" in src
    assert "length(title)" in src


def test_migration_ddl_declares_cascade_fks() -> None:
    _, path = _load_migration()
    src = path.read_text()
    # Conversations -> app_users
    assert "fk_conversations_user_id" in src
    assert "REFERENCES app_users(id)" in src
    assert "ON DELETE CASCADE" in src
    # Messages -> conversations
    assert "fk_conversation_messages_conv_id" in src
    assert "REFERENCES conversations(id)" in src


def test_migration_ddl_declares_expected_indexes() -> None:
    _, path = _load_migration()
    src = path.read_text()
    for expected in (
        "ix_conversations_user_id",
        "ix_conversations_user_updated",
        "ix_conversations_user_archived_lastmsg",
        "ix_conversation_messages_conv_created",
        "ix_conversation_messages_conv_role",
    ):
        assert expected in src, f"missing index: {expected}"


def test_migration_ddl_is_idempotent() -> None:
    _, path = _load_migration()
    src = path.read_text()
    # Both directions should be no-ops on a re-run.
    assert "CREATE TABLE IF NOT EXISTS conversations" in src
    assert "CREATE TABLE IF NOT EXISTS conversation_messages" in src
    assert "CREATE INDEX IF NOT EXISTS" in src
    assert "DROP INDEX IF EXISTS" in src
    assert "DROP TABLE IF EXISTS" in src


def test_migration_has_no_authorization_or_secrets_storage() -> None:
    """Defence-in-depth: no DDL column/constraint may carry secrets.

    Only the SQL statements are scanned — the migration's
    docstring prose (which mentions Authorization / JWT / AWS
    credentials to explain what is NOT stored) is excluded so the
    defensive narration does not trigger a false positive.

    Safe column names like ``token_usage`` (intentional
    provenance for assistant messages) are explicitly allowed.
    """
    import re

    _, path = _load_migration()
    src = path.read_text()

    # Extract the SQL fragments assigned to ``_UPGRADE_*`` and
    # ``_DOWNGRADE_*`` string constants — these are the only
    # places DDL can appear in the file.
    ddl_fragments: list[str] = []
    for m in re.finditer(
        r"^[A-Z_]*DDL\s*=\s*([\"']{3})(.+?)\1",
        src,
        flags=re.MULTILINE | re.DOTALL,
    ):
        ddl_fragments.append(m.group(2).lower())
    for m in re.finditer(
        r"^[A-Z_]*INDEX\s*=\s*([\"']{1,3})(.+?)\1",
        src,
        flags=re.MULTILINE | re.DOTALL,
    ):
        ddl_fragments.append(m.group(2).lower())
    for m in re.finditer(
        r"^[A-Z_]*FK(_ADD)?\s*=\s*([\"']{1,3})(.+?)\2",
        src,
        flags=re.MULTILINE | re.DOTALL,
    ):
        ddl_fragments.append(m.group(3).lower())
    for m in re.finditer(
        r"^[A-Z_]*CONSTRAINT(_ADD)?\s*=\s*([\"']{1,3})(.+?)\2",
        src,
        flags=re.MULTILINE | re.DOTALL,
    ):
        ddl_fragments.append(m.group(3).lower())
    for m in re.finditer(
        r"^[A-Z_]*TABLE\s*=\s*([\"']{1,3})(.+?)\1",
        src,
        flags=re.MULTILINE | re.DOTALL,
    ):
        ddl_fragments.append(m.group(2).lower())

    assert ddl_fragments, "no DDL fragments found in migration"

    forbidden_compound = (
        "authorization",
        "password_hash",
        "password ",
        "secret_key",
        "aws_secret_access_key",
        "aws_access_key_id",
        "api_key=",
        "bearer ",
        "jwt_secret",
        "jwt ",
        "raw_payload",
        "raw_response",
        "raw_provider_payload",
        "raw_litellm_payload",
    )
    for fragment in ddl_fragments:
        for token in forbidden_compound:
            assert token not in fragment, (
                f"forbidden token {token!r} appears in migration DDL fragment"
            )
