"""AWS CloudWatch metrics service — Phase 2.

Wraps the Boto3 ``cloudwatch`` ``GetMetricData`` API with:

* per-resource metric specs for EC2, RDS, Lambda, ALB, and NLB
  (Phase 1's ELBv2 enumerator already exposes ``LoadBalancerArn`` as
  the CloudWatch dimension — no Phase 1 surface change needed);
* period selection from the spec: ``{7:3600, 30:21600, 60:43200,
  90:86400}`` seconds;
* batched ``GetMetricData`` calls under the 500-query AWS limit,
  walking ``NextToken`` to completion;
* a ``classify_data_quality`` helper that returns one of
  ``high | medium | low | no_data`` based on datapoint count and
  time-window coverage — never inventing zero utilization from
  missing datapoints;
* sanitized ``CloudWatchError`` so credential text never reaches
  FastAPI handlers.

All AWS calls go through the read-only guard in
:mod:`app.services.aws.guard`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Iterable, List, Optional, Sequence, Tuple

import boto3
from botocore.client import BaseClient
from botocore.config import Config as BotoConfig

from app.services.aws.guard import assert_read_only


# ---------------------------------------------------------------------------
# Constants — pinned from the Phase 2 spec.
# ---------------------------------------------------------------------------

# Maximum number of MetricDataQuery objects per GetMetricData call.
AWS_GET_METRIC_DATA_MAX_QUERIES = 500

# Lookback -> aggregation period mapping, from the spec.
PERIOD_BY_LOOKBACK_DAYS: dict[int, int] = {
    7: 3600,
    30: 21600,
    60: 43200,
    90: 86400,
}

ALLOWED_LOOKBACK_DAYS: Tuple[int, ...] = (7, 30, 60, 90)


# ---------------------------------------------------------------------------
# Data-quality enum + classifier
# ---------------------------------------------------------------------------


class DataQuality(str, Enum):
    """How much CloudWatch coverage we got back for a metric.

    Phase 2 spec: missing datapoints are reported as ``no_data`` —
    never as a metric value of 0.  ``high / medium / low`` express
    partial coverage and are determined by datapoint count vs the
    expected number of datapoints given the lookback and period.
    """

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NO_DATA = "no_data"


def classify_data_quality(
    *,
    datapoint_count: int,
    lookback_days: int,
    period_seconds: int,
) -> DataQuality:
    """Return the data-quality bucket for a single metric.

    Heuristic:

    * ``no_data`` when no datapoints came back.
    * ``high`` when >= 80% of expected datapoints are present.
    * ``medium`` when >= 40%.
    * ``low`` otherwise.

    Expected count = ``(lookback_days * 86400) / period_seconds``
    (floored).  ``lookback_days`` must be in
    :data:`ALLOWED_LOOKBACK_DAYS`.
    """
    if datapoint_count <= 0:
        return DataQuality.NO_DATA
    expected = max(1, (lookback_days * 86400) // period_seconds)
    coverage = datapoint_count / expected
    if coverage >= 0.8:
        return DataQuality.HIGH
    if coverage >= 0.4:
        return DataQuality.MEDIUM
    return DataQuality.LOW


# ---------------------------------------------------------------------------
# Error type
# ---------------------------------------------------------------------------


class CloudWatchError(RuntimeError):
    """Sanitized wrapper for any CloudWatch failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


def _sanitize_boto_error(exc: Exception) -> CloudWatchError:
    code = "CloudWatchError"
    try:
        response = getattr(exc, "response", None) or {}
        err = response.get("Error") if isinstance(response, dict) else None
        if isinstance(err, dict) and err.get("Code"):
            code = str(err["Code"])
    except Exception:  # pragma: no cover - defensive
        pass
    return CloudWatchError(code=code, message="CloudWatch request failed")


# ---------------------------------------------------------------------------
# Client factory
# ---------------------------------------------------------------------------


