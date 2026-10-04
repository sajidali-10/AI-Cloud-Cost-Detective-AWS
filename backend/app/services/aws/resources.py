"""AWS resource enumeration service — Phase 1.

One enumerator per AWS service. Every enumerator returns a
:class:`~app.schemas.aws.ServiceResult` envelope and NEVER raises out;
per-service Boto3 errors are funneled into ``status="denied"`` (for
``AccessDenied``) or ``status="error"`` (anything else). The aggregator
in step 4 wires these into the ``GET /api/aws/resources`` endpoint.

Every AWS call here is read-only (``Describe*`` / ``List*`` /
``GetBucketLocation``). No write/mutation APIs are used.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

from botocore.exceptions import BotoCoreError, ClientError

from app.schemas.aws import (
    EbsVolume,
    Ec2Instance,
    ElasticIp,
    LambdaFunction,
    LoadBalancerV2,
    NatGateway,
    RdsInstance,
    S3Bucket,
    ServiceResult,
)
from app.services.aws.clients import get_aws_client

logger = logging.getLogger("cost-detective-backend.aws.resources")


def _envelope(
    service: str,
    items: Any,
    exc: Optional[BaseException] = None,
) -> ServiceResult:
    """Build a :class:`ServiceResult` from items or an exception."""
    if exc is None:
        return ServiceResult(service=service, status="ok", items=items)
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "ClientError")
        status = "denied" if code == "AccessDenied" else "error"
        # Log the code only; never the raw Boto3 message.
        logger.warning("aws.%s error_code=%s", service, code)
        return ServiceResult(
            service=service, status=status, items=[], error_code=code
        )
    if isinstance(exc, BotoCoreError):
        logger.warning("aws.%s error_code=%s", service, type(exc).__name__)
        return ServiceResult(
            service=service,
            status="error",
            items=[],
            error_code=type(exc).__name__,
        )
    # Unknown exception: still don't propagate.
    logger.warning("aws.%s error_code=%s", service, type(exc).__name__)
    return ServiceResult(
        service=service, status="error", items=[], error_code=type(exc).__name__
    )


def _safe_call(
    service: str,
    func: Callable[[], Any],
) -> ServiceResult:
    """Run ``func`` and funnel any Boto3 exception into the envelope."""
    try:
        items = func()
        return _envelope(service, items)
    except (ClientError, BotoCoreError) as exc:
        return _envelope(service, [], exc=exc)


def _normalize_tags(tag_list: Any) -> Dict[str, str]:
    if not tag_list:
        return {}
    return {t.get("Key"): t.get("Value") for t in tag_list if t.get("Key")}


# --- Per-service enumerators --------------------------------------------


def list_ec2_instances(region: Optional[str]) -> ServiceResult:
    """Enumerate EC2 instances via :code:`describe_instances`."""
    def _call() -> list:
        client = get_aws_client("ec2", region=region)
        items = []
        resp = client.describe_instances()
        for reservation in resp.get("Reservations", []):
            for inst in reservation.get("Instances", []):
                items.append(
                    Ec2Instance(
                        instance_id=inst.get("InstanceId"),
                        state=(inst.get("State") or {}).get("Name"),
                        instance_type=inst.get("InstanceType"),
                        region=region,
                        tags=_normalize_tags(inst.get("Tags")),
                    ).model_dump(mode="json")
                )
        return items

    return _safe_call("ec2", _call)


def list_ebs_volumes(region: Optional[str]) -> ServiceResult:
    """Enumerate EBS volumes via :code:`describe_volumes`."""
    def _call() -> list:
        client = get_aws_client("ec2", region=region)
        items = []
        resp = client.describe_volumes()
        for v in resp.get("Volumes", []):
            items.append(
                EbsVolume(
                    volume_id=v.get("VolumeId"),
                    size_gb=v.get("Size"),
                    state=v.get("State"),
                    region=region,
                    tags=_normalize_tags(v.get("Tags")),
                ).model_dump(mode="json")
            )
        return items

    return _safe_call("ebs", _call)


def list_elastic_ips(region: Optional[str]) -> ServiceResult:
    """Enumerate EC2 Elastic IPs via :code:`describe_addresses`."""
    def _call() -> list:
        client = get_aws_client("ec2", region=region)
        items = []
        resp = client.describe_addresses()
        for a in resp.get("Addresses", []):
            items.append(
                ElasticIp(
                    public_ip=a.get("PublicIp"),
                    allocation_id=a.get("AllocationId"),
                    region=region,
                    tags=_normalize_tags(a.get("Tags")),
                ).model_dump(mode="json")
            )
        return items

    return _safe_call("eip", _call)


def list_nat_gateways(region: Optional[str]) -> ServiceResult:
    """Enumerate NAT Gateways via :code:`describe_nat_gateways`."""
    def _call() -> list:
        client = get_aws_client("ec2", region=region)
        items = []
        resp = client.describe_nat_gateways()
        for ng in resp.get("NatGateways", []):
            items.append(
                NatGateway(
                    nat_gateway_id=ng.get("NatGatewayId"),
                    state=(ng.get("State") or "").lower() or None,
                    region=region,
                    tags=_normalize_tags(ng.get("Tags")),
                ).model_dump(mode="json")
            )
        return items

    return _safe_call("nat", _call)


def list_load_balancers_v2(region: Optional[str]) -> ServiceResult:
    """Enumerate ELBv2 load balancers via :code:`describe_load_balancers`."""
    def _call() -> list:
        client = get_aws_client("elbv2", region=region)
        items = []
        resp = client.describe_load_balancers()
        for lb in resp.get("LoadBalancers", []):
            lb_type = (lb.get("Type") or "application").lower()
            if lb_type not in ("application", "network", "gateway"):
                lb_type = "application"
            items.append(
                LoadBalancerV2(
                    arn=lb.get("LoadBalancerArn"),
                    name=lb.get("LoadBalancerName"),
                    type=lb_type,  # type: ignore[arg-type]
                    region=region,
                ).model_dump(mode="json")
            )
        return items

    return _safe_call("elbv2", _call)


def list_rds_instances(region: Optional[str]) -> ServiceResult:
    """Enumerate RDS DB instances via :code:`describe_db_instances`."""
    def _call() -> list:
        client = get_aws_client("rds", region=region)
        items = []
        resp = client.describe_db_instances()
        for dbi in resp.get("DBInstances", []):
            tag_list = dbi.get("TagList") or []
            items.append(
                RdsInstance(
                    db_instance_identifier=dbi.get("DBInstanceIdentifier"),
                    db_instance_class=dbi.get("DBInstanceClass"),
                    engine=dbi.get("Engine"),
                    status=dbi.get("DBInstanceStatus"),
                    region=region,
                    tags=_normalize_tags(tag_list),
                ).model_dump(mode="json")
            )
        return items

    return _safe_call("rds", _call)


def list_lambda_functions(region: Optional[str]) -> ServiceResult:
    """Enumerate Lambda functions via :code:`list_functions`."""
    def _call() -> list:
        client = get_aws_client("lambda", region=region)
        items = []
        resp = client.list_functions()
        for fn in resp.get("Functions", []):
            items.append(
                LambdaFunction(
                    function_name=fn.get("FunctionName"),
                    function_arn=fn.get("FunctionArn"),
                    runtime=fn.get("Runtime"),
                    region=region,
                ).model_dump(mode="json")
            )
        return items

    return _safe_call("lambda", _call)


def list_s3_buckets(region: Optional[str]) -> ServiceResult:
    """Enumerate S3 buckets (metadata only) via :code:`list_buckets` +
    per-bucket :code:`get_bucket_location`. No object listing."""
    def _call() -> list:
        client = get_aws_client("s3", region=region)
        items = []
        resp = client.list_buckets()
        for b in resp.get("Buckets", []):
            name = b.get("Name")
            bucket_region: Optional[str] = None
            try:
                loc = client.get_bucket_location(Bucket=name)
                # AWS returns "LocationConstraint" or None for us-east-1.
                bucket_region = loc.get("LocationConstraint") or "us-east-1"
            except (ClientError, BotoCoreError) as exc:
                # Per-bucket failure becomes "degraded"; still emit the row.
                logger.warning(
                    "aws.s3 bucket=%s location_error=%s",
                    name,
                    getattr(exc, "response", {}).get("Error", {}).get("Code", type(exc).__name__),
                )
                bucket_region = None
            items.append(
                S3Bucket(
                    name=name,
                    creation_date=b.get("CreationDate").isoformat() if b.get("CreationDate") else None,
                    region=bucket_region,
                ).model_dump(mode="json")
            )
        return items

    return _safe_call("s3", _call)


# --- Aggregator (used by the API layer in step 4) ----------------------


SERVICE_ENUMERATORS = [
    ("ec2", list_ec2_instances),
    ("ebs", list_ebs_volumes),
    ("eip", list_elastic_ips),
    ("nat", list_nat_gateways),
    ("elbv2", list_load_balancers_v2),
    ("rds", list_rds_instances),
    ("lambda", list_lambda_functions),
    ("s3", list_s3_buckets),
]


def enumerate_all_services(region: Optional[str]) -> Dict[str, ServiceResult]:
    """Run every enumerator and return a ``{service_name: ServiceResult}`` map."""
    return {name: fn(region) for name, fn in SERVICE_ENUMERATORS}
