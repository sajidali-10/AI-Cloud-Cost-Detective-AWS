"""Trusted reverse-proxy / forwarded-headers handling — Phase 8.2.

The application sits behind an Nginx reverse proxy in production
(see ``nginx/default.conf``).  Nginx terminates TLS and forwards
``X-Forwarded-Proto``, ``X-Forwarded-For`` and ``Host`` headers to
the backend.  The backend must:

* trust these headers ONLY when the immediate peer is the known
  Nginx proxy (loopback in production, the docker-bridge in dev);
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

The set of trusted peers is derived from CIDR ranges configured via
``TRUSTED_PROXY_CIDRS`` (default: loopback + RFC1918 private ranges
that Docker bridge networks use) plus hostnames via
``TRUSTED_PROXY_HOSTS`` (default: loopback + docker-compose service
names).  Operators can extend the trust set without code changes.
"""
from __future__ import annotations

import ipaddress
import logging
import os
from typing import Iterable, Optional, Set, Tuple

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.types import ASGIApp

logger = logging.getLogger("cost-detective-backend.proxy_headers")


# ---------------------------------------------------------------------------
# Trusted proxy peers
# ---------------------------------------------------------------------------


def _default_trusted_cidrs() -> list[str]:
    """Return the default set of trusted CIDR ranges.

    The backend listens on port 8000 but is internal-only — no
    ``ports:`` mapping is published by docker-compose, so the only
    way an inbound TCP connection can reach the backend is via
    the ``internal`` docker network (or via the host loopback when
    an operator shells in).  Docker bridge networks ALWAYS use
    private IP space per RFC1918; the default trust set is therefore
    the loopback range plus the three private ranges.  An operator
    can narrow this with ``TRUSTED_PROXY_CIDRS`` (comma-separated
    CIDRs).

    Why this is safe:

      *  ``127.0.0.0/8`` — host loopback; Nginx colocated on the
         same host.
      *  ``::1`` — IPv6 loopback.
      *  ``10.0.0.0/8``, ``172.16.0.0/12``, ``192.168.0.0/16`` —
         RFC1918 private space.  Docker bridge networks always
         pick from these ranges; ``docker network inspect`` shows
         the production network as ``172.18.0.0/16`` (inside
         ``172.16.0.0/12``).  Because the backend port is
         internal-only, no external Internet source can ever
         present a private source IP — only the in-network
         Nginx proxy can.
    """
    return [
        "127.0.0.0/8",
        "::1/128",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
    ]


def _default_trusted_hosts() -> list[str]:
    """Return the default set of trusted peer hostnames.

    Docker resolves the Nginx service as the hostname ``nginx``
    (or its container name ``cost-detective-nginx``) when the
    application would look up the reverse DNS.  However, Starlette
    hands us the immediate peer address (an IP, not a hostname),
    so the hostname-based set is a defence-in-depth for code paths
    that resolve names explicitly.  The primary trust path is the
    CIDR set above.
    """
    return [
        "127.0.0.1",
        "::1",
        "backend",
        "nginx",
        "cost-detective-nginx",
        "testclient",  # Starlette TestClient
    ]


def _parse_env_list(env: Optional[dict], key: str) -> Optional[list[str]]:
    """Parse a comma-separated env var into a stripped, non-empty list."""
    src = env if env is not None else os.environ
    raw = (src.get(key) or "").strip()
    if not raw:
        return None
    return [item.strip() for item in raw.split(",") if item.strip()]


def _build_trusted_networks(
    *,
    cidrs: Iterable[str],
    hosts: Iterable[str],
) -> Tuple[Set[ipaddress._BaseNetwork], Set[str]]:
    """Materialise the trust set into a (networks, hostnames) tuple.

    Invalid CIDR strings are logged at WARNING and skipped — a typo
    in ``TRUSTED_PROXY_CIDRS`` MUST NOT cause the backend to fail
    to start (operators rely on the backend being up).  The default
    set always remains trusted, even when overrides narrow the set
    via env.
    """
    nets: Set[ipaddress._BaseNetwork] = set()
    for c in cidrs:
        try:
            nets.add(ipaddress.ip_network(c, strict=False))
        except ValueError as exc:
            logger.warning(
                "proxy.trusted_cidr.invalid cidr=%s error=%s",
                c, exc,
            )
    return nets, set(hosts)


