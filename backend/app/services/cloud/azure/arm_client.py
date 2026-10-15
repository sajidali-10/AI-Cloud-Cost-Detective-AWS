"""Minimal Azure Resource Manager REST client — Phase 7C.2A.

The Azure adapter only needs a single ARM endpoint today: the
subscription read endpoint at
``GET https://management.azure.com/subscriptions/<id>?api-version=2022-12-01``.

A 200 response proves:

* The Entra token exchange succeeded (``Bearer`` token was accepted).
* The service principal can reach the subscription.
* The subscription id exists and is enabled.
* The Entra RBAC grant (``Reader`` / ``Cost Management Reader``)
  is in place.

We deliberately avoid the full ``azure-mgmt-resource`` SDK
surface for this phase — adding the management SDK pulls in a
large dependency tree.  Phase 7C.2A only requires a single GET,
and we already have ``httpx`` in the dependency graph for the
existing AI client.  Phase 7C.2B (Cost Management) can layer in
the heavier SDK when it actually needs to call Cost Management
APIs.

Sanitisation contract:

* No ``Authorization: Bearer …`` header is ever logged.
* No access token is ever echoed back to the caller.
* Any exception that escapes this module has been mapped onto a
  sanitised category in
  :mod:`app.services.cloud.azure.redaction`.

Thread-safety: a single :class:`AzureArmClient` instance is safe
to share across requests.  The underlying ``httpx.Client`` is
thread-safe for concurrent reads when ``trust_env=False``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping, Optional

import httpx

from app.services.cloud.azure.credential import (
    AzureAuthError,
    acquire_access_token,
)
from app.services.cloud.azure.redaction import (
    AZURE_STATUS_ACCESS_DENIED,
    AZURE_STATUS_AUTH_FAILED,
    AZURE_STATUS_CONNECTED,
    AZURE_STATUS_NOT_FOUND,
    safe_subscription_label,
    scrub_log_payload,
)


logger = logging.getLogger("cost-detective.azure.arm")


SUBSCRIPTION_API_VERSION = "2022-12-01"
ARM_BASE_URL = "https://management.azure.com"


@dataclass(frozen=True)
class AzureSubscriptionDescriptor:
    """Sanitised metadata about an Azure subscription.

    Returned by :meth:`AzureArmClient.validate_subscription`.  Only
    safe fields are surfaced; ARM resource ids / tenant ids are
    intentionally collapsed.
    """

    subscription_id: str
    display_name: str
    state: Optional[str]  # "Enabled", "Disabled", "Warned", etc.
    authorization_source: Optional[str]
    status: str  # CONNECTED / ACCESS_DENIED / NOT_FOUND / AUTH_FAILED / ERROR / NOT_CONFIGURED
    reason: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "subscription_id": self.subscription_id,
            "display_name": self.display_name,
            "state": self.state,
            "authorization_source": self.authorization_source,
            "status": self.status,
            "reason": self.reason,
        }


class AzureArmClient:
    """Thin ARM REST wrapper used for subscription validation.

    Construction is cheap — only the underlying ``httpx.Client`` is
    built.  No network call happens here.

    The client is intentionally minimal.  Phase 7C.2A calls exactly
    one ARM endpoint; future phases (7C.2B Cost Management, 7C.2C
    Resource Graph) can extend this client without breaking the
    existing call sites.
    """

    def __init__(
        self,
        *,
        base_url: str = ARM_BASE_URL,
        api_version: str = SUBSCRIPTION_API_VERSION,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_version = api_version
        self._timeout = timeout_seconds
        # ``trust_env=False`` so ``httpx`` never reads HTTP_PROXY /
        # NO_PROXY from the operator's shell — the federation
        # traffic must flow through the VPC's IMDS / STS endpoint
        # and we don't want an unrelated proxy to intercept the
        # outbound call.
        self._http = httpx.Client(
            base_url=self._base_url,
            timeout=timeout_seconds,
            trust_env=False,
            headers={"User-Agent": "cost-detective-azure-arm/phase7c2a"},
        )

    def close(self) -> None:
        try:
            self._http.close()
        except Exception:  # noqa: BLE001 — defensive
            pass

    # ---- operations -------------------------------------------------------

    def validate_subscription(
        self,
        subscription_id: str,
    ) -> AzureSubscriptionDescriptor:
        """Validate ``subscription_id`` against ARM.

        Returns a sanitised descriptor.  Never raises for ARM /
        auth errors — those are folded into the descriptor's
        ``status`` field.  Only programming errors (an invalid
        subscription_id type, a malformed URL) propagate as
        exceptions.
        """
        if not subscription_id or not isinstance(subscription_id, str):
            raise ValueError("subscription_id must be a non-empty string")

        try:
            token = acquire_access_token()
        except AzureAuthError as exc:
            label = safe_subscription_label(subscription_id)
            logger.warning(
                "azure.arm.validate.auth_failed sub=%s category=%s",
                label, exc.code,
            )
            return AzureSubscriptionDescriptor(
                subscription_id=subscription_id,
                display_name="",
                state=None,
                authorization_source=None,
                status=exc.code,
                reason=exc.code,
            )

        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        url = f"/subscriptions/{subscription_id}"
        params = {"api-version": self._api_version}
        label = safe_subscription_label(subscription_id)
        try:
            response = self._http.get(url, params=params, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning(
                "azure.arm.validate.network_error sub=%s code=%s",
                label, type(exc).__name__,
            )
            return AzureSubscriptionDescriptor(
                subscription_id=subscription_id,
                display_name="",
                state=None,
                authorization_source=None,
                status=AZURE_STATUS_AUTH_FAILED,
                reason="ARM network error",
            )

        return self._parse_validation_response(subscription_id, response, label)

    # ---- helpers ---------------------------------------------------------

    def _parse_validation_response(
        self,
        subscription_id: str,
        response: httpx.Response,
        label: str,
    ) -> AzureSubscriptionDescriptor:
        status = response.status_code
        # Never echo the response body — it can contain
        # correlation ids / subscription display names that may
        # be operator-sensitive.
        if status == 200:
            try:
                body = response.json()
            except ValueError:
                logger.warning(
                    "azure.arm.validate.bad_json sub=%s status=200",
                    label,
                )
                return AzureSubscriptionDescriptor(
                    subscription_id=subscription_id,
                    display_name="",
                    state=None,
                    authorization_source=None,
                    status=AZURE_STATUS_AUTH_FAILED,
                    reason="ARM returned non-JSON body",
                )
            return AzureSubscriptionDescriptor(
                subscription_id=body.get("subscriptionId", subscription_id),
                display_name=str(body.get("displayName") or ""),
                state=body.get("state"),
                authorization_source=body.get("authorizationSource"),
                status=AZURE_STATUS_CONNECTED,
                reason=None,
            )
        if status == 401:
            logger.warning(
                "azure.arm.validate.auth_failed sub=%s status=401",
                label,
            )
            return AzureSubscriptionDescriptor(
                subscription_id=subscription_id,
                display_name="",
                state=None,
                authorization_source=None,
                status=AZURE_STATUS_AUTH_FAILED,
                reason="ARM rejected the access token",
            )
        if status == 403:
            logger.warning(
                "azure.arm.validate.forbidden sub=%s status=403",
                label,
            )
            return AzureSubscriptionDescriptor(
                subscription_id=subscription_id,
                display_name="",
                state=None,
                authorization_source=None,
                status=AZURE_STATUS_ACCESS_DENIED,
                reason="service principal lacks the required RBAC",
            )
        if status == 404:
            logger.warning(
                "azure.arm.validate.not_found sub=%s status=404",
                label,
            )
            return AzureSubscriptionDescriptor(
                subscription_id=subscription_id,
                display_name="",
                state=None,
                authorization_source=None,
                status=AZURE_STATUS_NOT_FOUND,
                reason="subscription id is not visible to the service principal",
            )
        logger.warning(
            "azure.arm.validate.unexpected sub=%s status=%s",
            label, status,
        )
        return AzureSubscriptionDescriptor(
            subscription_id=subscription_id,
            display_name="",
            state=None,
            authorization_source=None,
            status=AZURE_STATUS_AUTH_FAILED,
            reason="ARM returned an unexpected response",
        )


__all__ = [
    "ARM_BASE_URL",
    "AzureArmClient",
    "AzureSubscriptionDescriptor",
    "SUBSCRIPTION_API_VERSION",
]


# ---------------------------------------------------------------------------
# Test-friendly module-level helpers
# ---------------------------------------------------------------------------


def make_safe_log_payload(mapping: Optional[Mapping[str, Any]]) -> dict:
    """Module-level re-export of the redaction helper.

    Imported by some tests to keep their imports surface tight.
    """
    from app.services.cloud.azure.redaction import safe_log_extra

    return dict(safe_log_extra(mapping))


__all__.append("make_safe_log_payload")