def get_cloudwatch_client(region: Optional[str] = None) -> BaseClient:
    config = BotoConfig(
        retries={"max_attempts": 5, "mode": "standard"},
        connect_timeout=5,
        read_timeout=30,
    )
    kwargs: dict[str, Any] = {"config": config}
    if region:
        kwargs["region_name"] = region
    return boto3.client("cloudwatch", **kwargs)


# ---------------------------------------------------------------------------
# Per-resource metric specs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MetricSpec:
    """One CloudWatch metric to fetch for a resource.

    ``dimension_name`` is the CloudWatch dimension key for this
    resource (e.g. ``InstanceId`` for EC2).  ``dimension_value`` is
    the runtime value supplied by the Phase 1 enumerator.
    """

    namespace: str
    metric_name: str
    statistic: str  # Average | Sum | Maximum | Minimum | SampleCount
    dimension_name: str
    dimension_value: str


@dataclass(frozen=True)
class ResourceDescriptor:
    """Minimal resource description Phase 1 already produces.

    The utilization route passes these to ``build_metric_queries`` to
    decide which metrics to fetch.
    """

    resource_id: str
    resource_type: str  # "ec2" | "rds" | "lambda" | "alb" | "nlb"
    region: str


# Specs grouped by resource_type.  All dimensions come from the
# resource_id field (or, for ALB/NLB, the LoadBalancerArn that Phase 1
# already exposes).
_METRIC_SPECS_BY_TYPE: dict[str, tuple[tuple[str, str], ...]] = {
    "ec2": (
        ("AWS/EC2", "CPUUtilization", "Average", "InstanceId"),
        ("AWS/EC2", "CPUUtilization", "Maximum", "InstanceId"),
        ("AWS/EC2", "NetworkIn", "Sum", "InstanceId"),
        ("AWS/EC2", "NetworkOut", "Sum", "InstanceId"),
    ),
    "rds": (
        ("AWS/RDS", "CPUUtilization", "Average", "DBInstanceIdentifier"),
        ("AWS/RDS", "CPUUtilization", "Maximum", "DBInstanceIdentifier"),
        ("AWS/RDS", "DatabaseConnections", "Average", "DBInstanceIdentifier"),
        ("AWS/RDS", "DatabaseConnections", "Maximum", "DBInstanceIdentifier"),
        ("AWS/RDS", "FreeableMemory", "Average", "DBInstanceIdentifier"),
        ("AWS/RDS", "FreeableMemory", "Minimum", "DBInstanceIdentifier"),
    ),
    "lambda": (
        ("AWS/Lambda", "Invocations", "Sum", "FunctionName"),
        ("AWS/Lambda", "Duration", "Average", "FunctionName"),
        ("AWS/Lambda", "Duration", "Maximum", "FunctionName"),
        ("AWS/Lambda", "Errors", "Sum", "FunctionName"),
        ("AWS/Lambda", "Throttles", "Sum", "FunctionName"),
    ),
    "alb": (
        ("AWS/ApplicationELB", "RequestCount", "Sum", "LoadBalancerArn"),
        ("AWS/ApplicationELB", "ProcessedBytes", "Sum", "LoadBalancerArn"),
    ),
    "nlb": (
        ("AWS/NetworkELB", "ProcessedBytes", "Sum", "LoadBalancerArn"),
    ),
}


def _resource_dimension_value(resource: ResourceDescriptor) -> str:
    """Return the dimension value for a resource (always resource_id)."""
    # Phase 1's ELBv2 enumerator uses the LoadBalancerArn as the
    # resource id — which is exactly what CloudWatch expects for ALB
    # and NLB.  So the dimension value is always ``resource_id``.
    return resource.resource_id


def specs_for_resource(resource: ResourceDescriptor) -> List[MetricSpec]:
    """Return the list of ``MetricSpec`` rows for a single resource."""
    raw = _METRIC_SPECS_BY_TYPE.get(resource.resource_type, ())
    dim_value = _resource_dimension_value(resource)
    return [
        MetricSpec(
            namespace=ns,
            metric_name=metric,
            statistic=stat,
            dimension_name=dim_name,
            dimension_value=dim_value,
        )
        for (ns, metric, stat, dim_name) in raw
    ]


