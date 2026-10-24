"""Phase 9B.2 — Rackspace canonical resource tests.

Covers :mod:`app.services.cloud.rackspace.resources` (normalization,
aggregation, empty-success, partial-success, failure semantics) and
:mod:`app.services.rackspace.inventory` (multi-region catalog
resolution, marker pagination, duplicate prevention, loop guards).

All Rackspace HTTP is stubbed — no test requires real credentials,
and no test issues a network call.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

import pytest

from app.services.cloud.models import (
    CloudProviderKind,
    CloudResource,
    CloudResourceSummary,
)
from app.services.cloud.rackspace.resources import (
    RACKSPACE_RESOURCE_SOURCE,
    RackspaceResourceError,
    RackspaceResourceErrorCategory,
    aggregate_rackspace_resources,
    build_resource_summary,
    normalize_load_balancer,
    normalize_server,
    normalize_volume,
)
from app.services.rackspace.exceptions import (
    RackspaceAuthError,
    RackspaceConfigError,
    RackspaceNotFoundError,
    RackspacePermissionError,
    RackspaceThrottledError,
    RackspaceTransportError,
)
from app.services.rackspace.identity import (
    ServiceCatalog,
    ServiceEndpoint,
)
from app.services.rackspace.inventory import (
    RackspaceInventoryClient,
    extract_next_marker,
    resolve_service_endpoints,
)


# ---------------------------------------------------------------------------
# Shared fixtures / stubs
# ---------------------------------------------------------------------------


TENANT = "8675309"


class _Row:
    def __init__(
        self, account_id: str = TENANT, display_name: str = "Rackspace Cloud"
    ) -> None:
        self.account_id = account_id
        self.display_name = display_name
        self.provider = "RACKSPACE"
        self.enabled = True


def _catalog(services: Dict[str, List[str]]) -> ServiceCatalog:
    """Build a catalog: service name -> list of regions."""
    endpoints = []
    for service_name, regions in services.items():
        service_type = service_name
        for region in regions:
            endpoints.append(
                ServiceEndpoint(
                    region=region,
                    service_type=service_type,
                    service_name=service_name,
                    public_url=f"https://{region.lower()}.{service_name}.example/v2/{TENANT}",
                )
            )
    return ServiceCatalog(endpoints=endpoints, raw_services=[])


def _server_payload(
    server_id: str,
    *,
    name: str = "web-1",
    status: str = "ACTIVE",
    flavor_id: str = "2",
) -> Dict[str, Any]:
    return {
        "id": server_id,
        "name": name,
        "status": status,
        "flavor": {"id": flavor_id},
        "image": {"id": "centos-7"},
        "created": "2026-09-01T12:34:56.000000Z",
        "OS-EXT-AZ:availability_zone": "DFW",
        # Sensitive fields — must NEVER survive normalisation.
        "addresses": {"private": [{"addr": "10.0.0.1", "version": 4}]},
        "adminPass": "super-secret",
        "metadata": {"customer_secret": "do-not-leak"},
        "hostId": "abc123",
    }


def _flavor_payload(flavor_id: str = "2") -> Dict[str, Any]:
    return {
        "id": flavor_id,
        "name": "2 GB General Purpose v1",
        "ram": 2048,
        "vcpus": 2,
        "disk": 80,
    }


class StubInventoryClient:
    """Stub of :class:`RackspaceInventoryClient` for aggregation tests."""

    def __init__(
        self,
        *,
        catalog: ServiceCatalog,
        responses: Dict[str, Any],
        session_error: Optional[Exception] = None,
        role_names: Optional[List[str]] = None,
    ) -> None:
        self._catalog = catalog
        self._responses = responses  # path -> list payload or exception
        self._session_error = session_error
        self._role_names = role_names or ["observer", "billing:observer"]
        self.collection_calls: List[str] = []
        self.flavor_calls = 0

    async def current_session(self):
        if self._session_error is not None:
            raise self._session_error

        class _Session:
            tenant_id = TENANT
            role_names = list(self._role_names)
            catalog = self._catalog

        return _Session()

    def endpoints_for(self, session, identifiers):
        return resolve_service_endpoints(session, identifiers)

    async def fetch_collection(self, *, base_url, path, collection_key):
        self.collection_calls.append(f"{base_url}{path}")
        if path == "/flavors/detail":
            self.flavor_calls += 1
        result = self._responses.get(path)
        if isinstance(result, Exception):
            raise result
        if result is None:
            return []
        return result


def _inventory(
    *,
    services: Dict[str, List[str]],
    responses: Dict[str, Any],
    session_error: Optional[Exception] = None,
    role_names: Optional[List[str]] = None,
) -> StubInventoryClient:
    return StubInventoryClient(
        catalog=_catalog(services),
        responses=responses,
        session_error=session_error,
        role_names=role_names,
    )


# ---------------------------------------------------------------------------
# Normalisation — servers
# ---------------------------------------------------------------------------


class TestServerNormalization:
    def test_maps_core_fields(self):
        resource = normalize_server(
            _server_payload("srv-1"),
            account_id=TENANT,
            region="DFW",
            flavor_index={"2": _flavor_payload()},
        )
        assert resource.provider == CloudProviderKind.RACKSPACE
        assert resource.account_id == TENANT
        assert resource.region == "DFW"
        assert resource.resource_type == "SERVER"
        assert resource.resource_id == "srv-1"
        assert resource.service == "Cloud Servers"
        assert resource.name == "web-1"
        assert resource.state == "ACTIVE"
        assert resource.composite_id == f"RACKSPACE|{TENANT}|DFW|SERVER|srv-1"

    def test_flavor_metadata_resolved_from_index(self):
        resource = normalize_server(
            _server_payload("srv-1", flavor_id="general1-2"),
            account_id=TENANT,
            region="ORD",
            flavor_index={"general1-2": _flavor_payload("general1-2")},
        )
        assert resource.metadata["flavor_id"] == "general1-2"
        assert resource.metadata["flavor_name"] == "2 GB General Purpose v1"
        assert resource.metadata["flavor_ram_mb"] == 2048
        assert resource.metadata["flavor_vcpus"] == 2
        assert resource.metadata["flavor_disk_gb"] == 80

    def test_missing_optional_fields_never_crash(self):
        resource = normalize_server(
            {"id": "srv-2"},
            account_id=TENANT,
            region=None,
        )
        assert resource.resource_id == "srv-2"
        assert resource.name is None
        assert resource.state is None
        assert resource.metadata == {}
        assert resource.composite_id == f"RACKSPACE|{TENANT}||SERVER|srv-2"

    def test_sensitive_fields_are_never_normalised(self):
        resource = normalize_server(
            _server_payload("srv-1"),
            account_id=TENANT,
            region="DFW",
            flavor_index={"2": _flavor_payload()},
        )
        serialized = str(resource.metadata)
        for forbidden in (
            "10.0.0.1", "super-secret", "customer_secret", "hostId", "abc123",
        ):
            assert forbidden not in serialized
        assert "addresses" not in resource.metadata
        assert "adminPass" not in resource.metadata
        assert "metadata" not in resource.metadata  # nested OpenStack dict

    def test_flavor_id_survives_without_flavor_detail(self):
        resource = normalize_server(
            _server_payload("srv-1", flavor_id="performance1-4"),
            account_id=TENANT,
            region="DFW",
            flavor_index=None,
        )
        assert resource.metadata["flavor_id"] == "performance1-4"
        assert "flavor_name" not in resource.metadata


# ---------------------------------------------------------------------------
# Normalisation — volumes / load balancers
# ---------------------------------------------------------------------------


class TestVolumeNormalization:
    def test_attached_volume(self):
        payload = {
            "id": "vol-1",
            "displayName": "data-vol",
            "size": 150,
            "status": "in-use",
            "volumeType": "SSD",
            "created_at": "2026-08-15T09:00:00Z",
            "availability_zone": "DFW",
            "attachments": [
                {"server_id": "srv-1", "device": "/dev/xvdb"},
                {"server_id": "srv-1", "device": "/dev/xvdc"},
            ],
        }
        resource = normalize_volume(payload, account_id=TENANT, region="DFW")
        assert resource.resource_type == "VOLUME"
        assert resource.service == "Cloud Block Storage"
        assert resource.state == "IN-USE"  # uppercased canonical status
        assert resource.name == "data-vol"
        assert resource.metadata["size_gb"] == 150
        assert resource.metadata["volume_type"] == "SSD"
        assert resource.metadata["attachment_state"] == "ATTACHED"
        assert resource.metadata["attached_server_ids"] == ["srv-1"]
        assert resource.metadata["created"].startswith("2026-08-15T09:00:00")

    def test_detached_volume_is_a_valid_resource(self):
        resource = normalize_volume(
            {"id": "vol-2", "size": 75, "status": "available"},
            account_id=TENANT,
            region="ORD",
        )
        assert resource.resource_type == "VOLUME"
        assert resource.metadata["attachment_state"] == "DETACHED"
        assert "attached_server_ids" not in resource.metadata
        assert resource.state == "AVAILABLE"


class TestLoadBalancerNormalization:
    def test_maps_basic_configuration(self):
        payload = {
            "id": "lb-1",
            "name": "www",
            "status": "ACTIVE",
            "protocol": "HTTP",
            "port": 80,
            "algorithm": "ROUND_ROBIN",
            "created": {"time": "2026-07-01T10:00:00.000000Z"},
            # Node/IP detail must NOT be normalised.
            "nodes": [{"address": "10.1.1.1", "port": 80, "condition": "ENABLED"}],
            "virtualIps": [{"address": "23.253.10.10", "type": "PUBLIC"}],
        }
        resource = normalize_load_balancer(payload, account_id=TENANT, region="DFW")
        assert resource.resource_type == "LOAD_BALANCER"
        assert resource.service == "Cloud Load Balancers"
        assert resource.state == "ACTIVE"
        assert resource.metadata["protocol"] == "HTTP"
        assert resource.metadata["port"] == 80
        assert resource.metadata["algorithm"] == "ROUND_ROBIN"
        assert resource.metadata["created"].startswith("2026-07-01T10:00:00")
        # No IP / node leakage.
        assert "10.1.1.1" not in str(resource.metadata)
        assert "23.253.10.10" not in str(resource.metadata)

    def test_missing_optional_fields(self):
        resource = normalize_load_balancer(
            {"id": "lb-2", "name": "svc", "status": "BUILD"},
            account_id=TENANT,
            region=None,
        )
        assert resource.resource_id == "lb-2"
        assert resource.state == "BUILD"
        assert resource.metadata == {}


# ---------------------------------------------------------------------------
# Pagination (transport-level stub)
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Minimal stand-in for :class:`RackspaceResponse`."""

    def __init__(self, body: Dict[str, Any]) -> None:
        self.body = body


