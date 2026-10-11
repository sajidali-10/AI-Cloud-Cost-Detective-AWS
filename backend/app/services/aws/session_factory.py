"""AWS session + credential caching for Phase 7B.

This module owns the two boundaries the rest of the application is
forbidden to cross:

* **AWS session construction** — never call ``boto3.client(...)``
  directly elsewhere; route through :func:`get_aws_client_for`.
* **STS credential storage** — credentials live in an in-process,
  TTL-bounded cache that is never persisted, never logged, and never
  exposed through any HTTP response.

Cache strategy:

* One entry per ``(account_id, role_arn, external_id, region)``
  tuple.  The tuple is hashed and used as the cache key.
* TTL = ``min(expiry, ASSUMED_ROLE_DURATION_SECONDS) - REFRESH_SAFETY``.
* On a cache miss / expiry, ``AssumeRole`` is re-invoked and the
  new credentials replace the old entry atomically.
* Two concurrent callers for the same key share a single
  ``AssumeRole`` call (in-process dedup) so a thundering herd of
  dashboard refreshes cannot multiply AWS API charges.
* ``refresh()`` is intentionally idempotent — it can be called
  preemptively without leaking credentials.

Security:

* The cache is **process-local** (no Redis, no DB).  Restarting the
  backend discards every temporary credential.
* ``RoleSessionName`` is randomised per call so CloudTrail records
  are distinguishable per request burst.
* ``DurationSeconds`` is capped at the STS maximum (12h = 43200s);
  we never request longer-lived credentials than necessary.
* ``ExternalId`` is passed through verbatim when configured; we
  never echo it back to the caller.
* :class:`InMemorySessionCache` exposes ``evict()`` and ``evict_all``
  for tests; production code never calls them.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import random
import string
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

import boto3
from botocore.client import BaseClient
from botocore.config import Config as BotoConfig
from botocore.credentials import RefreshableCredentials
from botocore.exceptions import BotoCoreError, ClientError
from botocore.session import Session as BotocoreSession

logger = logging.getLogger("cost-detective.session_factory")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# STS hard-cap on AssumeRole duration.  We deliberately request the
# full window and rely on the cache TTL to refresh well before
# expiry — staying well clear of the cap makes operational surprises
# less likely if AWS tightens quotas in the future.
ASSUMED_ROLE_DURATION_SECONDS = int(
    os.getenv("PHASE7B_ASSUMED_ROLE_DURATION_SECONDS", "3600")  # 1h by default
)
# Refresh this many seconds BEFORE actual expiry so a long-running
# request cannot pick up a credential that expires mid-flight.
REFRESH_SAFETY_SECONDS = int(
    os.getenv("PHASE7B_SESSION_REFRESH_SAFETY_SECONDS", "300")
)
# Hard ceiling — never request a session longer than this regardless
# of the env var above (defence against a misconfiguration).
ASSUMED_ROLE_DURATION_HARD_CAP = 43200  # 12h

# Boto3 client config — conservative retries and timeouts.  Matches
# the Phase 2 cost_explorer configuration.
DEFAULT_BOTO_CONFIG = BotoConfig(
    retries={"max_attempts": 5, "mode": "standard"},
    connect_timeout=5,
    read_timeout=30,
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AssumeRoleError(Exception):
    """Raised when STS AssumeRole fails for a target account.

    Carries the original Boto3 error code (e.g. ``"AccessDenied"``,
    ``"NoSuchEntity"``) so the account-provider can map it onto the
    public status enum.  Never carries the original exception
    message — those often echo request payloads that we do not want
    to surface.
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# ---------------------------------------------------------------------------
# In-memory credential cache
# ---------------------------------------------------------------------------


@dataclass
class _CachedCredentials:
    """A single AssumeRole result, kept in process memory only."""

    access_key: str
    secret_key: str
    session_token: str
    expiry: datetime
    role_arn: str
    account_id: Optional[str] = None

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        ts = now or datetime.now(timezone.utc)
        # Defensive: boto3 returns naive UTC for ``expiry`` on some
        # platforms — normalize so comparison stays correct.
        expiry = self.expiry
        if expiry is not None and expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        return ts >= expiry - timedelta(seconds=REFRESH_SAFETY_SECONDS)


