"""Unit tests for the Rackspace configuration helpers, the
shared HTTP client, and the missing-credentials behaviour.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
import pytest

from app.core.config import get_settings
from app.services.rackspace.client import (
    DEFAULT_TIMEOUT_SECONDS,
    RackspaceHttpClient,
    RackspaceResponse,
)
from app.services.rackspace.config import (
    RACKSPACE_IDENTITY_URL,
    RackspaceConfigStatus,
    RackspaceCredentials,
    get_config_status,
    get_credentials,
)
from app.services.rackspace.exceptions import (
    RackspaceAuthError,
    RackspaceConfigError,
    RackspaceNotFoundError,
    RackspacePermissionError,
    RackspaceTransportError,
)


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------


def _clear_rackspace_env(monkeypatch):
    """Pin the Rackspace env vars to empty strings.

    We SET to empty rather than ``delenv`` because pydantic-settings
    reads the host ``.env`` when an env var is absent — so a
    deleted variable would still pick up the operator's real
    credentials.  Setting to ``""`` makes pydantic-settings
    treat the var as authoritative and override any ``.env``
    value.
    """

    for var in (
        "RACKSPACE_USERNAME",
        "RACKSPACE_API_KEY",
        "RACKSPACE_ACCOUNT_ID",
        "RACKSPACE_BILLING_ACCOUNT",
    ):
        monkeypatch.setenv(var, "")


def test_config_status_when_all_missing(monkeypatch):
    _clear_rackspace_env(monkeypatch)
    get_settings.cache_clear()
    status = get_config_status()
    assert status.configured is False
    assert "RACKSPACE_USERNAME" in status.missing
    assert "RACKSPACE_API_KEY" in status.missing
    assert "RACKSPACE_ACCOUNT_ID" in status.missing
    # RAN is optional and is NOT in the missing list when unset.
    assert "RACKSPACE_BILLING_ACCOUNT" not in status.missing


def test_config_status_when_partial(monkeypatch):
    _clear_rackspace_env(monkeypatch)
    monkeypatch.setenv("RACKSPACE_USERNAME", "rackspace-user")
    get_settings.cache_clear()
    status = get_config_status()
    assert status.configured is False
    assert "RACKSPACE_API_KEY" in status.missing
    assert "RACKSPACE_ACCOUNT_ID" in status.missing
    assert "RACKSPACE_USERNAME" not in status.missing
    assert "RACKSPACE_BILLING_ACCOUNT" not in status.missing


def test_config_status_when_complete(monkeypatch):
    monkeypatch.setenv("RACKSPACE_USERNAME", "rackspace-user")
    monkeypatch.setenv("RACKSPACE_API_KEY", "rackspace-key")
    monkeypatch.setenv("RACKSPACE_ACCOUNT_ID", "rackspace-account")
    monkeypatch.setenv("RACKSPACE_BILLING_ACCOUNT", "020-123456")
    get_settings.cache_clear()
    status = get_config_status()
    assert status.configured is True
    assert status.missing == []
    # Billing RAN is reported separately.
    from app.services.rackspace.config import (
        billing_account_configured, get_billing_account,
    )
    assert billing_account_configured() is True
    assert get_billing_account() == "020-123456"


def test_get_credentials_raises_when_missing(monkeypatch):
    monkeypatch.setenv("RACKSPACE_USERNAME", "")
    monkeypatch.setenv("RACKSPACE_API_KEY", "")
    monkeypatch.setenv("RACKSPACE_ACCOUNT_ID", "")
    get_settings.cache_clear()
    with pytest.raises(RackspaceConfigError):
        get_credentials()


def test_credentials_redact_in_repr():
    """``RackspaceCredentials.__repr__`` must not leak the API key."""

    creds = RackspaceCredentials(
        username="rackspace-user", api_key="super-secret-key",
        account_id="rackspace-account",
    )
    text = repr(creds)
    assert "super-secret-key" not in text
    assert "redacted" in text


def test_missing_credentials_do_not_break_settings(monkeypatch):
    """Settings still construct when Rackspace variables are absent."""

    monkeypatch.setenv("RACKSPACE_USERNAME", "")
    monkeypatch.setenv("RACKSPACE_API_KEY", "")
    monkeypatch.setenv("RACKSPACE_ACCOUNT_ID", "")
    monkeypatch.setenv("RACKSPACE_BILLING_ACCOUNT", "")
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.rackspace_username == ""
    assert settings.rackspace_api_key == ""
    assert settings.rackspace_account_id == ""
    assert settings.rackspace_billing_account == ""


# ---------------------------------------------------------------------------
# Shared HTTP client (verb surface)
# ---------------------------------------------------------------------------


def test_http_client_only_exposes_get_and_post_json():
    """The shared HTTP client must not expose PUT/PATCH/DELETE."""

    forbidden = {"put", "patch", "delete"}
    for name in forbidden:
        assert not hasattr(RackspaceHttpClient, name), (
            f"RackspaceHttpClient must not expose {name!r}"
        )


def test_http_client_default_timeout_is_bounded():
    """Default timeout is bounded so a hung Rackspace endpoint cannot wedge the POC."""

    assert DEFAULT_TIMEOUT_SECONDS <= 60
    assert DEFAULT_TIMEOUT_SECONDS >= 5


def test_redact_headers_drops_auth_material():
    from app.services.rackspace.client import _redact_headers

    cleaned = _redact_headers({
        "X-Auth-Token": "should-be-dropped",
        "X-Server-Management-Url": "should-be-dropped",
        "Content-Type": "application/json",
    })
    assert "X-Auth-Token" not in cleaned
    assert "X-Server-Management-Url" not in cleaned
    assert cleaned.get("Content-Type") == "application/json"


# ---------------------------------------------------------------------------
# Identity URL is the only hard-coded endpoint
# ---------------------------------------------------------------------------


def test_identity_url_is_pinned_to_rackspace_identity():
    """The canonical Rackspace Identity URL is the only hard-coded endpoint."""

    assert RACKSPACE_IDENTITY_URL == (
        "https://identity.api.rackspacecloud.com/v2.0/tokens"
    )


def _repo_root_file(name: str) -> Optional[Path]:
    """Locate a repo-root file relative to this test file.

    The suite runs both on the host checkout (repo root is two
    levels up from ``backend/tests/``) and inside the Docker
    image, where only ``app`` / ``alembic`` / ``tests`` are
    copied and the repo root is absent.  Returns ``None`` in
    the latter case so callers can skip rather than fail.
    """

    candidate = Path(__file__).resolve().parents[2] / name
    return candidate if candidate.exists() else None


def test_environment_example_uses_placeholders_only():
    """.env.example only contains empty placeholders for Rackspace."""

    path = _repo_root_file(".env.example")
    if path is None:
        pytest.skip(
            ".env.example not reachable from this environment "
            "(repo root absent — e.g. running inside the Docker image)"
        )
    with open(path) as fp:
        lines = fp.readlines()
    rackspace_keys = {
        "RACKSPACE_USERNAME",
        "RACKSPACE_API_KEY",
        "RACKSPACE_ACCOUNT_ID",
        "RACKSPACE_BILLING_ACCOUNT",
    }
    seen_keys: set = set()
    rackspace_section = False
    for line in lines:
        if line.startswith("# --- Phase 9A: Rackspace Cloud"):
            rackspace_section = True
            continue
        if rackspace_section:
            if line.startswith("# ---"):
                break
            if not line.strip():
                continue
            if line.lstrip().startswith("#"):
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            assert key in rackspace_keys, (
                f"unexpected key in Rackspace section: {key!r}"
            )
            assert value == "", (
                f".env.example line must have empty value: {line!r}"
            )
            seen_keys.add(key)
    assert rackspace_keys <= seen_keys, (
        f"missing Rackspace placeholder(s): {rackspace_keys - seen_keys}"
    )


def test_env_file_is_gitignored():
    """.env must remain gitignored so real credentials never reach git."""

    path = _repo_root_file(".gitignore")
    if path is None:
        pytest.skip(
            ".gitignore not reachable from this environment "
            "(repo root absent — e.g. running inside the Docker image)"
        )
    with open(path) as fp:
        gitignore = fp.read()
    assert ".env" in gitignore
