"""CORS middleware — Phase 8.2.

Production-aware CORS allowlist sourced from the
``CORS_ALLOWED_ORIGINS`` environment variable (comma-separated).
Behavior:

* **Production hardening.**  When ``app_env`` is ``production`` we
  refuse to start with an empty allowlist, refuse ``*`` as an
  origin, and emit a startup warning when the canonical production
  origin (``https://finops.hiplink.com``) is not in the allowlist.
  This closes the "wildcard with credentials" hole that has
  compromised many FastAPI deployments.
* **Local development.**  When ``app_env`` is anything other than
  ``production`` the allowlist defaults to ``http://localhost`` and
  ``http://127.0.0.1`` (a conservative dev set) when the operator
  has not configured one explicitly.
* **No wildcard with credentials.**  We never return
  ``Access-Control-Allow-Origin: *`` for credentialed requests —
  Starlette's :class:`CORSMiddleware` already enforces this when
  ``allow_origins`` is a list of strings (vs. ``["*"]``).
* **Localhost origins are NEVER allowed in production.**  The
  resolver explicitly drops ``http://localhost`` and
  ``http://127.0.0.1`` from the allowlist when ``app_env`` is
  ``production`` — a deployment that accidentally carried dev
  origins would otherwise expose authenticated APIs to loopback
  cross-origin calls from the operator's browser.

The middleware exposes a single :func:`build_cors_middleware`
helper used by :mod:`app.main` — keeping the configuration logic
in one place keeps it easy to test and audit.
"""
from __future__ import annotations

import logging
from typing import List, Optional, Tuple

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import Settings

logger = logging.getLogger("cost-detective-backend.cors")


#: Canonical production origin.  Kept here (not in config) because
#: it is the deployment contract, not operator-overridable.
PRODUCTION_ORIGIN = "https://finops.hiplink.com"

#: Localhost origins we permit ONLY outside production.
_LOCAL_DEV_ORIGINS = (
    "http://localhost",
    "http://127.0.0.1",
    "http://localhost:8080",
    "http://127.0.0.1:8080",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
)


def _is_local_dev_origin(origin: str) -> bool:
    """Return True iff ``origin`` is a loopback dev origin."""
    o = (origin or "").strip().lower()
    return o in _LOCAL_DEV_ORIGINS


def _normalize(origins_raw: str) -> List[str]:
    """Split ``CORS_ALLOWED_ORIGINS`` into a clean list.

    Whitespace is stripped; empty entries are dropped; the ``*``
    wildcard is detected and handled by :func:`resolve_allowlist`
    (not silently allowed with credentials).
    """
    if not origins_raw:
        return []
    out: List[str] = []
    for item in origins_raw.split(","):
        o = item.strip()
        if not o:
            continue
        out.append(o)
    return out


def resolve_allowlist(settings: Settings) -> Tuple[List[str], List[str]]:
    """Return ``(allow_origins, allow_origin_regex)`` for the deployment.

    Side effect: emits a startup ``warning`` for any production
    misconfiguration (empty allowlist, wildcard, missing production
    origin, accidental loopback entries).
    """
    is_production = (settings.app_env or "").strip().lower() == "production"
    raw = _normalize(settings.cors_allowed_origins)

    if is_production:
        # Drop loopback origins defensively so a deployment that
        # carried dev origins cannot accidentally expose
        # authenticated APIs to loopback cross-origin calls.
        prod_only = [o for o in raw if not _is_local_dev_origin(o)]
        dropped = sorted(set(raw) - set(prod_only))
        if dropped:
            logger.warning(
                "cors.production.dropped_loopback_origins dropped=%s",
                dropped,
            )
        raw = prod_only

        if not raw:
            # Fail-closed — refusing to start is louder than silently
            # blocking every cross-origin request at runtime.
            raise RuntimeError(
                "CORS_ALLOWED_ORIGINS is empty in production. "
                f"Set it to a comma-separated allowlist that includes "
                f"at least {PRODUCTION_ORIGIN!r}."
            )
        if "*" in raw:
            raise RuntimeError(
                "CORS_ALLOWED_ORIGINS contains '*' in production. "
                "Wildcard origins are not permitted for authenticated APIs."
            )
        if PRODUCTION_ORIGIN not in raw:
            # Warn loudly but do not refuse — an operator running a
            # private staging deployment against a different origin
            # is legitimate.  We surface the gap so it cannot be
            # silently forgotten.
            logger.warning(
                "cors.production.missing_canonical_origin expected=%s allowed=%s",
                PRODUCTION_ORIGIN, raw,
            )
        return raw, []

    # Development: fall back to the loopback set when the operator
    # has not configured an allowlist.  This matches the Phase 0-4
    # behaviour where every dev origin was implicitly allowed.
    if not raw:
        raw = list(_LOCAL_DEV_ORIGINS)
    return raw, []


def build_cors_middleware(app: FastAPI, settings: Settings) -> List[str]:
    """Mount the CORS middleware on ``app`` and return the resolved origins.

    Used by :mod:`app.main` at module import time so the misconfiguration
    check runs before the first request.  Returns the allowlist so the
    caller can log it for the operator.
    """
    origins, regex = resolve_allowlist(settings)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_origin_regex=None if not regex else regex[0],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "Accept",
            "X-Requested-With",
        ],
        # Do not expose any browser-readable credentials beyond
        # what we already expose in JSON responses.
        expose_headers=[],
        max_age=600,
    )
    logger.info(
        "cors.configured app_env=%s auth_enabled=%s allow_origins=%s",
        settings.app_env, settings.auth_enabled, origins,
    )
    return origins


__all__ = [
    "PRODUCTION_ORIGIN",
    "build_cors_middleware",
    "resolve_allowlist",
]
