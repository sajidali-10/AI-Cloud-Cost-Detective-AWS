"""Phase 7B — cloud-account registry + validator tests."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, CloudAccount
from app.schemas.cloud_account import (
    CloudAccountCreate,
    CloudAccountUpdate,
)
from app.services.aws.account_provider import (
    AccountAlreadyExists,
    ManagementAccountImmutable,
    create_account,
    disable_account,
    get_account_by_id,
    get_management_account,
    list_accounts,
    update_account,
)
from app.services.aws.session_factory import set_default_session_factory, reset_default_session_factory


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    session = Session()
    yield session
    session.close()


def _seed_management(db) -> CloudAccount:
    row = CloudAccount(
        provider="AWS",
        account_id="111111111111",
        display_name="Management",
        role_arn="arn:aws:iam::111111111111:role/cost-detective-management-placeholder",
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


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


def test_create_validates_account_id_format():
    with pytest.raises(Exception):
        CloudAccountCreate(
            account_id="not-12-digits",
            display_name="X",
            role_arn="arn:aws:iam::111111111111:role/X",
        )


def test_create_validates_role_arn_format():
    with pytest.raises(Exception):
        CloudAccountCreate(
            account_id="111111111111",
            display_name="X",
            role_arn="arn:aws:s3:::bucket",  # wrong service
        )


def test_create_validates_arn_account_matches_account_id():
    with pytest.raises(Exception):
        CloudAccountCreate(
            account_id="111111111111",
            display_name="X",
            role_arn="arn:aws:iam::222222222222:role/Different",
        )


def test_create_rejects_unsupported_provider():
    with pytest.raises(Exception):
        CloudAccountCreate(
            provider="Azure",
            account_id="111111111111",
            display_name="X",
            role_arn="arn:aws:iam::111111111111:role/X",
        )


def test_external_id_is_not_echoed_in_response():
    # The response schema does not include external_id at all.
    fields = set(CloudAccountCreate.model_fields)
    assert "external_id" in fields  # accepted on input
    from app.schemas.cloud_account import CloudAccountResponse
    response_fields = set(CloudAccountResponse.model_fields)
    assert "external_id" not in response_fields
    assert "role_arn" in response_fields  # role_arn IS visible (not secret)
    assert "account_id" in response_fields


# ---------------------------------------------------------------------------
# Registry CRUD
# ---------------------------------------------------------------------------


def test_create_persists_row(db):
    _seed_management(db)
    payload = CloudAccountCreate(
        account_id="222222222222",
        display_name="Dev",
        role_arn="arn:aws:iam::222222222222:role/CostDetectiveReadOnly",
        external_id="abc",
        preferred_region="us-west-2",
    )
    row = create_account(db, payload)
    assert row.id is not None
    assert row.is_management is False
    assert row.external_id == "abc"  # stored but never returned


def test_create_rejects_duplicate(db):
    _seed_management(db)
    payload = CloudAccountCreate(
        account_id="222222222222",
        display_name="Dev",
        role_arn="arn:aws:iam::222222222222:role/X",
    )
    create_account(db, payload)
    with pytest.raises(AccountAlreadyExists):
        create_account(db, payload)


def test_update_changes_display_name_only(db):
    _seed_management(db)
    payload = CloudAccountCreate(
        account_id="222222222222",
        display_name="Dev",
        role_arn="arn:aws:iam::222222222222:role/X",
    )
    row = create_account(db, payload)
    updated = update_account(
        db,
        row,
        CloudAccountUpdate(display_name="Development"),
    )
    assert updated.display_name == "Development"
    assert updated.role_arn == payload.role_arn


def test_update_rejects_management_disable(db):
    mgmt = _seed_management(db)
    with pytest.raises(ManagementAccountImmutable):
        update_account(
            db,
            mgmt,
            CloudAccountUpdate(enabled=False),
        )


def test_update_validates_role_arn(db):
    _seed_management(db)
    payload = CloudAccountCreate(
        account_id="222222222222",
        display_name="Dev",
        role_arn="arn:aws:iam::222222222222:role/X",
    )
    row = create_account(db, payload)
    with pytest.raises(ValueError):
        update_account(
            db,
            row,
            CloudAccountUpdate(role_arn="arn:aws:iam::333333333333:role/Y"),
        )


def test_disable_soft_disables(db):
    _seed_management(db)
    row = create_account(
        db,
        CloudAccountCreate(
            account_id="222222222222",
            display_name="Dev",
            role_arn="arn:aws:iam::222222222222:role/X",
        ),
    )
    disabled = disable_account(db, row)
    assert disabled.enabled is False


def test_disable_management_raises(db):
    mgmt = _seed_management(db)
    with pytest.raises(ManagementAccountImmutable):
        disable_account(db, mgmt)


def test_list_accounts_orders_management_first(db):
    _seed_management(db)
    create_account(
        db,
        CloudAccountCreate(
            account_id="222222222222",
            display_name="Dev",
            role_arn="arn:aws:iam::222222222222:role/X",
        ),
    )
    rows = list_accounts(db, provider="AWS", enabled_only=False)
    assert rows[0].is_management is True


def test_get_management_account_returns_management_row(db):
    mgmt = _seed_management(db)
    assert get_management_account(db).id == mgmt.id


def test_get_account_by_id_returns_row(db):
    mgmt = _seed_management(db)
    assert get_account_by_id(db, mgmt.id).id == mgmt.id
    assert get_account_by_id(db, 9999) is None


# Cleanup: reset the global session factory in case any test mutated it.
@pytest.fixture(autouse=True)
def _reset_factory():
    yield
    reset_default_session_factory()