class _FakeTransport:
    """Minimal stand-in for :class:`RackspaceHttpClient`."""

    def __init__(self, pages: List[Dict[str, Any]]) -> None:
        self.pages = pages
        self.requests: List[Optional[Dict[str, Any]]] = []

    async def get(self, base_url, path, *, token_provider, params=None, headers=None):
        self.requests.append(dict(params) if params else None)
        page = self.pages[min(len(self.requests) - 1, len(self.pages) - 1)]
        return _FakeResponse(page)


class _FakeIdentity:
    async def current_token(self):
        return "stub-token"


def _client(pages: List[Dict[str, Any]], *, max_pages: int = 50):
    return RackspaceInventoryClient(
        http_client=_FakeTransport(pages),  # type: ignore[arg-type]
        identity=_FakeIdentity(),  # type: ignore[arg-type]
        max_pages=max_pages,
    )


class TestPagination:
    def test_extract_next_marker_nova_shape(self):
        body = {
            "servers": [{"id": "a"}],
            "servers_links": [
                {"rel": "next", "href": "https://api/servers/detail?marker=a"}
            ],
        }
        assert extract_next_marker(body, "servers") == "a"

    def test_extract_next_marker_lb_singular_shape(self):
        body = {
            "loadBalancers": [{"id": 7}],
            "loadBalancer_links": [
                {"rel": "next", "href": "https://api/loadbalancers?marker=7"}
            ],
        }
        assert extract_next_marker(body, "loadBalancers") == "7"

    def test_extract_next_marker_absent(self):
        assert extract_next_marker({"servers": []}, "servers") is None

    def test_single_page(self):
        client = _client([{"servers": [{"id": "a"}, {"id": "b"}]}])
        items = client_fetch = None
        import asyncio

        items = asyncio.run(
            client.fetch_collection(
                base_url="https://api", path="/servers/detail", collection_key="servers"
            )
        )
        assert [i["id"] for i in items] == ["a", "b"]
        assert len(client._http.requests) == 1

    def test_multiple_pages_follow_marker(self):
        pages = [
            {
                "servers": [{"id": "a"}, {"id": "b"}],
                "servers_links": [
                    {"rel": "next", "href": "https://api/servers/detail?marker=b"}
                ],
            },
            {
                "servers": [{"id": "c"}],
                "servers_links": [
                    {"rel": "next", "href": "https://api/servers/detail?marker=c"}
                ],
            },
            {"servers": []},
        ]
        client = _client(pages)
        import asyncio

        items = asyncio.run(
            client.fetch_collection(
                base_url="https://api", path="/servers/detail", collection_key="servers"
            )
        )
        assert [i["id"] for i in items] == ["a", "b", "c"]
        # Page 2 requested with marker=b, page 3 with marker=c.
        assert client._http.requests[1] == {"marker": "b"}
        assert client._http.requests[2] == {"marker": "c"}

    def test_empty_page_terminates(self):
        client = _client([{"servers": []}])
        import asyncio

        items = asyncio.run(
            client.fetch_collection(
                base_url="https://api", path="/servers/detail", collection_key="servers"
            )
        )
        assert items == []
        assert len(client._http.requests) == 1

    def test_duplicate_ids_are_dropped(self):
        pages = [
            {
                "servers": [{"id": "a"}, {"id": "b"}],
                "servers_links": [
                    {"rel": "next", "href": "https://api/servers/detail?marker=b"}
                ],
            },
            {
                "servers": [{"id": "b"}, {"id": "a"}, {"id": "d"}],
            },
        ]
        client = _client(pages)
        import asyncio

        items = asyncio.run(
            client.fetch_collection(
                base_url="https://api", path="/servers/detail", collection_key="servers"
            )
        )
        assert sorted(i["id"] for i in items) == ["a", "b", "d"]

    def test_unchanged_marker_stops_pagination(self):
        # A hostile endpoint that always returns the same marker —
        # the walker must stop instead of looping forever.
        page = {
            "servers": [{"id": "a"}],
            "servers_links": [
                {"rel": "next", "href": "https://api/servers/detail?marker=a"}
            ],
        }
        client = _client([page])
        import asyncio

        asyncio.run(
            client.fetch_collection(
                base_url="https://api", path="/servers/detail", collection_key="servers"
            )
        )
        # Page 1 (marker=None) + page 2 (marker=a, same next marker) = 2.
        assert len(client._http.requests) == 2

    def test_max_pages_cap(self):
        # Distinct ids + an advancing marker per page so only the
        # page-count cap (not duplicate/marker guards) stops the walk.
        pages = [
            {
                "servers": [{"id": f"p{i}"}],
                "servers_links": [
                    {"rel": "next", "href": f"https://api/servers/detail?marker=m{i}"}
                ],
            }
            for i in range(1, 4)
        ]
        client = _client(pages, max_pages=3)
        import asyncio

        asyncio.run(
            client.fetch_collection(
                base_url="https://api", path="/servers/detail", collection_key="servers"
            )
        )
        assert len(client._http.requests) == 3


