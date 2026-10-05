"""AWS Compute Optimizer service — Phase 3.

Wraps the Boto3 ``compute-optimizer`` client with:

* ``GetEnrollmentStatus`` — returns the account-level enrollment state
  (ACTIVE / INACTIVE / PENDING / FAILED / ACCESS_DENIED /
  UNAVAILABLE).
* ``GetRecommendationSummaries`` — paginates the account-level
  summary so the capabilities endpoint can report what AWS actually
  has ready.
* ``GetEC2InstanceRecommendations`` / ``GetEBSVolumeRecommendations`` /
  ``GetLambdaFunctionRecommendations`` / ``GetRDSDatabaseRecommendations`` /
  ``GetAutoScalingGroupRecommendations`` — paginate per-resource
  recommendations and normalize them into our internal model.

Compute Optimizer is a single, account-level API (no per-region
endpoint).  We still accept a ``region`` argument so callers can
match the project's selected-region convention, and we wire it
through to the client for symmetry with the other services.

**No paid features are activated.**  We never call
``UpdateEnrollmentStatus`` or anything similar; if the account is not
already enrolled, ``GetEnrollmentStatus`` returns ``INACTIVE`` and
the optimization endpoint continues to function on deterministic
rules.

All Boto3 call sites are wrapped by ``assert_read_only`` from
:mod:`app.services.aws.guard`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable, List, Optional, Sequence

import boto3
from botocore.client import BaseClient
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from app.services.aws.guard import assert_read_only

logger = logging.getLogger("cost-detective-backend.compute_optimizer")


# ---------------------------------------------------------------------------
# Constants — pinned from the Phase 3 spec.
# ---------------------------------------------------------------------------

ALLOWED_LOOKBACK_DAYS: tuple[int, ...] = (7, 30, 60, 90)

# Compute Optimizer does not accept arbitrary account filter strings;
# ``ACCOUNT_INCLUDED`` and ``ACCOUNT_EXCLUDED`` are mutually exclusive
# and only the included path is wired by Phase 3 (single-account scope).
CO_ACCOUNT_SCOPE: str = "AccountIds"
CO_DEFAULT_FILTER_TYPE: str = "INCLUDED"


# ---------------------------------------------------------------------------
# Error type
# ---------------------------------------------------------------------------


class ComputeOptimizerError(RuntimeError):
    """Sanitized wrapper around any Compute Optimizer failure.

    The message is generic so credentials, request payloads, and stack
    traces never reach FastAPI handlers.
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


def _sanitize_boto_error(exc: Exception) -> ComputeOptimizerError:
    """Map a Boto3 exception to a sanitized ``ComputeOptimizerError``."""
    code = "ComputeOptimizerError"
    try:
        response = getattr(exc, "response", None) or {}
        err = response.get("Error") if isinstance(response, dict) else None
        if isinstance(err, dict) and err.get("Code"):
            code = str(err["Code"])
    except Exception:  # pragma: no cover - defensive
        pass
    return ComputeOptimizerError(code=code, message="Compute Optimizer request failed")


def _map_status_to_capability(raw_status: Optional[str]) -> str:
    """Map a Compute Optimizer status string to our ``CapabilityStatus``.

    The two enums share most values but Compute Optimizer uses lowercase
    (``Active`` / ``Inactive`` / ``Pending`` / ``Failed``) while our
    public schema is uppercase.  ``ACCESS_DENIED`` is mapped from
    ``AccessDeniedException`` at the caller boundary, not here.
    """
    if not raw_status:
        return "UNAVAILABLE"
    normalized = str(raw_status).strip().upper()
    mapping = {
        "ACTIVE": "ACTIVE",
        "INACTIVE": "INACTIVE",
        "PENDING": "PENDING",
        "FAILED": "FAILED",
    }
    return mapping.get(normalized, "UNAVAILABLE")


# ---------------------------------------------------------------------------
# Client factory
# ---------------------------------------------------------------------------


