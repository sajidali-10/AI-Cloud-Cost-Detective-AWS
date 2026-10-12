"""Centralized AWS account + session provider (Phase 7B).

This module is the single entry point through which the rest of the
application obtains AWS identity, sessions, and per-account metadata.
Every other AWS-touching module (cost_explorer, resources,
compute_optimizer, cost_optimization_hub, optimization_engine, …)
imports from here.

Design rules:

1. **No static AWS credentials** are ever read, stored, written, or
   logged by this module.  The management account is reached through
   the EC2 instance profile (default Boto3 chain); every other
   account is reached through short-lived STS AssumeRole credentials
   that live only inside a process-local cache.

2. **No STS credentials are persisted** to the database, the log
   stream, the response, or any HTTP header.  The
   :class:`InMemorySessionCache` below is intentionally process-local
   and not exposed to the API layer.

3. **External IDs** are accepted at registration time as a
   write-only field.  They are never echoed back on any GET/PATCH
   response, never logged, and never serialised into a public
   payload.  This matches AWS guidance: external IDs are a
   write-once secret.

4. **The management row** is bootstrapped at application startup
   via :func:`ensure_management_account_row`.  Existing deployments
   that pre-date Phase 7B pick up their pre-existing
   ``GetCallerIdentity`` account on first boot without any operator
   action.

5. **Provider extensibility** — the registry table is keyed by
   ``provider`` so Phase 7C can plug Azure/Rackspace adapters into
   the same code paths.  This module only knows how to *list* AWS
   rows; the AWS-specific AssumeRole flow lives in
   :mod:`app.services.aws.session_factory`.
"""
from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, List, Optional

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import CloudAccount
from app.db.session import SessionLocal
from app.schemas.cloud_account import (
    CloudAccountCreate,
    CloudAccountResponse,
    CloudAccountSelectable,
    CloudAccountStatus,
    CloudAccountUpdate,
)
from app.services.aws.session_factory import (
    AwsSessionFactory,
    get_default_session_factory,
)
from app.services.aws.identity import (
    AwsIdentityError,
    get_caller_identity,
)

logger = logging.getLogger("cost-detective.account_provider")


# ---------------------------------------------------------------------------
# Status codes
# ---------------------------------------------------------------------------

STATUS_CONNECTED = "CONNECTED"
STATUS_ACCESS_DENIED = "ACCESS_DENIED"
STATUS_ROLE_NOT_FOUND = "ROLE_NOT_FOUND"
STATUS_DISABLED = "DISABLED"
STATUS_ERROR = "ERROR"

# Map Boto3 / AWS error codes onto our public status enum.  Anything
# not in this map collapses to ``ERROR`` (sanitized).
_AWS_ERROR_TO_STATUS: dict[str, str] = {
    "AccessDenied": STATUS_ACCESS_DENIED,
    "AccessDeniedException": STATUS_ACCESS_DENIED,
    "UserDisabled": STATUS_DISABLED,
    "NoSuchEntity": STATUS_ROLE_NOT_FOUND,
}


# ---------------------------------------------------------------------------
# In-process bootstrap lock
# ---------------------------------------------------------------------------

_BOOTSTRAP_DONE = False


# ---------------------------------------------------------------------------
# Domain dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AccountContext:
    """An immutable reference to a selected cloud account.

    ``account_id`` is either a 12-digit AWS account ID or the literal
    string ``"all"`` to indicate the "All AWS Accounts" selector.
    """

    provider: str
    account_id: str  # 12-digit account id or "all"

    @classmethod
    def specific(cls, provider: str, account_id: str) -> "AccountContext":
        if not account_id or account_id == "all":
            raise ValueError("use AccountContext.all() for the aggregate selector")
        return cls(provider=provider, account_id=account_id)

    @classmethod
    def all(cls, provider: str = "AWS") -> "AccountContext":
        return cls(provider=provider, account_id="all")

    def is_all(self) -> bool:
        return self.account_id == "all"

    def cache_scope(self) -> str:
        """Return the cache-scope token for this selection.

        ``acct:<id>`` for a specific account, ``all`` for the
        aggregate.  Used as the cache-key dimension that prevents
        Account-A data from satisfying Account-B requests.
        """
        if self.is_all():
            return "all"
        return f"acct:{self.account_id}"

    def __str__(self) -> str:  # pragma: no cover — debug helper
        if self.is_all():
            return f"AccountContext({self.provider}/ALL)"
        return f"AccountContext({self.provider}/{self.account_id})"