# ---------------------------------------------------------------------------
# Query building
# ---------------------------------------------------------------------------


@dataclass
class MetricQuery:
    """One ``MetricDataQuery`` row, post-mapping to AWS shape.

    ``id`` is a short, stable identifier that the route can use to
    reverse-map CloudWatch results back to (resource, metric, statistic).
    """

    id: str
    metric_spec: MetricSpec


def _make_query_id(resource_id: str, spec: MetricSpec) -> str:
    # CloudWatch ids are restricted to [A-Za-z0-9_].  Replace any other
    # characters with ``_`` so ARNs (which contain ``:`` and ``/``) are safe.
    safe_resource = "".join(c if c.isalnum() or c == "_" else "_" for c in resource_id)
    safe_metric = "".join(c if c.isalnum() or c == "_" else "_" for c in spec.metric_name)
    return f"{safe_resource}__{safe_metric}__{spec.statistic}"[:200]


def build_metric_queries(
    resources: Iterable[ResourceDescriptor],
) -> List[MetricQuery]:
    """Return the flat list of MetricDataQuery objects for these resources."""
    out: List[MetricQuery] = []
    for resource in resources:
        for spec in specs_for_resource(resource):
            out.append(MetricQuery(id=_make_query_id(resource.resource_id, spec), metric_spec=spec))
    return out