# ---------------------------------------------------------------------------
# Catalog resolution (multi-region)
# ---------------------------------------------------------------------------


class _CatalogSession:
    """Session-shaped wrapper — `resolve_service_endpoints` takes a session."""

    def __init__(self, catalog: ServiceCatalog) -> None:
        self.catalog = catalog


class TestCatalogResolution:
    def test_multi_region_endpoints_enumerated(self):
        session_catalog = _catalog(
            {
                "cloudServersOpenStack": ["DFW", "ORD", "IAD"],
                "cloudBlockStorage": ["DFW", "ORD"],
                "rax:load-balancer": ["DFW"],
            }
        )
        endpoints = resolve_service_endpoints(
            _CatalogSession(session_catalog),
            ("cloudServersOpenStack", "cloudServers", "compute"),
        )
        assert [e.region for e in endpoints] == ["DFW", "ORD", "IAD"]
        assert all(
            e.public_url.startswith("https://dfw.")
            or e.public_url.startswith("https://ord.")
            or e.public_url.startswith("https://iad.")
            for e in endpoints
        )

    def test_matches_type_or_name(self):
        catalog = ServiceCatalog(
            endpoints=[
                ServiceEndpoint(
                    region="LON",
                    service_type="compute",
                    service_name="cloudServersOpenStack",
                    public_url="https://lon.compute.example/v2/x",
                )
            ],
            raw_services=[],
        )
        session = _CatalogSession(catalog)
        assert len(resolve_service_endpoints(session, ("compute",))) == 1
        assert len(resolve_service_endpoints(session, ("cloudServersOpenStack",))) == 1
        assert resolve_service_endpoints(session, ("nope",)) == []

    def test_duplicate_urls_deduped(self):
        catalog = ServiceCatalog(
            endpoints=[
                ServiceEndpoint(
                    region="DFW",
                    service_type="compute",
                    service_name="cloudServersOpenStack",
                    public_url="https://same.example/v2/x",
                ),
                ServiceEndpoint(
                    region="DFW",
                    service_type="compute",
                    service_name="cloudServers",
                    public_url="https://same.example/v2/x",
                ),
            ],
            raw_services=[],
        )
        assert len(
            resolve_service_endpoints(_CatalogSession(catalog), ("compute",))
        ) == 1


