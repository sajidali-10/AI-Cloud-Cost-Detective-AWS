"""Phase 7D.5 — structured operational logging.

Single source of truth for the canonical operational log shape
that every canonical route emits at end-of-request.

Design constraints (Phase 7D.5 §5):

* One log line per request, with a stable set of fields so ops can
  grep / aggregate / alert on them.
* NEVER log secrets: bearer tokens, Azure JWTs, AWS access keys,
  auth headers, DB passwords, LiteLLM API keys, full secret env
  vars.  Use safe labels / truncation helpers (see
  ``app.services.cloud.azure.redaction``).
* Fields:

    request_id
    operation
    provider
    scope_kind            (all | single — never the raw GUID)
    status                (SUCCESS | FAILURE)
    duration_ms
    freshness             (LIVE | CACHED | STALE | PARTIAL | UNAVAILABLE | unknown)
    cache_status          (HIT | REFRESHED | STALE | MISS | DISABLED | unknown)
    upstream_call_count
    retry_count
    error_code            (only when status=FAILURE)
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from contextlib import contextmanager
from typing import Any, Dict, Optional


# Module-level logger — the canonical operational channel.
logger = logging.getLogger("cost-detective-backend.ops")


def new_request_id() -> str:
    """Generate a short correlation/request id.

    Uses ``uuid4`` because it is cheap, dependency-free, and the
    first 8 hex characters are collision-resistant enough for a
    single-process log aggregator.  Falls back to ``os.urandom`` if
    uuid is somehow unavailable.
    """
    try:
        return uuid.uuid4().hex[:12]
    except Exception:  # pragma: no cover - extremely defensive
        return os.urandom(6).hex()


@contextmanager
def operation_timer(operation: str):
    """Context manager that records wall-clock duration.

    Yields a mutable dict ``stats`` the caller fills in.  On exit
    the canonical operational log line is emitted exactly once.
    ``request_id`` is generated unless the caller pre-populated
    ``stats['request_id']`` (e.g. when the same id flows through
    multiple hops).
    """
    stats: Dict[str, Any] = {
        "operation": operation,
        "request_id": new_request_id(),
        "status": "UNKNOWN",
        "duration_ms": 0,
        "provider": None,
        "scope_kind": None,
        "freshness": None,
        "cache_status": None,
        "upstream_call_count": None,
        "retry_count": None,
        "error_code": None,
    }
    start = time.monotonic()
    try:
        yield stats
    except BaseException as exc:
        # Any exception (not just Exception) must be logged at FAILURE
        # before propagating.  We do NOT swallow the exception; the
        # caller decides how to translate it to an HTTP response.
        stats["status"] = "FAILURE"
        if stats["error_code"] is None:
            stats["error_code"] = type(exc).__name__
        _emit(stats, start)
        raise
    else:
        if stats["status"] != "FAILURE":
            stats["status"] = "SUCCESS"
        _emit(stats, start)


def _emit(stats: Dict[str, Any], start: float) -> None:
    stats["duration_ms"] = int((time.monotonic() - start) * 1000)
    # Field-level safety: strip anything that isn't a primitive.
    # Whitelist of documented operational fields.  Any key NOT in
    # this set is silently dropped — callers MUST use the
    # ``record_*`` helpers to populate non-canonical fields, and
    # arbitrary dict mutations (e.g. ``stats['bearer_token'] = ...``)
    # are ignored so secrets cannot leak into the log stream.
    allowed = {
        "operation",
        "request_id",
        "provider",
        "scope_kind",
        "status",
        "duration_ms",
        "freshness",
        "cache_status",
        "upstream_call_count",
        "retry_count",
        "error_code",
    }
    safe = {
        k: v
        for k, v in stats.items()
        if k in allowed and (v is None or isinstance(v, (str, int, float, bool)))
    }
    # Single-line, key=value, sorted for deterministic ordering.
    parts = [f"{k}={safe[k]!r}" for k in sorted(safe)]
    logger.info("cloud.ops " + " ".join(parts))


def record_upstream_calls(stats: Dict[str, Any], n: int) -> None:
    stats["upstream_call_count"] = int(n)


def record_retry_count(stats: Dict[str, Any], n: int) -> None:
    stats["retry_count"] = int(n)


def mark_failure(stats: Dict[str, Any], *, error_code: str) -> None:
    stats["status"] = "FAILURE"
    stats["error_code"] = error_code
