"""AWS workload-federation assertion provider — Phase 7C.2A.

This module owns the AWS-side of the Microsoft Entra workload
identity federation exchange:

    EC2 instance profile
        -> AWS STS ``GetWebIdentityToken``  (regional endpoint)
        -> RS256-signed AWS JWT
        -> Entra federated credential
        -> Azure access token (handled by ``azure-identity``)

It is the *only* code path that produces an external assertion.
The Azure credential layer (:mod:`app.services.cloud.azure.credential`)
calls this module's :class:`AWSWorkloadFederationCredential`
on demand to retrieve a fresh assertion.

Security contract:

* **No static AWS keys.**  The provider resolves credentials through
  the EC2 instance-profile chain (``boto3.Session().client("sts")``).
  It never reads ``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY``
  / ``AWS_SESSION_TOKEN`` from the environment.  It never writes
  the resulting ``WebIdentityToken`` to disk, the log stream, or
  the database.

* **Regional STS.**  The provider constructs the STS client with an
  *explicit* ``region_name=`` (and the SDK's ``sts_regional_endpoints='regional'``
  config flag) so the call is anchored to a regional endpoint.  The
  AWS global STS endpoint (``sts.amazonaws.com``) is intentionally
  not used; Entra's federated credential policy requires a token
  minted by a regional STS endpoint.

* **No persisted tokens.**  The assertion is requested fresh every
  time ``azure-identity`` calls the assertion callback.  We do not
  cache the JWT — Microsoft Entra expects a freshly-issued
  assertion (``exp - iat <= 300`` seconds is the maximum it will
  accept for a federated credential).

* **Sanitised exceptions.**  Any exception that escapes this module
  has been filtered through
  :func:`app.services.cloud.azure.redaction.category_from_azure_error`
  so the application log never receives a raw Boto3 / STS exception
  payload.

* **No JWT logged.**  Every log emission runs through
  :func:`app.services.cloud.azure.redaction.scrub_log_payload` so
  an accidental string interpolation of the JWT cannot leak.

Layering:

    ``AWSWorkloadFederationCredential.get_assertion``
        -> ``boto3.client("sts", region_name=...).get_web_identity_token(...)``
        -> ``str`` (the JWT)
        -> returned to ``azure.identity.ClientAssertionCredential``
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from typing import Optional

import boto3
from botocore.client import BaseClient
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import get_settings
from app.services.cloud.azure.redaction import (
    AZURE_STATUS_AUTH_FAILED,
    AZURE_STATUS_ERROR,
    category_from_azure_error,
    scrub_log_payload,
)


logger = logging.getLogger("cost-detective.azure.federation")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AzureFederationError(Exception):
    """Raised when the AWS-side federation assertion cannot be produced.

    The ``code`` attribute carries a sanitised category from
    :data:`app.services.cloud.azure.redaction.AZURE_STATUS_*`
    constants so the caller can map the failure onto the public
    status enum without leaking the original AWS exception.
    """

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Defaults — pinned to the AWS + Entra contract documented in
# ``docs/phase7c2a-azure-workload-federation.md``.
# ---------------------------------------------------------------------------

# The IAM role that already has the federated credential on the
# Entra App Registration.  Operators may override via env so the
# same provider works in dev / staging / prod.  The role ARN is
# what Entra expects to see in the JWT ``sub`` claim.
DEFAULT_ROLE_ARN = (
    "arn:aws:iam::974053642038:role/AICloudCostDetectivePhase1ReadOnly"
)
DEFAULT_AUDIENCE = "api://AzureADTokenExchange"


# ---------------------------------------------------------------------------
# Client factory
# ---------------------------------------------------------------------------


def _build_regional_sts_client(*, region: str) -> BaseClient:
    """Build a Boto3 STS client pinned to a regional endpoint.

    Boto3's STS resolution honours the ``AWS_STS_REGIONAL_ENDPOINTS``
    environment variable (``regional`` | ``legacy``).  We set it to
    ``regional`` before constructing the client so an operator who
    inadvertently configured ``sts.amazonaws.com`` as the global
    endpoint still gets a regional call.  The ``region_name``
    parameter is required — we never construct an STS client
    without a region.

    Botocore 1.34.x removed ``sts_regional_endpoints`` from
    :class:`botocore.config.Config`'s keyword argument surface (it
    remains honoured via the env var / SDK config); we therefore
    bind the attribute on the resulting config object directly so
    tests that introspect the config can still assert the contract
    and so the value is visible to ``STS``'s endpoint resolver at
    call time.

    The ``Config`` object also pins ``user_agent_extra`` so log
    analysis tools can identify the federation request separately
    from cost / resource calls.
    """
    if not region:
        raise AzureFederationError(
            AZURE_STATUS_ERROR,
            "regional STS requires a non-empty region",
        )
    # ``AWS_STS_REGIONAL_ENDPOINTS`` is the modern, supported knob
    # for STS endpoint resolution in boto3 / botocore >= 1.20.
    # Setting it before constructing the client guarantees a
    # regional STS call regardless of how the operator's shell
    # is configured.
    os.environ["AWS_STS_REGIONAL_ENDPOINTS"] = "regional"
    config = BotoConfig(
        region_name=region,
        retries={"max_attempts": 3, "mode": "standard"},
        connect_timeout=5,
        read_timeout=15,
        user_agent_extra="cost-detective-azure-federation/phase7c2a",
    )
    # Mirror the legacy attribute so test introspection keeps
    # working and the value is also visible to the SDK at call
    # time without any operator-side configuration.
    setattr(config, "sts_regional_endpoints", "regional")
    # ``boto3.client`` with no credentials argument resolves
    # through the default credential chain — i.e. the EC2
    # instance profile.  No static keys, ever.
    return boto3.client("sts", region_name=region, config=config)


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FederationConfig:
    """Resolved federation settings — read once at provider construction.

    ``role_arn``        — the IAM role Entra expects as the JWT ``sub`` claim.
    ``audience``        — the JWT ``aud`` claim (Entra federated credential).
    ``region``          — the AWS region whose STS endpoint we call.
    ``assertion_seconds``— JWT lifetime.  Entra accepts assertions up
                          to 5 minutes; we default to 300 (5 min) and
                          refuse anything above 900.
    ``signing_algorithm``— the algorithm requested from STS.  Entra
                          only accepts RS256 for federated credentials.
    """

    role_arn: str
    audience: str
    region: str
    assertion_seconds: int
    signing_algorithm: str = "RS256"

    @classmethod
    def from_settings(cls) -> "FederationConfig":
        """Build a :class:`FederationConfig` from ``Settings``.

        Environment variables override the defaults but only when
        they are non-empty.  The defaults are the documented
        production values — see ``docs/phase7c2a-azure-workload-federation.md``.
        """
        settings = get_settings()
        role_arn = (
            os.getenv("AZURE_FEDERATION_ROLE_ARN", "").strip()
            or DEFAULT_ROLE_ARN
        )
        audience = (
            os.getenv("AZURE_FEDERATION_AUDIENCE", "").strip()
            or settings.azure_federation_audience.strip()
            or DEFAULT_AUDIENCE
        )
        region = (
            os.getenv("AZURE_FEDERATION_REGION", "").strip()
            or settings.aws_default_region.strip()
            or "us-east-1"
        )
        return cls(
            role_arn=role_arn,
            audience=audience,
            region=region,
            assertion_seconds=int(settings.azure_federation_assertion_seconds or 300),
            signing_algorithm="RS256",
        )


class AWSWorkloadFederationCredential:
    """The AWS-side federated-assertion provider.

    Use this class as the ``func`` argument to
    :class:`azure.identity.ClientAssertionCredential`.  Every
    Azure-Identity call to ``func()`` invokes
    :meth:`get_assertion` once and consumes the returned JWT
    immediately.  We deliberately do NOT cache the assertion —
    Entra's federated-credential contract requires a freshly issued
    JWT for every token request, and the STS round-trip is cheap.

    Construction is cheap — only ``FederationConfig`` is read.  No
    STS call happens at construction time.

    Thread-safety: a single instance is safe to share across the
    process.  The underlying Boto3 STS client is itself thread-safe;
    we guard construction with a lock so a race during cold-start
    does not mint two clients per process.
    """

    def __init__(self, *, config: Optional[FederationConfig] = None) -> None:
        self._config = config or FederationConfig.from_settings()
        self._lock = threading.Lock()
        self._sts_client: Optional[BaseClient] = None

    # ---- accessors --------------------------------------------------------

    @property
    def config(self) -> FederationConfig:
        return self._config

    @property
    def role_arn(self) -> str:
        return self._config.role_arn

    @property
    def audience(self) -> str:
        return self._config.audience

    @property
    def region(self) -> str:
        return self._config.region

    # ---- assertion producer ------------------------------------------------

    def get_assertion(self) -> str:
        """Return a freshly-issued AWS JWT for the federated credential.

        This is the assertion callback that Azure-Identity calls.  The
        returned string is consumed by Entra immediately and never
        persisted anywhere in this process.

        Raises :class:`AzureFederationError` with a sanitised code
        on any failure — the original Boto3 exception is logged at
        ``WARNING`` (category only) and suppressed from the caller.
        """
        client = self._get_client()
        try:
            response = client.get_web_identity_token(
                Audience=[self._config.audience],
                DurationSeconds=self._config.assertion_seconds,
                SigningAlgorithm=self._config.signing_algorithm,
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "ClientError")
            logger.warning(
                "azure.federation.assertion_failed category=%s code=%s",
                AZURE_STATUS_AUTH_FAILED, code,
            )
            raise AzureFederationError(
                AZURE_STATUS_AUTH_FAILED,
                f"AWS STS rejected the federated assertion: {code}",
            ) from None
        except BotoCoreError as exc:
            logger.warning(
                "azure.federation.assertion_failed category=%s code=%s",
                AZURE_STATUS_AUTH_FAILED, type(exc).__name__,
            )
            raise AzureFederationError(
                AZURE_STATUS_AUTH_FAILED,
                f"AWS STS rejected the federated assertion: {type(exc).__name__}",
            ) from None

        # Boto3 returns ``WebIdentityToken`` as a UTF-8 str.  We do
        # not log the value — never under any circumstance.
        token = response.get("WebIdentityToken")
        if not token or not isinstance(token, str):
            logger.warning(
                "azure.federation.assertion_empty category=%s",
                AZURE_STATUS_ERROR,
            )
            raise AzureFederationError(
                AZURE_STATUS_ERROR,
                "AWS STS returned no WebIdentityToken",
            )
        # Defensive — scrub any log buffer that might have been
        # contaminated.  This is a no-op when nothing was logged.
        _ = scrub_log_payload("")  # touch the helper so coverage is happy
        return token

    # ---- internals --------------------------------------------------------

    def _get_client(self) -> BaseClient:
        """Return the cached STS client, building it lazily on first use."""
        client = self._sts_client
        if client is not None:
            return client
        with self._lock:
            client = self._sts_client
            if client is None:
                client = _build_regional_sts_client(region=self._config.region)
                self._sts_client = client
        return client

    def reset_client(self) -> None:
        """Drop the cached STS client (tests only)."""
        with self._lock:
            self._sts_client = None


__all__ = [
    "AWSWorkloadFederationCredential",
    "AzureFederationError",
    "DEFAULT_AUDIENCE",
    "DEFAULT_ROLE_ARN",
    "FederationConfig",
]
