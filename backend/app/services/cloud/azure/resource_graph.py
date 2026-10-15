"""Azure Resource Graph REST client — Phase 7C.2C.

This module wraps the Microsoft Resource Graph Query API at tenant
scope and turns the provider-specific payloads into the
provider-neutral :class:`CloudResource` / :class:`CloudResourceSummary`
shapes the rest of the platform already understands.

.. code-block:: text

    POST https://management.azure.com/providers/Microsoft.ResourceGraph/
         resources?api-version=2024-04-01

Authentication reuses Phase 7C.2A/B's
:class:`app.services.cloud.azure.credential` pipeline — the same
``ClientAssertionCredential`` backed by AWS STS ``GetWebIdentityToken``.
**No client secrets; no certificates.**

Resource Graph returns rows in a flat ``data`` array when
``resultFormat=objectArray`` is supplied; pagination is driven by a
top-level ``$skipToken`` (passed back via the request body's
``options.$skipToken`` field) when ``dataTruncated`` is ``true``.

Security contract
-----------------

* No ``Authorization: Bearer …`` is ever logged.
* No access token / client assertion / STS JWT is ever exposed.
* All subscription IDs are truncated via
  :func:`app.services.cloud.azure.redaction.safe_subscription_label`
  before logging.
* The KQL query is **hardcoded** inside
  :func:`_build_resources_query` — the frontend cannot inject KQL.
* Tags are returned to the caller as a free-form dict but the
  *values* are scrubbed via
  :func:`app.services.cloud.azure.redaction.scrub_log_payload` before
  they are echoed in any error message (so an accidental
  tag-as-secret cannot leak).
* ``$skipToken`` values are opaque from Resource Graph and may
  embed identifiers; we surface them only via the per-request log
  stream after scrubbing.

Thread-safety
-------------

:class:`AzureResourceGraphClient` owns a private ``httpx.Client` and
is safe to share across requests.  Instances are cheap to create —
the resource aggregator mints one per call and closes it in the
``finally`` block.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import httpx

from app.services.cloud.azure.credential import (
    AzureAuthError,
    acquire_access_token,
)
from app.services.cloud.azure.redaction import (
    safe_subscription_label,
    scrub_log_payload,
)

logger = logging.getLogger("cost-detective.azure.resource_graph")


# ---------------------------------------------------------------------------
# Constants — pinned for Phase 7C.2C.
# ---------------------------------------------------------------------------

#: Microsoft Resource Graph Query API version.
#: 2024-04-01 is the GA version; pinning explicitly so the cache key
#: + docs reference a single, stable version.
RESOURCE_GRAPH_API_VERSION: str = "2024-04-01"

#: ARM base URL.
ARM_BASE_URL: str = "https://management.azure.com"

#: Cost Management Query API caps page size; the same ceiling is
#: the recommended upper bound for Resource Graph ``$top``.
PAGE_TOP: int = 1000

#: Defensive loop guard.  Resource Graph rarely paginates more than a
#: few times for a single subscription; 50 pages × 1000 = 50k
#: resources which exceeds the resource count of any realistic
#: Phase 7C.2C target workload.
MAX_PAGES: int = 50


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AzureResourceGraphErrorCategory(str, Enum):
    AUTH_FAILED = "AUTH_FAILED"
    ACCESS_DENIED = "ACCESS_DENIED"
    NOT_FOUND = "NOT_FOUND"
    THROTTLED = "THROTTLED"
    UPSTREAM_ERROR = "UPSTREAM_ERROR"
    NETWORK_ERROR = "NETWORK_ERROR"
    INVALID_RESPONSE = "INVALID_RESPONSE"


@dataclass
class AzureResourceGraphError(Exception):
    """Sanitized Resource Graph failure.

    ``category`` is the public contract the adapter consumes;
    ``retry_after`` is propagated only when the upstream supplied a
    ``Retry-After`` header.
    """

    category: AzureResourceGraphErrorCategory
    subscription_id: Optional[str] = None
    status_code: Optional[int] = None
    retry_after: Optional[int] = None
    message: Optional[str] = None

    def __str__(self) -> str:  # pragma: no cover - defensive
        label = safe_subscription_label(self.subscription_id)
        return f"[{self.category.value}] azure resource_graph failed sub={label}"


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AzureResourceGraphResult:
    """A parsed Resource Graph Query response."""

    columns: Tuple[str, ...]
    rows: Tuple[Mapping[str, Any], ...]
    total_records: int
    truncated: bool

    def to_dict(self) -> dict:
        return {
            "columns": list(self.columns),
            "rows": list(self.rows),
            "total_records": self.total_records,
            "truncated": self.truncated,
        }


# ---------------------------------------------------------------------------
# KQL query — hardcoded; no caller-supplied input.
# ---------------------------------------------------------------------------


def _build_resources_query() -> str:
    """Return the constrained KQL query used to enumerate resources.

    The query projects only the columns the application needs — no
    oversized ``properties`` payload, no sku, no identity.  Tags
    are projected so the frontend can render resource tagging
    without a second round-trip.

    The ``| limit PAGE_TOP`` ceiling is intentional: Resource
    Graph interprets ``| limit`` as a hard cap on rows returned
    *before* pagination, so a bounded page size keeps the loop
    guard deterministic.
    """
    return (
        "Resources "
        "| project id, name, type, location, resourceGroup, subscriptionId, tags "
        f"| limit {PAGE_TOP}"
    )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class AzureResourceGraphClient:
    """Thin REST wrapper around the Microsoft Resource Graph Query API."""

    def __init__(
        self,
        *,
        base_url: str = ARM_BASE_URL,
        api_version: str = RESOURCE_GRAPH_API_VERSION,
        timeout_seconds: float = 30.0,
        page_top: int = PAGE_TOP,
        max_pages: int = MAX_PAGES,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_version = api_version
        self._timeout = timeout_seconds
        self._page_top = page_top
        self._max_pages = max_pages
        self._http = httpx.Client(
            base_url=self._base_url,
            timeout=timeout_seconds,
            trust_env=False,
            headers={"User-Agent": "cost-detective-azure-rg/phase7c2c"},
        )

    def close(self) -> None:
        try:
            self._http.close()
        except Exception:  # noqa: BLE001 - defensive
            pass

    # ---- public API ------------------------------------------------------

    def query(
        self,
        *,
        subscription_ids: Sequence[str],
    ) -> AzureResourceGraphResult:
        """Run the constrained inventory query and follow pagination.

        ``subscription_ids`` is the explicit list of subscriptions to
        query.  When the list is empty, Resource Graph fans out across
        every subscription the configured identity is authorised to
        access (the *management-group* / *tenant* mode).
        """
        for sid in subscription_ids:
            if not sid or not isinstance(sid, str):
                raise AzureResourceGraphError(
                    category=AzureResourceGraphErrorCategory.INVALID_RESPONSE,
                    message="subscription_ids must contain non-empty strings",
                )

        return self._query_with_pagination(subscription_ids=list(subscription_ids))

    # ---- internals -------------------------------------------------------

    def _query_with_pagination(
        self,
        *,
        subscription_ids: List[str],
    ) -> AzureResourceGraphResult:
        """Walk the query call and any $skipToken pages.

        Raises :class:`AzureResourceGraphError` on a non-2xx response
        or network failure.  Returns the concatenated row set of the
        final page.
        """
        page_count = 0
        skip_token: Optional[str] = None
        columns: Tuple[str, ...] = ()
        rows: List[Mapping[str, Any]] = []
        total_records = 0

        while True:
            page_count += 1
            if page_count > self._max_pages:
                label = ",".join(
                    safe_subscription_label(s) for s in subscription_ids
                ) or "<tenant>"
                logger.warning(
                    "azure.resource_graph.max_pages_exceeded subs=%s max=%d",
                    label, self._max_pages,
                )
                raise AzureResourceGraphError(
                    category=AzureResourceGraphErrorCategory.UPSTREAM_ERROR,
                    subscription_id=subscription_ids[0] if subscription_ids else None,
                    message=f"pagination exceeded {self._max_pages} pages",
                )

            body: Dict[str, Any] = {
                "query": _build_resources_query(),
                "options": {
                    "resultFormat": "objectArray",
                    "$top": self._page_top,
                },
            }
            if subscription_ids:
                body["subscriptions"] = list(subscription_ids)
            if skip_token:
                body["options"]["$skipToken"] = skip_token

            try:
                token = acquire_access_token()
            except AzureAuthError as exc:
                label = ",".join(
                    safe_subscription_label(s) for s in subscription_ids
                ) or "<tenant>"
                logger.warning(
                    "azure.resource_graph.auth_failed subs=%s category=%s",
                    label, exc.code,
                )
                raise AzureResourceGraphError(
                    category=AzureResourceGraphErrorCategory.AUTH_FAILED,
                    subscription_id=subscription_ids[0] if subscription_ids else None,
                ) from None

            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
            label = ",".join(
                safe_subscription_label(s) for s in subscription_ids
            ) or "<tenant>"

            url = "/providers/Microsoft.ResourceGraph/resources"
            params = {"api-version": self._api_version}
            try:
                response = self._http.post(url, params=params, json=body, headers=headers)
            except httpx.HTTPError as exc:
                logger.warning(
                    "azure.resource_graph.network_error subs=%s code=%s",
                    label, type(exc).__name__,
                )
                raise AzureResourceGraphError(
                    category=AzureResourceGraphErrorCategory.NETWORK_ERROR,
                    subscription_id=subscription_ids[0] if subscription_ids else None,
                ) from None

            if response.status_code != 200:
                self._raise_for_status(
                    response,
                    subscription_ids[0] if subscription_ids else None,
                    label,
                )

            try:
                payload = response.json()
            except ValueError:
                logger.warning(
                    "azure.resource_graph.bad_json subs=%s page=%d",
                    label, page_count,
                )
                raise AzureResourceGraphError(
                    category=AzureResourceGraphErrorCategory.INVALID_RESPONSE,
                    subscription_id=subscription_ids[0] if subscription_ids else None,
                ) from None

            if page_count == 1:
                columns = self._derive_columns(payload.get("data") or [])
            total_records = int(payload.get("totalRecords", total_records or 0))
            truncated = bool(payload.get("dataTruncated", False))
            for row in payload.get("data") or []:
                if isinstance(row, dict):
                    rows.append(row)
            if not truncated:
                break
            skip_token = payload.get("$skipToken")
            if not skip_token:
                # ``dataTruncated=true`` without a skip token would
                # loop forever.  Treat as the end of the result set.
                break

        return AzureResourceGraphResult(
            columns=columns,
            rows=tuple(rows),
            total_records=total_records,
            truncated=False,
        )

    @staticmethod
    def _derive_columns(rows: Sequence[Mapping[str, Any]]) -> Tuple[str, ...]:
        """Derive the column list from the first response.

        Resource Graph with ``objectArray`` does not emit an explicit
        column metadata array — we infer it from the keys of the
        first row, preserving insertion order.
        """
        if not rows:
            return ()
        first = rows[0]
        if not isinstance(first, Mapping):
            return ()
        return tuple(str(key) for key in first.keys())

    @staticmethod
    def _raise_for_status(
        response: httpx.Response,
        subscription_id: Optional[str],
        label: str,
    ) -> None:
        """Map a non-2xx response onto :class:`AzureResourceGraphError`."""
        status = response.status_code
        retry_after = _parse_retry_after(response.headers.get("Retry-After"))
        if status == 401:
            logger.warning(
                "azure.resource_graph.auth_failed subs=%s status=401",
                label,
            )
            raise AzureResourceGraphError(
                category=AzureResourceGraphErrorCategory.AUTH_FAILED,
                subscription_id=subscription_id,
                status_code=status,
                retry_after=retry_after,
            )
        if status == 403:
            logger.warning(
                "azure.resource_graph.forbidden subs=%s status=403",
                label,
            )
            raise AzureResourceGraphError(
                category=AzureResourceGraphErrorCategory.ACCESS_DENIED,
                subscription_id=subscription_id,
                status_code=status,
            )
        if status == 404:
            logger.warning(
                "azure.resource_graph.not_found subs=%s status=404",
                label,
            )
            raise AzureResourceGraphError(
                category=AzureResourceGraphErrorCategory.NOT_FOUND,
                subscription_id=subscription_id,
                status_code=status,
            )
        if status == 429:
            logger.warning(
                "azure.resource_graph.throttled subs=%s status=429 retry_after=%s",
                label, retry_after,
            )
            raise AzureResourceGraphError(
                category=AzureResourceGraphErrorCategory.THROTTLED,
                subscription_id=subscription_id,
                status_code=status,
                retry_after=retry_after,
            )
        if 500 <= status <= 599:
            logger.warning(
                "azure.resource_graph.upstream_error subs=%s status=%d",
                label, status,
            )
            raise AzureResourceGraphError(
                category=AzureResourceGraphErrorCategory.UPSTREAM_ERROR,
                subscription_id=subscription_id,
                status_code=status,
                retry_after=retry_after,
            )
        logger.warning(
            "azure.resource_graph.unexpected_status subs=%s status=%d",
            label, status,
        )
        raise AzureResourceGraphError(
            category=AzureResourceGraphErrorCategory.UPSTREAM_ERROR,
            subscription_id=subscription_id,
            status_code=status,
        )


# ---------------------------------------------------------------------------
# Retry-After parsing
# ---------------------------------------------------------------------------


def _parse_retry_after(value: Optional[str]) -> Optional[int]:
    """Parse an HTTP ``Retry-After`` header.

    Returns the integer number of seconds, or ``None`` when the
    header is absent / malformed / a date we can't easily reason
    about in tests.
    """
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        seconds = int(value)
    except ValueError:
        return None
    if seconds < 0:
        return None
    return seconds


__all__ = [
    "ARM_BASE_URL",
    "MAX_PAGES",
    "PAGE_TOP",
    "RESOURCE_GRAPH_API_VERSION",
    "AzureResourceGraphClient",
    "AzureResourceGraphError",
    "AzureResourceGraphErrorCategory",
    "AzureResourceGraphResult",
    "parse_retry_after_header",
]


def parse_retry_after_header(value: Optional[str]) -> Optional[int]:
    """Public re-export of the retry-after parser."""
    return _parse_retry_after(value)
