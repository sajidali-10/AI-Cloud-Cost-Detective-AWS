"""AWS caller-identity service — Phase 1 + Phase 7B.

A thin wrapper around ``sts.get_caller_identity()`` that returns the
caller's Account / Arn / UserId plus the resolved region.  All
errors are funneled into :class:`AwsIdentityError` so the API layer
can return a sanitized 502 without leaking exception text or stack
traces.

Phase 7B: callers may pass a pre-resolved ``boto3.Session`` (e.g.
one obtained from the account/session factory).  This lets the
identity probe reuse the same AssumeRole session that subsequent
Cost Explorer / CloudWatch calls will use, avoiding an extra
STS round-trip per request.  When ``session`` is omitted, the
legacy default-credential-chain path is used so the Phase 1
behaviour is preserved.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import boto3
from botocore.client import BaseClient
from botocore.exceptions import BotoCoreError, ClientError


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


def _client_from_session_or_default(
    *,
    session: Optional[boto3.Session],
    region: Optional[str],
) -> BaseClient:
    if session is None:
        # Lazy import to avoid a hard dependency on the AWS clients
        # factory at module import time (tests import identity.py in
        # isolation).
        from app.services.aws.clients import get_aws_client
        return get_aws_client("sts", region=region)
    kwargs: Dict[str, Any] = {"config": session._session.get_config_variable("client_config")}
    if region:
        kwargs["region_name"] = region
    return session.client("sts", **kwargs)


def get_caller_identity(
    region: Optional[str],
    *,
    session: Optional[boto3.Session] = None,
) -> Dict[str, Any]:
    """Return the AWS caller's identity.

    Parameters
    ----------
    region:
        Optional AWS region. ``None`` lets Boto3 resolve from the
        default chain (``AWS_DEFAULT_REGION``, shared config, etc.).
    session:
        Optional pre-resolved :class:`boto3.Session`.  When supplied
        (Phase 7B), it is used as the client backend so identity
        calls for target accounts share the same AssumeRole session
        as subsequent business calls.

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
    client = _client_from_session_or_default(session=session, region=region)
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