class InMemorySessionCache:
    """Process-local cache of AssumeRole credentials.

    Thread-safe.  The dedup lock is per-key so two threads waiting on
    the same credential do not both fire AssumeRole.
    """

    def __init__(self) -> None:
        self._entries: Dict[str, _CachedCredentials] = {}
        self._lock = threading.Lock()
        self._key_locks: Dict[str, threading.Lock] = {}
        self._key_locks_guard = threading.Lock()

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _build_key(
        *,
        role_arn: str,
        external_id: Optional[str],
        region: Optional[str],
    ) -> str:
        raw = "|".join([
            role_arn,
            external_id or "",
            region or "",
        ])
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _lock_for(self, key: str) -> threading.Lock:
        with self._key_locks_guard:
            lock = self._key_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._key_locks[key] = lock
            return lock

    # -- main API ----------------------------------------------------------

    def get(self, key: str) -> Optional[_CachedCredentials]:
        with self._lock:
            entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.is_expired():
            with self._lock:
                self._entries.pop(key, None)
            return None
        return entry

    def put(self, key: str, entry: _CachedCredentials) -> None:
        with self._lock:
            self._entries[key] = entry

    def evict(self, key: str) -> None:
        with self._lock:
            self._entries.pop(key, None)

    def evict_all(self) -> None:
        with self._lock:
            self._entries.clear()

    def size(self) -> int:
        with self._lock:
            return len(self._entries)

    def get_or_refresh(
        self,
        *,
        key: str,
        refresh: "_CredentialRefresher",
    ) -> _CachedCredentials:
        """Return cached credentials, refreshing transparently when expired."""
        lock = self._lock_for(key)
        with lock:
            entry = self.get(key)
            if entry is not None:
                return entry
            entry = refresh()
            self.put(key, entry)
            return entry


# Callable that returns a fresh `_CachedCredentials` row.
class _CredentialRefresher:  # pragma: no cover — type alias
    def __call__(self) -> _CachedCredentials: ...


# ---------------------------------------------------------------------------
# Session factory
# ---------------------------------------------------------------------------


@dataclass
class AwsSessionFactory:
    """Single place that constructs boto3 sessions + clients."""

    cache: InMemorySessionCache = field(default_factory=InMemorySessionCache)
    session_duration_seconds: int = ASSUMED_ROLE_DURATION_SECONDS

    # -- management account -------------------------------------------------

    def management_session(self) -> boto3.Session:
        """Return a session backed by the EC2 instance profile.

        No credentials are stored here — Boto3 resolves them lazily
        on the first API call from the IMDS / container role.
        """
        return boto3.Session()

    def management_client(
        self,
        service_name: str,
        *,
        region: Optional[str] = None,
    ) -> BaseClient:
        return self.management_session().client(
            service_name,
            region_name=region,
            config=DEFAULT_BOTO_CONFIG,
        )

    # -- target account -----------------------------------------------------

    def target_session(
        self,
        *,
        role_arn: str,
        external_id: Optional[str] = None,
        region: Optional[str] = None,
    ) -> boto3.Session:
        """Return a session for ``role_arn`` via STS AssumeRole.

        Credentials are cached in process memory with a TTL just
        under the assumed duration.  A second caller for the same
        key gets the cached entry without re-invoking AssumeRole.
        """
        if not role_arn:
            raise AssumeRoleError("InvalidParameter")
        key = InMemorySessionCache._build_key(
            role_arn=role_arn,
            external_id=external_id,
            region=region,
        )

        def _refresh() -> _CachedCredentials:
            return self._assume_role(
                role_arn=role_arn,
                external_id=external_id,
                region=region,
            )

        cached = self.cache.get_or_refresh(key=key, refresh=_refresh)

        # We now hand Boto3 a *RefreshableCredentials* so any client
        # built from this session picks up fresh credentials
        # transparently on the next API call after expiry.  The
        # underlying cache is the source of truth.
        def _refresh_inner() -> dict:
            fresh = self.cache.get(key)
            if fresh is None:
                fresh = _refresh()
                self.cache.put(key, fresh)
            return {
                "access_key": fresh.access_key,
                "secret_key": fresh.secret_key,
                "token": fresh.session_token,
                "expiry_time": fresh.expiry.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }

        refreshable = RefreshableCredentials(
            access_key=cached.access_key,
            secret_key=cached.secret_key,
            token=cached.session_token,
            expiry_time=cached.expiry,
            refresh_using=_refresh_inner,
            method="sts-assume-role",
        )
        botocore_session = BotocoreSession()
        botocore_session._credentials = refreshable
        session = boto3.Session(botocore_session=botocore_session)
        if region:
            session._session.set_config_variable("region", region)
        return session

    def target_client(
        self,
        service_name: str,
        *,
        role_arn: str,
        external_id: Optional[str] = None,
        region: Optional[str] = None,
    ) -> BaseClient:
        session = self.target_session(
            role_arn=role_arn,
            external_id=external_id,
            region=region,
        )
        return session.client(
            service_name,
            region_name=region,
            config=DEFAULT_BOTO_CONFIG,
        )

    # -- AssumeRole primitive ----------------------------------------------

    def _assume_role(
        self,
        *,
        role_arn: str,
        external_id: Optional[str],
        region: Optional[str],
    ) -> _CachedCredentials:
        duration = max(900, min(self.session_duration_seconds, ASSUMED_ROLE_DURATION_HARD_CAP))
        session_name = self._build_session_name(role_arn=role_arn)
        try:
            client = boto3.client(
                "sts",
                region_name=region,
                config=DEFAULT_BOTO_CONFIG,
            )
            params: Dict[str, Any] = {
                "RoleArn": role_arn,
                "RoleSessionName": session_name,
                "DurationSeconds": duration,
            }
            if external_id:
                params["ExternalId"] = external_id
            response = client.assume_role(**params)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "ClientError")
            logger.warning(
                "session_factory.assume_role_failed role=%s code=%s",
                _redact_role(role_arn), code,
            )
            raise AssumeRoleError(code) from None
        except BotoCoreError as exc:
            logger.warning(
                "session_factory.assume_role_failed role=%s code=%s",
                _redact_role(role_arn), type(exc).__name__,
            )
            raise AssumeRoleError(type(exc).__name__) from None

        creds = response.get("Credentials") or {}
        return _CachedCredentials(
            access_key=creds.get("AccessKeyId", ""),
            secret_key=creds.get("SecretAccessKey", ""),
            session_token=creds.get("SessionToken", ""),
            expiry=creds.get("Expiration"),
            role_arn=role_arn,
            account_id=(response.get("AssumedRoleUser") or {}).get("Arn"),
        )

    @staticmethod
    def _build_session_name(*, role_arn: str) -> str:
        # 64-char limit on RoleSessionName; we use a stable prefix
        # plus randomness so two backends in the same account never
        # share a session name (which would cause one to overwrite
        # the other's CloudTrail records).
        suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
        prefix = "cost-detective"
        return f"{prefix}-{suffix}"[:64]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _redact_role(role_arn: str) -> str:
    """Return a log-safe redacted copy of ``role_arn``.

    We log only the account-id segment of the ARN; the role name is
    intentionally omitted so a misconfigured log shipping policy
    cannot leak the operator's IAM layout.

    An IAM role ARN has the shape
    ``arn:aws:iam::123456789012:role/<name>`` — ``split(":")``
    yields ``['arn', 'aws', 'iam', '', '123456789012', 'role/<name>']``
    so the account-id segment lives at index 4.
    """
    if not role_arn or "::" not in role_arn:
        return "<invalid-arn>"
    parts = role_arn.split(":")
    if len(parts) < 6 or not parts[4]:
        return "<invalid-arn>"
    return f"arn:aws:iam::{parts[4]}:role/<redacted>"


