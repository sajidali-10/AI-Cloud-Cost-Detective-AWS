"""AWS adapter for the cloud-provider abstraction — Phase 7C.

:class:`AWSCloudProvider` is the single concrete implementation of
:class:`~app.services.cloud.base.CloudProvider` shipped with the
platform today.  It does **not** re-implement any AWS service
logic; it delegates to the existing Phase 7B modules
(:mod:`app.services.aws.cost_explorer`,
:mod:`app.services.aws.cost_aggregator`,
:mod:`app.services.aws.resource_aggregator`,
:mod:`app.services.aws.optimization_aggregator`,
:mod:`app.services.aws.account_provider`) and translates their
dataclasses into the provider-neutral shapes defined in
:mod:`app.services.cloud.models`.

The adapter also publishes the AWS capability surface
(``COSTS`` / ``RESOURCES`` / ``OPTIMIZATION`` / ``METRICS`` /
``AI_EVIDENCE`` are SUPPORTED; everything else is UNSUPPORTED).
Future Azure/Rackspace adapters will publish their own capability
tables; the UI will dispatch off this table rather than hard-coding
provider-specific branching.

Importing this module registers the adapter with the
:class:`ProviderRegistry` (see the bottom of the file).
"""
from __future__ import annotations

import logging
import os
from datetime import date
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from app.db.models import CloudAccount as _CloudAccountRow

from app.services.cloud.base import (
    AccountValidationResult,
    CloudCapability,
    CloudCapabilityState,
    CloudCredentialStrategy,
    CloudProvider,
    CloudProviderStatus,
)
from app.services.cloud.models import (
    CloudAccount,
    CloudCostSummary,
    CloudCostTrend,
    CloudProviderKind,
    CloudRecommendation,
    CloudRegion,
    CloudResource,
    CloudResourceSummary,
    CloudServiceCost,
)
from app.services.cloud.registry import ProviderRegistry

logger = logging.getLogger("cost-detective.cloud.aws")


# ---------------------------------------------------------------------------
# Static AWS metadata
# ---------------------------------------------------------------------------


