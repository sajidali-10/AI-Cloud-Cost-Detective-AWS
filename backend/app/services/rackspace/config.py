"""Rackspace configuration helpers — Phase 9A POC.

This module is the single point where the application consults
:mod:`app.core.config` for Rackspace credentials.  It deliberately
does NOT raise when credentials are missing — the application
startup MUST NOT fail when Rackspace is absent (it is an optional,
non-required provider for Phase 9A).  Callers instead receive a
:class:`RackspaceConfigStatus` describing whether the credentials
are usable, and may choose to skip Rackspace work entirely.

The functions here never log credential values, never include
them in returned objects, and never echo them in error messages.
The ``RackspaceConfigStatus`` dataclass exposes only a boolean
``configured`` flag and a list of *names* of the missing variables
("username", "api_key", "account_id") — not their values.

Rackspace endpoint URLs are not configurable in Phase 9A.  The
canonical Rackspace Identity URL is pinned in
:data:`RACKSPACE_IDENTITY_URL` and is used as the entry point for
authentication.  Once authenticated, the rest of the endpoints are
resolved from the Identity service catalog so no endpoint URLs
need to be hard-coded in this codebase.  The Billing API is a
special case: per Rackspace documentation the Billing endpoint is
intentionally NOT in the Identity service catalog — the
:mod:`app.services.rackspace.billing` client uses the documented
Billing base URL directly and the ``RACKSPACE_BILLING_ACCOUNT``
identifier (the RAN) which is *separate* from the Cloud-API
tenant ID.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from app.core.config import Settings, get_settings


#: Re-export the exception so callers don't need a separate
#: import path.  Keeping the alias here means the validator
#: script can import everything it needs from
#: ``app.services.rackspace.config``.
from app.services.rackspace.exceptions import RackspaceConfigError  # noqa: E402


#: Canonical Rackspace Identity v2.0 endpoint.  This is the only
#: hard-coded URL in the POC — every other endpoint is resolved
#: from the Identity service catalog returned by the authentication
#: call.
RACKSPACE_IDENTITY_URL = "https://identity.api.rackspacecloud.com/v2.0/tokens"


@dataclass(frozen=True)
class RackspaceCredentials:
    """In-memory credential bundle.

    Holds the raw values but never exposes them via ``__repr__`` or
    ``__str__`` so accidental logging cannot leak them.  The values
    are read once at construction time and never persisted.
    """

    username: str
    api_key: str
    account_id: str

    def __repr__(self) -> str:  # pragma: no cover - defensive
        return "RackspaceCredentials(username=<redacted>, api_key=<redacted>, account_id=<redacted>)"

    def __str__(self) -> str:  # pragma: no cover - defensive
        return self.__repr__()


@dataclass(frozen=True)
class RackspaceConfigStatus:
    """Snapshot of whether Rackspace credentials are usable.

    ``configured`` is True when every required variable is present
    AND non-empty.  ``missing`` lists the *names* of the missing
    variables so the validator script can produce a sanitized
    diagnostic without ever printing the variable values.
    """

    configured: bool
    missing: List[str] = field(default_factory=list)


def _read_setting(settings: Settings, name: str) -> str:
    """Return the (stripped) value of a Rackspace setting, or ``""``.

    pydantic-settings lowercases environment variables; the
    Settings model already declares ``rackspace_username`` etc.
    with ``case_sensitive=False``.  We accept either the canonical
    lowercase or the env-style UPPER_CASE form for robustness in
    case a future operator deploys with a custom alias.
    """

    value = getattr(settings, name, "") or ""
    return value.strip()


def get_credentials(settings: Settings | None = None) -> RackspaceCredentials:
    """Return a :class:`RackspaceCredentials` bundle from settings.

    Raises :class:`RackspaceConfigError` if any required field is
    missing — callers that want to gracefully degrade should call
    :func:`get_config_status` first.
    """

    settings = settings or get_settings()
    username = _read_setting(settings, "rackspace_username")
    api_key = _read_setting(settings, "rackspace_api_key")
    account_id = _read_setting(settings, "rackspace_account_id")
    missing = [name for name, value in (
        ("rackspace_username", username),
        ("rackspace_api_key", api_key),
        ("rackspace_account_id", account_id),
    ) if not value]
    if missing:
        raise RackspaceConfigError(
            "Rackspace credentials are not configured"
        )
    return RackspaceCredentials(
        username=username,
        api_key=api_key,
        account_id=account_id,
    )


def get_config_status(settings: Settings | None = None) -> RackspaceConfigStatus:
    """Return a sanitized status snapshot for the validator script.

    Never raises.  The returned object never contains credential
    values — only the names of missing variables.

    The required-for-Identity variables are::

        RACKSPACE_USERNAME
        RACKSPACE_API_KEY
        RACKSPACE_ACCOUNT_ID

    ``RACKSPACE_BILLING_ACCOUNT`` (the RAN) is OPTIONAL and
    listed separately so a tenant without Billing access still
    satisfies the *Identity* configuration requirement.  The
    Billing client returns ``NOT_CONFIGURED`` when the RAN is
    absent instead of issuing a request with an empty path.
    """

    settings = settings or get_settings()
    missing: List[str] = []
    if not _read_setting(settings, "rackspace_username"):
        missing.append("RACKSPACE_USERNAME")
    if not _read_setting(settings, "rackspace_api_key"):
        missing.append("RACKSPACE_API_KEY")
    if not _read_setting(settings, "rackspace_account_id"):
        missing.append("RACKSPACE_ACCOUNT_ID")
    return RackspaceConfigStatus(configured=not missing, missing=missing)


def get_billing_account(settings: Settings | None = None) -> str:
    """Return the Rackspace Billing Account Number (RAN).

    The RAN is read from the ``RACKSPACE_BILLING_ACCOUNT``
    environment variable (typed setting
    ``rackspace_billing_account``).  The Billing client uses
    this value as the ``{ran}`` path component on the documented
    Billing base URL.  Returns an empty string when the value
    is absent — the Billing client translates that into a
    ``NOT_CONFIGURED`` result rather than issuing a malformed
    request.

    The function never logs the value.  Callers that surface
    it (e.g. a one-line "RAN configured: yes/no" diagnostic)
    must NEVER print the value itself.
    """

    settings = settings or get_settings()
    return _read_setting(settings, "rackspace_billing_account")


def billing_account_configured(settings: Settings | None = None) -> bool:
    """Return True when a non-empty RAN is present in settings."""

    return bool(get_billing_account(settings=settings))


__all__ = [
    "RACKSPACE_IDENTITY_URL",
    "RackspaceConfigStatus",
    "RackspaceCredentials",
    "billing_account_configured",
    "get_billing_account",
    "get_config_status",
    "get_credentials",
]
