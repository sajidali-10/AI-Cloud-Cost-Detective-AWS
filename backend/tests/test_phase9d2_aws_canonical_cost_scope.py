"""Phase 9D.2 — AWS canonical individual-scope cost compatibility.

Root cause being pinned (Phase 9D.1 follow-up):

* The canonical ``GET /api/cloud/costs?provider=AWS`` route resolves
  an individual-account selector ``acct:<12-digit AWS account id>``
  by **provider + cloud-side account_id** (registry row matched on
  ``CloudAccount.account_id``, never on the row primary key).
* The legacy ``GET /api/aws/costs`` route resolves the SAME string
  via :func:`app.services.aws.account_provider.resolve_account_or_404`,
  which strips ``acct:`` and performs a **registry PRIMARY KEY**
  lookup — so a canonical cloud selector like ``acct:974053642038``
  404s there (no registry row has PK 974053642038).
* Phase 9D.2 migrates the last frontend caller (CostsPage AWS) to
  the canonical route.  The legacy route remains a backend
  compatibility surface with its documented PK grammar.

Tests prove:

1. ``acct:974053642038`` → AWS QA-Dev, HTTP 200, via the FULL
   production resolution path (registry PK lookup misses — PK space
   is {1, 2} — then the provider + account_id fallback hits).
2. ``acct:852428675923`` → AWS Production, HTTP 200, same path.
3. An unknown AWS account id returns a controlled 404
   ``AccountNotFound`` (never a 500).
4. The canonical scope/identity always echo the CLOUD account id —
   even for the explicit bare-registry-row-id accommodation, the
   scope canonicalises to ``acct:<cloud account_id>``, so a caller
   can never silently bind to the wrong account.
5. The legacy AWS route keeps its PK grammar (pinned explicitly):
   ``acct:<registry pk>`` resolves there while
   ``acct:974053642038`` 404s — which is exactly why the frontend
   must not call it (and no longer does).
6. Azure (``sub:<guid>``) and Rackspace (``acct:<tenant>``)
   selector contracts are unchanged.

No test touches the network — the AWS adapter is stubbed at the
registry boundary and the DB is an in-memory sqlite seeded through
the real ``get_db`` dependency override.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import Base, CloudAccount
from app.main import app
from app.db.session import get_db
from app.services.aws.account_provider import resolve_account_or_404
from app.services.cloud import ProviderRegistry
from app.services.cloud.models import (
    CloudCostSummary,
    CloudCostTrend,
    CloudProviderKind,
    CloudServiceCost,
)
from app.api.cloud_costs import _resolve_rows

QA_DEV_ACCOUNT_ID = "974053642038"
PROD_ACCOUNT_ID = "852428675923"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def auth_user(monkeypatch):
    """Replace ``get_current_user`` with a stub so no JWT is needed."""
    user = _StubUser()

    async def _stub():
        return user

    from app.api import deps

    monkeypatch.setattr(deps, "get_current_user", _stub)
    return user


class _StubUser:
    id = 1
    email = "alice@example.com"
    role = "ADMIN"


def _seed_aws_account(
    db,
    *,
    account_id: str,
    display_name: str,
) -> CloudAccount:
    row = CloudAccount(
        provider="AWS",
        account_id=account_id,
        display_name=display_name,
        role_arn=f"arn:aws:iam::{account_id}:role/cost-detective-readonly",
        external_id=None,
        enabled=True,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@pytest.fixture()
def seeded_db():
    """In-memory sqlite session + ``get_db`` dependency override.

    Seeds the two production-shaped AWS accounts.  The registry
    primary keys are tiny sequential ints (1, 2) — deliberately
    disjoint from the 12-digit cloud account ids — so any test
    asserting resolution by ``acct:<12 digits>`` inherently proves
    the selector was keyed on the CLOUD account id, not the PK.
    """
    # TestClient exercises the app from a different thread than the
    # fixture — share one in-memory connection across threads.
    engine = create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
    )
    session = factory()
    _seed_aws_account(session, account_id=QA_DEV_ACCOUNT_ID, display_name="QA-Dev")
    _seed_aws_account(session, account_id=PROD_ACCOUNT_ID, display_name="Production")
    app.dependency_overrides[get_db] = lambda: session
    yield session
    app.dependency_overrides.pop(get_db, None)
    session.close()
    engine.dispose()


class _AwsAdapterStub:
    """Stub the registered AWS adapter and record adapter calls."""

    def __init__(self) -> None:
        self.summary_calls: List[Dict[str, Any]] = []
        self.trend_calls: List[Dict[str, Any]] = []
        registry = ProviderRegistry.current()
        adapter = registry.require(CloudProviderKind.AWS)
        self._adapter = adapter
        self._original_summary = adapter.get_cost_summary
        self._original_trend = adapter.get_cost_trend
        adapter.get_cost_summary = self._fake_summary
        adapter.get_cost_trend = self._fake_trend

    async def _fake_summary(self, **kwargs):
        self.summary_calls.append(kwargs)
        rows = kwargs.get("accounts") or []
        primary = rows[0] if len(rows) == 1 else None
        account_id = primary.account_id if primary is not None else None
        return CloudCostSummary(
            provider=CloudProviderKind.AWS,
            account_id=account_id,
            scope=f"acct:{account_id}" if account_id else "all",
            period_start=kwargs["period_start"],
            period_end=kwargs["period_end"],
            currency="USD",
            current_total=Decimal("1500.00"),
            previous_total=Decimal("1000.00"),
            estimated=False,
            by_service=[
                CloudServiceCost(
                    service="Amazon EC2",
                    amount=Decimal("1500.00"),
                    currency="USD",
                    provider=CloudProviderKind.AWS,
                    account_id=account_id,
                )
            ],
            by_region=[],
            extensions={
                "status": "SUCCESS",
                "currency_status": "OK",
                "by_account": (
                    [
                        {
                            "account_id": account_id,
                            "display_name": primary.display_name,
                            "amount": "1500.00",
                        }
                    ]
                    if primary is not None
                    else []
                ),
            },
        )

    async def _fake_trend(self, **kwargs):
        self.trend_calls.append(kwargs)
        rows = kwargs.get("accounts") or []
        primary = rows[0] if len(rows) == 1 else None
        return CloudCostTrend(
            provider=CloudProviderKind.AWS,
            account_id=primary.account_id if primary is not None else None,
            period_start=kwargs["period_start"],
            period_end=kwargs["period_end"],
            currency="USD",
            daily=[],
            source="AWS_COST_EXPLORER",
        )

    def restore(self) -> None:
        self._adapter.get_cost_summary = self._original_summary
        self._adapter.get_cost_trend = self._original_trend


@pytest.fixture()
def aws_adapter_stub():
    stub = _AwsAdapterStub()
    yield stub
    stub.restore()


@pytest.fixture()
def client():
    return TestClient(app)


# ---------------------------------------------------------------------------
# Canonical individual-scope selectors (the Phase 9D.2 contract)
# ---------------------------------------------------------------------------


class TestCanonicalAwsIndividualScope:
    def test_registry_pks_are_disjoint_from_cloud_ids(self, seeded_db):
        """The proof scaffold: registry PK space is {1, 2} while the
        selectors under test are 12-digit cloud ids — so any 200 below
        can only have come from the provider + account_id fallback."""
        rows = (
            seeded_db.query(CloudAccount)
            .filter(CloudAccount.provider == "AWS")
            .order_by(CloudAccount.id)
            .all()
        )
        assert [r.id for r in rows] == [1, 2]
        assert {r.account_id for r in rows} == {QA_DEV_ACCOUNT_ID, PROD_ACCOUNT_ID}

    def test_qa_dev_canonical_selector_resolves_200(
        self, client, auth_user, seeded_db, aws_adapter_stub
    ):
        response = client.get(
            "/cloud/costs",
            params={
                "provider": "AWS",
                "days": "30",
                "account_id": f"acct:{QA_DEV_ACCOUNT_ID}",
            },
            headers={"Authorization": "Bearer test-token-not-real"},
        )
        assert response.status_code == 200
        report = response.json()["report"]
        # Scope + identity echo the CLOUD account id — never the row id.
        assert report["scope"] == f"acct:{QA_DEV_ACCOUNT_ID}"
        assert report["scope_kind"] == "single"
        assert report["account_id"] == QA_DEV_ACCOUNT_ID
        assert report["account_display_name"] == "QA-Dev"
        assert report["status"] == "SUCCESS"
        assert report["source"] == "AWS_COST_EXPLORER"
        assert Decimal(report["total_cost"]) == Decimal("1500.00")
        # The adapter received the QA-Dev registry row (cloud id on it).
        assert len(aws_adapter_stub.summary_calls) == 1
        adapter_rows = aws_adapter_stub.summary_calls[0]["accounts"]
        assert [r.account_id for r in adapter_rows] == [QA_DEV_ACCOUNT_ID]

    def test_production_canonical_selector_resolves_200(
        self, client, auth_user, seeded_db, aws_adapter_stub
    ):
        response = client.get(
            "/cloud/costs",
            params={
                "provider": "AWS",
                "days": "30",
                "account_id": f"acct:{PROD_ACCOUNT_ID}",
            },
            headers={"Authorization": "Bearer test-token-not-real"},
        )
        assert response.status_code == 200
        report = response.json()["report"]
        assert report["scope"] == f"acct:{PROD_ACCOUNT_ID}"
        assert report["scope_kind"] == "single"
        assert report["account_id"] == PROD_ACCOUNT_ID
        assert report["account_display_name"] == "Production"
        # Cross-account isolation: the QA-Dev account was NOT resolved.
        assert report["account_id"] != QA_DEV_ACCOUNT_ID
        adapter_rows = aws_adapter_stub.summary_calls[0]["accounts"]
        assert [r.account_id for r in adapter_rows] == [PROD_ACCOUNT_ID]

    def test_unknown_aws_account_is_controlled_404(
        self, client, auth_user, seeded_db, aws_adapter_stub
    ):
        response = client.get(
            "/cloud/costs",
            params={
                "provider": "AWS",
                "days": "30",
                "account_id": "acct:000000000000",
            },
            headers={"Authorization": "Bearer test-token-not-real"},
        )
        assert response.status_code == 404
        body = response.json()
        assert body["error_code"] == "AccountNotFound"
        assert "acct:000000000000" in body["message"]
        # Controlled failure — the adapter was never invoked.
        assert aws_adapter_stub.summary_calls == []

    def test_bare_row_id_accommodation_canonicalises_scope_to_cloud_id(
        self, client, auth_user, seeded_db, aws_adapter_stub
    ):
        """Explicit legacy accommodation: the canonical route also
        accepts a bare registry row id (documented on the Query
        param).  Even then, the resolved scope/identity echo the
        CLOUD account id — a row-id caller can never bind a scope
        to the wrong account or leak the PK as the account id."""
        response = client.get(
            "/cloud/costs",
            params={"provider": "AWS", "days": "30", "account_id": "acct:1"},
            headers={"Authorization": "Bearer test-token-not-real"},
        )
        assert response.status_code == 200
        report = response.json()["report"]
        assert report["scope"] == f"acct:{QA_DEV_ACCOUNT_ID}"
        assert report["account_id"] == QA_DEV_ACCOUNT_ID
        assert report["account_display_name"] == "QA-Dev"
        assert "1" != report["account_id"]  # PK never surfaces as identity


# ---------------------------------------------------------------------------
# Legacy /api/aws/costs grammar — pinned explicitly as LEGACY
# ---------------------------------------------------------------------------


class TestLegacyAwsRouteGrammar:
    def test_legacy_resolver_maps_acct_prefix_to_registry_pk(self, seeded_db):
        """The legacy route's documented grammar: ``acct:<n>`` is a
        REGISTRY PRIMARY KEY.  Pinned so the distinction with the
        canonical cloud-id grammar stays explicit and unambiguous."""
        row = resolve_account_or_404(seeded_db, "acct:1")
        assert row is not None
        assert row.account_id == QA_DEV_ACCOUNT_ID  # PK 1 → QA-Dev row

    def test_legacy_resolver_cannot_resolve_canonical_cloud_selector(
        self, seeded_db
    ):
        """The Phase 9D.2 root cause, pinned at the resolver seam:
        a 12-digit cloud selector is NOT a registry PK, so the legacy
        resolver misses it.  The current frontend therefore routes
        AWS costs through /api/cloud/costs (CostsPage Phase 9D.2)."""
        assert resolve_account_or_404(seeded_db, f"acct:{QA_DEV_ACCOUNT_ID}") is None
        assert resolve_account_or_404(seeded_db, f"acct:{PROD_ACCOUNT_ID}") is None

    def test_legacy_aws_costs_route_404s_on_canonical_selector(
        self, client, auth_user, seeded_db
    ):
        """Route-level pin of the legacy behaviour (network-free: the
        404 fires before any Cost Explorer call)."""
        response = client.get(
            "/aws/costs",
            params={"days": "30", "account_id": f"acct:{QA_DEV_ACCOUNT_ID}"},
            headers={"Authorization": "Bearer test-token-not-real"},
        )
        assert response.status_code == 404
        body = response.json()
        assert body["error_code"] == "AccountNotFound"


# ---------------------------------------------------------------------------
# Azure / Rackspace selector contracts — unchanged
# ---------------------------------------------------------------------------


class TestOtherProviderSelectorsUnchanged:
    def test_azure_sub_selector_resolves_by_subscription_guid(self, seeded_db):
        guid = "11111111-2222-3333-4444-555555555555"
        seeded_db.add(
            CloudAccount(
                provider="AZURE",
                account_id=guid,
                display_name="MCPP Subscription",
                enabled=True,
            )
        )
        seeded_db.commit()
        rows, scope, err = _resolve_rows(
            seeded_db,
            provider=CloudProviderKind.AZURE,
            selection=f"sub:{guid}",
        )
        assert err is None
        assert scope == f"sub:{guid}"  # Azure contract unchanged
        assert [r.account_id for r in rows] == [guid]

    def test_rackspace_acct_selector_resolves_by_tenant_id(self, seeded_db):
        tenant = "708274"
        seeded_db.add(
            CloudAccount(
                provider="RACKSPACE",
                account_id=tenant,
                display_name="Rackspace Cloud Account",
                enabled=True,
            )
        )
        seeded_db.commit()
        rows, scope, err = _resolve_rows(
            seeded_db,
            provider=CloudProviderKind.RACKSPACE,
            selection=f"acct:{tenant}",
        )
        assert err is None
        assert scope == f"acct:{tenant}"  # Rackspace contract unchanged
        assert [r.account_id for r in rows] == [tenant]

    def test_azure_selector_never_leaks_into_aws_scope(self, seeded_db):
        """A stale Azure ``sub:<guid>`` under the AWS provider must
        not resolve to any AWS account (Phase 9D.1 self-correction
        happens client-side; server-side it is a controlled 404)."""
        rows, scope, err = _resolve_rows(
            seeded_db,
            provider=CloudProviderKind.AWS,
            selection="sub:11111111-2222-3333-4444-555555555555",
        )
        assert rows is None
        assert err is not None
        assert err.status_code == 404