# A small, representative subset of AWS regions.  Adapters return
# only the regions they can actually address.  This list mirrors
# the regions Cost Explorer / Compute Optimizer / Cost Optimization
# Hub all support today.
_AWS_REGIONS: Tuple[CloudRegion, ...] = (
    CloudRegion(CloudProviderKind.AWS, "us-east-1", "US East (N. Virginia)"),
    CloudRegion(CloudProviderKind.AWS, "us-east-2", "US East (Ohio)"),
    CloudRegion(CloudProviderKind.AWS, "us-west-1", "US West (N. California)"),
    CloudRegion(CloudProviderKind.AWS, "us-west-2", "US West (Oregon)"),
    CloudRegion(CloudProviderKind.AWS, "eu-west-1", "Europe (Ireland)"),
    CloudRegion(CloudProviderKind.AWS, "eu-west-2", "Europe (London)"),
    CloudRegion(CloudProviderKind.AWS, "eu-central-1", "Europe (Frankfurt)"),
    CloudRegion(CloudProviderKind.AWS, "ap-southeast-1", "Asia Pacific (Singapore)"),
    CloudRegion(CloudProviderKind.AWS, "ap-southeast-2", "Asia Pacific (Sydney)"),
    CloudRegion(CloudProviderKind.AWS, "ap-northeast-1", "Asia Pacific (Tokyo)"),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _provider_value(provider: str) -> CloudProviderKind:
    """Map a DB string to a :class:`CloudProviderKind`.

    Tolerates the empty string (which the management-bootstrap path
    sometimes returns pre-migration) by defaulting to ``AWS``.
    """
    if not provider:
        return CloudProviderKind.AWS
    try:
        return CloudProviderKind(provider)
    except ValueError:
        return CloudProviderKind.AWS


def _previous_period(
    period_start: date, period_end: date
) -> Tuple[date, date]:
    """Return ``(previous_start, previous_end)`` for an equal-length window."""
    days = (period_end - period_start).days
    previous_start = date.fromordinal(period_start.toordinal() - days)
    return previous_start, period_start


def _decimal(value: Any) -> Decimal:
    """Coerce ``value`` to Decimal without binary-float corruption."""
    if isinstance(value, Decimal):
        return value
    if value is None:
        return Decimal("0")
    return Decimal(str(value))


def _unit(value: Optional[str]) -> str:
    return value or "USD"


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class AWSCloudProvider(CloudProvider):
    """The AWS implementation of the :class:`CloudProvider` contract."""

    provider_kind: CloudProviderKind = CloudProviderKind.AWS
    credential_strategies: Sequence[CloudCredentialStrategy] = (
        CloudCredentialStrategy.AWS_INSTANCE_PROFILE,
        CloudCredentialStrategy.AWS_ASSUME_ROLE,
    )

    # ---- Account / region metadata ----------------------------------------

    def list_regions(self) -> List[CloudRegion]:
        return list(_AWS_REGIONS)

    def get_provider_account(self, account: _CloudAccountRow) -> CloudAccount:
        """Translate a registry row into a provider-neutral :class:`CloudAccount`.

        Mirrors the existing
        :meth:`app.services.aws.account_provider._to_response` shape
        so the adapter does not need to reach back into the
        account_provider helpers for what is essentially a
        translation step.
        """
        return CloudAccount(
            provider=CloudProviderKind.AWS,
            account_id=account.account_id,
            display_name=account.display_name,
            is_management=bool(account.is_management),
            enabled=bool(account.enabled),
            external_ref=account.role_arn if not account.is_management else None,
            metadata={
                "preferred_region": account.preferred_region,
                # external_id is write-only at the API surface; we
                # do NOT surface it via the provider-neutral model
                # either.  An operator who needs to rotate an
                # external_id must PATCH the row directly.
            },
        )

    def validate_account(self, account: _CloudAccountRow) -> AccountValidationResult:
        """Delegate to the existing Phase 7B status resolver.

        The Phase 7B resolver already maps Boto3 error codes onto a
        stable status enum and never leaks raw exception payloads;
        we translate its :class:`CloudAccountStatus` into the
        provider-neutral :class:`AccountValidationResult`.

        Disabled accounts short-circuit to ``DISABLED`` so the
        bridge never makes a network call for a row the operator
        has turned off.
        """
        from app.services.aws.account_provider import (
            STATUS_DISABLED,
        )

        strategy = (
            CloudCredentialStrategy.AWS_INSTANCE_PROFILE
            if account.is_management
            else CloudCredentialStrategy.AWS_ASSUME_ROLE
        )
        if not account.enabled:
            return AccountValidationResult(
                provider=CloudProviderKind.AWS,
                account_id=account.account_id,
                display_name=account.display_name,
                status=STATUS_DISABLED,
                reason="account is disabled",
                credential_strategy=strategy,
            )
        adapter_status = _AdapterStatusBridge(account)
        status_response = adapter_status.resolve()
        return AccountValidationResult(
            provider=CloudProviderKind.AWS,
            account_id=account.account_id,
            display_name=account.display_name,
            status=status_response.status,
            reason=status_response.reason,
            arn=getattr(status_response, "arn", None),
            region=getattr(status_response, "region", None),
            credential_strategy=strategy,
        )

    # ---- Capabilities ------------------------------------------------------

    def get_capabilities(
        self, account: Optional[_CloudAccountRow] = None
    ) -> CloudProviderStatus:
        """Return the AWS capability snapshot.

        AWS supports ``COSTS``, ``RESOURCES``, ``OPTIMIZATION``,
        ``METRICS``, and ``AI_EVIDENCE``.  ``BILLING`` and
        ``ADVISOR`` are intentionally UNSUPPORTED on AWS (the
        provider does not expose them; AWS's billing console and
        Trusted Advisor are not part of this platform's read-only
        contract).
        """
        capabilities: Dict[CloudCapability, CloudCapabilityState] = {
            CloudCapability.COSTS: CloudCapabilityState.SUPPORTED,
            CloudCapability.RESOURCES: CloudCapabilityState.SUPPORTED,
            CloudCapability.OPTIMIZATION: CloudCapabilityState.SUPPORTED,
            CloudCapability.METRICS: CloudCapabilityState.SUPPORTED,
            CloudCapability.AI_EVIDENCE: CloudCapabilityState.SUPPORTED,
            CloudCapability.BILLING: CloudCapabilityState.UNSUPPORTED,
            CloudCapability.ADVISOR: CloudCapabilityState.UNSUPPORTED,
        }
        details: Dict[CloudCapability, Mapping[str, Any]] = {
            CloudCapability.COSTS: {
                "source": "AWS Cost Explorer",
                "metric": "UnblendedCost",
                "supported_lookback_days": [7, 30, 60, 90],
            },
            CloudCapability.RESOURCES: {
                "source": "AWS Resource APIs (EC2, ELBv2, RDS, Lambda, S3)",
            },
            CloudCapability.OPTIMIZATION: {
                "source": "AWS Cost Optimization Hub + Compute Optimizer + deterministic engine",
            },
            CloudCapability.METRICS: {
                "source": "AWS CloudWatch get_metric_data",
            },
            CloudCapability.AI_EVIDENCE: {
                "source": "Authoritative AWS evidence + grounded prompt rendering",
            },
        }
        return CloudProviderStatus(
            provider=CloudProviderKind.AWS,
            capabilities=capabilities,
            details=details,
            supported_regions=_AWS_REGIONS,
            supported_lookback_days=(7, 30, 60, 90),
        )

    # ---- Cost --------------------------------------------------------------

    async def get_cost_summary(
        self,
        *,
        accounts: Sequence[_CloudAccountRow],
        period_start: date,
        period_end: date,
        previous_period_start: date,
        previous_period_end: date,
        region: Optional[str] = None,
    ) -> CloudCostSummary:
        from app.services.aws.cost_aggregator import (
            aggregate_cost_report_for_context,
        )
        from app.services.aws.cost_explorer import CostPeriod

        period = CostPeriod(start=period_start, end=period_end, days=(period_end - period_start).days)
        previous = CostPeriod(
            start=previous_period_start,
            end=previous_period_end,
            days=(previous_period_end - previous_period_start).days,
        )
        rows = list(accounts) if accounts else []
        report = await aggregate_cost_report_for_context(
            rows=rows, period=period, previous=previous, region=region
        )
        change = _decimal(report.total_cost) - _decimal(report.previous_period_cost)
        if _decimal(report.previous_period_cost) == 0:
            change_percent: Optional[Decimal] = None
        else:
            change_percent = (change / _decimal(report.previous_period_cost)).quantize(
                Decimal("0.01")
            )
        scope = "all" if len(rows) != 1 else f"acct:{rows[0].account_id}"
        account_id_value: Optional[str] = None
        if len(rows) == 1:
            account_id_value = rows[0].account_id
        return CloudCostSummary(
            provider=CloudProviderKind.AWS,
            account_id=account_id_value,
            scope=scope,
            period_start=period.start,
            period_end=period.end,
            currency=report.currency or "USD",
            current_total=_decimal(report.total_cost),
            previous_total=_decimal(report.previous_period_cost),
            change_amount=change,
            change_percent=change_percent,
            estimated=False,
            by_service=[
                CloudServiceCost(
                    service=name,
                    amount=_decimal(amount),
                    currency=_unit(unit),
                    provider=CloudProviderKind.AWS,
                    account_id=account_id_value,
                    region=region,
                )
                for name, amount, unit in report.by_service
            ],
            by_region=[
                CloudServiceCost(
                    service=name,
                    amount=_decimal(amount),
                    currency=_unit(unit),
                    provider=CloudProviderKind.AWS,
                    account_id=account_id_value,
                    region=region,
                )
                for name, amount, unit in report.by_region
            ],
            extensions={
                "by_account": [
                    {"account_id": aid, "display_name": name, "amount": _decimal(amt)}
                    for aid, name, amt in report.by_account
                ],
                "account_errors": [
                    {
                        "account_id": e.account_id,
                        "display_name": e.display_name,
                        "code": e.code,
                        "message": e.message,
                    }
                    for e in report.account_errors
                ],
                "status": report.status,
            },
        )

    async def get_cost_trend(
        self,
        *,
        accounts: Sequence[_CloudAccountRow],
        period_start: date,
        period_end: date,
        region: Optional[str] = None,
    ) -> CloudCostTrend:
        from app.services.aws.cost_aggregator import (
            aggregate_cost_report_for_context,
        )
        from app.services.aws.cost_explorer import CostPeriod

        period = CostPeriod(start=period_start, end=period_end, days=(period_end - period_start).days)
        previous_start, previous_end = _previous_period(period_start, period_end)
        previous = CostPeriod(
            start=previous_start,
            end=previous_end,
            days=(period_end - period_start).days,
        )
        rows = list(accounts) if accounts else []
        report = await aggregate_cost_report_for_context(
            rows=rows, period=period, previous=previous, region=region
        )
        account_id_value = rows[0].account_id if len(rows) == 1 else None
        return CloudCostTrend(
            provider=CloudProviderKind.AWS,
            account_id=account_id_value,
            period_start=period.start,
            period_end=period.end,
            currency=report.currency or "USD",
            daily=[
                CloudServiceCost(
                    service=str(day),
                    amount=_decimal(amount),
                    currency=_unit(unit),
                    provider=CloudProviderKind.AWS,
                    account_id=account_id_value,
                    region=region,
                    extensions={"date": day.isoformat()},
                )
                for day, amount, unit in report.daily
            ],
            source="AWS_COST_EXPLORER",
        )

    async def get_cost_by_service(
        self,
        *,
        accounts: Sequence[_CloudAccountRow],
        period_start: date,
        period_end: date,
        region: Optional[str] = None,
    ) -> List[CloudServiceCost]:
        previous_start, previous_end = _previous_period(period_start, period_end)
        summary = await self.get_cost_summary(
            accounts=accounts,
            period_start=period_start,
            period_end=period_end,
            previous_period_start=previous_start,
            previous_period_end=previous_end,
            region=region,
        )
        return list(summary.by_service)

    # ---- Resources ---------------------------------------------------------

    async def get_resources(
        self,
        *,
        accounts: Sequence[_CloudAccountRow],
        region: Optional[str] = None,
    ) -> List[CloudResource]:
        from app.services.aws.resource_aggregator import (
            aggregate_inventory_for_context,
        )

        rows = list(accounts) if accounts else []
        inventory = await aggregate_inventory_for_context(rows=rows, region=region)
        out: List[CloudResource] = []
        for service_name, payload in inventory.services.items():
            status = payload.get("status", "ok")
            error_code = payload.get("error_code")
            items = payload.get("items", []) or []
            for item in items:
                if not isinstance(item, dict):
                    continue
                composite_id = item.get("composite_id")
                if not composite_id:
                    # Defensive — the aggregator always stamps this,
                    # but a misbehaving service could skip it.
                    composite_id = (
                        f"AWS|{item.get('account_id', 'unknown')}|"
                        f"{item.get('region') or region or 'unknown'}|"
                        f"{service_name}|{item.get('resource_id') or item.get('id') or 'unknown'}"
                    )
                metadata: Dict[str, Any] = dict(item)
                metadata.pop("composite_id", None)
                metadata.pop("account_id", None)
                metadata.pop("account_display_name", None)
                if error_code and status != "ok":
                    metadata["service_error_code"] = error_code
                out.append(
                    CloudResource(
                        provider=CloudProviderKind.AWS,
                        account_id=item.get("account_id", "unknown"),
                        region=item.get("region") or region,
                        resource_type=service_name,
                        resource_id=str(item.get("resource_id") or item.get("id") or composite_id),
                        service=payload.get("service", service_name),
                        name=item.get("name"),
                        state=item.get("state"),
                        composite_id=composite_id,
                        metadata=metadata,
                    )
                )
        return out

    async def get_resource_summary(
        self,
        *,
        accounts: Sequence[_CloudAccountRow],
        region: Optional[str] = None,
    ) -> CloudResourceSummary:
        from app.services.aws.resource_aggregator import (
            aggregate_inventory_for_context,
        )

        rows = list(accounts) if accounts else []
        inventory = await aggregate_inventory_for_context(rows=rows, region=region)
        scope = "all" if len(rows) != 1 else f"acct:{rows[0].account_id}"
        account_id_value: Optional[str] = rows[0].account_id if len(rows) == 1 else None
        return CloudResourceSummary(
            provider=CloudProviderKind.AWS,
            account_id=account_id_value,
            scope=scope,
            region=region,
            services={
                name: dict(payload) for name, payload in inventory.services.items()
            },
            totals_by_account=[
                {"account_id": aid, "display_name": name, "count": count}
                for aid, name, count in inventory.totals_by_account
            ],
            extensions={
                "status": inventory.status,
                "account_errors": [
                    {
                        "account_id": e.account_id,
                        "display_name": e.display_name,
                        "code": e.code,
                        "message": e.message,
                    }
                    for e in inventory.account_errors
                ],
            },
        )

    # ---- Recommendations ---------------------------------------------------

    async def get_recommendations(
        self,
        *,
        accounts: Sequence[_CloudAccountRow],
        days: int,
        region: Optional[str] = None,
    ) -> List[CloudRecommendation]:
        from app.services.aws.optimization_aggregator import (
            aggregate_recommendations_for_context,
        )

        rows = list(accounts) if accounts else []
        report = await aggregate_recommendations_for_context(
            rows=rows, region=region, days=days
        )
        out: List[CloudRecommendation] = []
        for rec in report.recommendations:
            savings = rec.estimated_monthly_savings
            out.append(
                CloudRecommendation(
                    provider=CloudProviderKind.AWS,
                    account_id=rec.account_id,
                    region=rec.region,
                    recommendation_id=rec.recommendation_id,
                    resource_id=rec.resource_id,
                    resource_type=rec.resource_type.value
                    if hasattr(rec.resource_type, "value")
                    else str(rec.resource_type),
                    service=rec.resource_type.value
                    if hasattr(rec.resource_type, "value")
                    else str(rec.resource_type),
                    action=rec.action.value if hasattr(rec.action, "value") else str(rec.action),
                    title=rec.title,
                    estimated_savings=_decimal(savings) if savings is not None else None,
                    currency=rec.currency or "USD",
                    source=rec.primary_source.value
                    if hasattr(rec.primary_source, "value")
                    else str(rec.primary_source),
                    extensions={
                        "account_display_name": rec.account_display_name,
                        "confidence": rec.confidence.value
                        if hasattr(rec.confidence, "value")
                        else str(rec.confidence),
                        "data_quality": rec.data_quality,
                        "savings_percentage": float(rec.savings_percentage)
                        if rec.savings_percentage is not None
                        else None,
                        "restart_needed": rec.restart_needed,
                        "rollback_possible": rec.rollback_possible,
                        "reason_codes": list(rec.reason_codes),
                        "finding": rec.finding,
                    },
                )
            )
        return out


# ---------------------------------------------------------------------------
# Internal: bridge that threads a short-lived session to ``resolve_status``.
#
# The Phase 7B ``resolve_status`` helper takes a SQLAlchemy ``Session``
# as its first positional argument so the same call can be reused
# from HTTP routes (which own a request session) and from background
# jobs (which mint their own).  The adapter's ``validate_account``
# only has an ORM row in hand; this bridge keeps the row attached
# to a transient session so the existing helper can be called
# unchanged.  We never mutate the row here, so the bridge does not
# need to commit.
# ---------------------------------------------------------------------------


class _AdapterStatusBridge:
    """Wrap an :class:`_CloudAccountRow` with a transient session for
    :func:`app.services.aws.account_provider.resolve_status`.
    """

    def __init__(self, row: _CloudAccountRow) -> None:
        self._row = row

    def resolve(self):
        from app.db.session import SessionLocal
        from app.services.aws.account_provider import resolve_status as _resolve_status

        with SessionLocal() as session:
            # Re-fetch the row in this session so attribute access
            # uses the bound session (SQLAlchemy raises on a
            # detached instance when lazy-load columns are
            # requested).
            attached = session.merge(self._row)
            return _resolve_status(session, attached)


# ---------------------------------------------------------------------------
# Self-registration
# ---------------------------------------------------------------------------


def _register() -> None:
    """Register this adapter with the singleton :class:`ProviderRegistry`.

    Called at module import time so ``app.services.cloud.aws`` being
    imported is enough to wire the adapter.  The AWS adapter is
    always enabled today — there is no operator-facing toggle yet.
    """
    registry = ProviderRegistry.current()
    registry.register(AWSCloudProvider())


_register()


__all__ = ["AWSCloudProvider"]
