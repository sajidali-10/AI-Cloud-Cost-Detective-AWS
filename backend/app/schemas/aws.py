"""Pydantic schemas for normalized AWS resource inventory — Phase 1.

Each model represents a single AWS resource as returned by the
per-service enumerators in :mod:`app.services.aws.resources`.
The schemas intentionally keep fields minimal and consistent across
services (id/arn/region/tags) so the aggregator in step 4 can present
a uniform response.
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


# --- Per-service resource models -----------------------------------------


class _TaggedModel(BaseModel):
    """Common mixin: every resource carries ``tags`` (may be empty)."""

    tags: Dict[str, str] = Field(default_factory=dict)


class Ec2Instance(_TaggedModel):
    instance_id: str
    state: Optional[str] = None
    instance_type: Optional[str] = None
    region: Optional[str] = None


class EbsVolume(_TaggedModel):
    volume_id: str
    size_gb: Optional[int] = None
    state: Optional[str] = None
    region: Optional[str] = None


class ElasticIp(_TaggedModel):
    public_ip: str
    allocation_id: Optional[str] = None
    region: Optional[str] = None


class NatGateway(_TaggedModel):
    nat_gateway_id: str
    state: Optional[str] = None
    region: Optional[str] = None


class LoadBalancerV2(_TaggedModel):
    arn: str
    name: str
    type: Literal["application", "network", "gateway"] = "application"
    region: Optional[str] = None


class RdsInstance(_TaggedModel):
    db_instance_identifier: str
    db_instance_class: Optional[str] = None
    engine: Optional[str] = None
    status: Optional[str] = None
    region: Optional[str] = None


class LambdaFunction(_TaggedModel):
    function_name: str
    function_arn: Optional[str] = None
    runtime: Optional[str] = None
    region: Optional[str] = None


class S3Bucket(_TaggedModel):
    name: str
    creation_date: Optional[str] = None
    region: Optional[str] = None


# --- Aggregation envelope -----------------------------------------------


ServiceStatus = Literal["ok", "denied", "error"]


class ServiceResult(BaseModel):
    """The uniform envelope returned for every service in the aggregator.

    ``items`` contains pydantic model instances (already serialized via
    ``.model_dump(mode="json")`` at the boundary). Per-service
    ``ClientError`` is funneled into ``status="denied"`` (for
    ``AccessDenied``) or ``status="error"`` (any other Boto3 error);
    the enumerator never raises out.
    """

    service: str
    status: ServiceStatus
    items: List[Dict] = Field(default_factory=list)
    error_code: Optional[str] = None


# --- Enrichment schemas (Resource Explorer + Tagging API) ----------------


class ResourceExplorerResult(BaseModel):
    """Optional enrichment layer: AWS Resource Explorer search results.

    ``available`` is False (with ``error_code`` set) when the account
    has no Resource Explorer index, when search is denied, or on any
    other Boto3 error. ``available=True`` means the search ran; the
    ``results`` list may still be empty if no resources matched.
    """

    available: bool
    query: str
    results: List[Dict] = Field(default_factory=list)
    error_code: Optional[str] = None


class TaggingApiResult(BaseModel):
    """Optional enrichment layer: Resource Groups Tagging API results.

    ``available`` is False when tagging is denied or any other Boto3
    error occurs. ``tags_by_arn`` keys are resource ARNs; values are
    tag dictionaries.
    """

    available: bool
    tags_by_arn: Dict[str, Dict[str, str]] = Field(default_factory=dict)
    error_code: Optional[str] = None


class ResourcesResponse(BaseModel):
    """Top-level response shape for ``GET /api/aws/resources``."""

    region: Optional[str] = None
    services: Dict[str, ServiceResult] = Field(default_factory=dict)
    enrichment: Dict[str, Any] = Field(default_factory=dict)
