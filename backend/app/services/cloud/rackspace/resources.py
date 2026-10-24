"""Rackspace canonical resource normalization — Phase 9B.2.

Translates read-only Rackspace Cloud product inventories into the
provider-neutral :class:`~app.services.cloud.models.CloudResource` /
:class:`~app.services.cloud.models.CloudResourceSummary` shapes so
the canonical ``/api/cloud/resources?provider=RACKSPACE`` route can
serve Rackspace alongside AWS and Azure with the SAME envelope.

Service-catalog mapping
-----------------------

Every product endpoint is resolved from the Rackspace Identity
service catalog (Phase 9A infrastructure).  Nothing is hard-coded:

=====================  ===========================================
canonical resource     catalog identifiers (type or name)
=====================  ===========================================
``SERVER``             ``cloudServersOpenStack`` / ``cloudServers`` /
                       ``compute``  → GET ``/servers/detail`` and
                       GET ``/flavors/detail``
``VOLUME``             ``cloudBlockStorage`` / ``volumev2`` →
                       GET ``/volumes/detail``
``LOAD_BALANCER``      ``cloudLoadBalancers`` / ``rax:load-balancer``
                       → GET ``/loadbalancers``
=====================  ===========================================

Status semantics (deliberate — mirrors the Phase 9A lesson)
-----------------------------------------------------------

* **Empty success is SUCCESS.**  A HTTP 200 with ``{"servers": []}``
  is a *successful* inventory of an empty tenant — never an error,
  never NO_DATA, never UNAVAILABLE.  Zero servers / zero volumes /
  zero load balancers with ``total_resources = 0`` is a first-class
  successful result.
* **Partial success is preserved.**  Each (service, region) fetch is
  failure-isolated; one unavailable service never discards the
  resources another service returned.  Failures are surfaced as
  sanitised per-service errors (``subscription_errors`` on the wire
  envelope, keyed to the safe Cloud tenant id) and the overall
  status becomes ``PARTIAL_SUCCESS``.
* **A failed API call is never an empty list.**  Total failure (for
  example an Identity authentication failure, or every region of
  every service failing) raises :class:`RackspaceResourceError` so
  the canonical route maps it onto a precise HTTP error envelope.

Multi-region
------------

The Identity catalog publishes one endpoint per authorized
datacenter.  Discovery enumerates every regional endpoint per
service, queries each safely, stamps the region onto every
normalized resource, and aggregates with duplicate prevention.  The
canonical identity is
``RACKSPACE|<tenant>|<region>|<resource_type>|<provider_resource_id>``
— Rackspace ids are not assumed globally unique, so the composite id
is constructed defensively per Phase 9B.2 requirements.

Read-only guarantee
-------------------

Only ``GET`` product requests are issued (plus the single Identity
authentication ``POST`` owned by the Phase 9A infrastructure — the
one permitted write-like verb in the entire integration).

Security / redaction
--------------------

Normalised metadata never includes server ``addresses`` (IPs),
``adminPass``, ``personality``, user-data, the OpenStack server
``metadata`` dict (arbitrary customer key/values), ``hostId``, or
``OS-EXT-SRV-ATTR`` host attributes.  Nothing logs the API key,
X-Auth-Token, or the Billing Account Number (RAN) — the RAN has no
role on the resource path and is never read here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from app.db.models import CloudAccount as _CloudAccountRow
from app.services.cloud.models import (
    CloudProviderKind,
    CloudResource,
    CloudResourceSummary,
)
from app.services.rackspace.exceptions import (
    RackspaceAuthError,
    RackspaceConfigError,
    RackspaceError,
    RackspaceNotFoundError,
    RackspacePermissionError,
    RackspaceThrottledError,
    RackspaceTransportError,
)
from app.services.rackspace.identity import ELEVATED_ROLES

logger = logging.getLogger("cost-detective.cloud.rackspace.resources")


#: Canonical resource source identifier for the Rackspace provider.
#: One canonical source mirrors the AWS (``AWS_INVENTORY``) and Azure
#: (``AZURE_RESOURCE_GRAPH``) convention — per-service sources are
#: carried in ``extensions.service_status``.
RACKSPACE_RESOURCE_SOURCE = "RACKSPACE_CLOUD_API"

#: Canonical resource types (Phase 9B.2 scope).
RACKSPACE_SERVER_TYPE = "SERVER"
RACKSPACE_VOLUME_TYPE = "VOLUME"
RACKSPACE_LOAD_BALANCER_TYPE = "LOAD_BALANCER"

#: Service display names — chosen to match the Billing V2 offering
#: labels so the cost surface and the resource surface agree.
SERVICE_CLOUD_SERVERS = "Cloud Servers"
SERVICE_CLOUD_BLOCK_STORAGE = "Cloud Block Storage"
SERVICE_CLOUD_LOAD_BALANCERS = "Cloud Load Balancers"

#: Catalog identifiers (matched against both ``type`` and ``name``).
SERVERS_SERVICE_IDENTIFIERS: Tuple[str, ...] = (
    "cloudServersOpenStack",
    "cloudServers",
    "compute",
)
VOLUMES_SERVICE_IDENTIFIERS: Tuple[str, ...] = (
    "cloudBlockStorage",
    "volumev2",
    "volumev3",
)
LOAD_BALANCER_SERVICE_IDENTIFIERS: Tuple[str, ...] = (
    "rax:load-balancer",
    "cloudLoadBalancers",
)

#: Fields that must NEVER be copied from a server payload into the
#: canonical resource, regardless of what upstream returns.  The
#: canonical layer is the last line of defence.
_FORBIDDEN_SERVER_FIELDS = frozenset({
    "addresses", "accessIPv4", "accessIPv6", "adminPass", "personality",
    "user_data", "metadata", "hostId", "key_name", "progress",
    "OS-EXT-SRV-ATTR:host", "OS-EXT-SRV-ATTR:hypervisor_hostname",
    "OS-EXT-SRV-ATTR:instance_name",
})


# ---------------------------------------------------------------------------
# Error model
# ---------------------------------------------------------------------------


class RackspaceResourceErrorCategory(str, Enum):
    """Stable failure categories for the canonical Rackspace resource path."""

    NOT_CONFIGURED = "NOT_CONFIGURED"
    AUTH_FAILED = "AUTH_FAILED"
    ACCESS_DENIED = "ACCESS_DENIED"
    NOT_FOUND = "NOT_FOUND"
    THROTTLED = "THROTTLED"
    NETWORK_ERROR = "NETWORK_ERROR"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    UNSUPPORTED = "UNSUPPORTED"


class RackspaceResourceError(Exception):
    """A sanitized, typed failure from the canonical Rackspace resource path.

    ``message`` never contains credentials, X-Auth-Token values, the
    RAN, or raw response bodies — only stable category semantics.
    """

    def __init__(
        self,
        category: RackspaceResourceErrorCategory,
        message: str = "",
        *,
        retry_after: Optional[float] = None,
    ) -> None:
        super().__init__(f"{category.value}: {message}")
        self.category = category
        self.message = message
        self.retry_after = retry_after


def _translate_exception(
    exc: RackspaceError,
) -> Tuple[RackspaceResourceErrorCategory, str, Optional[float]]:
    """Map a Phase 9A typed exception onto a canonical category."""
    if isinstance(exc, RackspaceConfigError):
        return (RackspaceResourceErrorCategory.NOT_CONFIGURED, "configuration is missing", None)
    if isinstance(exc, RackspaceAuthError):
        return (RackspaceResourceErrorCategory.AUTH_FAILED, "identity rejected the credentials", None)
    if isinstance(exc, RackspacePermissionError):
        return (RackspaceResourceErrorCategory.ACCESS_DENIED, "access denied", None)
    if isinstance(exc, RackspaceNotFoundError):
        return (RackspaceResourceErrorCategory.NOT_FOUND, "endpoint not available", None)
    if isinstance(exc, RackspaceThrottledError):
        return (RackspaceResourceErrorCategory.THROTTLED, "throttled", getattr(exc, "retry_after", None))
    if isinstance(exc, RackspaceTransportError):
        # Transport errors cover network failures, timeouts, 5xx and
        # non-JSON bodies from the shared Phase 9A client.
        return (RackspaceResourceErrorCategory.NETWORK_ERROR, "transport failure", None)
    return (RackspaceResourceErrorCategory.NETWORK_ERROR, "unexpected failure", None)


@dataclass(frozen=True)
class RackspaceServiceFailure:
    """A sanitized per-(service, region) failure surfaced in the rollup."""

    service: str
    region: str
    category: RackspaceResourceErrorCategory
    message: str
    retry_after: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "service": self.service,
            "region": self.region,
            "code": self.category.value,
            "message": self.message,
        }


@dataclass(frozen=True)
class RackspaceServiceSnapshot:
    """One service's contribution to the resource rollup."""

    service: str
    status: str  # SUCCESS | FAILED
    resources: Tuple[CloudResource, ...]
    regions_queried: Tuple[str, ...]
    error: Optional[RackspaceServiceFailure] = None


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------


