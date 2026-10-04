"""Optional enrichment layers — Phase 1.

Two optional enrichers sit on top of the per-service resource
enumeration:

- :func:`search_resource_explorer` — AWS Resource Explorer 2 (``resource-explorer-2``)
- :func:`get_tags_from_tagging_api` — Resource Groups Tagging API (``resourcegroupstaggingapi``)

Both NEVER raise out. When the account has no Resource Explorer index,
is denied access, or hits any other Boto3 error, they return a result
with ``available=False`` and a sanitized ``error_code``. The aggregator
in step 4 presents this as a ``degraded`` flag, not a 500.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from botocore.exceptions import BotoCoreError, ClientError

from app.schemas.aws import ResourceExplorerResult, TaggingApiResult
from app.services.aws.clients import get_aws_client

logger = logging.getLogger("cost-detective-backend.aws.enrichment")


# --- Resource Explorer ---------------------------------------------------


def _is_index_missing(exc: ClientError) -> bool:
    """True if the Resource Explorer index is not configured in this account."""
    code = exc.response.get("Error", {}).get("Code", "")
    # Resource Explorer returns "ResourceNotFoundException" with a
    # message that includes "Index not found" when no index exists.
    return code in ("ResourceNotFoundException", "IndexNotFoundException")


def search_resource_explorer(
    region: Optional[str],
    query: str = "",
) -> ResourceExplorerResult:
    """Search Resource Explorer. Returns ``available=False`` if no index exists.

    AWS Resource Explorer 2 ``Search`` requires ``QueryString`` to be
    present in the request (Boto3 validates the parameter set even when
    the value is the empty string). We therefore always include it: an
    empty ``QueryString`` is documented by AWS as a wildcard / match-all
    query that returns every indexed resource.

    Resource Explorer is regional — the same index may not be in every
    region. When the index is missing, access is denied, or the search
    fails for any other reason, we degrade gracefully so the rest of
    the discovery call still succeeds.
    """
    try:
        client = get_aws_client("resource-explorer-2", region=region)
        # Always send QueryString - empty string is a valid match-all query.
        # If the caller provided a more specific filter, use it.
        params: Dict[str, Any] = {"QueryString": query, "MaxResults": 100}
        response = client.search(**params)
        results: List[Dict[str, Any]] = []
        for entry in response.get("Resources", []) or []:
            results.append(
                {
                    "arn": entry.get("Arn"),
                    "service": entry.get("Service"),
                    "resource_type": entry.get("ResourceType"),
                    "region": entry.get("Region"),
                }
            )
        return ResourceExplorerResult(
            available=True, query=query, results=results
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "ClientError")
        logger.warning("aws.enrichment.resource_explorer error_code=%s", code)
        # Index missing -> degrade silently (account simply hasn't set up RE).
        return ResourceExplorerResult(
            available=False, query=query, error_code=code
        )
    except BotoCoreError as exc:
        code = type(exc).__name__
        logger.warning("aws.enrichment.resource_explorer error_code=%s", code)
        return ResourceExplorerResult(
            available=False, query=query, error_code=code
        )


# --- Resource Groups Tagging API -----------------------------------------


def get_tags_from_tagging_api(
    region: Optional[str],
    arns: Optional[List[str]] = None,
) -> TaggingApiResult:
    """Fetch tags via the Resource Groups Tagging API.

    When ``arns`` is empty or None, returns an empty ``tags_by_arn``
    dict with ``available=True`` (caller had nothing to enrich).
    On ``AccessDenied`` or any other Boto3 error, returns
    ``available=False`` with the sanitized ``error_code``.
    """
    arns = arns or []
    if not arns:
        return TaggingApiResult(available=True, tags_by_arn={})

    try:
        client = get_aws_client("resourcegroupstaggingapi", region=region)
        tags_by_arn: Dict[str, Dict[str, str]] = {}
        # Tagging API paginates via PaginationToken.
        pagination_token: Optional[str] = None
        while True:
            kwargs: Dict[str, Any] = {"ResourcesPerPage": 100}
            if arns:
                # ResourceARNList accepts up to 100 ARNs per call.
                kwargs["ResourceARNList"] = arns[:100]
            if pagination_token:
                kwargs["PaginationToken"] = pagination_token
            response = client.get_resources(**kwargs)
            for r in response.get("ResourceTagMappingList", []) or []:
                arn = r.get("ResourceARN")
                if not arn:
                    continue
                tags = {
                    t.get("Key"): t.get("Value")
                    for t in (r.get("Tags") or [])
                    if t.get("Key")
                }
                tags_by_arn[arn] = tags
            pagination_token = response.get("PaginationToken")
            if not pagination_token:
                break
        return TaggingApiResult(available=True, tags_by_arn=tags_by_arn)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "ClientError")
        logger.warning("aws.enrichment.tagging error_code=%s", code)
        return TaggingApiResult(available=False, error_code=code)
    except BotoCoreError as exc:
        code = type(exc).__name__
        logger.warning("aws.enrichment.tagging error_code=%s", code)
        return TaggingApiResult(available=False, error_code=code)


# --- Aggregator (used by the API layer in step 4) ----------------------


def collect_enrichment(region: Optional[str]) -> Dict[str, Any]:
    """Run both enrichment layers and return their serialized envelopes."""
    re = search_resource_explorer(region=region)
    tagging = get_tags_from_tagging_api(region=region, arns=None)
    return {
        "resource_explorer": re.model_dump(mode="json"),
        "tagging": tagging.model_dump(mode="json"),
    }
