"""Phase 7B — resource aggregator tests.

These tests verify the multi-account resource fan-out: composite
identity, per-account error capture, partial-success behaviour, and
that two accounts with the same resource_id remain distinct.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from app.db.models import CloudAccount
from app.services.aws import resource_aggregator as ra
from app.services.aws.resource_aggregator import (
    aggregate_inventory_for_context,
)


def _row(account_id: str, display_name: str = "X") -> CloudAccount:
    return CloudAccount(
        provider="AWS",
        account_id=account_id,
        display_name=display_name,
        role_arn=f"arn:aws:iam::{account_id}:role/X",
        external_id=None,
        enabled=True,
        is_management=False,
        preferred_region="us-east-1",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def patch_enumerate():
    """Replace ``_enumerate_account`` for the duration of one test.

    Pytest's monkeypatch on a module's own symbol does not always
    propagate to consumers inside the same module because the module
    has already bound the name into its own globals at import time.
    The most reliable pattern is a try/finally around a direct
    setattr.
    """
    original = ra._enumerate_account

    def _set(fake):
        ra._enumerate_account = fake

    yield _set

    ra._enumerate_account = original


@pytest.mark.asyncio
async def test_resource_aggregator_handles_disabled_account_gracefully(patch_enumerate):
    """An empty rows list returns SUCCESS, not ERROR."""
    async def _noop(row, region):
        return {}
    patch_enumerate(_noop)
    inventory = await aggregate_inventory_for_context(
        rows=[], region="us-east-1"
    )
    assert inventory.status == "SUCCESS"
    assert inventory.account_errors == []


@pytest.mark.asyncio
async def test_resource_aggregator_partial_success(patch_enumerate):
    """One denied account does not blank the rollup."""
    rows = [_row("111111111111", "Denied"), _row("222222222222", "OK")]

    async def _fake_enumerate(row, region):
        if row.account_id == "222222222222":
            return {
                "ec2": {
                    "service": "ec2",
                    "status": "ok",
                    "items": [],
                    "error_code": None,
                }
            }
        from botocore.exceptions import ClientError
        raise ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "x"}}, "DescribeInstances"
        )

    patch_enumerate(_fake_enumerate)

    inventory = await aggregate_inventory_for_context(rows=rows, region="us-east-1")
    assert inventory.status == "PARTIAL_SUCCESS"
    assert len(inventory.account_errors) == 1
    err = inventory.account_errors[0]
    assert err.account_id == "111111111111"
    assert err.code == "AccessDenied"


@pytest.mark.asyncio
async def test_resource_aggregator_composite_id_unique_per_account(patch_enumerate):
    """Two accounts with the same resource_id remain distinct."""
    rows = [_row("111111111111", "A"), _row("222222222222", "B")]

    async def _fake_enumerate(row, region):
        return {
            "ec2": {
                "service": "ec2",
                "status": "ok",
                "items": [
                    {
                        "instance_id": "i-sameid",
                        "state": "running",
                        "instance_type": "t3.micro",
                        "region": region,
                    }
                ],
                "error_code": None,
            }
        }

    patch_enumerate(_fake_enumerate)
    inventory = await aggregate_inventory_for_context(rows=rows, region="us-east-1")
    assert inventory.status == "SUCCESS"

    ec2 = inventory.services["ec2"]
    # Two rows from two distinct accounts.
    assert len(ec2["items"]) == 2
    account_ids = {item["account_id"] for item in ec2["items"]}
    assert account_ids == {"111111111111", "222222222222"}
    # Composite IDs are distinct.
    composite_ids = {item["composite_id"] for item in ec2["items"]}
    assert len(composite_ids) == 2
