"""Boto3 client factory for Phase 1 AWS discovery.

A single thin wrapper around :func:`boto3.client` so the rest of the
AWS service code can request a regional client without caring about
credential resolution. Credentials come from the Boto3 default chain
(env / shared config / EC2 instance profile); this module never
introduces AWS keys and never reads them from ``Settings``.
"""
from __future__ import annotations

from typing import Optional

import boto3
from botocore.client import BaseClient


def get_aws_client(service_name: str, region: Optional[str] = None) -> BaseClient:
    """Return a Boto3 client for ``service_name`` in the given region.

    Parameters
    ----------
    service_name:
        The lowercase Boto3 service identifier, e.g. ``"sts"``,
        ``"ec2"``, ``"s3"``.
    region:
        Optional AWS region override (e.g. ``"us-east-1"``). When
        ``None``, Boto3 falls back to its own region resolution chain
        (``AWS_DEFAULT_REGION`` env, shared config, instance metadata).

    Returns
    -------
    botocore.client.BaseClient
        A configured Boto3 client. Credential lookup happens lazily
        on the first API call, so a missing-credentials error surfaces
        from the call site, not from this factory.
    """
    client_kwargs = {}
    if region:
        client_kwargs["region_name"] = region
    return boto3.client(service_name, **client_kwargs)