# ---------------------------------------------------------------------------
# Process-wide factory
# ---------------------------------------------------------------------------


_DEFAULT_FACTORY: Optional[AwsSessionFactory] = None
_FACTORY_GUARD = threading.Lock()


def get_default_session_factory() -> AwsSessionFactory:
    """Return the process-wide :class:`AwsSessionFactory`.

    Tests can call :func:`set_default_session_factory` to inject a
    mock.  Production code never sets it.
    """
    global _DEFAULT_FACTORY
    if _DEFAULT_FACTORY is None:
        with _FACTORY_GUARD:
            if _DEFAULT_FACTORY is None:
                _DEFAULT_FACTORY = AwsSessionFactory()
    return _DEFAULT_FACTORY


def set_default_session_factory(factory: AwsSessionFactory) -> None:
    """Override the process-wide factory (tests only)."""
    global _DEFAULT_FACTORY
    with _FACTORY_GUARD:
        _DEFAULT_FACTORY = factory


def reset_default_session_factory() -> None:
    """Forget the cached factory (tests only)."""
    global _DEFAULT_FACTORY
    with _FACTORY_GUARD:
        _DEFAULT_FACTORY = None


# ---------------------------------------------------------------------------
# Top-level convenience
# ---------------------------------------------------------------------------


def get_aws_client_for(
    service_name: str,
    *,
    account_row: Optional[Any] = None,
    role_arn: Optional[str] = None,
    external_id: Optional[str] = None,
    region: Optional[str] = None,
) -> BaseClient:
    """Return a Boto3 client routed through the account factory.

    Exactly one of ``account_row`` (a :class:`CloudAccount`) or
    ``role_arn`` may be supplied; if neither, the management
    account / instance profile is used.
    """
    factory = get_default_session_factory()
    if account_row is not None:
        if getattr(account_row, "is_management", False):
            return factory.management_client(service_name, region=region)
        return factory.target_client(
            service_name,
            role_arn=account_row.role_arn,
            external_id=account_row.external_id,
            region=region or account_row.preferred_region,
        )
    if role_arn:
        return factory.target_client(
            service_name,
            role_arn=role_arn,
            external_id=external_id,
            region=region,
        )
    return factory.management_client(service_name, region=region)


__all__ = [
    "ASSUMED_ROLE_DURATION_HARD_CAP",
    "ASSUMED_ROLE_DURATION_SECONDS",
    "AssumeRoleError",
    "AwsSessionFactory",
    "InMemorySessionCache",
    "REFRESH_SAFETY_SECONDS",
    "get_aws_client_for",
    "get_default_session_factory",
    "reset_default_session_factory",
    "set_default_session_factory",
]