# ---------------------------------------------------------------------------
# Aggregation semantics
# ---------------------------------------------------------------------------


class TestAggregation:
    @pytest.mark.asyncio
    async def test_empty_inventory_is_success(self):
        """200 + [] is SUCCESS — never ERROR / NO_DATA / UNAVAILABLE."""
        client = _inventory(
            services={
                "cloudServersOpenStack": ["DFW"],
                "cloudBlockStorage": ["DFW"],
                "rax:load-balancer": ["DFW"],
            },
            responses={
                "/servers/detail": [],
                "/flavors/detail": [],
                "/volumes/detail": [],
                "/loadbalancers": [],
            },
        )
        summary, resources, failures = await aggregate_rackspace_resources(
            rows=[_Row()], inventory_client=client
        )
        assert summary.extensions["status"] == "SUCCESS"
        assert resources == []
        assert failures == []
        assert summary.extensions["total_resources"] == 0
        assert summary.extensions["by_type"] == []
        assert summary.extensions["source"] == RACKSPACE_RESOURCE_SOURCE

    @pytest.mark.asyncio
    async def test_multiple_servers_across_regions(self):
        client = _inventory(
            services={
                "cloudServersOpenStack": ["DFW", "ORD"],
                "cloudBlockStorage": ["DFW"],
                "rax:load-balancer": ["DFW"],
            },
            responses={
                "/servers/detail": [
                    _server_payload("srv-dfw-1", name="dfw-web", flavor_id="general1-2"),
                    _server_payload("srv-dfw-2", status="ERROR", name="dfw-broken"),
                ],
                "/flavors/detail": [_flavor_payload("general1-2")],
                "/volumes/detail": [],
                "/loadbalancers": [],
            },
        )
        summary, resources, failures = await aggregate_rackspace_resources(
            rows=[_Row()], inventory_client=client
        )
        assert summary.extensions["status"] == "SUCCESS"
        assert failures == []
        # Both servers come from both regions — the DFW/ORD stub
        # returns the same two servers per region, so 4 total, all
        # with distinct composite ids (region is part of identity).
        assert len(resources) == 4
        regions = {r.region for r in resources}
        assert regions == {"DFW", "ORD"}
        statuses = {r.state for r in resources}
        assert statuses == {"ACTIVE", "ERROR"}
        by_location = {
            entry["key"]: entry["count"]
            for entry in summary.extensions["by_location"]
        }
        assert by_location == {"DFW": 2, "ORD": 2}
        by_type = {
            entry["key"]: entry["count"] for entry in summary.extensions["by_type"]
        }
        assert by_type == {"SERVER": 4}
        by_status = {
            entry["key"]: entry["count"] for entry in summary.extensions["by_status"]
        }
        assert by_status == {"ACTIVE": 2, "ERROR": 2}

    @pytest.mark.asyncio
    async def test_flavor_fetched_once_per_region(self):
        """No N+1: flavors/detail is one call per region, indexed by id."""
        client = _inventory(
            services={"cloudServersOpenStack": ["DFW"], "cloudBlockStorage": [], "rax:load-balancer": []},
            responses={
                "/servers/detail": [
                    _server_payload("srv-1"),
                    _server_payload("srv-2"),
                    _server_payload("srv-3"),
                ],
                "/flavors/detail": [_flavor_payload()],
                "/volumes/detail": [],
                "/loadbalancers": [],
            },
        )
        _summary, resources, _f = await aggregate_rackspace_resources(
            rows=[_Row()], inventory_client=client
        )
        assert client.flavor_calls == 1
        assert all(r.metadata.get("flavor_name") == "2 GB General Purpose v1" for r in resources)
        assert all(r.metadata.get("flavor_ram_mb") == 2048 for r in resources)

    @pytest.mark.asyncio
    async def test_flavor_failure_degrades_to_flavor_id(self):
        client = _inventory(
            # Every service is present in the catalog so inventory
            # itself succeeds — the assertion isolates flavor
            # degradation, not missing-catalog semantics (covered by
            # test_service_missing_from_catalog_is_unsupported).
            services={"cloudServersOpenStack": ["DFW"], "cloudBlockStorage": ["DFW"], "rax:load-balancer": ["DFW"]},
            responses={
                "/servers/detail": [_server_payload("srv-1", flavor_id="general1-4")],
                "/flavors/detail": RackspaceNotFoundError("flavors unavailable"),
                "/volumes/detail": [],
                "/loadbalancers": [],
            },
        )
        summary, resources, failures = await aggregate_rackspace_resources(
            rows=[_Row()], inventory_client=client
        )
        # Inventory itself still succeeds — flavors are enrichment.
        assert summary.extensions["status"] == "SUCCESS"
        assert resources[0].metadata["flavor_id"] == "general1-4"
        assert "flavor_name" not in resources[0].metadata
        assert any(
            "flavors_detail_unavailable" in w for w in summary.extensions["warnings"]
        )

    @pytest.mark.asyncio
    async def test_volumes_and_load_balancers_success(self):
        client = _inventory(
            services={
                "cloudServersOpenStack": ["DFW"],
                "cloudBlockStorage": ["DFW"],
                "rax:load-balancer": ["DFW"],
            },
            responses={
                "/servers/detail": [_server_payload("srv-1")],
                "/flavors/detail": [],
                "/volumes/detail": [
                    {"id": "vol-1", "displayName": "db", "size": 100, "status": "in-use",
                     "attachments": [{"server_id": "srv-1"}]},
                    {"id": "vol-2", "size": 50, "status": "available"},
                ],
                "/loadbalancers": [
                    {"id": "lb-1", "name": "www", "status": "ACTIVE", "protocol": "HTTP"},
                ],
            },
        )
        summary, resources, failures = await aggregate_rackspace_resources(
            rows=[_Row()], inventory_client=client
        )
        assert summary.extensions["status"] == "SUCCESS"
        assert failures == []
        types = sorted(r.resource_type for r in resources)
        assert types == ["LOAD_BALANCER", "SERVER", "VOLUME", "VOLUME"]
        by_type = {
            entry["key"]: entry["count"] for entry in summary.extensions["by_type"]
        }
        assert by_type == {"SERVER": 1, "VOLUME": 2, "LOAD_BALANCER": 1}
        # Summary derives from normalized records only.
        assert summary.extensions["total_resources"] == len(resources) == 4