def _clean_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _clean_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clean_created(value: Any) -> Optional[str]:
    """Normalise a provider-created timestamp into an ISO-8601 string.

    Rackspace emits ISO-8601 with a ``Z`` suffix (``created`` /
    ``created_at``) or a nested ``{"time": ...}`` object (Cloud Load
    Balancers v1).  A value that cannot be parsed is preserved as its
    string form only when it looks textual — never fabricated.
    """

    raw = value
    if isinstance(raw, dict):
        raw = raw.get("time") or raw.get("created") or raw.get("createdAt")
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    cleaned = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _composite_id(
    *,
    account_id: str,
    region: Optional[str],
    resource_type: str,
    resource_id: str,
) -> str:
    return "|".join(
        (
            CloudProviderKind.RACKSPACE.value,
            account_id or "",
            (region or "").upper(),
            resource_type,
            resource_id,
        )
    )


def normalize_server(
    payload: Mapping[str, Any],
    *,
    account_id: str,
    region: Optional[str],
    flavor_index: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> CloudResource:
    """Translate a Cloud Servers detail row into a CloudResource.

    Only safe operational metadata is retained.  IP addresses,
    admin passwords, personality/user-data, the arbitrary OpenStack
    ``metadata`` dict, and host attributes are explicitly excluded —
    they are never placed on the canonical surface.
    """

    server_id = _clean_str(payload.get("id")) or ""
    name = _clean_str(payload.get("name"))
    status = (_clean_str(payload.get("status")) or "").upper() or None

    flavor = payload.get("flavor") if isinstance(payload.get("flavor"), dict) else {}
    flavor_id = _clean_str(flavor.get("id"))
    flavor_detail = (flavor_index or {}).get(flavor_id or "") if flavor_id else None

    image = payload.get("image") if isinstance(payload.get("image"), dict) else {}
    image_id = _clean_str(image.get("id"))

    metadata: Dict[str, Any] = {}
    if flavor_id:
        metadata["flavor_id"] = flavor_id
    if flavor_detail:
        # Authoritative values from GET /flavors/detail — indexed
        # once per region, never one API call per server.
        if _clean_str(flavor_detail.get("name")):
            metadata["flavor_name"] = _clean_str(flavor_detail.get("name"))
        if _clean_int(flavor_detail.get("ram")) is not None:
            metadata["flavor_ram_mb"] = _clean_int(flavor_detail.get("ram"))
        if _clean_int(flavor_detail.get("vcpus")) is not None:
            metadata["flavor_vcpus"] = _clean_int(flavor_detail.get("vcpus"))
        if _clean_int(flavor_detail.get("disk")) is not None:
            metadata["flavor_disk_gb"] = _clean_int(flavor_detail.get("disk"))
    if image_id:
        metadata["image_id"] = image_id
    availability_zone = _clean_str(payload.get("OS-EXT-AZ:availability_zone"))
    if availability_zone:
        metadata["availability_zone"] = availability_zone
    created = _clean_created(payload.get("created"))
    if created:
        metadata["created"] = created

    return CloudResource(
        provider=CloudProviderKind.RACKSPACE,
        account_id=account_id,
        region=region,
        resource_type=RACKSPACE_SERVER_TYPE,
        resource_id=server_id,
        service=SERVICE_CLOUD_SERVERS,
        name=name,
        state=status,
        composite_id=_composite_id(
            account_id=account_id,
            region=region,
            resource_type=RACKSPACE_SERVER_TYPE,
            resource_id=server_id,
        ),
        metadata=metadata,
    )


def normalize_volume(
    payload: Mapping[str, Any],
    *,
    account_id: str,
    region: Optional[str],
) -> CloudResource:
    """Translate a Cloud Block Storage volume row into a CloudResource.

    A detached volume is a valid resource — it is inventoried with
    ``attachment_state = DETACHED`` so downstream phases (9C) can
    evaluate idle capacity.  No utilization is inferred here.
    """

    volume_id = _clean_str(payload.get("id")) or ""
    name = _clean_str(payload.get("displayName") or payload.get("name"))
    status = (_clean_str(payload.get("status")) or "").upper() or None

    attachments_raw = payload.get("attachments")
    attachments = [
        item for item in (attachments_raw or [])
        if isinstance(item, dict)
    ] if isinstance(attachments_raw, list) else []
    attached_server_ids = sorted(
        {
            server_id
            for server_id in (
                _clean_str(item.get("server_id"))
                for item in attachments
            )
            if server_id
        }
    )

    metadata: Dict[str, Any] = {}
    size_gb = _clean_int(payload.get("size"))
    if size_gb is not None:
        metadata["size_gb"] = size_gb
    volume_type = _clean_str(payload.get("volumeType") or payload.get("volume_type"))
    if volume_type:
        metadata["volume_type"] = volume_type
    metadata["attachment_state"] = "ATTACHED" if attached_server_ids else "DETACHED"
    if attached_server_ids:
        # Server ids only — no device paths or connection credentials.
        metadata["attached_server_ids"] = attached_server_ids
    availability_zone = _clean_str(payload.get("availability_zone"))
    if availability_zone:
        metadata["availability_zone"] = availability_zone
    created = _clean_created(payload.get("created_at") or payload.get("created"))
    if created:
        metadata["created"] = created

    return CloudResource(
        provider=CloudProviderKind.RACKSPACE,
        account_id=account_id,
        region=region,
        resource_type=RACKSPACE_VOLUME_TYPE,
        resource_id=volume_id,
        service=SERVICE_CLOUD_BLOCK_STORAGE,
        name=name,
        state=status,
        composite_id=_composite_id(
            account_id=account_id,
            region=region,
            resource_type=RACKSPACE_VOLUME_TYPE,
            resource_id=volume_id,
        ),
        metadata=metadata,
    )


def normalize_load_balancer(
    payload: Mapping[str, Any],
    *,
    account_id: str,
    region: Optional[str],
) -> CloudResource:
    """Translate a Cloud Load Balancers v1 row into a CloudResource.

    Only descriptive configuration is retained (protocol / port /
    algorithm / created).  Node lists and virtual IP addresses are
    deliberately excluded — they are not needed for inventory and
    carry operational detail with no FinOps value in 9B.2.
    """

    lb_id = _clean_str(payload.get("id")) or ""
    name = _clean_str(payload.get("name"))
    status = (_clean_str(payload.get("status")) or "").upper() or None

    metadata: Dict[str, Any] = {}
    protocol = _clean_str(payload.get("protocol"))
    if protocol:
        metadata["protocol"] = protocol
    port = _clean_int(payload.get("port"))
    if port is not None:
        metadata["port"] = port
    algorithm = _clean_str(payload.get("algorithm"))
    if algorithm:
        metadata["algorithm"] = algorithm
    created = _clean_created(payload.get("created"))
    if created:
        metadata["created"] = created

    return CloudResource(
        provider=CloudProviderKind.RACKSPACE,
        account_id=account_id,
        region=region,
        resource_type=RACKSPACE_LOAD_BALANCER_TYPE,
        resource_id=lb_id,
        service=SERVICE_CLOUD_LOAD_BALANCERS,
        name=name,
        state=status,
        composite_id=_composite_id(
            account_id=account_id,
            region=region,
            resource_type=RACKSPACE_LOAD_BALANCER_TYPE,
            resource_id=lb_id,
        ),
        metadata=metadata,
    )


# ---------------------------------------------------------------------------
# Per-service fetch (failure-isolated per region)
# ---------------------------------------------------------------------------


class _Inventory:
    """Adapter seam over :class:`RackspaceInventoryClient`.

    Exists so tests can inject a stub transport without touching the
    process-wide singletons; production callers pass ``None`` and the
    shared client is resolved lazily.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    async def session(self):
        if self._client is None:
            from app.services.rackspace import get_inventory_client

            self._client = get_inventory_client()
        return await self._client.current_session()

    async def endpoints(self, identifiers: Sequence[str]):
        session = await self.session()
        return self._client.endpoints_for(session, identifiers)

    async def collection(self, base_url: str, path: str, key: str):
        return await self._client.fetch_collection(
            base_url=base_url, path=path, collection_key=key,
        )


def _dominant_failure(
    region_failures: Sequence[RackspaceServiceFailure],
) -> RackspaceServiceFailure:
    """Pick the failure to surface when a service failed everywhere.

    NOT_CONFIGURED wins when present (an unconfigured deployment is
    a different remediation than a broken product API); otherwise the
    first observed failure carries its real category — a service
    that failed with 403s everywhere is ACCESS_DENIED, not a generic
    network error.
    """

    for failure in region_failures:
        if failure.category == RackspaceResourceErrorCategory.NOT_CONFIGURED:
            return failure
    if region_failures:
        return region_failures[0]
    return RackspaceServiceFailure(
        service="",
        region="",
        category=RackspaceResourceErrorCategory.NETWORK_ERROR,
        message="no regional endpoint could be queried",
    )


async def _fetch_servers(
    inventory: _Inventory, *, account_id: str, warnings: List[str]
) -> RackspaceServiceSnapshot:
    """Enumerate Cloud Servers across every authorized region."""
    try:
        endpoints = await inventory.endpoints(SERVERS_SERVICE_IDENTIFIERS)
    except RackspaceError as exc:
        category, message, retry_after = _translate_exception(exc)
        return RackspaceServiceSnapshot(
            service=SERVICE_CLOUD_SERVERS,
            status="FAILED",
            resources=(),
            regions_queried=(),
            error=RackspaceServiceFailure(
                service=SERVICE_CLOUD_SERVERS,
                region="",
                category=category,
                message=message,
                retry_after=retry_after,
            ),
        )
    if not endpoints:
        return RackspaceServiceSnapshot(
            service=SERVICE_CLOUD_SERVERS,
            status="FAILED",
            resources=(),
            regions_queried=(),
            error=RackspaceServiceFailure(
                service=SERVICE_CLOUD_SERVERS,
                region="",
                category=RackspaceResourceErrorCategory.UNSUPPORTED,
                message="service not present in the Identity catalog",
            ),
        )

    resources: List[CloudResource] = []
    regions: List[str] = []
    region_failures: List[RackspaceServiceFailure] = []
    for endpoint in endpoints:
        # Authoritative flavor data — fetched ONCE per region and
        # indexed by id so normalisation never issues per-server calls.
        flavor_index: Dict[str, Dict[str, Any]] = {}
        try:
            flavors = await inventory.collection(
                endpoint.public_url, "/flavors/detail", "flavors",
            )
        except RackspaceError as exc:
            # Flavor detail is enrichment, not inventory — degrade to
            # flavor ids only and surface a warning.
            _, message, _retry = _translate_exception(exc)
            warnings.append(f"flavors_detail_unavailable region={endpoint.region or 'unknown'} code={message}")
        else:
            for flavor in flavors:
                flavor_id = _clean_str(flavor.get("id"))
                if flavor_id:
                    flavor_index[flavor_id] = flavor

        try:
            servers = await inventory.collection(
                endpoint.public_url, "/servers/detail", "servers",
            )
        except RackspaceError as exc:
            category, message, retry_after = _translate_exception(exc)
            warnings.append(
                f"servers_region_failed region={endpoint.region or 'unknown'} code={category.value}"
            )
            region_failures.append(
                RackspaceServiceFailure(
                    service=SERVICE_CLOUD_SERVERS,
                    region=endpoint.region,
                    category=category,
                    message=message,
                    retry_after=retry_after,
                )
            )
            continue

        regions.append(endpoint.region or "")
        for server in servers:
            resources.append(
                normalize_server(
                    server,
                    account_id=account_id,
                    region=endpoint.region or None,
                    flavor_index=flavor_index,
                )
            )

    status = "SUCCESS" if regions else "FAILED"
    error = _dominant_failure(region_failures) if not regions else None
    return RackspaceServiceSnapshot(
        service=SERVICE_CLOUD_SERVERS,
        status=status,
        resources=tuple(resources),
        regions_queried=tuple(sorted({r for r in regions if r})),
        error=error,
    )


async def _fetch_volumes(
    inventory: _Inventory, *, account_id: str
) -> RackspaceServiceSnapshot:
    """Enumerate Cloud Block Storage volumes across every region."""
    try:
        endpoints = await inventory.endpoints(VOLUMES_SERVICE_IDENTIFIERS)
    except RackspaceError as exc:
        category, message, retry_after = _translate_exception(exc)
        return RackspaceServiceSnapshot(
            service=SERVICE_CLOUD_BLOCK_STORAGE,
            status="FAILED",
            resources=(),
            regions_queried=(),
            error=RackspaceServiceFailure(
                service=SERVICE_CLOUD_BLOCK_STORAGE,
                region="",
                category=category,
                message=message,
                retry_after=retry_after,
            ),
        )
    if not endpoints:
        return RackspaceServiceSnapshot(
            service=SERVICE_CLOUD_BLOCK_STORAGE,
            status="FAILED",
            resources=(),
            regions_queried=(),
            error=RackspaceServiceFailure(
                service=SERVICE_CLOUD_BLOCK_STORAGE,
                region="",
                category=RackspaceResourceErrorCategory.UNSUPPORTED,
                message="service not present in the Identity catalog",
            ),
        )

    resources: List[CloudResource] = []
    regions: List[str] = []
    region_failures: List[RackspaceServiceFailure] = []
    for endpoint in endpoints:
        try:
            volumes = await inventory.collection(
                endpoint.public_url, "/volumes/detail", "volumes",
            )
        except RackspaceError as exc:
            category, message, retry_after = _translate_exception(exc)
            region_failures.append(
                RackspaceServiceFailure(
                    service=SERVICE_CLOUD_BLOCK_STORAGE,
                    region=endpoint.region,
                    category=category,
                    message=message,
                    retry_after=retry_after,
                )
            )
            continue
        regions.append(endpoint.region or "")
        for volume in volumes:
            resources.append(
                normalize_volume(
                    volume,
                    account_id=account_id,
                    region=endpoint.region or None,
                )
            )

    status = "SUCCESS" if regions else "FAILED"
    error = _dominant_failure(region_failures) if not regions else None
    return RackspaceServiceSnapshot(
        service=SERVICE_CLOUD_BLOCK_STORAGE,
        status=status,
        resources=tuple(resources),
        regions_queried=tuple(sorted({r for r in regions if r})),
        error=error,
    )


async def _fetch_load_balancers(
    inventory: _Inventory, *, account_id: str
) -> RackspaceServiceSnapshot:
    """Enumerate Cloud Load Balancers across every region (Tier 3).

    The v1 load-balancer API is legacy.  Any failure (404 / 403 /
    absent from the catalog) is reported accurately as a per-service
    failure — never forced, and never allowed to discard the other
    services' resources.
    """
    try:
        endpoints = await inventory.endpoints(LOAD_BALANCER_SERVICE_IDENTIFIERS)
    except RackspaceError as exc:
        category, message, retry_after = _translate_exception(exc)
        return RackspaceServiceSnapshot(
            service=SERVICE_CLOUD_LOAD_BALANCERS,
            status="FAILED",
            resources=(),
            regions_queried=(),
            error=RackspaceServiceFailure(
                service=SERVICE_CLOUD_LOAD_BALANCERS,
                region="",
                category=category,
                message=message,
                retry_after=retry_after,
            ),
        )
    if not endpoints:
        return RackspaceServiceSnapshot(
            service=SERVICE_CLOUD_LOAD_BALANCERS,
            status="FAILED",
            resources=(),
            regions_queried=(),
            error=RackspaceServiceFailure(
                service=SERVICE_CLOUD_LOAD_BALANCERS,
                region="",
                category=RackspaceResourceErrorCategory.UNSUPPORTED,
                message="service not present in the Identity catalog",
            ),
        )

    resources: List[CloudResource] = []
    regions: List[str] = []
    region_failures: List[RackspaceServiceFailure] = []
    for endpoint in endpoints:
        try:
            load_balancers = await inventory.collection(
                endpoint.public_url, "/loadbalancers", "loadBalancers",
            )
        except RackspaceError as exc:
            category, message, retry_after = _translate_exception(exc)
            region_failures.append(
                RackspaceServiceFailure(
                    service=SERVICE_CLOUD_LOAD_BALANCERS,
                    region=endpoint.region,
                    category=category,
                    message=message,
                    retry_after=retry_after,
                )
            )
            continue
        regions.append(endpoint.region or "")
        for lb in load_balancers:
            resources.append(
                normalize_load_balancer(
                    lb,
                    account_id=account_id,
                    region=endpoint.region or None,
                )
            )

    status = "SUCCESS" if regions else "FAILED"
    error = _dominant_failure(region_failures) if not regions else None
    return RackspaceServiceSnapshot(
        service=SERVICE_CLOUD_LOAD_BALANCERS,
        status=status,
        resources=tuple(resources),
        regions_queried=tuple(sorted({r for r in regions if r})),
        error=error,
    )


# ---------------------------------------------------------------------------
# Rollup
# ---------------------------------------------------------------------------


def build_resource_summary(
    snapshots: Sequence[RackspaceServiceSnapshot],
    *,
    rows: Sequence[_CloudAccountRow],
    warnings: Sequence[str],
) -> Tuple[CloudResourceSummary, List[CloudResource]]:
    """Fold per-service snapshots into the canonical summary + list.

    Counts derive exclusively from the normalised resource records —
    nothing is fabricated.  The flat list is deduplicated by
    ``composite_id`` (multi-region aggregation guard).
    """

    failures = [snap.error for snap in snapshots if snap.error is not None]
    successful = [snap for snap in snapshots if snap.error is None]

    # Status rollup — an empty SUCCESS is still SUCCESS (Phase 9A
    # lesson: 200 + [] is a valid inventory of an empty tenant).
    if not successful:
        status = "ERROR"
    elif failures:
        status = "PARTIAL_SUCCESS"
    else:
        status = "SUCCESS"

    seen_composite: set = set()
    all_resources: List[CloudResource] = []
    for snap in successful:
        for resource in snap.resources:
            if resource.composite_id in seen_composite:
                continue
            seen_composite.add(resource.composite_id)
            all_resources.append(resource)

    by_type_counts: Dict[str, int] = {}
    by_location_counts: Dict[str, int] = {}
    by_status_counts: Dict[str, int] = {}
    for resource in all_resources:
        key = resource.resource_type or "Unassigned"
        by_type_counts[key] = by_type_counts.get(key, 0) + 1
        location = resource.region if resource.region else "Unassigned"
        by_location_counts[location] = by_location_counts.get(location, 0) + 1
        state = (resource.state or "UNKNOWN").upper()
        by_status_counts[state] = by_status_counts.get(state, 0) + 1

    def _grouped(counts: Dict[str, int]) -> List[Dict[str, Any]]:
        return sorted(
            ({"key": key, "count": count} for key, count in counts.items()),
            key=lambda item: (-item["count"], item["key"]),
        )

    account_ids = sorted({resource.account_id for resource in all_resources})
    by_subscription: List[Dict[str, Any]] = []
    display_names = {row.account_id: row.display_name for row in rows}
    for account in account_ids:
        by_subscription.append(
            {
                "subscription_id": account,
                "display_name": display_names.get(account, account),
                "count": sum(
                    1 for r in all_resources if r.account_id == account
                ),
            }
        )

    services_map: Dict[str, Dict[str, Any]] = {}
    for snap in snapshots:
        services_map[snap.service] = {
            "service": snap.service,
            "status": "ok" if snap.error is None else "error",
            "error_code": snap.error.category.value if snap.error else None,
            "regions": list(snap.regions_queried),
            "count": sum(1 for r in all_resources if r.service == snap.service),
        }

    primary = rows[0] if rows else None
    scope = f"acct:{primary.account_id}" if len(rows) == 1 else "all"
    account_id_value: Optional[str] = (
        primary.account_id if len(rows) == 1 else None
    )

    summary = CloudResourceSummary(
        provider=CloudProviderKind.RACKSPACE,
        account_id=account_id_value,
        scope=scope,
        region=None,
        services=services_map,
        totals_by_account=[
            {
                "subscription_id": entry["subscription_id"],
                "display_name": entry["display_name"],
                "count": entry["count"],
            }
            for entry in by_subscription
        ],
        extensions={
            "status": status,
            "source": RACKSPACE_RESOURCE_SOURCE,
            "total_resources": len(all_resources),
            "by_type": _grouped(by_type_counts),
            "by_location": _grouped(by_location_counts),
            "by_resource_group": [],
            "by_subscription": by_subscription,
            "by_status": _grouped(by_status_counts),
            "subscription_errors": [
                {
                    # Reuses the canonical per-scope error slot: the
                    # scope here is the safe Cloud tenant id and the
                    # label names the service that failed.
                    "subscription_id": primary.account_id if primary else "",
                    "display_name": failure.service,
                    "code": failure.category.value,
                    "message": failure.message,
                }
                for failure in failures
            ],
            "service_status": {
                snap.service: {
                    "status": "SUCCESS" if snap.error is None else "FAILED",
                    "regions": list(snap.regions_queried),
                    "error_code": snap.error.category.value if snap.error else None,
                    "resource_count": sum(
                        1 for r in all_resources if r.service == snap.service
                    ),
                }
                for snap in snapshots
            },
            "regions_queried": sorted(
                {
                    region
                    for snap in snapshots
                    for region in snap.regions_queried
                    if region
                }
            ),
            "warnings": list(warnings),
            "read_only": True,
        },
    )
    return summary, all_resources


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def aggregate_rackspace_resources(
    *,
    rows: Sequence[_CloudAccountRow],
    inventory_client: Any = None,
) -> Tuple[CloudResourceSummary, List[CloudResource], List[RackspaceServiceFailure]]:
    """Aggregate the canonical Rackspace resource inventory for ``rows``.

    Returns ``(summary, flat_resources, service_failures)``.
    ``flat_resources`` is the deduplicated union across every
    successful (service, region) fetch.  Raises
    :class:`RackspaceResourceError` on TOTAL failure — a missing
    configuration, an Identity authentication failure, an elevated
    role, or every service failing everywhere — so the canonical
    route can answer with a precise error envelope instead of a
    misleading empty inventory.
    """
    rows = [row for row in (rows or []) if row is not None]
    if not rows:
        raise RackspaceResourceError(
            RackspaceResourceErrorCategory.NOT_CONFIGURED,
            "no Rackspace accounts are registered",
        )

    inventory = _Inventory(inventory_client)

    # Authenticate once — the single Identity POST owned by Phase 9A.
    try:
        session = await inventory.session()
    except RackspaceConfigError as exc:
        raise RackspaceResourceError(
            RackspaceResourceErrorCategory.NOT_CONFIGURED,
            _translate_exception(exc)[1],
        ) from exc
    except RackspaceAuthError as exc:
        raise RackspaceResourceError(
            RackspaceResourceErrorCategory.AUTH_FAILED,
            _translate_exception(exc)[1],
        ) from exc
    except RackspacePermissionError as exc:
        raise RackspaceResourceError(
            RackspaceResourceErrorCategory.ACCESS_DENIED,
            _translate_exception(exc)[1],
        ) from exc
    except RackspaceTransportError as exc:
        raise RackspaceResourceError(
            RackspaceResourceErrorCategory.NETWORK_ERROR,
            _translate_exception(exc)[1],
        ) from exc

    # Fail closed on elevated roles — the read-only product contract.
    elevated = sorted(set(session.role_names) & set(ELEVATED_ROLES))
    if elevated:
        logger.warning(
            "rackspace.resources.elevated_roles_present count=%d",
            len(elevated),
        )
        raise RackspaceResourceError(
            RackspaceResourceErrorCategory.ACCESS_DENIED,
            "elevated role detected; read-only contract requires observer roles",
        )

    # The safe Cloud tenant id from the Identity session — used for
    # attribution when several registry rows are requested.  The RAN
    # is never read on the resource path.
    warnings: List[str] = []
    snapshots: List[RackspaceServiceSnapshot] = []

    for row in rows:
        account_id = row.account_id or session.tenant_id
        snapshots.append(
            await _fetch_servers(inventory, account_id=account_id, warnings=warnings)
        )
        snapshots.append(
            await _fetch_volumes(inventory, account_id=account_id)
        )
        snapshots.append(
            await _fetch_load_balancers(inventory, account_id=account_id)
        )

    if all(snap.error is not None for snap in snapshots):
        # Every service failed — surface the dominant (first) failure
        # as the total error.  NOT_CONFIGURED wins when present.
        dominant = next(
            (
                snap.error
                for snap in snapshots
                if snap.error
                and snap.error.category == RackspaceResourceErrorCategory.NOT_CONFIGURED
            ),
            snapshots[0].error,
        )
        raise RackspaceResourceError(
            dominant.category,
            dominant.message,
            retry_after=dominant.retry_after,
        )

    summary, resources = build_resource_summary(
        snapshots, rows=rows, warnings=warnings
    )
    failures = [snap.error for snap in snapshots if snap.error is not None]
    return summary, resources, failures


__all__ = [
    "RACKSPACE_RESOURCE_SOURCE",
    "RACKSPACE_LOAD_BALANCER_TYPE",
    "RACKSPACE_SERVER_TYPE",
    "RACKSPACE_VOLUME_TYPE",
    "RackspaceResourceError",
    "RackspaceResourceErrorCategory",
    "RackspaceServiceFailure",
    "RackspaceServiceSnapshot",
    "aggregate_rackspace_resources",
    "build_resource_summary",
    "normalize_load_balancer",
    "normalize_server",
    "normalize_volume",
]