def to_aws_query_payload(
    queries: Sequence[MetricQuery],
    *,
    start: datetime,
    end: datetime,
    period_seconds: int,
) -> List[dict[str, Any]]:
    """Convert our internal ``MetricQuery`` rows to AWS request shape."""
    iso_start = start.astimezone(timezone.utc).isoformat()
    iso_end = end.astimezone(timezone.utc).isoformat()
    out: List[dict[str, Any]] = []
    for q in queries:
        spec = q.metric_spec
        out.append(
            {
                "Id": q.id,
                "MetricStat": {
                    "Metric": {
                        "Namespace": spec.namespace,
                        "MetricName": spec.metric_name,
                        "Dimensions": [
                            {"Name": spec.dimension_name, "Value": spec.dimension_value},
                        ],
                    },
                    "Period": period_seconds,
                    "Stat": spec.statistic,
                },
                "ReturnData": True,
                # Defensive: these mirror the top-level Start/End so
                # per-query time ranges are explicit even if AWS ever
                # allows them to differ.
                "StartTime": iso_start,
                "EndTime": iso_end,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Batched retrieval
# ---------------------------------------------------------------------------


def batch_query(
    client: BaseClient,
    queries: Sequence[MetricQuery],
    *,
    start: datetime,
    end: datetime,
    period_seconds: int,
    batch_size: int = AWS_GET_METRIC_DATA_MAX_QUERIES,
) -> List[dict[str, Any]]:
    """Execute ``GetMetricData`` in chunks of ``batch_size`` queries.

    Returns the concatenated list of ``MetricDataResult`` rows (each
    carrying ``Id``, ``Label``, ``Timestamps``, and ``Values``).
    Walks ``NextToken`` on every call.

    Raises :class:`CloudWatchError` on any Boto3 error — the caller
    is expected to map the failure into a structured warning so a
    single CloudWatch failure does not invalidate other sources.
    """
    assert_read_only(client, "get_metric_data")
    if not queries:
        return []
    payload = to_aws_query_payload(queries, start=start, end=end, period_seconds=period_seconds)
    out: List[dict[str, Any]] = []
    for chunk_start in range(0, len(payload), batch_size):
        chunk = payload[chunk_start : chunk_start + batch_size]
        next_token: Optional[str] = None
        while True:
            kwargs: dict[str, Any] = {
                "MetricDataQueries": chunk,
                "StartTime": start,
                "EndTime": end,
            }
            if next_token:
                kwargs["NextPageToken"] = next_token
            try:
                response = client.get_metric_data(**kwargs)
            except Exception as exc:
                raise _sanitize_boto_error(exc) from None
            out.extend(response.get("MetricDataResults", []) or [])
            next_token = response.get("NextPageToken")
            if not next_token:
                break
    return out


# ---------------------------------------------------------------------------
# Result aggregation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MetricDatapoint:
    timestamp: datetime
    value: Decimal


@dataclass
class MetricSeries:
    """The CloudWatch data we received for one (resource, metric, statistic)."""

    query_id: str
    resource_id: str
    resource_type: str
    namespace: str
    metric_name: str
    statistic: str
    unit: str
    datapoints: List[MetricDatapoint] = field(default_factory=list)
    data_quality: DataQuality = DataQuality.NO_DATA

    @property
    def has_data(self) -> bool:
        return bool(self.datapoints)


def aggregate_results(
    results: List[dict[str, Any]],
    *,
    queries: Sequence[MetricQuery],
    resources_by_id: dict[str, ResourceDescriptor],
    lookback_days: int,
    period_seconds: int,
) -> List[MetricSeries]:
    """Map ``MetricDataResult`` rows back to per-resource metric series."""
    series_by_id: dict[str, MetricSeries] = {}
    queries_by_id = {q.id: q for q in queries}
    for result in results:
        query_id = result.get("Id", "")
        query = queries_by_id.get(query_id)
        if query is None:
            continue
        spec = query.metric_spec
        series = series_by_id.get(query_id) or MetricSeries(
            query_id=query_id,
            resource_id=spec.dimension_value,
            resource_type=resources_by_id.get(spec.dimension_value, ResourceDescriptor(
                resource_id=spec.dimension_value, resource_type="unknown", region="unknown"
            )).resource_type,
            namespace=spec.namespace,
            metric_name=spec.metric_name,
            statistic=spec.statistic,
            unit="",
        )
        unit = ""
        label = result.get("Label", "")
        if "|" in label:
            unit = label.split("|", 1)[1].strip()
        timestamps = result.get("Timestamps", []) or []
        values = result.get("Values", []) or []
        for ts, val in zip(timestamps, values):
            try:
                series.datapoints.append(
                    MetricDatapoint(
                        timestamp=ts if isinstance(ts, datetime) else datetime.now(timezone.utc),
                        value=Decimal(str(val)),
                    )
                )
            except Exception:  # pragma: no cover - defensive
                continue
        series.unit = unit or series.unit
        series.data_quality = classify_data_quality(
            datapoint_count=len(series.datapoints),
            lookback_days=lookback_days,
            period_seconds=period_seconds,
        )
        series_by_id[query_id] = series

    # Preserve the query ordering so callers can iterate deterministically.
    out: List[MetricSeries] = []
    for q in queries:
        s = series_by_id.get(q.id)
        if s is not None:
            out.append(s)
    return out


# ---------------------------------------------------------------------------
# Aggregation periods
# ---------------------------------------------------------------------------


def period_seconds_for(lookback_days: int) -> int:
    """Return the AWS aggregation period for the lookback, per the spec."""
    if lookback_days not in PERIOD_BY_LOOKBACK_DAYS:
        raise CloudWatchError(
            code="InvalidLookbackDays",
            message=f"lookback_days={lookback_days!r} is not allowed; "
            f"allowed values are {sorted(PERIOD_BY_LOOKBACK_DAYS)}",
        )
    return PERIOD_BY_LOOKBACK_DAYS[lookback_days]


__all__ = [
    "ALLOWED_LOOKBACK_DAYS",
    "AWS_GET_METRIC_DATA_MAX_QUERIES",
    "CloudWatchError",
    "DataQuality",
    "MetricDatapoint",
    "MetricQuery",
    "MetricSeries",
    "MetricSpec",
    "PERIOD_BY_LOOKBACK_DAYS",
    "ResourceDescriptor",
    "aggregate_results",
    "batch_query",
    "build_metric_queries",
    "classify_data_quality",
    "get_cloudwatch_client",
    "period_seconds_for",
    "specs_for_resource",
    "to_aws_query_payload",
]
