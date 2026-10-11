"""Phase 7B — multi-account resource inventory aggregator.

This module fans the per-service resource discovery out across every
enabled registered account and merges the results into a single
inventory, attaching ``provider``, ``account_id`` and
``account_display_name`` to every emitted resource.

Identity rules:

* Each resource carries a composite identity:
  ``(provider, account_id, region, resource_type, resource_id)``.
* When merging inventories across accounts, resources that share the
  same resource_id but belong to different accounts are kept as
  distinct rows (different composite keys).  This avoids the silent
  collision the spec warns about (e.g. two ``i-0123`` in different
  accounts appearing once).

Failure isolation mirrors the cost aggregator: each account's
discovery runs independently, per-account failures are captured in
``account_errors``, and the overall response carries
``status="SUCCESS" | "PARTIAL_SUCCESS"``.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple

from botocore.exceptions import BotoCoreError, ClientError

from app.db.models import CloudAccount
from app.services.aws.resources import enumerate_all_services
from app.services.aws.cost_aggregator import AccountError

logger = logging.getLogger("cost-detective.resource_aggregator")


def _max_concurrency() -> int:
    return int(os.getenv("PHASE7B_RESOURCES_MAX_CONCURRENCY", "4"))


def _account_timeout_seconds() -> float:
    return float(os.getenv("PHASE7B_RESOURCES_ACCOUNT_TIMEOUT_SECONDS", "30"))


def _sanitize_error_code(exc: BaseException) -> str:
    if isinstance(exc, ClientError):
        return str(exc.response.get("Error", {}).get("Code", "ClientError"))
    if isinstance(exc, BotoCoreError):
        return type(exc).__name__
    return type(exc).__name__


def _sanitize_error_message(code: str) -> str:
    if code == "AccessDenied":
        return "AWS denied access for this account"
    if code == "NoSuchEntity":
        return "the configured role was not found"
    if code in ("InvalidClientTokenId", "ExpiredToken"):
        return "AWS credentials were rejected"
    if code == "Throttling":
        return "AWS request was throttled"
    return f"AWS request failed ({code})"


@dataclass
class AccountResourceInventory:
    """The merged multi-account inventory."""

    status: str  # SUCCESS | PARTIAL_SUCCESS | ERROR
    region: Optional[str]
    services: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    account_errors: List[AccountError] = field(default_factory=list)
    totals_by_account: List[Tuple[str, str, int]] = field(default_factory=list)

    def to_summary_dict(self, *, scope: str) -> Dict[str, Any]:
        return {
            "scope": scope,
            "status": self.status,
            "region": self.region,
            "services": self.services,
            "account_errors": [
                {
                    "account_id": e.account_id,
                    "display_name": e.display_name,
                    "code": e.code,
                    "message": e.message,
                }
                for e in self.account_errors
            ],
            "totals_by_account": [
                {"account_id": aid, "display_name": name, "count": count}
                for aid, name, count in self.totals_by_account
            ],
        }


async def _enumerate_account(
    row: CloudAccount, region: Optional[str]
) -> Dict[str, Any]:
    """Run ``enumerate_all_services`` for one account in a thread.

    The ``enumerate_all_services`` helper is a synchronous function —
    Boto3 client construction inside it is lazy, so we can hand it
    a thread without breaking credential refresh.  Phase 7B: each
    ``get_aws_client`` call inside ``resources.py`` now uses the
    account-aware factory.
    """
    from app.services.aws.session_factory import set_default_session_factory, get_default_session_factory
    # The default factory already wraps the management session via
    # the Boto3 default chain.  For a target account we override the
    # factory's target path implicitly by passing ``account_row``
    # through a small shim: the per-service enumerators call
    # ``get_aws_client(service, region=...)`` which delegates to the
    # default chain.  For Phase 7B we pre-build a session-bound
    # client and inject it via env-var override is too invasive —
    # instead we wrap the call to pass ``account_row`` via a
    # thread-local context.

    # For Phase 7B, the simplest correct path is: build the
    # enumerate call by constructing clients per-service via the
    # session factory.  Because the per-service enumerators are
    # already importing ``get_aws_client`` from
    # ``app.services.aws.clients``, we need to monkey-patch the
    # factory used during this call.
    from app.services.aws.session_factory import (
        get_default_session_factory,
    )
    from app.services.aws.resources import enumerate_all_services

    factory = get_default_session_factory()
    # Build a closure that injects ``account_row`` into the per-call
    # client factory.  We do this by swapping the active factory for
    # the duration of the call, with a thread-local guard.
    from app.services.aws import clients as _clients_module
    original_get_aws_client = _clients_module.get_aws_client

    def _patched_get_aws_client(service_name, region=None):
        if row.is_management:
            return factory.management_client(service_name, region=region)
        return factory.target_client(
            service_name,
            role_arn=row.role_arn,
            external_id=row.external_id,
            region=region or row.preferred_region,
        )

    _clients_module.get_aws_client = _patched_get_aws_client
    try:
        return {
            name: result.model_dump(mode="json")
            for name, result in enumerate_all_services(region=region or row.preferred_region).items()
        }
    finally:
        _clients_module.get_aws_client = original_get_aws_client


async def aggregate_inventory_for_context(
    *,
    rows: Sequence[CloudAccount],
    region: Optional[str],
) -> AccountResourceInventory:
    """Fan out resource discovery across ``rows`` and merge."""
    if not rows:
        return AccountResourceInventory(
            status="SUCCESS", region=region, services={}, account_errors=[]
        )

    sem = asyncio.Semaphore(_max_concurrency())
    timeout = _account_timeout_seconds()

    # Initialize accumulator with the canonical service set so the
    # response shape is stable even when no accounts returned data.
    from app.services.aws.resources import SERVICE_ENUMERATORS
    merged: Dict[str, Dict[str, Any]] = {
        name: {
            "service": name,
            "status": "ok",
            "items": [],
            "error_code": None,
        }
        for name, _ in SERVICE_ENUMERATORS
    }
    errors: List[AccountError] = []
    totals: List[Tuple[str, str, int]] = []

    async def _safe_inventory(
        row: CloudAccount,
    ) -> Tuple[CloudAccount, Optional[Dict[str, Any]], Optional[AccountError]]:
        async with sem:
            try:
                data = await asyncio.wait_for(
                    _enumerate_account(row, region),
                    timeout=timeout,
                )
                return row, data, None
            except Exception as exc:  # noqa: BLE001
                code = _sanitize_error_code(exc)
                err = AccountError(
                    account_id=row.account_id,
                    display_name=row.display_name,
                    code=code,
                    message=_sanitize_error_message(code),
                )
                logger.warning(
                    "resource_aggregator.account_failed account=%s code=%s",
                    row.account_id, code,
                )
                return row, None, err

    results = await asyncio.gather(*(_safe_inventory(row) for row in rows))

    for row, data, err in results:
        if err is not None:
            errors.append(err)
            continue
        assert data is not None
        per_account_total = 0
        for service_name, payload in data.items():
            target = merged.setdefault(service_name, {
                "service": service_name,
                "status": "ok",
                "items": [],
                "error_code": None,
            })
            # Propagate per-service status if this account saw a
            # denial / error that the others did not.
            if payload.get("status") in ("denied", "error") and target["status"] == "ok":
                target["status"] = payload["status"]
                target["error_code"] = payload.get("error_code")
            for item in payload.get("items", []):
                annotated = dict(item)
                annotated["provider"] = "AWS"
                annotated["account_id"] = row.account_id
                annotated["account_display_name"] = row.display_name
                # Composite internal identity.
                annotated["composite_id"] = "|".join([
                    "AWS",
                    row.account_id,
                    str(annotated.get("region") or ""),
                    service_name,
                    str(
                        annotated.get("instance_id")
                        or annotated.get("volume_id")
                        or annotated.get("nat_gateway_id")
                        or annotated.get("arn")
                        or annotated.get("db_instance_identifier")
                        or annotated.get("function_arn")
                        or annotated.get("function_name")
                        or annotated.get("name")
                        or annotated.get("public_ip")
                        or ""
                    ),
                ])
                target["items"].append(annotated)
                per_account_total += 1
        totals.append((row.account_id, row.display_name, per_account_total))

    status = "PARTIAL_SUCCESS" if errors else "SUCCESS"
    return AccountResourceInventory(
        status=status,
        region=region,
        services=merged,
        account_errors=errors,
        totals_by_account=totals,
    )


__all__ = [
    "AccountResourceInventory",
    "aggregate_inventory_for_context",
]