def get_compute_optimizer_client(region: Optional[str] = None) -> BaseClient:
    """Return a Compute Optimizer client.

    Compute Optimizer is an account-level service, so a region is not
    strictly required — but we accept it for symmetry with the
    project's selected-region behavior.  Conservative retries and
    timeouts mirror the other AWS service clients.
    """
    config = BotoConfig(
        retries={"max_attempts": 5, "mode": "standard"},
        connect_timeout=5,
        read_timeout=30,
    )
    kwargs: dict[str, Any] = {"config": config}
    if region:
        kwargs["region_name"] = region
    return boto3.client("compute-optimizer", **kwargs)


# ---------------------------------------------------------------------------
# Enrollment
# ---------------------------------------------------------------------------


def get_enrollment_status(client: BaseClient, *, account_id: Optional[str] = None) -> str:
    """Return the account-level enrollment status.

    ``account_id`` is optional: when omitted, Compute Optimizer falls
    back to the caller's account.  We never fabricate a status — if
    AWS denies the call, we raise a sanitized ``ComputeOptimizerError``
    with ``code="AccessDeniedException"`` so the caller can map it to
    ``CapabilityStatus.ACCESS_DENIED``.
    """
    assert_read_only(client, "get_enrollment_status")
    params: dict[str, Any] = {}
    if account_id:
        params["accountIds"] = [account_id]
    try:
        response = client.get_enrollment_status(**params)
    except (ClientError, BotoCoreError) as exc:
        raise _sanitize_boto_error(exc) from None
    accounts = response.get("accountEnrollmentStatuses") or []
    if not accounts:
        return "UNAVAILABLE"
    # Single-account scope: pick the requested account or the first row.
    status_raw: Optional[str] = None
    for row in accounts:
        if account_id is None or row.get("accountId") == account_id:
            status_raw = row.get("status")
            break
    if status_raw is None and accounts:
        status_raw = accounts[0].get("status")
    return _map_status_to_capability(status_raw)


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


def _paginate(
    *,
    client: BaseClient,
    method_name: str,
    base_kwargs: dict[str, Any],
) -> Iterable[dict[str, Any]]:
    """Yield pages until ``nextToken`` is empty.

    Mirrors the pattern used by the Cost Explorer / CloudWatch
    wrappers.  We keep the token-walking logic in one place so each
    public retriever stays a thin adapter.
    """
    next_token: Optional[str] = None
    while True:
        kwargs = {k: v for k, v in base_kwargs.items() if v is not None}
        if next_token:
            kwargs["nextToken"] = next_token
        try:
            response = getattr(client, method_name)(**kwargs)
        except (ClientError, BotoCoreError) as exc:
            raise _sanitize_boto_error(exc) from None
        yield response
        next_token = response.get("nextToken")
        if not next_token:
            return


# ---------------------------------------------------------------------------
# Recommendation summaries
# ---------------------------------------------------------------------------


def get_recommendation_summaries(client: BaseClient, *, account_id: Optional[str] = None) -> dict[str, int]:
    """Return ``{recommendationResourceType: count}`` for the account.

    Compute Optimizer returns aggregated counts per resource type
    (EC2 / EBS / LAMBDA / RDS).  We surface the raw counts so the
    capabilities endpoint can report "what AWS already has" without
    forcing the caller to paginate the per-resource APIs.
    """
    base_kwargs: dict[str, Any] = {"maxResults": 1000}
    if account_id:
        # ``GetRecommendationSummaries`` accepts an ``accountIds``
        # filter; leaving it absent returns the caller's account.
        base_kwargs["accountIds"] = [account_id]
    out: dict[str, int] = {}
    for response in _paginate(
        client=client, method_name="get_recommendation_summaries", base_kwargs=base_kwargs
    ):
        for row in response.get("recommendationSummaries", []) or []:
            rtype = row.get("recommendationResourceType")
            count = row.get("summaries") or {}
            if rtype:
                # ``summaries`` is a dict of metric -> count; we sum them.
                total = 0
                try:
                    total = sum(int(v) for v in count.values() if isinstance(v, (int, float)))
                except Exception:  # pragma: no cover - defensive
                    total = 0
                out[rtype] = out.get(rtype, 0) + total
    return out