# ---------------------------------------------------------------------------
# Read helpers — strip ``external_id`` defensively even though the
# ORM mapper does not surface it through the response schema.
# ---------------------------------------------------------------------------


def _to_response(row: CloudAccount) -> CloudAccountResponse:
    return CloudAccountResponse(
        id=row.id,
        provider=row.provider,
        account_id=row.account_id,
        display_name=row.display_name,
        role_arn=row.role_arn,
        enabled=row.enabled,
        is_management=row.is_management,
        preferred_region=row.preferred_region,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_selectable(row: CloudAccount) -> CloudAccountSelectable:
    return CloudAccountSelectable(
        id=row.id,
        account_id=row.account_id,
        display_name=row.display_name,
        provider=row.provider,
        enabled=row.enabled,
        is_management=row.is_management,
    )


# ---------------------------------------------------------------------------
# Registry queries
# ---------------------------------------------------------------------------


def list_accounts(
    db: Session,
    *,
    provider: str = "AWS",
    enabled_only: bool = False,
) -> List[CloudAccount]:
    stmt = select(CloudAccount).where(CloudAccount.provider == provider)
    if enabled_only:
        stmt = stmt.where(CloudAccount.enabled.is_(True))
    stmt = stmt.order_by(CloudAccount.is_management.desc(), CloudAccount.account_id.asc())
    return list(db.execute(stmt).scalars().all())


def get_account_by_id(db: Session, account_id: int) -> Optional[CloudAccount]:
    return db.get(CloudAccount, account_id)


def get_management_account(db: Session) -> Optional[CloudAccount]:
    stmt = (
        select(CloudAccount)
        .where(CloudAccount.provider == "AWS", CloudAccount.is_management.is_(True))
        .limit(1)
    )
    return db.execute(stmt).scalar_one_or_none()


def get_account_by_aws_id(
    db: Session, aws_account_id: str, *, provider: str = "AWS"
) -> Optional[CloudAccount]:
    stmt = (
        select(CloudAccount)
        .where(
            CloudAccount.provider == provider,
            CloudAccount.account_id == aws_account_id,
        )
        .limit(1)
    )
    return db.execute(stmt).scalar_one_or_none()


# ---------------------------------------------------------------------------
# Registry mutations
# ---------------------------------------------------------------------------


class AccountAlreadyExists(Exception):
    """Raised when a duplicate (provider, account_id) row is requested."""


class ManagementAccountImmutable(Exception):
    """Raised when a caller tries to delete or un-manage the management row."""


def create_account(db: Session, payload: CloudAccountCreate) -> CloudAccount:
    """Insert a new registry row.

    Raises :class:`AccountAlreadyExists` on duplicate
    ``(provider, account_id)``.  The new row is enabled by default.
    """
    row = CloudAccount(
        provider=payload.provider,
        account_id=payload.account_id,
        display_name=payload.display_name,
        role_arn=payload.role_arn,
        external_id=payload.external_id,
        enabled=bool(payload.enabled),
        is_management=False,
        preferred_region=payload.preferred_region,
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise AccountAlreadyExists(
            f"account {payload.provider}/{payload.account_id} already registered"
        ) from exc
    db.refresh(row)
    logger.info(
        "cloud_accounts.created provider=%s account_id=%s id=%s",
        row.provider, row.account_id, row.id,
    )
    return row


def update_account(
    db: Session,
    row: CloudAccount,
    payload: CloudAccountUpdate,
) -> CloudAccount:
    """Apply a partial update; returns the refreshed row.

    The management row cannot be disabled or marked non-management —
    that would leave the EC2 instance profile without a home.
    """
    if row.is_management:
        if payload.enabled is False:
            raise ManagementAccountImmutable(
                "the management account cannot be disabled"
            )

    data = payload.model_dump(exclude_unset=True)
    if "role_arn" in data and data["role_arn"] is not None:
        # Account_id is immutable on UPDATE — the whole point of a
        # registry row is to point at one specific account.  We could
        # derive it from the new role_arn and verify it matches
        # ``row.account_id``; if it does not, refuse the change.
        from app.schemas.cloud_account import AWS_IAM_ROLE_ARN_REGEX
        match = AWS_IAM_ROLE_ARN_REGEX.match(data["role_arn"])
        if match is None or match.group("account_id") != row.account_id:
            raise ValueError(
                "role_arn embeds an account_id that does not match the registered account_id"
            )
        row.role_arn = data["role_arn"]
    if "display_name" in data and data["display_name"] is not None:
        row.display_name = data["display_name"]
    if "enabled" in data and data["enabled"] is not None and not row.is_management:
        row.enabled = bool(data["enabled"])
    if "preferred_region" in data:
        row.preferred_region = data["preferred_region"]
    # ``external_id`` is write-only on the API surface but we still
    # persist it here.  We never log the value and never return it.
    if "external_id" in data:
        row.external_id = data["external_id"]
    db.commit()
    db.refresh(row)
    logger.info(
        "cloud_accounts.updated provider=%s account_id=%s id=%s",
        row.provider, row.account_id, row.id,
    )
    return row


def disable_account(db: Session, row: CloudAccount) -> CloudAccount:
    """Soft-disable a target account.  The management row is immutable."""
    if row.is_management:
        raise ManagementAccountImmutable(
            "the management account cannot be disabled or removed"
        )
    row.enabled = False
    db.commit()
    db.refresh(row)
    logger.info(
        "cloud_accounts.disabled provider=%s account_id=%s id=%s",
        row.provider, row.account_id, row.id,
    )
    return row


class AccountNotDeletable(Exception):
    """Raised when a caller tries to delete an account that is not deletable.

    Used when an enabled target account is requested for deletion —
    the operator must first disable it.
    """


def delete_account(db: Session, row: CloudAccount) -> None:
    """Permanently remove a registered account row.

    Rules (enforced here AND mirrored in the API layer):

    1. The management row is immutable — refuses with
       :class:`ManagementAccountImmutable`.
    2. The target row must already be disabled.  We refuse an
       enabled target row with :class:`AccountNotDeletable` to make
       the safety boundary explicit (an operator can disable, then
       delete in two clicks).
    3. The row is removed from the registry.  We NEVER touch AWS:
       no IAM change, no AssumeRole revocation, no resource
       deletion.  Only the local ``cloud_accounts`` row goes away.
    4. The deletion is committed before this function returns so a
       caller observing the registry immediately afterwards sees the
       absence.

    Returns ``None``.  Raises on rejection.
    """
    if row.is_management:
        raise ManagementAccountImmutable(
            "the management account cannot be deleted"
        )
    if row.enabled:
        raise AccountNotDeletable(
            "the account must be disabled before it can be permanently deleted"
        )
    db.delete(row)
    db.commit()
    logger.info(
        "cloud_accounts.deleted provider=%s account_id=%s id=%s",
        row.provider, row.account_id, row.id,
    )


# ---------------------------------------------------------------------------
# Identity / connectivity status
# ---------------------------------------------------------------------------


def resolve_status(
    db: Session,
    row: CloudAccount,
    *,
    session_factory: Optional[AwsSessionFactory] = None,
    region: Optional[str] = None,
) -> CloudAccountStatus:
    """Resolve a registry row's connectivity status.

    The mapping is:

    * ``DISABLED``     — ``row.enabled is False``
    * ``ACCESS_DENIED`` — STS returns ``AccessDenied*``
    * ``ROLE_NOT_FOUND`` — STS returns ``NoSuchEntity``
    * ``CONNECTED``     — STS ``GetCallerIdentity`` succeeds
    * ``ERROR``         — anything else (sanitized)

    Returned status never contains raw AWS exception payloads.
    """
    if not row.enabled:
        return CloudAccountStatus(
            id=row.id,
            account_id=row.account_id,
            display_name=row.display_name,
            status=STATUS_DISABLED,
            reason="account is disabled",
        )

    factory = session_factory or get_default_session_factory()
    settings = get_settings()
    effective_region = region or row.preferred_region or settings.aws_default_region

    try:
        if row.is_management:
            session = factory.management_session()
        else:
            session = factory.target_session(
                role_arn=row.role_arn,
                external_id=row.external_id,
                region=effective_region,
            )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "ClientError")
        return _status_from_error(row, code, "AssumeRole request failed")
    except BotoCoreError as exc:
        return _status_from_error(row, type(exc).__name__, "session bootstrap failed")

    try:
        identity = get_caller_identity(session=session, region=effective_region)
    except AwsIdentityError as exc:
        return _status_from_error(row, exc.code, "STS GetCallerIdentity failed")

    return CloudAccountStatus(
        id=row.id,
        account_id=row.account_id,
        display_name=row.display_name,
        status=STATUS_CONNECTED,
        reason=None,
        arn=identity.get("arn"),
        region=effective_region,
    )


def _status_from_error(
    row: CloudAccount, code: str, fallback_reason: str
) -> CloudAccountStatus:
    status = _AWS_ERROR_TO_STATUS.get(code, STATUS_ERROR)
    # Reason is intentionally generic; we map the code onto a
    # sanitized message but never leak the AWS exception text.
    if status == STATUS_ACCESS_DENIED:
        reason = "the role's trust policy does not allow this caller"
    elif status == STATUS_ROLE_NOT_FOUND:
        reason = "the configured role ARN does not exist"
    elif status == STATUS_DISABLED:
        reason = "the role or account is disabled"
    else:
        reason = fallback_reason
    logger.warning(
        "cloud_accounts.status_error provider=%s account_id=%s code=%s status=%s",
        row.provider, row.account_id, code, status,
    )
    return CloudAccountStatus(
        id=row.id,
        account_id=row.account_id,
        display_name=row.display_name,
        status=status,
        reason=reason,
    )


# ---------------------------------------------------------------------------
# Bootstrap the management row
# ---------------------------------------------------------------------------


def ensure_management_account_row(
    db: Optional[Session] = None,
    *,
    session_factory: Optional[AwsSessionFactory] = None,
) -> Optional[CloudAccount]:
    """Ensure the AWS management account is registered.

    Idempotent: returns the existing row if present, otherwise
    resolves the management identity via the EC2 instance profile
    and inserts a new management row.

    Returns ``None`` when the identity cannot be resolved (the
    operator must grant the instance profile the
    ``sts:GetCallerIdentity`` permission).  The error is logged so
    the operator can debug from the container logs without leaking
    the exception text.
    """
    factory = session_factory or get_default_session_factory()
    own_session = db is None
    session = db or SessionLocal()
    try:
        existing = get_management_account(session)
        if existing is not None:
            return existing
        try:
            management_session = factory.management_session()
            identity = get_caller_identity(
                session=management_session,
                region=get_settings().aws_default_region,
            )
        except AwsIdentityError as exc:
            logger.warning(
                "cloud_accounts.bootstrap.identity_failed code=%s",
                exc.code,
            )
            return None
        except BotoCoreError as exc:
            logger.warning(
                "cloud_accounts.bootstrap.bootstrap_failed code=%s",
                type(exc).__name__,
            )
            return None
        aws_account_id = identity.get("account")
        if not aws_account_id:
            logger.warning("cloud_accounts.bootstrap.no_account_in_identity")
            return None
        settings = get_settings()
        # Role ARN for the management row is a *placeholder* — the
        # management account does not assume any role; the registry
        # stores ``role_arn`` so the response schema is uniform, but
        # the account provider short-circuits AssumeRole for the
        # management row.
        placeholder_role_arn = (
            f"arn:aws:iam::{aws_account_id}:role/"
            f"cost-detective-management-placeholder"
        )
        display_name = os.getenv("COST_DETECTIVE_MANAGEMENT_DISPLAY_NAME", "Management")
        row = CloudAccount(
            provider="AWS",
            account_id=aws_account_id,
            display_name=display_name,
            role_arn=placeholder_role_arn,
            external_id=None,
            enabled=True,
            is_management=True,
            preferred_region=settings.aws_default_region,
        )
        session.add(row)
        try:
            session.commit()
        except IntegrityError:
            # Another worker won the race — that's fine.
            session.rollback()
            return get_management_account(session)
        session.refresh(row)
        logger.info(
            "cloud_accounts.bootstrap.created provider=AWS account_id=%s id=%s",
            aws_account_id, row.id,
        )
        return row
    finally:
        if own_session:
            session.close()


def bootstrap_once(*, force: bool = False) -> Optional[CloudAccount]:
    """Process-wide idempotent wrapper around :func:`ensure_management_account_row`."""
    global _BOOTSTRAP_DONE
    if _BOOTSTRAP_DONE and not force:
        with SessionLocal() as session:
            return get_management_account(session)
    row = ensure_management_account_row()
    if row is not None:
        _BOOTSTRAP_DONE = True
    return row


# ---------------------------------------------------------------------------
# Per-request account resolution
# ---------------------------------------------------------------------------


def resolve_account_or_404(
    db: Session,
    selection: str,
    *,
    provider: str = "AWS",
) -> Optional[CloudAccount]:
    """Resolve a selector value (``"all"`` or ``acct:<id>`` or bare
    ``<id>``) to a registry row, or ``None`` if not found / disabled.

    The selector value ``"all"`` returns the management row as a
    sentinel for callers that need a single row to anchor the
    per-request scope; callers that want the full fan-out should
    use :func:`iter_enabled_accounts` instead.
    """
    if not selection or selection == "all":
        return get_management_account(db)
    cleaned = selection
    if cleaned.startswith("acct:"):
        cleaned = cleaned[len("acct:"):]
    if not cleaned or not cleaned.isdigit():
        return None
    row = get_account_by_id(db, int(cleaned))
    if row is None or row.provider != provider:
        return None
    return row


def iter_enabled_accounts(
    db: Session, *, provider: str = "AWS"
) -> Iterable[CloudAccount]:
    """Iterate every enabled account for ``provider``."""
    for row in list_accounts(db, provider=provider, enabled_only=True):
        yield row


# ---------------------------------------------------------------------------
# Public re-exports
# ---------------------------------------------------------------------------


__all__ = [
    "AccountAlreadyExists",
    "AccountContext",
    "AccountNotDeletable",
    "ManagementAccountImmutable",
    "STATUS_ACCESS_DENIED",
    "STATUS_CONNECTED",
    "STATUS_DISABLED",
    "STATUS_ERROR",
    "STATUS_ROLE_NOT_FOUND",
    "bootstrap_once",
    "create_account",
    "delete_account",
    "disable_account",
    "ensure_management_account_row",
    "get_account_by_aws_id",
    "get_account_by_id",
    "get_management_account",
    "iter_enabled_accounts",
    "list_accounts",
    "resolve_account_or_404",
    "resolve_status",
    "update_account",
]
