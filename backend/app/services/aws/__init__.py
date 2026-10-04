"""AWS service package — Phase 1 (read-only identity + resource discovery).

This package exposes the Boto3 client factory and the per-service
enumerators used by ``backend/app/api/aws.py``. Every AWS call here
is read-only and goes through the Boto3 default credential chain
(env / shared config / EC2 instance profile).
"""
from app.services.aws.clients import get_aws_client

__all__ = ["get_aws_client"]
