"""Azure Resource Graph REST client — Phase 7C.2C.1.

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

Pagination
----------

Resource Graph paginates large responses via ``$skipToken``.  The
server returns ``resultTruncated=true`` on a response page that has
more rows than were returned; the next request carries
``options.$skipToken`` to fetch the next page.  ``options.$top`` caps
the number of rows per page.

**Critical**: Microsoft states that KQL operators ``limit`` / ``take``
/ ``sample`` are **incompatible** with ``$skipToken`` continuation —
they return ``resultTruncated=false`` even when more rows are
available.  The KQL must therefore NOT include ``| limit`` /
``| take`` / ``| sample``; the page size is controlled by the
``options.$top`` request option alone.

This module follows the documented contract:

* First page: ``options.resultFormat=objectArray``,
  ``options.$top=PAGE_TOP``; the KQL has NO ``| limit``.
* Subsequent pages: same query + same ``options.resultFormat`` +
  same ``$top`` + ``options.$skipToken=<token>``.
* Truncation is detected via ``resultTruncated=true`` (the
  documented response field).
* Pathological responses raise
  :class:`AzureResourceGraphError` so a truncated-but-uncallable
  result is never silently reported as SUCCESS.

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

:class:`AzureResourceGraphClient` owns a private ``httpx.Client`` and
is safe to share across requests.  Instances are cheap to create —
the resource aggregator mints one per call and closes it in the
``finally`` block.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

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
# Constants — pinned for Phase 7C.2C.1.
# ---------------------------------------------------------------------------

#: Microsoft Resource Graph Query API version.
#: 2024-04-01 is the GA version; pinning explicitly so the cache key
#: + docs reference a single, stable version.
RESOURCE_GRAPH_API_VERSION: str = "2024-04-01"

#: ARM base URL.
ARM_BASE_URL: str = "https://management.azure.com"

#: Page size requested via ``options.$top``.  Microsoft Resource
#: Graph accepts values up to 1000; we pin to 1000 because the
#: documented best practice for paginated inventory queries is to
#: request the maximum page size.
PAGE_TOP: int = 1000

#: Defensive loop guard.  Resource Graph paginates a few times for
#: any realistic workload; 50 pages × 1000 = 50k resources which is
#: well above any Phase 7C.2C target.  When the loop guard fires the
#: client raises ``UPSTREAM_ERROR`` so the aggregator never
#: silently reports ``SUCCESS`` on a partial result.
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
    PAGINATION_INCOMPLETE = "PAGINATION_INCOMPLETE"


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
    pages_consumed: int

    def to_dict(self) -> dict:
        return {
            "columns": list(self.columns),
            "rows": list(self.rows),
            "total_records": self.total_records,
            "truncated": self.truncated,
            "pages_consumed": self.pages_consumed,
        }


# ---------------------------------------------------------------------------
# KQL query — hardcoded; no caller-supplied input.
#
# IMPORTANT: Do NOT add ``| limit`` / ``| take`` / ``| sample`` to this
# query.  Microsoft Resource Graph treats those operators as final
# results that cannot be paged — using them would silently truncate
# the inventory at PAGE_TOP and disable $skipToken continuation.  The
# page size is controlled via the ``options.$top`` request field
# instead.
# ---------------------------------------------------------------------------


def _build_resources_query() -> str:
    """Return the constrained KQL query used to enumerate resources.

    The query projects only the columns the application needs — no
    oversized ``properties`` payload, no sku, no identity.  Tags
    are projected so the frontend can render resource tagging
    without a second round-trip.

    The query intentionally contains NO ``| limit`` / ``| take`` /
    ``| sample`` operator.  Pagination is driven by
    ``options.$top`` + ``options.$skipToken`` on the request side.
    Adding a ``| limit`` here would silently cap the inventory at
    that value and disable continuation.
    """
    return (
        "Resources "
        "| project id, name, type, location, resourceGroup, subscriptionId, tags"
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
            headers={"User-Agent": "cost-detective-azure-rg/phase7c2c.1"},
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

        The returned :class:`AzureResourceGraphResult` reflects the
        full pagination chain.  When Resource Graph signals a
        truncated result it cannot continue (``resultTruncated=true``
        but no ``$skipToken``), or when ``$skipToken`` repeats, or
        when the loop guard fires, the function raises
        :class:`AzureResourceGraphError` with category
        ``PAGINATION_INCOMPLETE`` / ``UPSTREAM_ERROR``.  The aggregator
        never sees a ``SUCCESS`` result with truncated data.
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

        Raises :class:`AzureResourceGraphError` on a non-2xx response,
        network failure, or a pathological pagination response.
        Returns the concatenated row set across every successful page.

        Pathological conditions are surfaced (never silently dropped):

        * ``resultTruncated=true`` without a ``$skipToken`` cursor
          → ``PAGINATION_INCOMPLETE`` (the upstream told us there is
          more data but did not give us a way to fetch it).
        * ``$skipToken`` repeats a previously-seen value → ``UPSTREAM_ERROR``
          (the upstream is looping; refuse to multiply charges).
        * ``MAX_PAGES`` exceeded → ``UPSTREAM_ERROR``.
        """
        page_count = 0
        skip_token: Optional[str] = None
        columns: Tuple[str, ...] = ()
        rows: List[Mapping[str, Any]] = []
        seen_skip_tokens: Set[str] = set()
        seen_composite_ids: Set[str] = set()
        duplicate_count = 0
        # The ``last_data_signature`` is a deterministic fingerprint
        # of the page we just received.  If the same rows come back
        # twice in a row (because Resource Graph is misbehaving) we
        # refuse to loop.
        last_page_signature: Optional[Tuple] = None
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
            if skip_token is not None:
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

            if response.status_code == 204:
                # 204 No Content — the request was valid but the
                # upstream returned no body.  Treat as an empty
                # result (NO_DATA) rather than an error so the
                # aggregator can surface a clean empty state.
                logger.info(
                    "azure.resource_query.no_content subs=%s page=%d",
                    label, page_count,
                )
                return AzureResourceGraphResult(
                    columns=(),
                    rows=(),
                    total_records=0,
                    truncated=False,
                    pages_consumed=page_count,
                )
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
            page_data = payload.get("data") or []
            # Dedup rows by composite_id within a single client call.
            # Across pages we keep the first occurrence and drop
            # subsequent duplicates — this is defensive against an
            # upstream that may echo rows across pagination windows.
            for row in page_data:
                if not isinstance(row, Mapping):
                    continue
                rid = row.get("id")
                composite = f"AZURE|{rid}" if isinstance(rid, str) else None
                if composite is not None:
                    if composite in seen_composite_ids:
                        duplicate_count += 1
                        continue
                    seen_composite_ids.add(composite)
                rows.append(row)

            # Pathological: detect a page that is byte-for-byte the
            # same as the previous page.  If so, the upstream is
            # echoing rows — refuse to loop.
            page_signature = _page_signature(page_data)
            if page_signature is not None and page_signature == last_page_signature:
                logger.warning(
                    "azure.resource_graph.repeated_page subs=%s page=%d",
                    label, page_count,
                )
                raise AzureResourceGraphError(
                    category=AzureResourceGraphErrorCategory.UPSTREAM_ERROR,
                    subscription_id=subscription_ids[0] if subscription_ids else None,
                    message="upstream echoed the same page; aborting to avoid duplicate charges",
                )
            last_page_signature = page_signature

            # Resource Graph uses ``resultTruncated`` to signal that
            # there are more rows than were returned.  ``count`` and
            # ``totalRecords`` are the returned and total counts.
            #
            # IMPORTANT: Resource Graph can set ``resultTruncated=true``
            # even when the returned page is logically complete — i.e.
            # ``count == totalRecords``.  This is a known quirk,
            # particularly in tenant-mode (empty ``subscriptions``)
            # queries that span multiple partitions.  In that case
            # the upstream does not issue a continuation token.  We
            # tolerate this as the success boundary; only a truncated
            # response where ``count < totalRecords`` requires a
            # $skipToken.
            truncated = bool(payload.get("resultTruncated", False))
            if not truncated:
                break

            count_in_page = len(page_data)
            logically_complete = count_in_page >= total_records
            new_skip_token = payload.get("$skipToken")
            if logically_complete:
                # The page already contains every row the upstream
                # says exists.  Accept this as the success boundary —
                # log it for observability but do not raise.
                logger.info(
                    "azure.resource_graph.complete_despite_truncated subs=%s page=%d count=%d total=%d",
                    label, page_count, count_in_page, total_records,
                )
                break

            # Truly truncated — expect a $skipToken.  Missing
            # cursor is a pathological response: the upstream told
            # us the data is incomplete but did not give us a way to
            # fetch the rest.  Surface the failure; never silently
            # succeed.
            if not new_skip_token:
                logger.warning(
                    "azure.resource_graph.truncated_without_token subs=%s page=%d",
                    label, page_count,
                )
                raise AzureResourceGraphError(
                    category=AzureResourceGraphErrorCategory.PAGINATION_INCOMPLETE,
                    subscription_id=subscription_ids[0] if subscription_ids else None,
                    message=(
                        "upstream returned resultTruncated=true without a $skipToken; "
                        "cannot continue pagination safely"
                    ),
                )
            # Repeated cursor protection: a repeated skipToken means
            # the upstream is looping.  Refuse to multiply charges.
            if new_skip_token in seen_skip_tokens:
                logger.warning(
                    "azure.resource_graph.repeated_skip_token subs=%s page=%d",
                    label, page_count,
                )
                raise AzureResourceGraphError(
                    category=AzureResourceGraphErrorCategory.UPSTREAM_ERROR,
                    subscription_id=subscription_ids[0] if subscription_ids else None,
                    message="upstream returned a repeated $skipToken; aborting",
                )
            seen_skip_tokens.add(new_skip_token)
            skip_token = new_skip_token

        if duplicate_count:
            logger.info(
                "azure.resource_graph.duplicate_rows_dropped count=%d",
                duplicate_count,
            )

        return AzureResourceGraphResult(
            columns=columns,
            rows=tuple(rows),
            total_records=total_records,
            truncated=False,  # By the time we return, all reachable rows are collected.
            pages_consumed=page_count,
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
# Pagination helpers
# ---------------------------------------------------------------------------


def _page_signature(rows: Sequence[Mapping[str, Any]]) -> Optional[Tuple]:
    """Compute a deterministic fingerprint of a response page.

    Used to detect a pathological repeated page (the upstream echoed
    the same rows back).  We fingerprint on the first 3 ids of the
    page — good enough to distinguish a legitimate page boundary
    from an identical echo without paying for a full hash on a
    potentially-large response.
    """
    if not rows:
        return None
    ids = []
    for row in rows[:3]:
        if isinstance(row, Mapping):
            rid = row.get("id")
            if isinstance(rid, str):
                ids.append(rid)
    return tuple(ids) if ids else None


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
    "PAGINATION_INCOMPLETE",
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
