"""Phase 9A — Rackspace read-only POC runner.

This module is the inner driver for
``scripts/phase9a_rackspace_poc_validation.sh``.  It performs the
same actions the test suite performs against a *mocked* HTTP
client — but it points the real :class:`RackspaceHttpClient` at
the production Rackspace endpoints.  No credentials are ever
printed.

The runner emits a single JSON object on stdout with a fixed
schema:

    {
      "identity_ok": bool,
      "token_seconds_to_expiry": int | None,
      "token_expires_human": str | None,
      "roles_count": int | None,
      "roles": list[str] | None,
      "elevated_roles_present": list[str] | None,
      "service_types": list[str] | None,

      "ran_configured": bool,             # RAN present in env
      "billing_account":                  # /v2/accounts/{ran}
          "OK" | "AUTH_FAILED" | "PERMISSION_DENIED"
          | "NOT_FOUND" | "NOT_CONFIGURED" | "TRANSPORT_ERROR",
      "billing_account_summary": dict,
      "billing_currency": ...,            # /v2/accounts/{ran}/currency
      "billing_currency_summary": dict,
      "billing_estimated_charges": ...,   # /v2/accounts/{ran}/estimated_charges
      "billing_estimated_charges_summary": dict,
      "billing_billing_summary": ...,     # /v2/accounts/{ran}/billing-summary
      "billing_billing_summary_summary": dict,
      "billing_invoice_latest": ...,      # /v2/accounts/{ran}/invoices/latest
      "billing_invoice_latest_summary": dict,

      "servers_status": ...,
      "servers_total": int | None,
      "servers_regions": list[str] | None,
      "servers_status_counts": dict | None,
      "servers_flavor_ids": list[str] | None,
      "write_methods_exposed": list[str],
      "no_token_in_output": bool,
    }

Exit code 0 on success (even when individual product endpoints
return non-OK — the validator decides what to warn vs fail).
Non-zero exit on a fatal transport failure that prevents
reporting.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from typing import Any, Dict, List

# Make ``app`` importable when the script is invoked from the
# repository root.  The venv lives at ``backend/.venv`` and
# ``app`` is installed editable from ``backend/``.
_BACKEND = os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir, "backend")
)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from app.services.rackspace import (  # noqa: E402
    RackspaceBillingClient,
    RackspaceHttpClient,
    RackspaceIdentity,
    RackspaceServersClient,
)
from app.services.rackspace.billing import BILLING_BASE_URL  # noqa: E402
from app.services.rackspace.config import (  # noqa: E402
    billing_account_configured,
)


# Tokens we must never leak into the output.  We grep the rendered
# JSON against these strings before declaring success.
SENTINEL_SECRET = "RACKSPACE_API_KEY_VALUE_SENTINEL"
# Populate the sentinel with the *actual* API key value as it
# sits in the environment, but immediately scrub the local
# variable.  Any inadvertent leak of the API key will fail the
# token-redaction check.
_API_KEY_VALUE = os.environ.get("RACKSPACE_API_KEY", "")
_RAN_VALUE = os.environ.get("RACKSPACE_BILLING_ACCOUNT", "")
_ACCOUNT_ID_VALUE = os.environ.get("RACKSPACE_ACCOUNT_ID", "")
_USERNAME_VALUE = os.environ.get("RACKSPACE_USERNAME", "")
SENTINEL_VALUES = [
    v for v in (
        _API_KEY_VALUE,
        _RAN_VALUE,
        _ACCOUNT_ID_VALUE,
        _USERNAME_VALUE,
        SENTINEL_SECRET,
    ) if v
]


def _safe_str(value: Any) -> str:
    """Stringify a value while replacing API-key / token substrings."""

    text = str(value)
    for sentinel in SENTINEL_VALUES:
        if sentinel and sentinel in text:
            text = text.replace(sentinel, "<redacted>")
    return text


def _public_verbs() -> List[str]:
    """Return the HTTP verbs exposed by the Rackspace product client."""

    http = RackspaceHttpClient()
    methods: List[str] = []
    for name in ("get", "post_json", "put", "patch", "delete", "head"):
        if hasattr(http, name):
            methods.append(name)
    return methods


def _public_methods(client: Any) -> List[str]:
    """Return the public (non-underscore) methods on a client."""

    return [
        name for name in dir(client)
        if not name.startswith("_") and callable(getattr(client, name))
    ]


def _sanitize_summary(summary: Dict[str, Any]) -> Dict[str, Any]:
    """Strip any sensitive substrings from a summary dict."""

    cleaned: Dict[str, Any] = {}
    for key, value in summary.items():
        if isinstance(value, str):
            cleaned[key] = _safe_str(value)
        elif isinstance(value, dict):
            cleaned[key] = _sanitize_summary(value)
        elif isinstance(value, list):
            cleaned[key] = [_safe_str(item) if isinstance(item, str) else item for item in value]
        else:
            cleaned[key] = value
    return cleaned


async def _run() -> Dict[str, Any]:
    """Drive the POC.  Returns the JSON-ready dict."""

    output: Dict[str, Any] = {
        "identity_ok": False,
        "token_seconds_to_expiry": None,
        "token_expires_human": None,
        "roles_count": None,
        "roles": None,
        "elevated_roles_present": None,
        "service_types": None,
        "ran_configured": billing_account_configured(),
        "billing_base_url": BILLING_BASE_URL,
        "billing_account": None,
        "billing_account_summary": {},
        "billing_currency": None,
        "billing_currency_summary": {},
        "billing_estimated_charges": None,
        "billing_estimated_charges_summary": {},
        "billing_billing_summary": None,
        "billing_billing_summary_summary": {},
        "billing_invoice_latest": None,
        "billing_invoice_latest_summary": {},
        "servers_status": None,
        "servers_total": None,
        "servers_regions": None,
        "servers_status_counts": None,
        "servers_flavor_ids": None,
        "write_methods_exposed": [],
        "no_token_in_output": True,
    }

    identity = RackspaceIdentity(http_client=RackspaceHttpClient())
    billing = RackspaceBillingClient(
        http_client=identity._http, identity=identity,  # type: ignore[attr-defined]
    )
    servers = RackspaceServersClient(
        http_client=identity._http, identity=identity,  # type: ignore[attr-defined]
    )

    # 1) Identity authentication.
    try:
        session = await identity.authenticate()
    except Exception as exc:  # noqa: BLE001 - we want to surface anything
        output["identity_ok"] = False
        output["identity_error"] = type(exc).__name__
        return output

    output["identity_ok"] = True
    output["token_seconds_to_expiry"] = int(session.seconds_until_expiry())
    output["token_expires_human"] = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(session.expires_at),
    )
    output["roles_count"] = len(session.roles)
    output["roles"] = sorted({r.name for r in session.roles if r.name})
    output["elevated_roles_present"] = session.elevated_roles_present
    output["service_types"] = session.catalog.discovered_service_types()

    # 2) Billing probes — safe order, per-endpoint status + summary.
    try:
        billing_results = await billing.probe_all()
        for result in billing_results:
            key = result.endpoint  # e.g. ``billing_account``
            output[key] = result.status
            output[f"{key}_summary"] = _sanitize_summary(dict(result.summary))
    except Exception as exc:  # noqa: BLE001
        for key in (
            "billing_account", "billing_currency",
            "billing_estimated_charges", "billing_billing_summary",
            "billing_invoice_latest",
        ):
            output[key] = "TRANSPORT_ERROR"
        output["billing_error"] = type(exc).__name__

    # 3) Cloud Servers probes.
    try:
        summary = await servers.get_aggregate_summary()
        if summary.detail.startswith("AUTH_FAILED"):
            output["servers_status"] = "AUTH_FAILED"
        elif summary.detail.startswith("PERMISSION_DENIED"):
            output["servers_status"] = "PERMISSION_DENIED"
        elif summary.detail.startswith("NOT_FOUND"):
            output["servers_status"] = "NOT_FOUND"
        elif summary.detail.startswith("TRANSPORT_ERROR"):
            output["servers_status"] = "TRANSPORT_ERROR"
        elif summary.detail.startswith("NOT_CONFIGURED"):
            output["servers_status"] = "NOT_CONFIGURED"
        else:
            output["servers_status"] = "OK"
            output["servers_total"] = summary.total_count
            output["servers_regions"] = sorted(summary.regions)
            output["servers_status_counts"] = dict(
                sorted(summary.status_counts.items()),
            )
            output["servers_flavor_ids"] = sorted(set(summary.flavor_ids))
    except Exception as exc:  # noqa: BLE001
        output["servers_status"] = "TRANSPORT_ERROR"
        output["servers_error"] = type(exc).__name__

    # 4) Verb surface check.
    output["write_methods_exposed"] = [
        name for name in _public_verbs()
        if name not in ("get", "post_json", "head", "aclose")
    ]

    # 5) Public methods on each client — must contain no write-like
    # verbs.  We list every public method for transparency.
    output["billing_public_methods"] = _public_methods(billing)
    output["servers_public_methods"] = _public_methods(servers)

    return output


def _scrub_output(output: Dict[str, Any]) -> Dict[str, Any]:
    """Strip any API-key / token material that may have leaked into the dict."""

    def _walk(value: Any) -> Any:
        if isinstance(value, str):
            return _safe_str(value)
        if isinstance(value, list):
            return [_walk(item) for item in value]
        if isinstance(value, dict):
            return {k: _walk(v) for k, v in value.items()}
        return value

    return _walk(output)


def main() -> int:
    output = asyncio.run(_run())
    output = _scrub_output(output)

    # Final guard — make sure the API key value is absent from the
    # rendered JSON.  This is a belt-and-braces check; ``_safe_str``
    # already removed anything that matched.
    rendered = json.dumps(output, sort_keys=True)
    leak_found = False
    for sentinel in SENTINEL_VALUES:
        if sentinel and sentinel in rendered:
            leak_found = True
            rendered = rendered.replace(sentinel, "<redacted>")
    output["no_token_in_output"] = not leak_found

    sys.stdout.write(rendered)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