class TestPartialSuccess:
    @pytest.mark.asyncio
    async def test_one_service_failing_preserves_the_others(self):
        client = _inventory(
            services={
                "cloudServersOpenStack": ["DFW"],
                "cloudBlockStorage": ["DFW"],
                "rax:load-balancer": ["DFW"],
            },
            responses={
                "/servers/detail": [_server_payload("srv-1")],
                "/flavors/detail": [],
                "/volumes/detail": RackspacePermissionError("CBS denied"),
                "/loadbalancers": RackspaceNotFoundError("LB v1 gone"),
            },
        )
        summary, resources, failures = await aggregate_rackspace_resources(
            rows=[_Row()], inventory_client=client
        )
        assert summary.extensions["status"] == "PARTIAL_SUCCESS"
        # Successful resource types are preserved.
        assert [r.resource_type for r in resources] == ["SERVER"]
        # Both failing services surface as sanitized errors.
        codes = sorted(f.category.value for f in failures)
        assert codes == ["ACCESS_DENIED", "NOT_FOUND"]
        sub_errors = summary.extensions["subscription_errors"]
        assert all(e["subscription_id"] == TENANT for e in sub_errors)
        labels = sorted(e["display_name"] for e in sub_errors)
        assert labels == ["Cloud Block Storage", "Cloud Load Balancers"]
        # Per-service status visible for the UI.
        service_status = summary.extensions["service_status"]
        assert service_status["Cloud Servers"]["status"] == "SUCCESS"
        assert service_status["Cloud Block Storage"]["status"] == "FAILED"

    @pytest.mark.asyncio
    async def test_service_missing_from_catalog_is_unsupported(self):
        client = _inventory(
            services={"cloudServersOpenStack": ["DFW"]},
            responses={
                "/servers/detail": [_server_payload("srv-1")],
                "/flavors/detail": [],
            },
        )
        summary, resources, failures = await aggregate_rackspace_resources(
            rows=[_Row()], inventory_client=client
        )
        assert summary.extensions["status"] == "PARTIAL_SUCCESS"
        codes = {f.service: f.category.value for f in failures}
        assert codes["Cloud Load Balancers"] == "UNSUPPORTED"
        assert codes["Cloud Block Storage"] == "UNSUPPORTED"
        assert [r.resource_type for r in resources] == ["SERVER"]


