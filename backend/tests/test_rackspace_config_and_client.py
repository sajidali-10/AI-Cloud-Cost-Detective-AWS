"""Unit tests for the Rackspace configuration helpers, the
shared HTTP client, and the missing-credentials behaviour.
"""

from __future__ import annotations

import os
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
    for var in ("RACKSPACE_USERNAME", "RACKSPACE_API_KEY", "RACKSPACE_ACCOUNT_ID"):
        monkeypatch.delenv(var, raising=False)


def test_config_status_when_all_missing(monkeypatch):
    _clear_rackspace_env(monkeypatch)
    get_settings.cache_clear()
    status = get_config_status()
    assert status.configured is False
    assert "RACKSPACE_USERNAME" in status.missing
    assert "RACKSPACE_API_KEY" in status.missing
    assert "RACKSPACE_ACCOUNT_ID" in status.missing


def test_config_status_when_partial(monkeypatch):
    _clear_rackspace_env(monkeypatch)
    monkeypatch.setenv("RACKSPACE_USERNAME", "rackspace-user")
    get_settings.cache_clear()
    status = get_config_status()
    assert status.configured is False
    assert "RACKSPACE_API_KEY" in status.missing
    assert "RACKSPACE_ACCOUNT_ID" in status.missing
    assert "RACKSPACE_USERNAME" not in status.missing


def test_config_status_when_complete(monkeypatch):
    monkeypatch.setenv("RACKSPACE_USERNAME", "rackspace-user")
    monkeypatch.setenv("RACKSPACE_API_KEY", "rackspace-key")
    monkeypatch.setenv("RACKSPACE_ACCOUNT_ID", "rackspace-account")
    get_settings.cache_clear()
    status = get_config_status()
    assert status.configured is True
    assert status.missing == []


def test_get_credentials_raises_when_missing(monkeypatch):
    _clear_rackspace_env(monkeypatch)
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

    _clear_rackspace_env(monkeypatch)
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.rackspace_username == ""
    assert settings.rackspace_api_key == ""
    assert settings.rackspace_account_id == ""


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


def test_environment_example_uses_placeholders_only():
    """.env.example only contains empty placeholders for Rackspace."""

    path = "/home/ubuntu/ai-cloud-cost-detective/.env.example"
    with open(path) as fp:
        lines = fp.readlines()
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
            # The value (after ``=``) must be empty.
            value = line.split("=", 1)[1].strip()
            assert value == "", (
                f".env.example line must have empty value: {line!r}"
            )


def test_env_file_is_gitignored():
    """.env must remain gitignored so real credentials never reach git."""

    with open("/home/ubuntu/ai-cloud-cost-detective/.gitignore") as fp:
        gitignore = fp.read()
    assert ".env" in gitignore
