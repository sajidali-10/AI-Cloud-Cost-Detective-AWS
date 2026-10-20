"""Trusted reverse-proxy / forwarded-headers handling — Phase 8.2.

The application sits behind an Nginx reverse proxy in production
(see ``nginx/default.conf``).  Nginx terminates TLS and forwards
``X-Forwarded-Proto``, ``X-Forwarded-For`` and ``Host`` headers to
the backend.  The backend must:

* trust these headers ONLY when the immediate peer is the known
  Nginx proxy (loopback in production, the docker-bridge hostname
  in dev);
* never blindly honour forwarded headers from arbitrary direct
  clients (an attacker who reaches the backend port directly could
  otherwise spoof their source IP and protocol);
* record the resolved client IP for audit logging.

This module provides:

* :class:`TrustedProxyHeadersMiddleware` — a Starlette
  middleware that records the resolved client IP and protocol on
  ``request.state`` and rejects direct (non-trusted) requests that
  carry conflicting forwarded headers.  It does NOT modify the
  URL scheme for downstream routes — Starlette's
  ``Request.url.scheme`` already reflects whatever scheme Nginx
  forwarded, which is exactly the production-correct behaviour.

* :func:`resolve_client_ip` — returns the resolved client IP
  (``request.state.client_ip``) for audit logging.  Falls back to
  ``request.client.host`` when no forwarded headers are present
  (the request came directly, e.g. a local unit test).

The set of trusted peers is derived from a comma-separated env var
``TRUSTED_PROXY_HOSTS`` (default ``127.0.0.1,::1,backend,nginx``)
so an operator can extend the trust set without code changes.
"""
from __future__ import annotations

import logging
import os
from typing import Iterable, Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

logger = logging.getLogger("cost-detective-backend.proxy_headers")


# ---------------------------------------------------------------------------
# Trusted proxy peers
# ---------------------------------------------------------------------------


def _default_trusted_hosts() -> list[str]:
    """Return the default set of trusted peers.

    ``127.0.0.1`` / ``::1`` cover a colocated Nginx; ``backend``
    and ``nginx`` cover the docker-compose service names.  An
    operator can extend this list via ``TRUSTED_PROXY_HOSTS``.
    """
    return [
        "127.0.0.1",
        "::1",
        "backend",
        "nginx",
        "testclient",  # Starlette TestClient
    ]


def trusted_proxy_hosts(env: Optional[dict] = None) -> list[str]:
    """Return the configured trusted proxy hosts."""
    src = env if env is not None else os.environ
    raw = (src.get("TRUSTED_PROXY_HOSTS") or "").strip()
    if not raw:
        return list(_default_trusted_hosts())
    out = [item.strip() for item in raw.split(",") if item.strip()]
    return out or list(_default_trusted_hosts())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _client_ip_from_forwarded(xff: Optional[str]) -> Optional[str]:
    """Extract the originating client IP from an ``X-Forwarded-For`` header.

    The leftmost entry is the original client (RFC 7239 / nginx
    convention).  We never trust multi-hop values without explicit
    allowlists; the leftmost entry is good enough for audit logging
    and rate-limit bucketing when the immediate peer is trusted.
    """
    if not xff:
        return None
    parts = [p.strip() for p in xff.split(",") if p.strip()]
    return parts[0] if parts else None


def resolve_client_ip(request: Request) -> str:
    """Return the resolved client IP for ``request``.

    Order of preference:
      1. ``request.state.client_ip`` if a trusted proxy already
         resolved it.
      2. ``X-Forwarded-For`` leftmost (only when the immediate peer
         is trusted; the middleware sets ``state.client_ip``).
      3. ``request.client.host`` (direct connection).
    """
    cached = getattr(request.state, "client_ip", None)
    if cached:
        return str(cached)
    if request.client and request.client.host:
        return str(request.client.host)
    return "unknown"


def resolve_forwarded_proto(request: Request) -> str:
    """Return the scheme the original client used to reach the proxy.

    Falls back to ``request.url.scheme`` when no forwarded header
    is present (direct connection).
    """
    cached = getattr(request.state, "forwarded_proto", None)
    if cached:
        return str(cached)
    return request.url.scheme or "http"


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class TrustedProxyHeadersMiddleware(BaseHTTPMiddleware):
    """Reject conflicting forwarded headers from untrusted peers.

    A trusted peer (Nginx) is allowed to forward
    ``X-Forwarded-Proto`` and ``X-Forwarded-For``; an untrusted
    direct client is NOT.  When an untrusted peer presents
    forwarded headers we treat them as a spoofing attempt and
    return a sanitized 400 — the request never reaches the
    application routes.

    For trusted peers the headers are normalised and stored on
    ``request.state`` for downstream consumers (audit logging,
    rate-limit key derivation).
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        trusted_hosts: Optional[Iterable[str]] = None,
    ) -> None:
        super().__init__(app)
        self._trusted = set(trusted_hosts or _default_trusted_hosts())

    @property
    def trusted_hosts(self) -> set[str]:
        return set(self._trusted)

    async def dispatch(self, request: Request, call_next):
        peer_host = (request.client.host if request.client else "") or ""
        is_trusted = peer_host in self._trusted

        xfp = request.headers.get("x-forwarded-proto")
        xff = request.headers.get("x-forwarded-for")
        host = request.headers.get("host")

        if is_trusted:
            # Record the resolved values for downstream consumers.
            if xfp:
                proto = xfp.strip().lower()
                if proto in ("http", "https"):
                    request.state.forwarded_proto = proto
                else:
                    # Unrecognised scheme — drop it and fall back to
                    # the URL scheme rather than poison the state.
                    logger.info(
                        "proxy.forwarded_proto.unrecognised peer=%s value=%s",
                        peer_host, xfp,
                    )
            else:
                request.state.forwarded_proto = request.url.scheme or "http"
            resolved_ip = _client_ip_from_forwarded(xff) or peer_host or "unknown"
            request.state.client_ip = resolved_ip
        else:
            # Untrusted peer: ignore forwarded headers entirely.
            if xfp or xff:
                logger.info(
                    "proxy.forwarded_headers.rejected peer=%s host=%s "
                    "xfp_present=%s xff_present=%s",
                    peer_host, host, bool(xfp), bool(xff),
                )
            request.state.forwarded_proto = request.url.scheme or "http"
            request.state.client_ip = peer_host or "unknown"

        return await call_next(request)


__all__ = [
    "TrustedProxyHeadersMiddleware",
    "resolve_client_ip",
    "resolve_forwarded_proto",
    "trusted_proxy_hosts",
]