class TestTotalFailures:
    @pytest.mark.asyncio
    async def test_no_rows_raises_not_configured(self):
        with pytest.raises(RackspaceResourceError) as excinfo:
            await aggregate_rackspace_resources(rows=[])
        assert excinfo.value.category == RackspaceResourceErrorCategory.NOT_CONFIGURED

    @pytest.mark.asyncio
    async def test_identity_config_error(self):
        client = _inventory(
            services={},
            responses={},
            session_error=RackspaceConfigError("not configured"),
        )
        with pytest.raises(RackspaceResourceError) as excinfo:
            await aggregate_rackspace_resources(
                rows=[_Row()], inventory_client=client
            )
        assert excinfo.value.category == RackspaceResourceErrorCategory.NOT_CONFIGURED

    @pytest.mark.asyncio
    async def test_identity_auth_error(self):
        client = _inventory(
            services={},
            responses={},
            session_error=RackspaceAuthError("401"),
        )
        with pytest.raises(RackspaceResourceError) as excinfo:
            await aggregate_rackspace_resources(
                rows=[_Row()], inventory_client=client
            )
        assert excinfo.value.category == RackspaceResourceErrorCategory.AUTH_FAILED

    @pytest.mark.asyncio
    async def test_identity_transport_error(self):
        client = _inventory(
            services={},
            responses={},
            session_error=RackspaceTransportError("timeout"),
        )
        with pytest.raises(RackspaceResourceError) as excinfo:
            await aggregate_rackspace_resources(
                rows=[_Row()], inventory_client=client
            )
        assert excinfo.value.category == RackspaceResourceErrorCategory.NETWORK_ERROR

    @pytest.mark.asyncio
    async def test_elevated_role_fails_closed(self):
        client = _inventory(
            services={"cloudServersOpenStack": ["DFW"]},
            responses={},
            role_names=["admin"],
        )
        with pytest.raises(RackspaceResourceError) as excinfo:
            await aggregate_rackspace_resources(
                rows=[_Row()], inventory_client=client
            )
        assert excinfo.value.category == RackspaceResourceErrorCategory.ACCESS_DENIED

    @pytest.mark.asyncio
    async def test_every_service_failing_raises_with_dominant_category(self):
        client = _inventory(
            services={
                "cloudServersOpenStack": ["DFW"],
                "cloudBlockStorage": ["DFW"],
                "rax:load-balancer": ["DFW"],
            },
            responses={
                "/servers/detail": RackspaceThrottledError("429", retry_after=30),
                "/volumes/detail": RackspaceThrottledError("429", retry_after=30),
                "/loadbalancers": RackspaceThrottledError("429", retry_after=30),
            },
        )
        with pytest.raises(RackspaceResourceError) as excinfo:
            await aggregate_rackspace_resources(
                rows=[_Row()], inventory_client=client
            )
        assert excinfo.value.category == RackspaceResourceErrorCategory.THROTTLED
        assert excinfo.value.retry_after == 30

    @pytest.mark.asyncio
    async def test_error_messages_carry_no_secrets(self):
        client = _inventory(
            services={"cloudServersOpenStack": ["DFW"]},
            responses={
                "/servers/detail": RackspacePermissionError("secret-token-123 leaked"),
                "/volumes/detail": RackspacePermissionError("denied"),
                "/loadbalancers": RackspacePermissionError("denied"),
            },
        )
        with pytest.raises(RackspaceResourceError) as excinfo:
            await aggregate_rackspace_resources(
                rows=[_Row()], inventory_client=client
            )
        # The sanitized category message replaces the upstream detail.
        assert "secret-token-123" not in str(excinfo.value)


