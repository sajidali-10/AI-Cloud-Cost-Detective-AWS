"""AzureIdentity credential layer — Phase 7C.2A.

Wraps :class:`azure.identity.ClientAssertionCredential` so the rest
of the application obtains Azure access tokens via:

    ClientAssertionCredential(
        tenant_id=azure_tenant_id,
        client_id=azure_client_id,
        func=get_aws_federated_assertion,
    )

The assertion callback (``func``) is an instance of
:class:`app.services.cloud.azure.federation.AWSWorkloadFederationCredential`.
Every token request triggers exactly one STS
``GetWebIdentityToken`` call, which is exactly what Entra's
federated-credential contract requires.

The credential layer is intentionally thin:

* ``azure-identity`` owns the in-memory access-token cache.  We do
  not implement our own token cache, persistence, or refresh logic.
* No secrets are stored at this layer.
* All exceptions are sanitised before reaching the application log
  stream; see :mod:`app.services.cloud.azure.redaction`.

The credential is exposed via :func:`get_credential` so a
caller never has to instantiate ``ClientAssertionCredential``
itself.  When runtime configuration is missing, the helper raises
:class:`AzureNotConfiguredError` *once*; subsequent calls return
the cached error without trying again.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Optional

from app.core.config import get_settings
from app.services.cloud.azure.federation import (
    AWSWorkloadFederationCredential,
    AzureFederationError,
    FederationConfig,
)
from app.services.cloud.azure.redaction import (
    AZURE_STATUS_NOT_CONFIGURED,
    scrub_log_payload,
)


logger = logging.getLogger("cost-detective.azure.credential")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AzureNotConfiguredError(Exception):
    """Raised when runtime configuration is insufficient for Azure auth.

    Phase 7C.2A fails explicitly — we never silently fall back to a
    different credential strategy.  The error message names the
    missing variables; it never includes a token, JWT, or secret.
    """

    def __init__(self, missing: tuple[str, ...]) -> None:
        self.missing = missing
        super().__init__(
            "Azure is not configured: missing "
            + ", ".join(missing)
            + ". Set the variables via deployment configuration."
        )


class AzureAuthError(Exception):
    """Raised when an Azure token exchange fails.

    The ``code`` attribute carries a sanitised category (see
    :mod:`app.services.cloud.azure.redaction`).
    """

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Config snapshot
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AzureRuntimeConfig:
    """Snapshot of Azure-side runtime configuration.

    ``tenant_id`` and ``client_id`` are non-secret identifiers — they
    identify the Entra App Registration.  They are stored alongside
    the credential so the rest of the application can attribute
    Azure activity to a single Entra application.  Operators
    configure them via environment variables (deployment .env / secret
    manager), never via source code.
    """

    tenant_id: str
    client_id: str
    arm_scope: str

    @classmethod
    def from_settings(cls) -> Optional["AzureRuntimeConfig"]:
        """Return a snapshot if the runtime is configured; ``None`` otherwise."""
        settings = get_settings()
        tenant_id = (settings.azure_tenant_id or "").strip()
        client_id = (settings.azure_client_id or "").strip()
        if not tenant_id or not client_id:
            return None
        return cls(
            tenant_id=tenant_id,
            client_id=client_id,
            arm_scope=(
                (settings.azure_arm_scope or "").strip()
                or "https://management.azure.com/.default"
            ),
        )

    def missing_variables(self) -> tuple[str, ...]:
        missing: list[str] = []
        if not self.tenant_id:
            missing.append("AZURE_TENANT_ID")
        if not self.client_id:
            missing.append("AZURE_CLIENT_ID")
        return tuple(missing)


# ---------------------------------------------------------------------------
# ClientAssertionCredential construction
# ---------------------------------------------------------------------------


def build_client_assertion_credential(
    *,
    runtime: AzureRuntimeConfig,
    federation: AWSWorkloadFederationCredential,
) -> Any:
    """Construct a ``ClientAssertionCredential`` for the runtime config.

    The caller is responsible for caching the result.  We do NOT
    cache at this layer — the ``azure-identity`` SDK already owns an
    in-memory access-token cache that survives across requests.

    The ``func`` argument is the assertion callback; every token
    request triggers one STS ``GetWebIdentityToken`` call.
    """
    try:
        from azure.identity import ClientAssertionCredential
    except ImportError as exc:  # pragma: no cover — covered by env
        raise AzureAuthError(
            AZURE_STATUS_NOT_CONFIGURED,
            "azure-identity is not installed",
        ) from exc

    return ClientAssertionCredential(
        tenant_id=runtime.tenant_id,
        client_id=runtime.client_id,
        func=federation.get_assertion,
    )


# ---------------------------------------------------------------------------
# Process-wide cache
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _CredentialHandle:
    """The credential + the runtime config it was built from.

    Cached process-wide so every Azure access goes through the same
    in-memory token cache.
    """

    credential: Any
    runtime: AzureRuntimeConfig


_HANDLE_LOCK = threading.Lock()
_HANDLE: Optional[_CredentialHandle] = None
_FAIL_STATE: Optional[AzureNotConfiguredError] = None


def get_credential() -> _CredentialHandle:
    """Return a process-wide ``ClientAssertionCredential`` handle.

    Construction is idempotent: the first call builds the handle
    and every subsequent call returns the same instance.  When the
    runtime is not configured, :class:`AzureNotConfiguredError`
    is raised on every call without trying to build the credential.
    """
    global _HANDLE, _FAIL_STATE
    if _HANDLE is not None:
        return _HANDLE
    if _FAIL_STATE is not None:
        raise _FAIL_STATE

    runtime = AzureRuntimeConfig.from_settings()
    if runtime is None or runtime.missing_variables():
        missing = runtime.missing_variables() if runtime else (
            "AZURE_TENANT_ID", "AZURE_CLIENT_ID",
        )
        err = AzureNotConfiguredError(missing)
        with _HANDLE_LOCK:
            if _FAIL_STATE is None:
                _FAIL_STATE = err
        raise err

    federation = AWSWorkloadFederationCredential(
        config=FederationConfig.from_settings(),
    )
    credential = build_client_assertion_credential(
        runtime=runtime, federation=federation,
    )
    handle = _CredentialHandle(credential=credential, runtime=runtime)
    with _HANDLE_LOCK:
        if _HANDLE is None:
            _HANDLE = handle
    return handle


def reset_credential_cache() -> None:
    """Drop the cached credential handle (tests only)."""
    global _HANDLE, _FAIL_STATE
    with _HANDLE_LOCK:
        _HANDLE = None
        _FAIL_STATE = None


def is_configured() -> bool:
    """Return True iff the Azure runtime is configured.

    Cheap, idempotent — never raises.  Use this to decide whether
    to register the Azure adapter as ENABLED or DISABLED.
    """
    runtime = AzureRuntimeConfig.from_settings()
    return runtime is not None and not runtime.missing_variables()


def acquire_access_token(*, scopes: Optional[list[str]] = None) -> str:
    """Return a short-lived Azure access token for ``scopes``.

    ``scopes`` defaults to the ARM management scope.  Tokens are
    cached by ``azure-identity`` in process memory; we never persist
    the returned value.

    Sanitised errors only — no JWT / Authorization header is ever
    echoed to the caller.
    """
    handle = get_credential()
    requested_scopes = list(scopes) if scopes else [handle.runtime.arm_scope]
    try:
        token = handle.credential.get_token(*requested_scopes)
    except AzureFederationError as exc:
        # Re-raise with the same sanitised code.
        raise AzureAuthError(exc.code, "federation assertion failed") from None
    except Exception as exc:  # noqa: BLE001 — sanitised
        from app.services.cloud.azure.redaction import category_from_azure_error

        code = category_from_azure_error(exc)
        logger.warning(
            "azure.credential.token_failed category=%s code=%s",
            code, type(exc).__name__,
        )
        # Defensive scrub — if the original exception message happens
        # to mention a JWT (it shouldn't, but…) we strip it before
        # it can reach the application log.
        safe = scrub_log_payload(str(exc))[:200]
        raise AzureAuthError(code, safe) from None
    token_value = getattr(token, "token", None)
    if not token_value:
        raise AzureAuthError(AZURE_STATUS_NOT_CONFIGURED, "no token returned")
    return token_value


__all__ = [
    "AzureAuthError",
    "AzureNotConfiguredError",
    "AzureRuntimeConfig",
    "acquire_access_token",
    "build_client_assertion_credential",
    "get_credential",
    "is_configured",
    "reset_credential_cache",
]