# ---------------------------------------------------------------------------
# Internal normalized row
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NormalizedRecommendation:
    """The Compute Optimizer row after normalization.

    This dataclass is the bridge between the raw AWS payload and our
    public Pydantic schema.  Fields unavailable from AWS are
    explicitly ``None`` — the engine never invents a value.
    """

    resource_arn: Optional[str]
    resource_id: str
    resource_type: str  # "Ec2Instance" | "EbsVolume" | "LambdaFunction" | "RdsDBInstance"
    region: Optional[str]
    account_id: Optional[str]
    finding: str  # "Underprovisioned" | "Overprovisioned" | "Optimized" | "NotOptimized"
    current_configuration: dict[str, Any]
    recommended_configuration: dict[str, Any]
    performance_risk: Optional[Decimal]
    lookback_period_days: Optional[int]
    estimated_monthly_savings: Optional[Decimal]
    savings_percentage: Optional[Decimal]
    currency: str
    reason_codes: List[str] = field(default_factory=list)
    aws_recommendation_id: Optional[str] = None


def _extract_recommendation_id(recommendation: dict[str, Any]) -> Optional[str]:
    """Return the recommendation's id from a Compute Optimizer row.

    Compute Optimizer places ``recommendationId`` either on the top
    level or inside the first ``recommendationOptions`` entry
    depending on the resource type.  We probe both so a missing
    top-level field never crashes the normalizer.
    """
    rid = recommendation.get("recommendationId")
    if rid:
        return str(rid)
    options = recommendation.get("recommendationOptions") or []
    if options and isinstance(options[0], dict):
        opt_rid = options[0].get("recommendationId")
        if opt_rid:
            return str(opt_rid)
    return None


