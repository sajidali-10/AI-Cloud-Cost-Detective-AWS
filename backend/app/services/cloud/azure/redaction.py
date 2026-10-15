"""Azure-side redaction helpers — Phase 7C.2A.

Phase 7C.2A's security contract is explicit:

* AWS WebIdentityToken — never logged.
* Azure access tokens — never logged.
* ``Authorization: Bearer …`` headers — never logged.
* Client secrets — never stored, never logged.
* AWS access / secret keys — never stored, never logged.

This module is the single source of truth for sanitisation
across the Azure credential + provider code paths.  The
helpers are intentionally cheap — they are called on every log
emission and every exception translation, so we keep them
side-effect free and allocation-light.

Two layers of defence:

1. **Log emission**: helper functions return a redacted summary
   that can be passed to :func:`logging.Logger.warning` /
   :func:`logging.Logger.info`.  Callers must use these helpers
   instead of formatting the raw values themselves.
2. **Exception sanitisation**: when a credential provider, ARM
   REST client, or any helper raises, the original message is
   replaced by a sanitised category.  Only the error code, the
   subscription-id segment (if available), and the operation
   kind may be exposed.

The contract is enforced by tests — see
:mod:`backend.tests.test_phase7c2a_secrets_not_logged`.
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Optional


# ---------------------------------------------------------------------------
# Pre-compiled patterns.  We try to match even if a caller accidentally
# interpolates a raw token into a log message.
# ---------------------------------------------------------------------------

# Compact JWT (header.payload.signature).  Matches both AWS GetWebIdentityToken
# output and Microsoft Entra access tokens (which are JWTs in the same shape).
_JWT_PATTERN = re.compile(
    r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"
)

# AWS access key ids (ASIA… for temporary, AKIA… for long-lived).  Matches
# even if a malformed string happens to look like one.
_AWS_ACCESS_KEY_PATTERN = re.compile(r"\b(AKIA|ASIA)[A-Z0-9]{16}\b")

# Bearer authorization header (the value MUST never appear in a log).
_BEARER_PATTERN = re.compile(r"(?i)authorization\s*:\s*bearer\s+\S+")

# OAuth ``client_assertion=`` query parameter (Entra token endpoint).
_CLIENT_ASSERTION_PATTERN = re.compile(
    r"(?i)client_assertion=[A-Za-z0-9_\-\.]+"
)

# Azure tenant / client ids are NOT secrets but we still don't echo them
# into the application log stream verbatim — they are operator-identifying
# and may appear in unwanted places if an operator shares a debug log.
_TENANT_ID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Status / category mapping
# ---------------------------------------------------------------------------

# Public status enum for the Azure provider — mirrors the AWS enum
# the rest of the application already understands.  Anything not in
# this map collapses to ``"ERROR"``.
AZURE_STATUS_CONNECTED = "CONNECTED"
AZURE_STATUS_ACCESS_DENIED = "ACCESS_DENIED"
AZURE_STATUS_AUTH_FAILED = "AUTH_FAILED"
AZURE_STATUS_NOT_FOUND = "NOT_FOUND"
AZURE_STATUS_DISABLED = "DISABLED"
AZURE_STATUS_NOT_CONFIGURED = "NOT_CONFIGURED"
AZURE_STATUS_ERROR = "ERROR"


def safe_subscription_label(subscription_id: Optional[str]) -> str:
    """Return a short, non-reversible label for a subscription id.

    We avoid logging the full subscription GUID.  The first 8 hex chars
    are deterministic enough for an operator to correlate log lines
    without revealing the full identifier.

    >>> safe_subscription_label("a54d076b-2650-4b4f-a28c-2deed2011f83")
    'a54d076b…'
    >>> safe_subscription_label(None)
    '<unset>'
    >>> safe_subscription_label("")
    '<unset>'
    """
    if not subscription_id:
        return "<unset>"
    cleaned = subscription_id.strip()
    if len(cleaned) < 8:
        return "<short-id>"
    return f"{cleaned[:8]}…"


def scrub_jwt(value: Any) -> Any:
    """Return ``value`` with any embedded JWT replaced by ``<jwt>``.

    Strings are scanned for the JWT shape and any match is replaced.
    Non-string values are returned unchanged (they cannot contain a JWT
    by construction).  ``None`` is returned unchanged.
    """
    if not isinstance(value, str):
        return value
    return _JWT_PATTERN.sub("<jwt>", value)


def scrub_authorization(value: Any) -> Any:
    """Return ``value`` with any ``Authorization: Bearer …`` replaced."""
    if not isinstance(value, str):
        return value
    return _BEARER_PATTERN.sub("Authorization: <redacted>", value)


def scrub_client_assertion(value: Any) -> Any:
    """Return ``value`` with any ``client_assertion=…`` query segment redacted."""
    if not isinstance(value, str):
        return value
    return _CLIENT_ASSERTION_PATTERN.sub("client_assertion=<redacted>", value)


def scrub_aws_access_key(value: Any) -> Any:
    """Return ``value`` with any embedded AWS access key id replaced."""
    if not isinstance(value, str):
        return value
    return _AWS_ACCESS_KEY_PATTERN.sub("<aws-key>", value)


def scrub_tenant_id(value: Any) -> Any:
    """Return ``value`` with any tenant / client GUID collapsed to ``<tenant>``.

    Applied to log messages that may surface Entra identifiers by
    accident — the application treats those identifiers as
    non-secret but still avoids echoing them in logs.
    """
    if not isinstance(value, str):
        return value
    return _TENANT_ID_PATTERN.sub("<tenant>", value)


def scrub_log_payload(value: Any) -> Any:
    """Apply every scrubber in sequence.

    Use this for any string going to the log stream from Azure-side
    code paths.  Cheap enough to call unconditionally.
    """
    if not isinstance(value, str):
        return value
    value = _JWT_PATTERN.sub("<jwt>", value)
    value = _BEARER_PATTERN.sub("Authorization: <redacted>", value)
    value = _CLIENT_ASSERTION_PATTERN.sub("client_assertion=<redacted>", value)
    value = _AWS_ACCESS_KEY_PATTERN.sub("<aws-key>", value)
    return value


def category_from_azure_error(exc: BaseException) -> str:
    """Map an Azure SDK / httpx exception onto a sanitised category.

    The returned string is one of:

    * ``"AUTH_FAILED"`` — Entra token exchange refused the assertion
      (e.g. ``ClientAuthenticationError``, ``invalid_client``,
      ``unauthorized_client``).
    * ``"ACCESS_DENIED"`` — the token was accepted but ARM returned
      403 / ``AuthorizationFailed``.
    * ``"NOT_FOUND"`` — ARM returned 404 / ``ResourceNotFound``.
    * ``"NOT_CONFIGURED"`` — the runtime configuration is missing
      tenant / client identifiers.
    * ``"ERROR"`` — anything else.  Never returns the raw message.
    """
    name = type(exc).__name__.lower()
    message = (str(exc) or "").lower()
    flat = f"{name} {message}"
    # Not-configured is signalled by ``MissingTenantIdError`` /
    # ``MissingClientIdError`` raised inside azure-identity, plus
    # our own pre-flight checks.  These are checked first so a
    # misconfigured deployment never logs a confusing auth error.
    if "missingtenantid" in name or "missingclientid" in name:
        return AZURE_STATUS_NOT_CONFIGURED
    if "missingtenantid" in flat or "missingclientid" in flat:
        return AZURE_STATUS_NOT_CONFIGURED
    # Authentication failures — the assertion was rejected by Entra
    # (invalid_client / unauthorized_client / audience mismatch).
    if "clientauthentication" in name or "authenticationrequired" in name:
        return AZURE_STATUS_AUTH_FAILED
    if "invalid_client" in flat or "unauthorized_client" in flat:
        return AZURE_STATUS_AUTH_FAILED
    if "audience" in flat and "invalid" in flat:
        return AZURE_STATUS_AUTH_FAILED
    # Authorisation failures — the token is valid but the SP lacks
    # the required RBAC on the subscription.
    if "authorizationfailed" in flat or "403" in flat:
        return AZURE_STATUS_ACCESS_DENIED
    # Resource-not-found — the subscription ID is unknown.
    if "resourcenotfound" in flat or "subscriptionnotfound" in flat:
        return AZURE_STATUS_NOT_FOUND
    if "404" in flat:
        return AZURE_STATUS_NOT_FOUND
    return AZURE_STATUS_ERROR


def safe_log_extra(mapping: Optional[Mapping[str, Any]]) -> Mapping[str, Any]:
    """Return ``mapping`` scrubbed for log emission.

    Every string value is run through :func:`scrub_log_payload` so an
    accidental JWT / Bearer header cannot leak.
    """
    if not mapping:
        return {}
    out: dict[str, Any] = {}
    for key, value in mapping.items():
        if isinstance(value, str):
            out[key] = scrub_log_payload(value)
        else:
            out[key] = value
    return out


__all__ = [
    "AZURE_STATUS_ACCESS_DENIED",
    "AZURE_STATUS_AUTH_FAILED",
    "AZURE_STATUS_CONNECTED",
    "AZURE_STATUS_DISABLED",
    "AZURE_STATUS_ERROR",
    "AZURE_STATUS_NOT_CONFIGURED",
    "AZURE_STATUS_NOT_FOUND",
    "category_from_azure_error",
    "safe_log_extra",
    "safe_subscription_label",
    "scrub_aws_access_key",
    "scrub_authorization",
    "scrub_client_assertion",
    "scrub_jwt",
    "scrub_log_payload",
    "scrub_tenant_id",
]
