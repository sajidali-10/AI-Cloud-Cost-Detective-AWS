"""Boto3 client factory — Phase 1 with Phase 7B account-routing.

A single thin wrapper around :func:`boto3.client` so the rest of the
AWS service code can request a regional client without caring about
credential resolution. Credentials come from the Boto3 default chain
(env / shared config / EC2 instance profile); this module never
introduces AWS keys and never reads them from ``Settings``.

Phase 7B — Active-account context (the hard fix for the Phase 7B
"selected account leaks management data" defect):

The Phase 7A implementation monkey-patched
``_clients_module.get_aws_client`` from the route layer. That worked
ONLY for call sites that looked up the function via the module
attribute (``app.services.aws.clients.get_aws_client(...)``). It did
NOT reach call sites that used the import-time binding
(``from app.services.aws.clients import get_aws_client``) because
``from X import Y`` copies ``Y`` into the importer's namespace — the
importer never re-resolves ``Y`` from the ``clients`` module.

We solve this with a :class:`contextvars.ContextVar` that the route
layer (or aggregator) sets to the resolved :class:`CloudAccount`
row before invoking AWS-touching code. Every client factory consults
the ContextVar and:

* uses the **management** session (Boto3 default chain) when the
  var is unset — preserves Phase 1/2/7A behaviour exactly;
* uses the **management** session when the active row is the
  management row (preserves the EC2 instance profile contract);
* routes through the **target** STS AssumeRole session when the
  active row is a registered target account.

The ContextVar is async-loop safe (``contextvars`` are designed for
this — they propagate through ``await`` and ``asyncio.to_thread``
without thread-pool surprise).  The route layer pairs every
``set_active_account_row`` with a ``reset_active_account_row`` in a
``finally`` so a partial failure cannot leave the loop pointing at
the wrong account.
"""
from __future__ import annotations

import contextvars
from typing import TYPE_CHECKING, Any, Optional

import boto3
from botocore.client import BaseClient

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.db.models import CloudAccount


# ---------------------------------------------------------------------------
# Active-account context
# ---------------------------------------------------------------------------

#: The currently active registered :class:`CloudAccount` row, or
#: ``None`` when no account has been bound (the default — calls fall
#: through to the Boto3 default credential chain).
_active_account: contextvars.ContextVar[Optional["CloudAccount"]] = contextvars.ContextVar(
    "cost_detective_active_account", default=None
)


def set_active_account_row(row: Optional["CloudAccount"]):
    """Bind ``row`` as the active account for the current context.

    Returns a token that MUST be passed to :func:`reset_active_account_row`
    from a ``finally`` block.  Calling this without resetting in the
    same context will leak the row to subsequent unrelated work.
    """
    return _active_account.set(row)


def reset_active_account_row(token) -> None:
    """Restore the previously bound active-account row."""
    _active_account.reset(token)


def get_active_account_row() -> Optional["CloudAccount"]:
    """Return the currently bound account row, or ``None``."""
    return _active_account.get()


def clear_active_account_row() -> None:
    """Unconditionally clear the active row (test/debug helper)."""
    _active_account.set(None)


# ---------------------------------------------------------------------------
# Client factory
# ---------------------------------------------------------------------------


def _client_via_session_factory(
    service_name: str, region: Optional[str]
) -> BaseClient:
    """Resolve a client through the centralised session factory.

    Used when the active row is a registered target account.  The
    factory owns STS credential caching and role assumption; this
    function is intentionally thin so test code can stub it.
    """
    # Local import to avoid an import cycle at module load time
    # (clients ↔ session_factory share context but never runtime
    # state during import).
    from app.services.aws.session_factory import get_aws_client_for

    row = get_active_account_row()
    if row is None:
        # Defensive: if we get here without a bound row, fall back
        # to the management chain rather than crashing — this matches
        # the Phase 1/2 contract.
        kwargs: dict[str, Any] = {}
        if region:
            kwargs["region_name"] = region
        return boto3.client(service_name, **kwargs)
    return get_aws_client_for(
        service_name,
        account_row=row,
        region=region or row.preferred_region,
    )


def get_aws_client(service_name: str, region: Optional[str] = None) -> BaseClient:
    """Return a Boto3 client for ``service_name`` in the given region.

    Routing:

    * If the active-account ContextVar is bound to a registered
      target row, the client is constructed through the centralised
      session factory (``STS AssumeRole``).
    * If the active row is the management row (or unset), the client
      is constructed through the Boto3 default credential chain
      (EC2 instance profile / env / shared config).  This preserves
      Phase 1/2/7A behaviour exactly.

    Parameters
    ----------
    service_name:
        The lowercase Boto3 service identifier, e.g. ``"sts"``,
        ``"ec2"``, ``"s3"``.
    region:
        Optional AWS region override (e.g. ``"us-east-1"``). When
        ``None``, the active row's ``preferred_region`` is used for
        target accounts; the Boto3 default chain's region resolution
        is used for management / unbound callers.

    Returns
    -------
    botocore.client.BaseClient
        A configured Boto3 client. Credential lookup happens lazily
        on the first API call, so a missing-credentials error surfaces
        from the call site, not from this factory.
    """
    row = get_active_account_row()
    if row is not None and not getattr(row, "is_management", False):
        return _client_via_session_factory(service_name, region)
    # Management row OR no active row → use the Boto3 default chain.
    # We deliberately do NOT route management through the session
    # factory's ``management_session`` because that factory wraps a
    # bare ``boto3.Session()`` which has identical semantics to the
    # default chain.  Calling it adds an extra hop for no benefit
    # and would complicate the management-row identity flow.
    client_kwargs: dict[str, Any] = {}
    if region:
        client_kwargs["region_name"] = region
    return boto3.client(service_name, **client_kwargs)


__all__ = [
    "clear_active_account_row",
    "get_active_account_row",
    "get_aws_client",
    "reset_active_account_row",
    "set_active_account_row",
]