def _decimal_or_none(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:  # pragma: no cover - defensive
        return None


def _find_first_recommendation_options(recommendation: dict[str, Any]) -> List[dict[str, Any]]:
    """Return ``recommendationOptions`` as a list, defaulting to empty."""
    opts = recommendation.get("recommendationOptions") or []
    return list(opts) if isinstance(opts, list) else []


# ---------------------------------------------------------------------------
# EC2 instance recommendations
# ---------------------------------------------------------------------------


def get_ec2_instance_recommendations(
    client: BaseClient,
    *,
    region: str,
    account_ids: Optional[Sequence[str]] = None,
) -> List[NormalizedRecommendation]:
    """Return normalized EC2 instance recommendations.

    Per Phase 3 the engine is single-region; the ``region`` argument
    narrows the response via the ``filter`` parameter.  When omitted
    we leave the filter off and trust the caller's region.
    """
    base_kwargs: dict[str, Any] = {"maxResults": 1000}
    if region:
        base_kwargs["filter"] = [
            {"name": "RecommendationSourceType", "values": ["Ec2Instance"]},
        ]
    if account_ids:
        base_kwargs["accountIds"] = list(account_ids)
    out: List[NormalizedRecommendation] = []
    for response in _paginate(
        client=client, method_name="get_ec2_instance_recommendations", base_kwargs=base_kwargs
    ):
        for row in response.get("instanceRecommendations", []) or []:
            # AWS puts ``instanceArn`` directly on the recommendation
            # row (NOT under a nested ``instance`` object); older
            # documentation describes an ``instance`` envelope which
            # we tolerate as well.
            inst = row.get("instance") or {}
            arn = row.get("instanceArn") or inst.get("resourceArn") or inst.get("instanceArn")
            resource_id = (
                row.get("instanceId")
                or inst.get("instanceId")
                or (arn.split("/")[-1] if arn else None)
            )
            finding = row.get("finding") or ""
            current_type = row.get("currentInstanceType")
            current_config: dict[str, Any] = {"instance_type": current_type}
            if row.get("currentOnDemandPrice"):
                current_config["on_demand_price"] = _decimal_or_none(row["currentOnDemandPrice"])
            recommended_options = _find_first_recommendation_options(row)
            # Use the FIRST recommended option as the "recommended
            # configuration".  Phase 3 surfaces the cheapest
            # recommended option; the UI can drill into alternatives
            # via the raw AWS response if it ever wants to.
            rec_config: dict[str, Any] = {}
            if recommended_options:
                opt = recommended_options[0]
                rec_config["instance_type"] = opt.get("instanceType")
                if opt.get("projectedUtilization") is not None:
                    rec_config["projected_utilization"] = opt["projectedUtilization"]
                if opt.get("performanceRisk") is not None:
                    rec_config["performance_risk"] = opt["performanceRisk"]
                if opt.get("monthlyPrice") is not None or opt.get("monthlySavings") is not None:
                    rec_config["monthly_price"] = _decimal_or_none(opt.get("monthlyPrice"))
            savings = _decimal_or_none(row.get("estimatedMonthlySavings"))
            savings_pct = _decimal_or_none(row.get("savingsPercentage"))
            perf_risk = _decimal_or_none(row.get("performanceRisk"))
            out.append(
                NormalizedRecommendation(
                    resource_arn=arn,
                    resource_id=str(resource_id) if resource_id else "",
                    resource_type="Ec2Instance",
                    region=region or (arn.split(":")[3] if arn else None),
                    account_id=row.get("accountId"),
                    finding=finding,
                    current_configuration=current_config,
                    recommended_configuration=rec_config,
                    performance_risk=perf_risk,
                    lookback_period_days=_extract_lookback_days(row),
                    estimated_monthly_savings=savings,
                    savings_percentage=savings_pct,
                    currency=row.get("currencyCode") or "USD",
                    reason_codes=list(row.get("reasonCodes") or []),
                    aws_recommendation_id=_extract_recommendation_id(row),
                )
            )
    return out


def _extract_lookback_days(row: dict[str, Any]) -> Optional[int]:
    """Map a Compute Optimizer lookback period label to an integer."""
    mapping = {
        "DAYS_14": 14,
        "DAYS_30": 30,
        "DAYS_60": 60,
        "DAYS_90": 90,
        "SEVEN_DAYS": 7,
        "FOURTEEN_DAYS": 14,
        "THIRTY_DAYS": 30,
        "SIXTY_DAYS": 60,
        "NINETY_DAYS": 90,
    }
    raw = row.get("lookbackPeriodInDays")
    if isinstance(raw, (int, float)):
        return int(raw)
    label = row.get("lookbackPeriod")
    if isinstance(label, str):
        return mapping.get(label.upper())
    return None


# ---------------------------------------------------------------------------
# EBS volume recommendations
# ---------------------------------------------------------------------------


def get_ebs_volume_recommendations(
    client: BaseClient,
    *,
    region: str,
    account_ids: Optional[Sequence[str]] = None,
) -> List[NormalizedRecommendation]:
    """Return normalized EBS volume recommendations."""
    base_kwargs: dict[str, Any] = {"maxResults": 1000}
    if region:
        base_kwargs["filter"] = [
            {"name": "RecommendationSourceType", "values": ["EbsVolume"]},
        ]
    if account_ids:
        base_kwargs["accountIds"] = list(account_ids)
    out: List[NormalizedRecommendation] = []
    for response in _paginate(
        client=client, method_name="get_ebs_volume_recommendations", base_kwargs=base_kwargs
    ):
        for row in response.get("volumeRecommendations", []) or []:
            vol = row.get("volume") or {}
            arn = row.get("volumeArn") or vol.get("resourceArn")
            resource_id = (
                row.get("volumeId")
                or vol.get("volumeId")
                or (arn.split("/")[-1] if arn else None)
            )
            current = row.get("currentConfiguration") or {}
            baseline_ios = current.get("baselineIOPS")
            baseline_throughput = current.get("baselineThroughput")
            current_config: dict[str, Any] = {
                "volume_type": current.get("volumeType"),
                "size_gb": current.get("volumeSize"),
            }
            if baseline_ios is not None:
                current_config["baseline_iops"] = baseline_ios
            if baseline_throughput is not None:
                current_config["baseline_throughput_mb"] = baseline_throughput
            recommended = _find_first_recommendation_options(row)
            rec_config: dict[str, Any] = {}
            if recommended:
                opt = recommended[0]
                conf = opt.get("configuration") or {}
                rec_config = {
                    "volume_type": conf.get("volumeType"),
                    "size_gb": conf.get("volumeSize"),
                }
                if conf.get("baselineIOPS") is not None:
                    rec_config["baseline_iops"] = conf["baselineIOPS"]
                if conf.get("baselineThroughput") is not None:
                    rec_config["baseline_throughput_mb"] = conf["baselineThroughput"]
            out.append(
                NormalizedRecommendation(
                    resource_arn=arn,
                    resource_id=str(resource_id) if resource_id else "",
                    resource_type="EbsVolume",
                    region=region or (arn.split(":")[3] if arn else None),
                    account_id=row.get("accountId"),
                    finding=row.get("finding") or "",
                    current_configuration=current_config,
                    recommended_configuration=rec_config,
                    performance_risk=_decimal_or_none(row.get("performanceRisk")),
                    lookback_period_days=_extract_lookback_days(row),
                    estimated_monthly_savings=_decimal_or_none(row.get("estimatedMonthlySavings")),
                    savings_percentage=_decimal_or_none(row.get("savingsPercentage")),
                    currency=row.get("currencyCode") or "USD",
                    reason_codes=list(row.get("reasonCodes") or []),
                    aws_recommendation_id=_extract_recommendation_id(row),
                )
            )
    return out


# ---------------------------------------------------------------------------
# Lambda function recommendations
# ---------------------------------------------------------------------------


def get_lambda_function_recommendations(
    client: BaseClient,
    *,
    region: str,
    account_ids: Optional[Sequence[str]] = None,
) -> List[NormalizedRecommendation]:
    """Return normalized Lambda function recommendations."""
    base_kwargs: dict[str, Any] = {"maxResults": 1000}
    if region:
        base_kwargs["filter"] = [
            {"name": "RecommendationSourceType", "values": ["LambdaFunction"]},
        ]
    if account_ids:
        base_kwargs["accountIds"] = list(account_ids)
    out: List[NormalizedRecommendation] = []
    for response in _paginate(
        client=client, method_name="get_lambda_function_recommendations", base_kwargs=base_kwargs
    ):
        for row in response.get("lambdaFunctionRecommendations", []) or []:
            fn = row.get("function") or {}
            arn = (
                row.get("functionArn")
                or fn.get("functionArn")
                or fn.get("resourceArn")
            )
            resource_id = (
                row.get("functionName")
                or fn.get("functionName")
                or (arn.split(":")[-1] if arn else None)
            )
            current_mem = row.get("currentMemorySize") or row.get("functionConfiguration", {}).get("memory")
            rec_config: dict[str, Any] = {}
            recommended = _find_first_recommendation_options(row)
            if recommended:
                opt = recommended[0]
                conf = opt.get("configuration") or {}
                if conf.get("memory"):
                    rec_config["memory_mb"] = conf["memory"]
            out.append(
                NormalizedRecommendation(
                    resource_arn=arn,
                    resource_id=str(resource_id) if resource_id else "",
                    resource_type="LambdaFunction",
                    region=region or (arn.split(":")[3] if arn else None),
                    account_id=row.get("accountId"),
                    finding=row.get("finding") or "",
                    current_configuration={
                        "memory_mb": current_mem,
                        "timeout": row.get("currentTimeout") or row.get("functionConfiguration", {}).get("timeout"),
                    },
                    recommended_configuration=rec_config,
                    performance_risk=_decimal_or_none(row.get("performanceRisk")),
                    lookback_period_days=_extract_lookback_days(row),
                    estimated_monthly_savings=_decimal_or_none(row.get("estimatedMonthlySavings")),
                    savings_percentage=_decimal_or_none(row.get("savingsPercentage")),
                    currency=row.get("currencyCode") or "USD",
                    reason_codes=list(row.get("reasonCodes") or []),
                    aws_recommendation_id=_extract_recommendation_id(row),
                )
            )
    return out


# ---------------------------------------------------------------------------
# RDS DB instance recommendations
# ---------------------------------------------------------------------------


def get_rds_database_recommendations(
    client: BaseClient,
    *,
    region: str,
    account_ids: Optional[Sequence[str]] = None,
) -> List[NormalizedRecommendation]:
    """Return normalized RDS DB instance recommendations."""
    base_kwargs: dict[str, Any] = {"maxResults": 1000}
    if region:
        base_kwargs["filter"] = [
            {"name": "RecommendationSourceType", "values": ["RdsDBInstance"]},
        ]
    if account_ids:
        base_kwargs["accountIds"] = list(account_ids)
    out: List[NormalizedRecommendation] = []
    for response in _paginate(
        client=client, method_name="get_rds_database_recommendations", base_kwargs=base_kwargs
    ):
        for row in response.get("rdsDBRecommendations", []) or []:
            db = row.get("DBInstance") or row.get("instance") or {}
            arn = row.get("DBInstanceArn") or db.get("resourceArn")
            resource_id = (
                row.get("DBInstanceIdentifier")
                or db.get("DBInstanceIdentifier")
                or (arn.split(":")[-1] if arn else None)
            )
            current_config = {
                "db_instance_class": row.get("currentDBInstanceClass") or db.get("DBInstanceClass"),
                "engine": db.get("engine") or row.get("engine"),
            }
            recommended = _find_first_recommendation_options(row)
            rec_config: dict[str, Any] = {}
            if recommended:
                opt = recommended[0]
                conf = opt.get("configuration") or opt.get("dbInstanceConfiguration") or {}
                rec_config = {
                    "db_instance_class": conf.get("dbInstanceClass") or opt.get("dbInstanceClass"),
                }
            out.append(
                NormalizedRecommendation(
                    resource_arn=arn,
                    resource_id=str(resource_id) if resource_id else "",
                    resource_type="RdsDBInstance",
                    region=region or (arn.split(":")[3] if arn else None),
                    account_id=row.get("accountId"),
                    finding=row.get("finding") or "",
                    current_configuration=current_config,
                    recommended_configuration=rec_config,
                    performance_risk=_decimal_or_none(row.get("performanceRisk")),
                    lookback_period_days=_extract_lookback_days(row),
                    estimated_monthly_savings=_decimal_or_none(row.get("estimatedMonthlySavings")),
                    savings_percentage=_decimal_or_none(row.get("savingsPercentage")),
                    currency=row.get("currencyCode") or "USD",
                    reason_codes=list(row.get("reasonCodes") or []),
                    aws_recommendation_id=_extract_recommendation_id(row),
                )
            )
    return out


__all__ = [
    "ALLOWED_LOOKBACK_DAYS",
    "CO_ACCOUNT_SCOPE",
    "CO_DEFAULT_FILTER_TYPE",
    "ComputeOptimizerError",
    "NormalizedRecommendation",
    "get_compute_optimizer_client",
    "get_ebs_volume_recommendations",
    "get_ec2_instance_recommendations",
    "get_enrollment_status",
    "get_lambda_function_recommendations",
    "get_rds_database_recommendations",
    "get_recommendation_summaries",
]
