"""Azure Advisor cost-recommendation client — Phase 7C.2D.

This module wraps the Microsoft Resource Graph ``advisorresources``
table to retrieve ACTIVE Cost recommendations for one or more Azure
subscriptions.

.. code-block:: text

    POST https://management.azure.com/providers/Microsoft.ResourceGraph/
         resources?api-version=2024-04-01

Authentication reuses Phase 7C.2A/B/C's
:class:`app.services.cloud.azure.credential` pipeline.  No new
credentials.

**READ-ONLY**: This client only issues read queries.  It never
calls Advisor recommendation-generation, never dismisses /
postpones / completes recommendations, and never mutates any
Azure resource.  This is enforced by the fact that the
implementation only ever issues ``POST /providers/Microsoft.
ResourceGraph/resources`` (a read).

KQL
---

The query targets the ``advisorresources`` table — the documented
source for Advisor recommendations exposed through Resource
Graph.  We project only the columns the application needs and
filter to the documented ACTIVE Cost recommendation lifecycle
stage.

Microsoft's consolidated lifecycle field
``properties.recommendationStatus`` is used (NOT the internal
``customerState`` / ``platformState`` fields).  The documented
ACTIVE values are: ``"Active"``.  ``"Completed"`` and
``"Dismissed"`` are excluded.

The query contains NO ``| limit`` / ``| take`` / ``| sample``
operator — pagination is driven by ``options.$top`` and
``options.$skipToken`` (Phase 7C.2C.1 contract).

Savings semantics
-----------------

We surface the explicit ``extendedProperties.annualSavingsAmount``
and ``extendedProperties.savingsCurrency`` when they parse as a
valid Decimal.  ``annualSavingsAmount`` is the documented annual
savings field; we never annualise a monthly amount ourselves.

When ``annualSavingsAmount`` is absent, the recommendation is
STILL returned with ``annual_savings=None`` — the spec explicitly
forbids discarding recommendations that lack savings data.

Currency safety
---------------

Identical to Phase 7C.2B's Cost semantics: when every
recommendation shares one currency, the rollup totals
``potential_annual_savings`` in that currency.  When currencies
differ, the aggregator returns ``savings_status="MIXED_CURRENCY"``
and exposes ``savings_by_currency`` rather than inventing an FX
rate.

Security
--------

* No ``Authorization: Bearer …`` is ever logged.
* No access token / client assertion / STS JWT is ever exposed.
* Subscription IDs are truncated via
  :func:`app.services.cloud.azure.redaction.safe_subscription_label`.
* ``annualSavingsAmount`` / ``savingsCurrency`` values are not
  echoed in error messages; the failure category alone is exposed.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
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

logger = logging.getLogger("cost-detective.azure.advisor")


# ---------------------------------------------------------------------------
# Constants — pinned for Phase 7C.2D.
# ---------------------------------------------------------------------------

#: Microsoft Resource Graph Query API version (same as Resource Graph).
RESOURCE_GRAPH_API_VERSION: str = "2024-04-01"

#: ARM base URL.
ARM_BASE_URL: str = "https://management.azure.com"

#: Page size requested via ``options.$top``.
PAGE_TOP: int = 1000

#: Defensive loop guard.
MAX_PAGES: int = 50

#: Consolidated Advisor lifecycle status — Active state.
#:
#: Microsoft Learn documents the consolidated
#: ``properties.recommendationStatus`` field with the following
#: values:
#:
#:   * ``"New"``         — newly generated, action required.
#:   * ``"InProgress"``  — remediation is in progress.
#:   * ``"Completed"``   — remediation has completed.
#:   * ``"Postponed"``   — user postponed the recommendation.
#:   * ``"Dismissed"``   — user dismissed the recommendation.
#:
#: Microsoft's documented "Active (new) recommendations" query
#: filters ``recommendationStatus == "New"``.  The literal value
#: ``"Active"`` is NOT a documented ``recommendationStatus``
#: value — it is the Azure Portal's user-facing concept that
#: encompasses ``"New"`` (and sometimes ``"InProgress"``).
#:
#: Phase 7C.2D.1 surfaces ``"New"`` only, matching the
#: documented query.  ``"InProgress"`` is intentionally NOT
#: surfaced because the project treats the Advisor Cost surface
#: as a "newly generated, action required" snapshot; remediation
#: tracking belongs to a separate workflow.
ACTIVE_STATUS: str = "New"

#: Recommendation category — only "Cost" is in scope for Phase 7C.2D.
COST_CATEGORY: str = "Cost"

#: Azure resource-type identifier for Advisor recommendations.
ADVISOR_RECOMMENDATION_TYPE: str = "microsoft.advisor/recommendations"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AzureAdvisorErrorCategory(str, Enum):
    AUTH_FAILED = "AUTH_FAILED"
    ACCESS_DENIED = "ACCESS_DENIED"
    NOT_FOUND = "NOT_FOUND"
    THROTTLED = "THROTTLED"
    UPSTREAM_ERROR = "UPSTREAM_ERROR"
    NETWORK_ERROR = "NETWORK_ERROR"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    PAGINATION_INCOMPLETE = "PAGINATION_INCOMPLETE"


@dataclass
class AzureAdvisorError(Exception):
    """Sanitized Azure Advisor (via Resource Graph) failure."""

    category: AzureAdvisorErrorCategory
    subscription_id: Optional[str] = None
    status_code: Optional[int] = None
    retry_after: Optional[int] = None
    message: Optional[str] = None

    def __str__(self) -> str:  # pragma: no cover - defensive
        label = safe_subscription_label(self.subscription_id)
        return f"[{self.category.value}] azure advisor failed sub={label}"


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AzureAdvisorRecommendation:
    """A single Azure Advisor cost recommendation, normalised.

    The shape is **provider-neutral** — it matches the Phase 7C
    :class:`CloudRecommendation` model the aggregator produces so
    the route layer can serialise either AWS or Azure adapter
    output into the same envelope.
    """

    recommendation_id: str
    subscription_id: str
    resource_id: str
    resource_type: str
    resource_group: Optional[str]
    recommendation_type_id: str
    subcategory: Optional[str]
    category: str
    impact: Optional[str]
    title: Optional[str]
    description: Optional[str]
    solution: Optional[str]
    annual_savings: Optional[Decimal]
    savings_currency: Optional[str]
    term: Optional[str]
    lookback_period: Optional[str]
    last_updated: Optional[str]
    recommendation_status: str
    raw: Mapping[str, Any]


# ---------------------------------------------------------------------------
# KQL query — hardcoded; no caller input.
# ---------------------------------------------------------------------------


def _build_active_cost_query() -> str:
    """Return the KQL query that surfaces ACTIVE Cost recommendations.

    IMPORTANT: Do NOT add ``| limit`` / ``| take`` / ``| sample``
    to this query — they break ``$skipToken`` continuation.  Page
    size is controlled by ``options.$top``.

    The query projects:

    * subscriptionId — for per-subscription attribution.
    * id — the canonical Azure resource id (used as
      ``recommendation_id``).
    * name — display name.
    * type — always ``microsoft.advisor/recommendations``.
    * properties — the full Advisor recommendation properties bag.
      We parse only the documented sub-fields inside the
      aggregator; we never echo ``properties`` or
      ``properties.extendedProperties`` JSON to the caller.

    Filters:

    * ``type == "microsoft.advisor/recommendations"`` — only the
      recommendation resource type; excludes related advisor
      resources (e.g. scorecards).
    * ``properties.category == "Cost"`` — only Cost recommendations
      (Reliability / Security / Performance / OperationalExcellence
      are out of scope for this phase).
    * ``properties.recommendationStatus == "<ACTIVE_STATUS>"`` —
      only currently-actionable recommendations.  Phase 7C.2D.1
      uses Microsoft's documented ``"New"`` lifecycle value
      (NOT the literal string ``"Active"`` which is the Azure
      Portal concept, not a Resource Graph value).
    """
    return (
        'advisorresources '
        '| where type == "microsoft.advisor/recommendations" '
        '| where properties.category == "Cost" '
        f'| where properties.recommendationStatus == "{ACTIVE_STATUS}" '
        '| project subscriptionId, id, name, type, properties'
    )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class AzureAdvisorClient:
    """REST wrapper around Resource Graph for Advisor Cost recommendations."""

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
            headers={"User-Agent": "cost-detective-azure-advisor/phase7c2d"},
        )

    def close(self) -> None:
        try:
            self._http.close()
        except Exception:  # noqa: BLE001 - defensive
            pass

    # ---- public API ------------------------------------------------------

    def query_active_cost_recommendations(
        self,
        *,
        subscription_ids: Sequence[str],
    ) -> List[Mapping[str, Any]]:
        """Run the KQL query and return the raw ``data`` rows.

        Pagination follows the Phase 7C.2C.1 contract: ``$top`` +
        ``$skipToken`` + ``MAX_PAGES`` loop guard + repeated-cursor
        detection + truncated-without-token detection.  Identical
        query + ``$top`` + subscriptions across every page; only
        ``$skipToken`` mutates.

        Returns the raw ``data`` rows.  The aggregator parses each
        row into :class:`AzureAdvisorRecommendation` so the raw
        Azure payload never escapes this module.
        """
        for sid in subscription_ids:
            if not sid or not isinstance(sid, str):
                raise AzureAdvisorError(
                    category=AzureAdvisorErrorCategory.INVALID_RESPONSE,
                    message="subscription_ids must contain non-empty strings",
                )

        return self._query_with_pagination(subscription_ids=list(subscription_ids))

    # ---- internals -------------------------------------------------------

    def _query_with_pagination(
        self,
        *,
        subscription_ids: List[str],
    ) -> List[Mapping[str, Any]]:
        page_count = 0
        skip_token: Optional[str] = None
        rows: List[Mapping[str, Any]] = []
        seen_skip_tokens: Set[str] = set()
        last_page_signature: Optional[Tuple] = None

        while True:
            page_count += 1
            if page_count > self._max_pages:
                label = ",".join(
                    safe_subscription_label(s) for s in subscription_ids
                ) or "<tenant>"
                logger.warning(
                    "azure.advisor.max_pages_exceeded subs=%s max=%d",
                    label, self._max_pages,
                )
                raise AzureAdvisorError(
                    category=AzureAdvisorErrorCategory.UPSTREAM_ERROR,
                    subscription_id=subscription_ids[0] if subscription_ids else None,
                    message=f"pagination exceeded {self._max_pages} pages",
                )

            body: Dict[str, Any] = {
                "query": _build_active_cost_query(),
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
                    "azure.advisor.auth_failed subs=%s category=%s",
                    label, exc.code,
                )
                raise AzureAdvisorError(
                    category=AzureAdvisorErrorCategory.AUTH_FAILED,
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
                response = self._http.post(
                    url, params=params, json=body, headers=headers
                )
            except httpx.HTTPError as exc:
                logger.warning(
                    "azure.advisor.network_error subs=%s code=%s",
                    label, type(exc).__name__,
                )
                raise AzureAdvisorError(
                    category=AzureAdvisorErrorCategory.NETWORK_ERROR,
                    subscription_id=subscription_ids[0] if subscription_ids else None,
                ) from None

            if response.status_code == 204:
                logger.info(
                    "azure.advisor.no_content subs=%s page=%d",
                    label, page_count,
                )
                return []
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
                    "azure.advisor.bad_json subs=%s page=%d",
                    label, page_count,
                )
                raise AzureAdvisorError(
                    category=AzureAdvisorErrorCategory.INVALID_RESPONSE,
                    subscription_id=subscription_ids[0] if subscription_ids else None,
                ) from None

            page_data = payload.get("data") or []
            for row in page_data:
                if isinstance(row, Mapping):
                    rows.append(row)

            truncated = bool(payload.get("resultTruncated", False))
            count_in_page = len(page_data)
            total_records = int(payload.get("totalRecords", 0))
            if not truncated:
                break

            logically_complete = count_in_page >= total_records
            new_skip_token = payload.get("$skipToken")
            if logically_complete:
                logger.info(
                    "azure.advisor.complete_despite_truncated subs=%s page=%d count=%d total=%d",
                    label, page_count, count_in_page, total_records,
                )
                break

            if not new_skip_token:
                logger.warning(
                    "azure.advisor.truncated_without_token subs=%s page=%d",
                    label, page_count,
                )
                raise AzureAdvisorError(
                    category=AzureAdvisorErrorCategory.PAGINATION_INCOMPLETE,
                    subscription_id=subscription_ids[0] if subscription_ids else None,
                    message=(
                        "upstream returned resultTruncated=true without a $skipToken; "
                        "cannot continue pagination safely"
                    ),
                )
            if new_skip_token in seen_skip_tokens:
                logger.warning(
                    "azure.advisor.repeated_skip_token subs=%s page=%d",
                    label, page_count,
                )
                raise AzureAdvisorError(
                    category=AzureAdvisorErrorCategory.UPSTREAM_ERROR,
                    subscription_id=subscription_ids[0] if subscription_ids else None,
                    message="upstream returned a repeated $skipToken; aborting",
                )
            seen_skip_tokens.add(new_skip_token)
            skip_token = new_skip_token

        return rows

    @staticmethod
    def _raise_for_status(
        response: httpx.Response,
        subscription_id: Optional[str],
        label: str,
    ) -> None:
        status = response.status_code
        retry_after = _parse_retry_after(response.headers.get("Retry-After"))
        if status == 401:
            raise AzureAdvisorError(
                category=AzureAdvisorErrorCategory.AUTH_FAILED,
                subscription_id=subscription_id,
                status_code=status,
                retry_after=retry_after,
            )
        if status == 403:
            raise AzureAdvisorError(
                category=AzureAdvisorErrorCategory.ACCESS_DENIED,
                subscription_id=subscription_id,
                status_code=status,
            )
        if status == 404:
            raise AzureAdvisorError(
                category=AzureAdvisorErrorCategory.NOT_FOUND,
                subscription_id=subscription_id,
                status_code=status,
            )
        if status == 429:
            raise AzureAdvisorError(
                category=AzureAdvisorErrorCategory.THROTTLED,
                subscription_id=subscription_id,
                status_code=status,
                retry_after=retry_after,
            )
        if 500 <= status <= 599:
            raise AzureAdvisorError(
                category=AzureAdvisorErrorCategory.UPSTREAM_ERROR,
                subscription_id=subscription_id,
                status_code=status,
                retry_after=retry_after,
            )
        raise AzureAdvisorError(
            category=AzureAdvisorErrorCategory.UPSTREAM_ERROR,
            subscription_id=subscription_id,
            status_code=status,
        )


def _parse_retry_after(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    try:
        seconds = int(str(value).strip())
    except ValueError:
        return None
    if seconds < 0:
        return None
    return seconds


def parse_retry_after_header(value: Optional[str]) -> Optional[int]:
    """Public re-export of the retry-after parser."""
    return _parse_retry_after(value)


# ---------------------------------------------------------------------------
# Row normalisation
# ---------------------------------------------------------------------------


_ARM_RESOURCE_ID_RE = re.compile(
    r"^/subscriptions/(?P<sub>[^/]+)/resource[Gg]roups/(?P<rg>[^/]+)/providers/(?P<rest>.+)$"
)
_ARM_RESOURCE_ID_NO_RG_RE = re.compile(
    r"^/subscriptions/(?P<sub>[^/]+)/providers/(?P<rest>.+)$"
)
_ARM_RESOURCE_ID_SUB_ONLY_RE = re.compile(
    r"^/subscriptions/(?P<sub>[^/]+)$"
)


def _split_arm_resource_id(resource_id: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Decompose an ARM resource id into ``(sub, rg, type/name)``.

    Returns ``(sub, rg, type/name)``.  Some Azure resource ids are
    subscription-scoped (no RG); we still extract the subscription.
    """
    if not isinstance(resource_id, str) or not resource_id:
        return None, None, None
    m = _ARM_RESOURCE_ID_RE.match(resource_id)
    if m:
        return m.group("sub"), m.group("rg"), m.group("rest")
    m = _ARM_RESOURCE_ID_NO_RG_RE.match(resource_id)
    if m:
        return m.group("sub"), None, m.group("rest")
    m = _ARM_RESOURCE_ID_SUB_ONLY_RE.match(resource_id)
    if m:
        return m.group("sub"), None, None
    return None, None, None


