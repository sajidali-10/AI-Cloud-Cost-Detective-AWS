"""In-process auth rate limiter — Phase 8.2.

Bounded-token-bucket rate limiter applied to authentication-sensitive
endpoints (currently ``POST /api/auth/login``).  Implementation notes:

* **In-memory only.**  A single FastAPI worker holds the counters;
  this is appropriate for the Phase 8.2 scope ("bounded rate limiting
  to actual login endpoints", not a globally consistent distributed
  throttle).  For multi-worker horizontal scaling, swap the backend
  for Redis — the public surface (``check``/``reset``) is designed
  to make that swap mechanical.
* **Bounded key set.**  Old entries are evicted on a deterministic
  schedule (callers call :func:`evict_stale` after every check) so
  the limiter can never grow without bound under attacker churn.
  Bounded memory is a hard requirement: an unbounded key set would
  itself be a denial-of-service vector.
* **Predictable 429.**  When a client is over budget we raise a
  controlled :class:`AuthRateLimited` exception that the route
  layer translates to a 429 with a ``Retry-After`` header.
* **Key = (endpoint, client_ip).**  Using the request's remote
  address — which behind a trusted reverse proxy is the
  X-Forwarded-For hop-1 value (see ``TrustedForwardedHeaders``
  middleware) — buckets attacks per source.  Endpoints that
  legitimately need a different key (e.g. per-account lockout) are
  out of scope for Phase 8.2.
* **Argon2id cost is already a brute-force defence.**  Even
  without this limiter, each ``verify`` call costs ~50-250 ms.
  This layer closes the cheap loop where an attacker probes the
  endpoint without ever performing a verify.

This module is intentionally dependency-free (no FastAPI imports)
so it can be exercised by direct unit tests and by the Phase 8.2
validator via the ``/api/auth/login`` HTTP surface.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

logger = logging.getLogger("cost-detective-backend.auth_rate_limiter")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthRateLimit:
    """Configuration for :class:`AuthRateLimiter`.

    ``capacity`` = maximum tokens in the bucket; ``refill_per_second``
    is the steady-state allowance.  Default: 10 attempts in a 60s
    sliding window, refilled at 10/60s ≈ 0.167/s, retry-after 6s.
    """

    capacity: int = 10
    refill_per_second: float = 10.0 / 60.0
    retry_after_seconds: int = 6
    # Hard cap on the number of distinct keys tracked.  When the
    # cap is reached the oldest key is evicted (FIFO).  Bounded
    # memory is a hard requirement.
    max_keys: int = 10_000


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AuthRateLimited(Exception):
    """Raised when the rate-limit budget for the key is exhausted.

    The route layer translates this to ``HTTP 429`` with a
    ``Retry-After`` header.
    """

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(f"auth rate limit exceeded (retry after {retry_after_seconds}s)")
        self.retry_after_seconds = retry_after_seconds


# ---------------------------------------------------------------------------
# Token-bucket state
# ---------------------------------------------------------------------------


@dataclass
class _Bucket:
    tokens: float
    last_refill: float

    def refill(self, *, now: float, rate_per_sec: float, capacity: int) -> None:
        elapsed = max(0.0, now - self.last_refill)
        if elapsed > 0:
            self.tokens = min(float(capacity), self.tokens + elapsed * rate_per_sec)
            self.last_refill = now


# ---------------------------------------------------------------------------
# Limiter
# ---------------------------------------------------------------------------


class AuthRateLimiter:
    """Thread-safe in-memory token-bucket rate limiter.

    Key = ``(endpoint, client_key)``.  Tests instantiate a fresh
    instance; production wires a module-level singleton via
    :func:`get_auth_rate_limiter`.
    """

    def __init__(self, config: AuthRateLimit | None = None) -> None:
        self._cfg = config or AuthRateLimit()
        self._buckets: "OrderedDict[Tuple[str, str], _Bucket]" = OrderedDict()
        self._lock = threading.Lock()

    @property
    def config(self) -> AuthRateLimit:
        return self._cfg

    # ----- public API -----

    def check(self, *, endpoint: str, client_key: str) -> None:
        """Consume a token for ``(endpoint, client_key)``.

        Raises :class:`AuthRateLimited` if the bucket is empty.
        """
        if not endpoint:
            raise ValueError("endpoint must be a non-empty string")
        if not client_key:
            # Fail closed: an unknown client cannot be safely
            # bucket-tracked, so we refuse.  The route layer is
            # responsible for providing a stable key.
            raise ValueError("client_key must be a non-empty string")

        cfg = self._cfg
        key = (endpoint, client_key)
        now = time.monotonic()

        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(tokens=float(cfg.capacity), last_refill=now)
                self._buckets[key] = bucket
            else:
                bucket.refill(
                    now=now,
                    rate_per_sec=cfg.refill_per_second,
                    capacity=cfg.capacity,
                )
                # Touch for FIFO ordering.
                self._buckets.move_to_end(key)

            if bucket.tokens < 1.0:
                # Calculate how long until 1 token is available.
                deficit = 1.0 - bucket.tokens
                wait = max(1, int(round(deficit / max(cfg.refill_per_second, 1e-9))))
                raise AuthRateLimited(retry_after_seconds=wait)

            bucket.tokens -= 1.0

        # Opportunistic eviction — keeps the dict bounded without a
        # background thread.
        self.evict_stale(now=now)

    def reset(self) -> None:
        """Drop every tracked key.  Test-only."""
        with self._lock:
            self._buckets.clear()

    def evict_stale(self, *, now: Optional[float] = None) -> int:
        """Evict buckets that are already at full capacity.

        Returns the number of keys dropped.  Called opportunistically
        after every ``check`` so the bucket dict can never grow
        without bound under attacker churn.
        """
        now = now if now is not None else time.monotonic()
        cfg = self._cfg
        evicted = 0

        with self._lock:
            # Enforce hard cap.
            while len(self._buckets) > cfg.max_keys:
                self._buckets.popitem(last=False)
                evicted += 1

            # Drop buckets that have refilled to full capacity and
            # have not been touched in the past 10 minutes.  These
            # keys are not interesting from a brute-force defence
            # perspective and only consume memory.
            stale_threshold = 600.0  # 10 minutes
            stale_keys = [
                k for k, b in self._buckets.items()
                if (now - b.last_refill) > stale_threshold
                and b.tokens >= float(cfg.capacity) - 1e-9
            ]
            for k in stale_keys:
                self._buckets.pop(k, None)
                evicted += 1

        return evicted

    # ----- introspection (tests + validator) -----

    def stats(self) -> Dict[str, int]:
        with self._lock:
            return {
                "tracked_keys": len(self._buckets),
                "max_keys": self._cfg.max_keys,
            }


# ---------------------------------------------------------------------------
# Singleton wiring
# ---------------------------------------------------------------------------


_auth_rate_limiter_singleton: AuthRateLimiter | None = None


def get_auth_rate_limiter() -> AuthRateLimiter:
    """Return the process-wide :class:`AuthRateLimiter`."""
    global _auth_rate_limiter_singleton
    if _auth_rate_limiter_singleton is None:
        _auth_rate_limiter_singleton = AuthRateLimiter()
    return _auth_rate_limiter_singleton


def reset_auth_rate_limiter_for_tests() -> None:
    """Drop the singleton (and its state).  Test-only."""
    global _auth_rate_limiter_singleton
    _auth_rate_limiter_singleton = None


__all__ = [
    "AuthRateLimit",
    "AuthRateLimited",
    "AuthRateLimiter",
    "get_auth_rate_limiter",
    "reset_auth_rate_limiter_for_tests",
]
