"""AWS caller-identity service — Phase 1.

A thin wrapper around :func:`boto3.client` + ``sts.get_caller_identity()``
that returns the caller's Account / Arn / UserId plus the resolved region.
All errors are funneled into :class:`AwsIdentityError` so the API layer
can return a sanitized 502 without leaking exception text or stack traces.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from botocore.exceptions import BotoCoreError, ClientError

from app.services.aws.clients import get_aws_client


class AwsIdentityError(Exception):
    """Raised when the caller's AWS identity cannot be resolved.

    The ``code`` attribute carries the original Boto3 error code
    (e.g. ``"AccessDenied"``, ``"NoCredentialsError"``, ``"InvalidClientTokenId"``)
    so the caller can return it in a sanitized response body.
    """

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.message = message


def get_caller_identity(region: Optional[str]) -> Dict[str, Any]:
    """Return the AWS caller's identity for ``region``.

    Parameters
    ----------
    region:
        Optional AWS region. ``None`` lets Boto3 resolve from the
        default chain (``AWS_DEFAULT_REGION``, shared config, etc.).

    Returns
    -------
    dict
        ``{"account": str, "arn": str, "user_id": str, "region": str | None}``.

    Raises
    ------
    AwsIdentityError
        On any Boto3 or BotoCore error, with ``code`` set to the
        original error class name or ``ClientError`` ``error["Code"]``.
    """
    client = get_aws_client("sts", region=region)
    try:
        response = client.get_caller_identity()
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "ClientError")
        raise AwsIdentityError(code=code, message="STS get_caller_identity failed") from exc
    except BotoCoreError as exc:
        # NoCredentialsError, EndpointConnectionError, PartialCredentialsError, etc.
        raise AwsIdentityError(code=type(exc).__name__) from exc

    return {
        "account": response.get("Account"),
        "arn": response.get("Arn"),
        "user_id": response.get("UserId"),
        "region": region,
    }