class TestSummaryReconciliation:
    @pytest.mark.asyncio
    async def test_counts_reconcile_with_resources(self):
        client = _inventory(
            services={
                "cloudServersOpenStack": ["DFW"],
                "cloudBlockStorage": ["DFW"],
                "rax:load-balancer": ["DFW"],
            },
            responses={
                "/servers/detail": [_server_payload("srv-1"), _server_payload("srv-2")],
                "/flavors/detail": [],
                "/volumes/detail": [
                    {"id": "vol-1", "size": 100, "status": "in-use"},
                    {"id": "vol-2", "size": 50, "status": "available"},
                    {"id": "vol-3", "size": 25, "status": "available"},
                ],
                "/loadbalancers": [{"id": "lb-1", "status": "ACTIVE"}],
            },
        )
        summary, resources, failures = await aggregate_rackspace_resources(
            rows=[_Row()], inventory_client=client
        )
        assert not failures
        assert summary.extensions["total_resources"] == len(resources) == 6
        assert sum(e["count"] for e in summary.extensions["by_type"]) == 6
        assert sum(e["count"] for e in summary.extensions["by_location"]) == 6
        assert sum(e["count"] for e in summary.extensions["by_status"]) == 6
        by_subscription = summary.extensions["by_subscription"]
        assert by_subscription == [
            {"subscription_id": TENANT, "display_name": "Rackspace Cloud", "count": 6}
        ]
        assert summary.totals_by_account[0]["count"] == 6


