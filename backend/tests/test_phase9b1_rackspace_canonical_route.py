"""Phase 9B.1 — Rackspace canonical route + cache tests.

Route tests (FastAPI TestClient):

* ``GET /cloud/costs?provider=RACKSPACE`` returns the canonical
  envelope shape (provider / currency / actual source period /
  total / by_service / empty unsupported dimensions / previous
  period unavailable / RACKSPACE_BILLING source)
* typed error mapping (401 / 403 / 404 / 429 / 502 / 503 / 422)
* account-scoped requests resolve to ``acct:<tenant>``
* true zero returns 200 SUCCESS (never an error)
* cross-provider regression: AWS and Azure envelopes unchanged while
  the Rackspace adapter is registered

Cache tests (in-memory sqlite, deterministic):

* MISS → REFRESHED with the row written under the ``rackspace``
  namespace; HIT on the second call (loader not invoked)
* STALE last-known-good fallback when the refresh fails
* failed / non-authoritative refreshes never poison the LKG
* a true ``$0`` success IS cached as authoritative
* provider cache isolation — Rackspace rows never collide with
  Azure keys; different scopes never collide
* env TTL knobs are honoured

No test touches the network — the billing fetch is stubbed at the
adapter boundary and no real credentials are read.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.main import app
from app.services.cloud import ProviderRegistry
from app.services.cloud.models import CloudProviderKind
from app.services.cloud.rackspace import adapter as rackspace_adapter_module
from app.services.cloud.rackspace.cost_cache import (
    build_rackspace_storage_account_id,
    get_or_refresh_rackspace_canonical,
)
from app.services.cloud.rackspace.costs import (
    RackspaceCostError,
    RackspaceCostErrorCategory,
    RackspaceCostSnapshot,
    RackspaceServiceCost,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _snapshot(
    *,
    total: str = "1250.50",
    currency: str = "USD",
    period_start: date = date(2026, 10, 1),
    period_end: date = date(2026, 10, 31),
) -> RackspaceCostSnapshot:
    total_dec = Decimal(total)
    by_service: tuple = ()
    if total_dec != 0:
        servers = (total_dec / 2).quantize(Decimal("0.01"))
        files = total_dec - servers
        by_service = (
            RackspaceServiceCost("Cloud Servers", servers),
            RackspaceServiceCost("Cloud Files", files),
        )
    return RackspaceCostSnapshot(
        currency=currency,
        period_start=period_start,
        period_end=period_end,
        total=total_dec,
        by_service=by_service,
        estimated=True,
        record_count=len(by_service),
        body_total=None,
        warnings=(),
        extensions={"source": "RACKSPACE_BILLING"},
    )


class _Row:
    def __init__(self, account_id: str, display_name: str = "Rackspace Cloud") -> None:
        self.account_id = account_id
        self.display_name = display_name
        self.provider = "RACKSPACE"
        self.enabled = True


@pytest.fixture
def auth_user(monkeypatch):
    from types import SimpleNamespace

    user = SimpleNamespace(id=1, email="alice@example.com", role="ADMIN")

    async def _stub():
        return user

    from app.api import deps

    monkeypatch.setattr(deps, "get_current_user", _stub)
    return user


@pytest.fixture
def client():
    return TestClient(app)


def _install_rackspace_stub(
    monkeypatch,
    *,
    snapshot: Optional[RackspaceCostSnapshot] = None,
    error: Optional[RackspaceCostError] = None,
    tenant_id: Optional[str] = None,
):
    """Patch the registered Rackspace adapter + row resolution.

    Returns a dict with the call log.  ``tenant_id`` defaults to a
    fresh uuid so every test gets a unique cache scope — the
    canonical route writes cache rows into the real ``cost_cache``
    table when a database is reachable (container runs), so a
    shared ``all`` scope would leak one test's payload into the
    next.  Every request in these tests selects the account
    explicitly (``account_id=1`` with a patched numeric resolver)
    so the cache key is scoped to the unique tenant.
    """
    registry = ProviderRegistry.current()
    adapter = registry.require(CloudProviderKind.RACKSPACE)
    snapshot = snapshot if snapshot is not None else _snapshot()
    tenant_id = tenant_id or f"tenant-{uuid.uuid4().hex[:12]}"
    summary_calls: List[Dict[str, Any]] = []

    original_summary = adapter.get_cost_summary

    async def fake_summary(**kwargs):
        summary_calls.append(kwargs)
        if error is not None:
            raise error
        rows = kwargs.get("accounts") or []
        primary = rows[0] if rows else _Row(tenant_id)
        from app.services.cloud.models import (
            CloudCostSummary,
            CloudServiceCost,
        )

        return CloudCostSummary(
            provider=CloudProviderKind.RACKSPACE,
            account_id=primary.account_id if len(rows) == 1 else None,
            scope=f"acct:{primary.account_id}",
            period_start=snapshot.period_start,
            period_end=snapshot.period_end,
            currency=snapshot.currency,
            current_total=snapshot.total,
            previous_total=None,
            estimated=snapshot.estimated,
            by_service=[
                CloudServiceCost(
                    service=s.service,
                    amount=s.amount,
                    currency=snapshot.currency,
                    provider=CloudProviderKind.RACKSPACE,
                    account_id=primary.account_id if len(rows) == 1 else None,
                )
                for s in snapshot.by_service
            ],
            by_region=[],
            extensions={
                "status": "SUCCESS",
                "source": "RACKSPACE_BILLING",
                "warnings": [],
                "by_account": [
                    {
                        "account_id": primary.account_id,
                        "display_name": primary.display_name,
                        "amount": str(snapshot.total),
                    }
                ],
                "previous_period_unavailable": True,
                "unsupported_dimensions": [
                    "daily_trend",
                    "region_breakdown",
                    "previous_period",
                ],
                "currency_status": "OK",
            },
        )

    adapter.get_cost_summary = fake_summary

    rows = [_Row(tenant_id)]

    from app.api import cloud_costs as cloud_costs_module

    monkeypatch.setattr(
        cloud_costs_module,
        "iter_enabled_accounts",
        lambda db, provider="AWS": rows,
    )

    # Numeric selection path: route resolves ``account_id=1`` via
    # ``resolve_account_or_404`` — patch it so the unique stub row
    # is returned and the cache scope becomes ``acct:<tenant>``.
    monkeypatch.setattr(
        cloud_costs_module,
        "resolve_account_or_404",
        lambda db, selection: rows[0],
    )

    def _restore():
        adapter.get_cost_summary = original_summary

    return {
        "adapter": adapter,
        "rows": rows,
        "summary_calls": summary_calls,
        "tenant_id": tenant_id,
        "restore": _restore,
    }


# ---------------------------------------------------------------------------
# Route tests
# ---------------------------------------------------------------------------


class TestRackspaceCanonicalRoute:
    def test_envelope_shape(self, client, auth_user, monkeypatch):
        stub = _install_rackspace_stub(monkeypatch)
        try:
            response = client.get(
                "/cloud/costs",
                params={"provider": "RACKSPACE", "days": "30", "account_id": "1"},
                headers={"Authorization": "Bearer test-token-not-real"},
            )
            assert response.status_code == 200
            body = response.json()
            report = body["report"]
            assert report["provider"] == "RACKSPACE"
            assert report["status"] == "SUCCESS"
            assert report["currency"] == "USD"
            # The ACTUAL source period (not the requested window).
            assert report["period"]["start"] == "2026-10-01"
            assert report["period"]["end"] == "2026-10-31"
            # Exclusive-end semantics: 2026-10-01 → 2026-10-31 is a
            # 30-day window (start inclusive, end exclusive).
            assert report["period"]["days"] == 30
            assert Decimal(report["total_cost"]) == Decimal("1250.50")
            assert report["source"] == "RACKSPACE_BILLING"
            assert report["estimated"] is True
            # Unsupported dimensions are EMPTY — never fabricated.
            assert report["daily_trend"] == []
            assert report["by_region"] == []
            assert report["by_service"]
            services = {i["service"] for i in report["by_service"]}
            assert services == {"Cloud Servers", "Cloud Files"}
            # Previous period unavailable — no fabricated baseline.
            assert report["previous_period_cost"] is None
            assert report["change_amount"] is None
            assert report["previous_period_unavailable"] is True
            assert report["successful_accounts"] == 1
            assert report["failed_accounts"] == 0
            # Cache envelope fields present.
            assert "cache_status" in body
            assert "cached_at" in body
            assert "expires_at" in body
        finally:
            stub["restore"]()

    def test_breakdown_reconciles_with_total(self, client, auth_user, monkeypatch):
        stub = _install_rackspace_stub(monkeypatch)
        try:
            response = client.get(
                "/cloud/costs",
                params={"provider": "RACKSPACE", "days": "30", "account_id": "1"},
                headers={"Authorization": "Bearer test-token-not-real"},
            )
            assert response.status_code == 200
            report = response.json()["report"]
            breakdown = sum(
                (Decimal(i["amount"]) for i in report["by_service"]),
                Decimal("0"),
            )
            assert breakdown == Decimal(report["total_cost"])
        finally:
            stub["restore"]()

    def test_true_zero_returns_success(self, client, auth_user, monkeypatch):
        stub = _install_rackspace_stub(
            monkeypatch, snapshot=_snapshot(total="0.00")
        )
        try:
            response = client.get(
                "/cloud/costs",
                params={"provider": "RACKSPACE", "days": "30", "account_id": "1"},
                headers={"Authorization": "Bearer test-token-not-real"},
            )
            assert response.status_code == 200
            report = response.json()["report"]
            assert report["status"] == "SUCCESS"
            assert Decimal(report["total_cost"]) == Decimal("0.00")
            assert report["by_service"] == []
        finally:
            stub["restore"]()

    def test_currency_propagates(self, client, auth_user, monkeypatch):
        stub = _install_rackspace_stub(
            monkeypatch, snapshot=_snapshot(currency="EUR", total="10.00")
        )
        try:
            response = client.get(
                "/cloud/costs",
                params={"provider": "RACKSPACE", "days": "30", "account_id": "1"},
                headers={"Authorization": "Bearer test-token-not-real"},
            )
            assert response.status_code == 200
            report = response.json()["report"]
            assert report["currency"] == "EUR"
            assert Decimal(report["total_cost"]) == Decimal("10.00")
        finally:
            stub["restore"]()

    def test_account_scope_resolves_to_acct(self, client, auth_user, monkeypatch):
        stub = _install_rackspace_stub(monkeypatch)
        try:
            from app.api import cloud_costs

            def fake_resolve(db, selection):
                return stub["rows"][0]

            monkeypatch.setattr(cloud_costs, "resolve_account_or_404", fake_resolve)
            response = client.get(
                "/cloud/costs",
                params={
                    "provider": "RACKSPACE",
                    "days": "30",
                    "account_id": "1",
                },
                headers={"Authorization": "Bearer test-token-not-real"},
            )
            assert response.status_code == 200
            report = response.json()["report"]
            assert report["scope_kind"] == "single"
            assert report["scope"] == f"acct:{stub['tenant_id']}"
        finally:
            stub["restore"]()

    def test_no_accounts_registered_maps_to_503(self, client, auth_user, monkeypatch):
        from app.api import cloud_costs as cloud_costs_module

        monkeypatch.setattr(
            cloud_costs_module,
            "iter_enabled_accounts",
            lambda db, provider="AWS": [],
        )
        response = client.get(
            "/cloud/costs",
            params={"provider": "RACKSPACE", "days": "30"},
            headers={"Authorization": "Bearer test-token-not-real"},
        )
        assert response.status_code == 503
        assert response.json()["error_code"] == "NoAccountsRegistered"


class TestRackspaceErrorMapping:
    def _assert_error(
        self,
        client,
        monkeypatch,
        category: RackspaceCostErrorCategory,
        expected_status: int,
        expected_code: str,
        *,
        retry_after: Optional[float] = None,
    ):
        stub = _install_rackspace_stub(
            monkeypatch,
            error=RackspaceCostError(
                category, "sanitized test message", retry_after=retry_after
            ),
        )
        try:
            response = client.get(
                "/cloud/costs",
                params={"provider": "RACKSPACE", "days": "30", "account_id": "1"},
                headers={"Authorization": "Bearer test-token-not-real"},
            )
            assert response.status_code == expected_status
            body = response.json()
            assert body["error_code"] == expected_code
            assert body["category"] == category.value
            if retry_after is not None:
                assert response.headers.get("Retry-After") == str(int(retry_after))
            # Never any secret material in the error envelope.
            assert "api_key" not in str(body).lower()
        finally:
            stub["restore"]()

    def test_auth_failed_maps_to_401(self, client, auth_user, monkeypatch):
        self._assert_error(
            client, monkeypatch, RackspaceCostErrorCategory.AUTH_FAILED, 401, "AUTH_FAILED"
        )

    def test_access_denied_maps_to_403(self, client, auth_user, monkeypatch):
        self._assert_error(
            client, monkeypatch, RackspaceCostErrorCategory.ACCESS_DENIED, 403, "ACCESS_DENIED"
        )

    def test_not_found_maps_to_404(self, client, auth_user, monkeypatch):
        self._assert_error(
            client, monkeypatch, RackspaceCostErrorCategory.NOT_FOUND, 404, "NOT_FOUND"
        )

    def test_throttled_maps_to_429_with_retry_after(self, client, auth_user, monkeypatch):
        self._assert_error(
            client, monkeypatch, RackspaceCostErrorCategory.THROTTLED, 429, "THROTTLED",
            retry_after=42.0,
        )

    def test_network_error_maps_to_502(self, client, auth_user, monkeypatch):
        self._assert_error(
            client, monkeypatch, RackspaceCostErrorCategory.NETWORK_ERROR, 502, "UPSTREAM_ERROR"
        )

    def test_invalid_response_maps_to_502(self, client, auth_user, monkeypatch):
        self._assert_error(
            client, monkeypatch, RackspaceCostErrorCategory.INVALID_RESPONSE, 502, "UPSTREAM_ERROR"
        )

    def test_not_configured_maps_to_503(self, client, auth_user, monkeypatch):
        self._assert_error(
            client, monkeypatch, RackspaceCostErrorCategory.NOT_CONFIGURED, 503, "NOT_CONFIGURED"
        )

    def test_period_unsupported_maps_to_422(self, client, auth_user, monkeypatch):
        stub = _install_rackspace_stub(
            monkeypatch,
            error=RackspaceCostError(
                RackspaceCostErrorCategory.PERIOD_UNSUPPORTED,
                "Rackspace Billing supports only the current billing cycle",
            ),
        )
        try:
            response = client.get(
                "/cloud/costs",
                params={"provider": "RACKSPACE", "month": "2026-01", "account_id": "1"},
                headers={"Authorization": "Bearer test-token-not-real"},
            )
            assert response.status_code == 422
            assert response.json()["error_code"] == "PERIOD_UNSUPPORTED"
        finally:
            stub["restore"]()


class TestCrossProviderRegression:
    def test_aws_and_azure_unchanged_while_rackspace_registered(
        self, client, auth_user, monkeypatch
    ):
        """Rackspace registration must not alter AWS/Azure envelopes."""
        # --- AWS ---
        from app.services.cloud.models import (
            CloudCostSummary,
            CloudServiceCost,
        )

        registry = ProviderRegistry.current()
        aws_adapter = registry.require(CloudProviderKind.AWS)
        original_aws_summary = aws_adapter.get_cost_summary
        original_aws_trend = aws_adapter.get_cost_trend

        async def fake_aws_summary(**kwargs):
            return CloudCostSummary(
                provider=CloudProviderKind.AWS,
                account_id="123456789012",
                scope="acct:123456789012",
                period_start=date(2026, 10, 1),
                period_end=date(2026, 10, 31),
                currency="USD",
                current_total=Decimal("500.00"),
                previous_total=Decimal("400.00"),
                estimated=False,
                by_service=[
                    CloudServiceCost(
                        service="Amazon EC2",
                        amount=Decimal("500.00"),
                        currency="USD",
                        provider=CloudProviderKind.AWS,
                        account_id="123456789012",
                    )
                ],
                by_region=[],
                extensions={"status": "SUCCESS", "by_account": []},
            )

        async def fake_aws_trend(**kwargs):
            from app.services.cloud.models import CloudCostTrend

            return CloudCostTrend(
                provider=CloudProviderKind.AWS,
                account_id="123456789012",
                period_start=date(2026, 10, 1),
                period_end=date(2026, 10, 31),
                currency="USD",
                daily=[],
                source="AWS_COST_EXPLORER",
            )

        aws_adapter.get_cost_summary = fake_aws_summary
        aws_adapter.get_cost_trend = fake_aws_trend

        class _AwsRow:
            account_id = "123456789012"
            display_name = "Management"
            provider = "AWS"
            enabled = True

        from app.api import cloud_costs as cloud_costs_module

        monkeypatch.setattr(
            cloud_costs_module,
            "iter_enabled_accounts",
            lambda db, provider="AWS": [_AwsRow()],
        )
        try:
            response = client.get(
                "/cloud/costs",
                params={"provider": "AWS", "days": "30"},
                headers={"Authorization": "Bearer test-token-not-real"},
            )
            assert response.status_code == 200
            report = response.json()["report"]
            assert report["provider"] == "AWS"
            assert Decimal(report["total_cost"]) == Decimal("500.00")
            assert report["source"] == "AWS_COST_EXPLORER"
        finally:
            aws_adapter.get_cost_summary = original_aws_summary
            aws_adapter.get_cost_trend = original_aws_trend

        # --- Azure (stubbed similarly) ---
        azure_adapter = registry.try_get(CloudProviderKind.AZURE)
        original_azure_summary = azure_adapter.get_cost_summary

        async def fake_azure_summary(**kwargs):
            return CloudCostSummary(
                provider=CloudProviderKind.AZURE,
                account_id="sub-aaaa-aaaa-aaaa",
                scope="sub:sub-aaaa-aaaa-aaaa",
                period_start=date(2026, 10, 1),
                period_end=date(2026, 10, 31),
                currency="USD",
                current_total=Decimal("10.00"),
                previous_total=Decimal("9.00"),
                estimated=False,
                by_service=[],
                by_region=[],
                extensions={
                    "status": "SUCCESS",
                    "by_subscription": [],
                    "currency_status": "OK",
                    "_daily_points": [],
                },
            )

        azure_adapter.get_cost_summary = fake_azure_summary

        class _AzureRow:
            account_id = "sub-aaaa-aaaa-aaaa"
            display_name = "MCPP Subscription"
            provider = "AZURE"
            enabled = True

        monkeypatch.setattr(
            cloud_costs_module,
            "iter_enabled_accounts",
            lambda db, provider="AWS": [_AzureRow()],
        )
        try:
            response = client.get(
                "/cloud/costs",
                params={"provider": "AZURE", "days": "30"},
                headers={"Authorization": "Bearer test-token-not-real"},
            )
            assert response.status_code == 200
            report = response.json()["report"]
            assert report["provider"] == "AZURE"
            assert Decimal(report["total_cost"]) == Decimal("10.00")
            assert report["source"] == "AZURE_COST_MANAGEMENT"
        finally:
            azure_adapter.get_cost_summary = original_azure_summary


# ---------------------------------------------------------------------------
# Cache layer tests (sqlite)
# ---------------------------------------------------------------------------


DDL_STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS cost_cache (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        cache_namespace VARCHAR(32) NOT NULL DEFAULT 'aws',
        account_id   VARCHAR(64)  NOT NULL,
        cache_key    VARCHAR(255) NOT NULL,
        query_type   VARCHAR(64)  NOT NULL,
        period_start TIMESTAMP    NOT NULL,
        period_end   TIMESTAMP    NOT NULL,
        payload      TEXT         NOT NULL,
        created_at   TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        expires_at   TIMESTAMP    NOT NULL,
        last_successful_at TIMESTAMP,
        stale_payload TEXT,
        stale_until TIMESTAMP,
        azure_post_count INTEGER NOT NULL DEFAULT 0
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_cost_cache_namespace_account_key "
    "ON cost_cache (cache_namespace, account_id, cache_key)",
]


@pytest.fixture()
def engine():
    eng = create_engine("sqlite:///:memory:", future=True)
    with eng.begin() as conn:
        for stmt in DDL_STATEMENTS:
            conn.execute(text(stmt))
    yield eng
    eng.dispose()


@pytest.fixture()
def session_factory(engine):
    return sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
    )


@pytest.fixture(autouse=True)
def reset_locks():
    from app.services.cloud.azure.cost_cache import reset_locks_for_tests

    reset_locks_for_tests()
    yield
    reset_locks_for_tests()


class StubLoader:
    def __init__(self, returns=None, raises=None):
        self.returns = returns
        self.raises = raises
        self.calls = 0

    async def __call__(self):
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return dict(self.returns)


def _payload(total: str = "100.00", status: str = "SUCCESS") -> Dict[str, Any]:
    return {
        "provider": "RACKSPACE",
        "status": status,
        "currency": "USD",
        "total_cost": total,
        "successful_accounts": 1 if status == "SUCCESS" else 0,
        "failed_accounts": 0 if status == "SUCCESS" else 1,
        "period": {"start": "2026-10-01", "end": "2026-10-31", "days": 31},
        "previous_period": {"start": "2026-09-01", "end": "2026-10-01", "days": 31},
        "previous_period_cost": None,
        "change_amount": None,
        "change_percent": None,
        "daily_trend": [],
        "by_service": [],
        "by_region": [],
        "by_account": [],
        "account_errors": [],
        "scope": "all",
        "scope_kind": "all",
        "source": "RACKSPACE_BILLING",
        "estimated": True,
    }


class TestRackspaceCache:
    @pytest.mark.asyncio
    async def test_miss_then_refreshed_and_hit(self, session_factory):
        db = session_factory()
        loader = StubLoader(returns=_payload())
        entry = await get_or_refresh_rackspace_canonical(
            scope="all",
            region="",
            kind="days",
            signature="30",
            loader=loader,
            db=db,
        )
        assert entry.cache_status.value == "REFRESHED"
        assert entry.data_freshness.value == "LIVE"
        assert loader.calls == 1
        # The row exists under the rackspace namespace.
        row = db.execute(
            text(
                "SELECT cache_namespace, account_id FROM cost_cache "
                "WHERE cache_key = :k"
            ),
            {"k": entry.cache_key},
        ).first()
        assert row is not None
        assert row.cache_namespace == "rackspace"
        assert row.account_id == "rackspace:all"

        # Second call → HIT, no loader invocation.
        entry2 = await get_or_refresh_rackspace_canonical(
            scope="all",
            region="",
            kind="days",
            signature="30",
            loader=loader,
            db=db,
        )
        assert entry2.cache_status.value == "HIT"
        assert entry2.data_freshness.value == "CACHED"
        assert loader.calls == 1

    @pytest.mark.asyncio
    async def test_stale_lkg_served_when_refresh_fails(self, session_factory):
        db = session_factory()
        good = StubLoader(returns=_payload("200.00"))
        entry = await get_or_refresh_rackspace_canonical(
            scope="all", region="", kind="days", signature="30",
            loader=good, db=db, fresh_ttl_seconds=0,
        )
        assert entry.cache_status.value == "REFRESHED"

        from app.services.cloud.rackspace.costs import (
            RackspaceCostError as _Err,
            RackspaceCostErrorCategory as _Cat,
        )

        failing = StubLoader(
            raises=_Err(_Cat.THROTTLED, "throttled", retry_after=30.0)
        )
        entry2 = await get_or_refresh_rackspace_canonical(
            scope="all", region="", kind="days", signature="30",
            loader=failing, db=db,
        )
        assert entry2.cache_status.value == "STALE"
        assert entry2.data_freshness.value == "STALE"
        # The LKG payload survived the failed refresh.
        assert entry2.payload["total_cost"] == "200.00"

    @pytest.mark.asyncio
    async def test_failed_refresh_does_not_poison_lkg(self, session_factory):
        db = session_factory()
        good = StubLoader(returns=_payload("200.00"))
        await get_or_refresh_rackspace_canonical(
            scope="all", region="", kind="days", signature="30",
            loader=good, db=db, fresh_ttl_seconds=0,
        )

        from app.services.cloud.rackspace.costs import (
            RackspaceCostError as _Err,
            RackspaceCostErrorCategory as _Cat,
        )

        error_payload = _payload("0.00")
        error_payload["status"] = "ERROR"
        error_payload["successful_accounts"] = 0
        error_payload["failed_accounts"] = 1
        bad = StubLoader(returns=error_payload)
        entry = await get_or_refresh_rackspace_canonical(
            scope="all", region="", kind="days", signature="30",
            loader=bad, db=db,
        )
        # The non-authoritative response is returned live...
        assert entry.cache_status.value == "REFRESHED"
        # ...but the cached row still holds the authoritative LKG.
        row = db.execute(
            text(
                "SELECT payload FROM cost_cache "
                "WHERE cache_namespace = 'rackspace' AND account_id = 'rackspace:all'"
            )
        ).first()
        import json as _json

        stored = _json.loads(row.payload)
        assert stored["total_cost"] == "200.00"

    @pytest.mark.asyncio
    async def test_true_zero_cached_as_authoritative(self, session_factory):
        db = session_factory()
        zero = StubLoader(returns=_payload("0.00"))
        entry = await get_or_refresh_rackspace_canonical(
            scope="all", region="", kind="days", signature="30",
            loader=zero, db=db,
        )
        assert entry.payload["total_cost"] == "0.00"
        # A true $0 IS authoritative — the next call HITs it.
        probe = StubLoader(returns=_payload("999.00"))
        entry2 = await get_or_refresh_rackspace_canonical(
            scope="all", region="", kind="days", signature="30",
            loader=probe, db=db,
        )
        assert entry2.cache_status.value == "HIT"
        assert entry2.payload["total_cost"] == "0.00"

    @pytest.mark.asyncio
    async def test_error_with_no_lkg_raises(self, session_factory):
        from app.services.cloud.rackspace.costs import (
            RackspaceCostError as _Err,
            RackspaceCostErrorCategory as _Cat,
        )

        db = session_factory()
        failing = StubLoader(raises=_Err(_Cat.AUTH_FAILED, "auth"))
        with pytest.raises(_Err):
            await get_or_refresh_rackspace_canonical(
                scope="all", region="", kind="days", signature="30",
                loader=failing, db=db,
            )

    @pytest.mark.asyncio
    async def test_rackspace_rows_never_collide_with_azure(self, session_factory):
        """Provider selection cannot cross-contaminate caches."""
        from app.services.cloud.azure.cost_cache import (
            get_or_refresh_azure_canonical,
        )

        db = session_factory()
        rackspace_loader = StubLoader(returns=_payload("111.00"))
        entry = await get_or_refresh_rackspace_canonical(
            scope="all", region="", kind="days", signature="30",
            loader=rackspace_loader, db=db,
        )
        # Same scope / kind / signature, different provider → the
        # Azure lookup must MISS (its own loader runs).
        azure_loader = StubLoader(
            returns={
                "provider": "AZURE",
                "status": "SUCCESS",
                "total_cost": "222.00",
                "successful_accounts": 1,
            }
        )
        azure_entry = await get_or_refresh_azure_canonical(
            scope="all", region="", kind="days", signature="30",
            loader=azure_loader, db=db,
        )
        assert azure_entry.cache_status.value == "REFRESHED"
        assert azure_loader.calls == 1
        assert azure_entry.payload["provider"] == "AZURE"
        assert entry.payload["provider"] == "RACKSPACE"
        # The two rows live under different namespaces.
        rows = db.execute(
            text(
                "SELECT cache_namespace, COUNT(*) AS n FROM cost_cache "
                "GROUP BY cache_namespace"
            )
        ).all()
        namespaces = {r.cache_namespace for r in rows}
        assert namespaces == {"rackspace", "azure"}

    @pytest.mark.asyncio
    async def test_different_scopes_do_not_collide(self, session_factory):
        db = session_factory()
        loader_a = StubLoader(returns=_payload("10.00"))
        await get_or_refresh_rackspace_canonical(
            scope="acct:tenant-a", region="", kind="days", signature="30",
            loader=loader_a, db=db,
        )
        loader_b = StubLoader(returns=_payload("20.00"))
        entry_b = await get_or_refresh_rackspace_canonical(
            scope="acct:tenant-b", region="", kind="days", signature="30",
            loader=loader_b, db=db,
        )
        assert entry_b.cache_status.value == "REFRESHED"
        assert entry_b.payload["total_cost"] == "20.00"
        assert build_rackspace_storage_account_id("acct:tenant-a") != (
            build_rackspace_storage_account_id("acct:tenant-b")
        )

    def test_storage_account_ids_hashed_not_plaintext(self):
        sid = build_rackspace_storage_account_id("acct:8675309")
        assert sid.startswith("rackspace:acct:")
        # The tenant id itself never appears in the storage id.
        assert "8675309" not in sid
        assert build_rackspace_storage_account_id("all") == "rackspace:all"

    @pytest.mark.asyncio
    async def test_env_ttl_knobs_honoured(self, session_factory, monkeypatch):
        db = session_factory()
        monkeypatch.setenv("RACKSPACE_COST_CACHE_FRESH_TTL_SECONDS", "0")
        loader = StubLoader(returns=_payload())
        entry = await get_or_refresh_rackspace_canonical(
            scope="all", region="", kind="days", signature="30",
            loader=loader, db=db,
        )
        assert entry.cache_status.value == "REFRESHED"
        # Fresh TTL 0 → the very next call must refresh again.
        entry2 = await get_or_refresh_rackspace_canonical(
            scope="all", region="", kind="days", signature="30",
            loader=loader, db=db,
        )
        assert entry2.cache_status.value == "REFRESHED"
        assert loader.calls == 2


# ---------------------------------------------------------------------------
# FinOps context guard — Rackspace AI is out of scope for 9B.1
# ---------------------------------------------------------------------------


class TestFinOpsContextGuard:
    @pytest.mark.asyncio
    async def test_safe_cost_non_rackspace_kind_no_nameerror(self):
        """The guard must not break AWS / Azure cost slices.

        Regression for the Phase 9B.1 bug where the Rackspace guard
        referenced ``CloudProviderKind`` without the lazy import —
        raising ``NameError`` for EVERY provider (caught live by the
        Phase 7D.5 gate, not by the unit suite).
        """
        from datetime import date as _date

        from app.services.cloud.finops_context import FinOpsContextBuilder

        builder = FinOpsContextBuilder()
        result = await builder._safe_cost(
            adapter=None,
            kind=CloudProviderKind.AWS,
            rows=[],
            period_start=_date(2026, 10, 1),
            period_end=_date(2026, 10, 31),
            prev_start=_date(2026, 9, 1),
            prev_end=_date(2026, 10, 1),
            effective_region="",
            scope="all",
        )
        assert result is None  # adapter None → controlled None, no NameError

    @pytest.mark.asyncio
    async def test_safe_cost_rackspace_returns_none_without_adapter_call(self):
        """Rackspace cost slices are out of scope — the builder must
        return ``None`` without ever calling the adapter."""
        from datetime import date as _date

        from app.services.cloud.finops_context import FinOpsContextBuilder

        class _BoomAdapter:
            async def get_cost_summary(self, **kwargs):  # pragma: no cover
                raise AssertionError("Rackspace AI FinOps must not call the adapter")

        builder = FinOpsContextBuilder()
        result = await builder._safe_cost(
            adapter=_BoomAdapter(),
            kind=CloudProviderKind.RACKSPACE,
            rows=[],
            period_start=_date(2026, 10, 1),
            period_end=_date(2026, 10, 31),
            prev_start=_date(2026, 9, 1),
            prev_end=_date(2026, 10, 1),
            effective_region="",
            scope="all",
        )
        assert result is None


# ---------------------------------------------------------------------------
# Secret redaction — no RAN / token / api key anywhere in a response
# ---------------------------------------------------------------------------


class TestSecretRedactionOnResponses:
    def test_response_carries_no_secret_material(self, client, auth_user, monkeypatch):
        # Configure a realistic RAN-shaped value in the process env —
        # the response must never echo it.
        monkeypatch.setenv("RACKSPACE_BILLING_ACCOUNT", "020-123456")
        monkeypatch.setenv("RACKSPACE_API_KEY", "supersecretkey123")
        stub = _install_rackspace_stub(monkeypatch)
        try:
            response = client.get(
                "/cloud/costs",
                params={"provider": "RACKSPACE", "days": "30", "account_id": "1"},
                headers={"Authorization": "Bearer test-token-not-real"},
            )
            assert response.status_code == 200
            body_text = response.text
            assert "020-123456" not in body_text
            assert "supersecretkey123" not in body_text
            assert "RACKSPACE_BILLING_ACCOUNT" not in body_text
        finally:
            stub["restore"]()