def _coerce_string(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    return str(value)


def _coerce_decimal(value: Any) -> Optional[Decimal]:
    """Coerce a string-or-numeric value to Decimal.

    Returns ``None`` for missing / blank / non-numeric values so
    the caller can distinguish "Azure did not return this field"
    from "Azure returned zero".  We never invent or annualise —
    only explicit Decimal-parseable values pass.
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        # Booleans are a subclass of int in Python; reject to avoid
        # silently accepting True as 1.
        return None
    if isinstance(value, (int, float)):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None
        try:
            return Decimal(cleaned)
        except InvalidOperation:
            return None
    return None


def _coerce_short_description(short: Any) -> Tuple[Optional[str], Optional[str]]:
    """Pull the human-readable title / problem / solution from
    ``properties.shortDescription``.

    The documented shape is::

        { "problem": "...", "solution": "..." }

    Older recommendations may carry a free-form ``description`` or
    no shortDescription at all.  We tolerate both.
    """
    if not isinstance(short, Mapping):
        return None, None
    problem = _coerce_string(short.get("problem"))
    solution = _coerce_string(short.get("solution"))
    # Use the problem as the human-readable title when present,
    # otherwise fall back to the solution; we never invent a title.
    if problem:
        title = problem
    elif solution:
        title = solution
    else:
        title = None
    return title, solution


def _coerce_extended(ext: Any) -> Mapping[str, Any]:
    if isinstance(ext, Mapping):
        return ext
    return {}


def normalize_recommendation_row(
    row: Mapping[str, Any],
    *,
    subscription_display_name: Optional[str] = None,
) -> AzureAdvisorRecommendation:
    """Translate one Resource Graph ``advisorresources`` row into
    the provider-neutral recommendation shape.

    The Advisor properties bag is the only source of truth.  We
    explicitly project the documented sub-fields we care about
    and never echo ``properties`` or
    ``properties.extendedProperties`` to the caller.
    """
    subscription_id = _coerce_string(row.get("subscriptionId")) or ""
    arm_resource_id = _coerce_string(row.get("id")) or ""
    name = _coerce_string(row.get("name"))
    # ``type`` is always ``microsoft.advisor/recommendations``;
    # we don't echo it into the canonical model because the
    # ``recommendation_type_id`` field already disambiguates
    # different Advisor recommendation types.
    _ = _coerce_string(row.get("type"))

    properties = row.get("properties") if isinstance(row.get("properties"), Mapping) else {}
    extended = _coerce_extended(properties.get("extendedProperties"))

    # The ARM id of the target resource is usually inside
    # properties.resourceMetadata.resourceId; some Advisor records
    # surface it at the top level instead.  We accept both and
    # prefer the explicit ``resourceMetadata.resourceId``.
    resource_metadata = (
        properties.get("resourceMetadata")
        if isinstance(properties.get("resourceMetadata"), Mapping)
        else {}
    )
    target_resource_id = (
        _coerce_string(resource_metadata.get("resourceId"))
        or arm_resource_id
    )

    # Extract subscription / resource-group / resource type from
    # the target resource's ARM id (if present).
    sub_from_id, rg_from_id, type_name_from_id = _split_arm_resource_id(
        target_resource_id
    )
    if sub_from_id and not subscription_id:
        subscription_id = sub_from_id
    resource_group = rg_from_id
    # Prefer ``properties.impactedField`` (e.g.
    # ``"Microsoft.Compute/virtualMachines"``) as the resource type
    # because it is the canonical Azure resource type.  Fall back
    # to the parsed ARM-id provider segment.
    resource_type = _coerce_string(properties.get("impactedField"))
    if not resource_type and type_name_from_id:
        # ``type_name_from_id`` is ``providers/<type>/<name>``; we
        # surface only the ``<type>`` portion.
        resource_type = type_name_from_id.split("/", 1)[0] or "unknown"

    title, solution = _coerce_short_description(properties.get("shortDescription"))

    annual_savings = _coerce_decimal(extended.get("annualSavingsAmount"))
    savings_currency = _coerce_string(extended.get("savingsCurrency"))
    term = _coerce_string(extended.get("term"))
    lookback_period = _coerce_string(extended.get("lookbackPeriod"))

    # ``properties.description`` is the human-readable free-form
    # long-form text.  We carry it on the canonical model as
    # ``description``.  When shortDescription is absent we fall
    # back to ``properties.shortDescription.problem`` so the UI
    # always has SOMETHING to display.
    description = _coerce_string(properties.get("description"))
    if not description and title:
        description = title

    impact = _coerce_string(properties.get("impact"))
    recommendation_status = _coerce_string(
        properties.get("recommendationStatus")
    ) or "Unknown"
    recommendation_type_id = (
        _coerce_string(properties.get("recommendationTypeId")) or ""
    )
    subcategory = _coerce_string(extended.get("recommendationSubCategory"))
    last_updated = _coerce_string(properties.get("lastUpdated"))

    # The canonical ``recommendation_id`` is the Advisor row's
    # full ARM id.  It is guaranteed unique within a tenant and is
    # stable across pagination windows.
    recommendation_id = arm_resource_id

    return AzureAdvisorRecommendation(
        recommendation_id=recommendation_id,
        subscription_id=subscription_id,
        resource_id=target_resource_id or recommendation_id,
        resource_type=resource_type or "unknown",
        resource_group=resource_group,
        recommendation_type_id=recommendation_type_id,
        subcategory=subcategory,
        category=COST_CATEGORY,
        impact=impact,
        title=title,
        description=description,
        solution=solution,
        annual_savings=annual_savings,
        savings_currency=savings_currency,
        term=term,
        lookback_period=lookback_period,
        last_updated=last_updated,
        recommendation_status=recommendation_status,
        raw={
            "resourceMetadata": dict(resource_metadata) if resource_metadata else {},
        },
    )


__all__ = [
    "ACTIVE_STATUS",
    "ADVISOR_RECOMMENDATION_TYPE",
    "ARM_BASE_URL",
    "COST_CATEGORY",
    "MAX_PAGES",
    "PAGE_TOP",
    "RESOURCE_GRAPH_API_VERSION",
    "AzureAdvisorClient",
    "AzureAdvisorError",
    "AzureAdvisorErrorCategory",
    "AzureAdvisorRecommendation",
    "normalize_recommendation_row",
    "parse_retry_after_header",
]