class TestLoggingHygiene:
    @pytest.mark.asyncio
    async def test_no_secrets_in_log_stream(self, caplog):
        client = _inventory(
            services={"cloudServersOpenStack": ["DFW"], "cloudBlockStorage": [], "rax:load-balancer": []},
            responses={
                "/servers/detail": [_server_payload("srv-1")],
                "/flavors/detail": [],
            },
        )
        with caplog.at_level(logging.INFO, logger="cost-detective.cloud.rackspace.resources"):
            await aggregate_rackspace_resources(rows=[_Row()], inventory_client=client)
        stream = "\n".join(r.getMessage() for r in caplog.records)
        for secret in ("X-Auth-Token", "RACKSPACE_API_KEY", "RACKSPACE_BILLING_ACCOUNT"):
            assert secret not in stream


class TestReadOnlySurface:
    def test_inventory_client_exposes_no_product_write_verbs(self):
        for verb in ("post", "post_json", "put", "put_json", "patch", "delete", "delete_json"):
            assert not hasattr(RackspaceInventoryClient, verb), (
                f"RackspaceInventoryClient must not expose {verb!r}"
            )

    def test_resource_module_issues_no_write_calls(self):
        """The inventory client's only transport verbs reach the shared
        Phase 9A client — which is GET-only plus the Identity POST."""
        import inspect

        from app.services.rackspace.client import RackspaceHttpClient

        public = [
            name for name in dir(RackspaceHttpClient) if not name.startswith("_")
        ]
        assert "get" in public
        assert "post_json" in public  # Identity authentication only
        for verb in ("put", "patch", "delete"):
            assert verb not in public