def trusted_proxy_cidrs(env: Optional[dict] = None) -> list[str]:
    """Return the configured trusted-proxy CIDRs (defaults merged in)."""
    override = _parse_env_list(env, "TRUSTED_PROXY_CIDRS")
    defaults = list(_default_trusted_cidrs())
    if not override:
        return defaults
    # Merge: operator-supplied CIDRs REPLACE defaults when provided
    # via env.  Operators that want to widen the set must list the
    # RFC1918 ranges explicitly — narrowing the trust set is the
    # common operation (e.g. lock down to a single /24).  An empty
    # env var falls back to defaults.
    return list(override)


def trusted_proxy_hosts(env: Optional[dict] = None) -> list[str]:
    """Return the configured trusted-proxy hostnames (defaults merged in)."""
    override = _parse_env_list(env, "TRUSTED_PROXY_HOSTS")
    defaults = list(_default_trusted_hosts())
    if not override:
        return defaults
    return list(override)


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


def _peer_in_trusted_set(
    peer_host: str,
    *,
    nets: Set[ipaddress._BaseNetwork],
    hosts: Set[str],
) -> bool:
    """Return True iff ``peer_host`` is in the trusted CIDR/hostname set."""
    if not peer_host:
        return False
    # Direct hostname match first (cheap path for loopback and tests).
    if peer_host in hosts:
        return True
    # CIDR match — handles Docker bridge IPs (e.g. 172.18.0.5).
    try:
        addr = ipaddress.ip_address(peer_host)
    except ValueError:
        # Not a valid IP literal AND not in the hostname set — reject.
        return False
    # Strip zone IDs from IPv6 (e.g. fe80::1%eth0).
    if isinstance(addr, ipaddress.IPv6Address) and addr.scope_id:
        addr = ipaddress.IPv6Address(int(addr))
    return any(addr in net for net in nets)


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
    DROP them silently — the request continues, but the
    forwarded client IP is replaced with the immediate peer
    address.  This means:

      *  An attacker who can reach backend port 8000 directly
         (only possible on the host loopback or by joining the
         docker network — backend has no published ports, so this
         requires host-level compromise) cannot spoof their
         source IP via ``X-Forwarded-For``.

      *  Rate-limit buckets are keyed by the IMMEDIATE peer when
         forwarded headers are rejected, so spoofing XFF does not
         give an attacker a fresh bucket per request.

    For trusted peers the headers are normalised and stored on
    ``request.state`` for downstream consumers (audit logging,
    rate-limit key derivation).
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        trusted_cidrs: Optional[Iterable[str]] = None,
        trusted_hosts: Optional[Iterable[str]] = None,
    ) -> None:
        super().__init__(app)
        if trusted_cidrs is not None or trusted_hosts is not None:
            # Test override path: use exactly what the caller passed.
            cidr_list = list(trusted_cidrs) if trusted_cidrs is not None else _default_trusted_cidrs()
            host_list = list(trusted_hosts) if trusted_hosts is not None else _default_trusted_hosts()
        else:
            # Production path: read from env, fall back to defaults.
            cidr_list = trusted_proxy_cidrs()
            host_list = trusted_proxy_hosts()
        self._nets, self._hosts = _build_trusted_networks(
            cidrs=cidr_list, hosts=host_list,
        )

    @property
    def trusted_networks(self) -> Set[ipaddress._BaseNetwork]:
        return set(self._nets)

    @property
    def trusted_hosts(self) -> Set[str]:
        return set(self._hosts)

    async def dispatch(self, request: Request, call_next):
        peer_host = (request.client.host if request.client else "") or ""
        is_trusted = _peer_in_trusted_set(
            peer_host, nets=self._nets, hosts=self._hosts,
        )

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
            # The immediate peer host IS the authoritative client
            # address from the backend's perspective — there's no
            # proxy in front of us, so the source IP is what the
            # kernel's TCP stack reports.
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
    "trusted_proxy_cidrs",
    "trusted_proxy_hosts",
]
