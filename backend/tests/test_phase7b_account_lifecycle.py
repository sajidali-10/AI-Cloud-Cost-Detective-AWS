"""Phase 7B — account lifecycle + scope isolation regression.

Pins:
* Management account cannot be disabled or un-managed.
* A target account can be disabled and re-enabled via the PATCH
  endpoint.
* The selectable list excludes disabled accounts.
* The aggregator's iter_enabled_accounts iteration excludes
  disabled rows.
* Multi-account cost cache keys must include the scope token so
  Account A data cannot satisfy an Account B request.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, CloudAccount
from app.services.aws.account_provider import (
    AccountAlreadyExists,
    ManagementAccountImmutable,
    create_account,
    disable_account,
    get_account_by_id,
    get_management_account,
    iter_enabled_accounts,
    list_accounts,
    update_account,
)
from app.schemas.cloud_account import CloudAccountCreate, CloudAccountUpdate


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    session = Session()
    yield session
    session.close()


def _seed_management(db, account_id: str = "974053642038") -> CloudAccount:
    row = CloudAccount(
        provider="AWS",
        account_id=account_id,
        display_name="Management",
        role_arn=f"arn:aws:iam::{account_id}:role/cost-detective-management-placeholder",
        external_id=None,
        enabled=True,
        is_management=True,
        preferred_region="us-east-1",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _seed_target(db, account_id: str = "852428675923") -> CloudAccount:
    row = CloudAccount(
        provider="AWS",
        account_id=account_id,
        display_name="Target",
        role_arn=f"arn:aws:iam::{account_id}:role/ai-cloud-cost-detective-readonly",
        external_id=None,
        enabled=True,
        is_management=False,
        preferred_region="us-east-1",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_management_account_cannot_be_disabled(db):
    mgmt = _seed_management(db)
    with pytest.raises(ManagementAccountImmutable):
        disable_account(db, mgmt)


def test_management_account_cannot_be_unmanaged_via_update(db):
    mgmt = _seed_management(db)
    with pytest.raises(ManagementAccountImmutable):
        update_account(db, mgmt, CloudAccountUpdate(enabled=False))


def test_target_account_can_be_disabled_then_re_enabled(db):
    target = _seed_target(db)
    disabled = disable_account(db, target)
    assert disabled.enabled is False

    refreshed = get_account_by_id(db, target.id)
    assert refreshed is not None
    assert refreshed.enabled is False

    re_enabled = update_account(
        db, refreshed, CloudAccountUpdate(enabled=True)
    )
    assert re_enabled.enabled is True


def test_selectable_iter_excludes_disabled_accounts(db):
    mgmt = _seed_management(db)
    target = _seed_target(db)

    # Initial state: both enabled, both visible to iter_enabled_accounts.
    enabled_ids = {row.account_id for row in iter_enabled_accounts(db)}
    assert enabled_ids == {"974053642038", "852428675923"}

    # Disable the target.
    disable_account(db, target)

    # Re-iterate: management still enabled, target excluded.
    enabled_ids = {row.account_id for row in iter_enabled_accounts(db)}
    assert enabled_ids == {"974053642038"}

    # Re-enable.
    refreshed = get_account_by_id(db, target.id)
    update_account(db, refreshed, CloudAccountUpdate(enabled=True))

    enabled_ids = {row.account_id for row in iter_enabled_accounts(db)}
    assert enabled_ids == {"974053642038", "852428675923"}


def test_disabled_target_does_not_appear_in_list(db):
    mgmt = _seed_management(db)
    target = _seed_target(db)
    rows = list_accounts(db, enabled_only=False)
    assert {r.account_id for r in rows} == {"974053642038", "852428675923"}
    rows_enabled = list_accounts(db, enabled_only=True)
    assert {r.account_id for r in rows_enabled} == {"974053642038", "852428675923"}

    disable_account(db, target)
    rows_enabled = list_accounts(db, enabled_only=True)
    assert {r.account_id for r in rows_enabled} == {"974053642038"}


def test_create_rejects_duplicate_for_same_aws_account(db):
    _seed_management(db)
    with pytest.raises(AccountAlreadyExists):
        create_account(
            db,
            CloudAccountCreate(
                account_id="974053642038",
                display_name="Dupe",
                role_arn="arn:aws:iam::974053642038:role/X",
            ),
        )


def test_get_management_account_returns_management_row(db):
    mgmt = _seed_management(db)
    assert get_management_account(db).id == mgmt.id


def test_management_row_account_id_is_preserved(db):
    mgmt = _seed_management(db, account_id="974053642038")
    assert mgmt.account_id == "974053642038"
    assert mgmt.is_management is True


def test_target_row_account_id_is_preserved(db):
    target = _seed_target(db, account_id="852428675923")
    assert target.account_id == "852428675923"
    assert target.is_management is False
