"""FastAPI application — Phase 0 platform foundation.

Endpoints:
    GET /health         → liveness, always 200.
    GET /health/ready   → readiness, performs safe dependency checks.

The app does NOT touch AWS, does NOT call any LLM, and does NOT include
authentication. Those are deferred to later phases.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.core.cors import build_cors_middleware
from app.core.proxy_headers import TrustedProxyHeadersMiddleware
from app.api.aws import router as aws_router
# Phase 4: AI Cost Analyst router (own /ai prefix).  Imported for
# its side-effect of registering @router.<verb> decorators.
from app.api import ai as ai_routes  # noqa: F401  (registers /api/ai/*)
# Phase 2: cost + utilization + evidence routes.  Importing these
# modules is what triggers their @aws_router.<verb> decorators, which
# in turn registers the routes on the FastAPI app.  The modules are
# ordered to match the spec (costs -> utilization -> evidence).
from app.api import aws_costs  # noqa: F401  (registers /api/aws/costs)
from app.api import aws_utilization  # noqa: F401  (registers /api/aws/utilization)
from app.api import aws_evidence  # noqa: F401  (registers /api/aws/evidence)
from app.api import aws_optimization  # noqa: F401  (registers /api/aws/optimization/*)
# Phase 7B: cloud-account registry routes.  Mounted on the same
# aws_router so the public URL is /api/cloud-accounts/* after Nginx
# strips /api/.
from app.api import cloud_accounts  # noqa: F401  (registers /api/cloud-accounts/*)
# Phase 7C.1: provider-neutral routes.  Mounted with the
# ``/cloud`` prefix so the public URL is /api/cloud/* after
# Nginx strips /api/.  These are the canonical provider-neutral
# surface; the AWS-specific routes below remain for backwards
# compatibility.
from app.api.cloud_providers import router as cloud_providers_router
# Phase 7C.2B: provider-neutral cloud cost surface.  Mounted
# alongside cloud_providers so the canonical route is
# ``/api/cloud/costs`` after Nginx strips ``/api/``.
from app.api.cloud_costs import router as cloud_costs_router
# Phase 7C.2C: provider-neutral cloud resource surface.  Mounted
# alongside cloud_providers / cloud_costs so the canonical route
# is ``/api/cloud/resources`` after Nginx strips ``/api/``.
from app.api.cloud_resources import router as cloud_resources_router
# Phase 7C.2D: provider-neutral cloud recommendations surface.
# Mounted alongside cloud_providers / cloud_costs / cloud_resources
# so the canonical route is ``/api/cloud/recommendations`` after
# Nginx strips ``/api/``.
from app.api.cloud_recommendations import router as cloud_recommendations_router
from app.api.cloud_ai import router as cloud_ai_router
# Phase 5A: authentication + admin user management routers.  These
# are mounted with their own prefixes (/auth, /admin/users); nginx
# strips /api/ as usual.
from app.api.auth import router as auth_router
from app.api.admin_users import router as admin_users_router
# Phase 5B: durable conversation + AI history routes.
from app.api.conversations import router as conversations_router
# Phase 5C: secure WebSocket realtime conversation transport.
# Mounted under its own prefix (/ws/conversations) so the public path
# becomes /api/ws/conversations/{id} after nginx strips /api/.  The
# WebSocket router does NOT inherit the JSON ``HTTPException``
# handler below because it sends structured events, not HTTP
# responses.
from app.api.ws_conversations import router as ws_conversations_router
from app.core.config import get_settings
from app.db.session import get_engine

# --- Structured logging (JSON-ish key=value lines) ---
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format='%(asctime)s level=%(levelname)s logger=%(name)s msg="%(message)s"',
)
logger = logging.getLogger("cost-detective-backend")

settings = get_settings()
app = FastAPI(
    title="AI Cloud Cost Detective — Backend",
    version="0.1.0",
    description=(
        "Phase 4 AI Cost Analyst + LiteLLM Gateway integration "
        "(read-only AWS evidence + grounded advisory AI)."
    ),
)

# Phase 8.2 — trusted reverse-proxy awareness MUST be mounted
# BEFORE CORS so the resolved client IP is available to the CORS
# middleware and to auth rate-limit key derivation.
app.add_middleware(TrustedProxyHeadersMiddleware)

# Phase 8.2 — production-aware CORS allowlist.  This call validates
# the ``CORS_ALLOWED_ORIGINS`` env var against ``app_env`` and
# raises at startup on a misconfiguration (empty production
# allowlist, wildcard with credentials, accidental loopback origins
# in production).  The same configuration is logged at INFO so
# operators can verify the resolved allowlist at boot.
build_cors_middleware(app, settings)

# Phase 1: read-only AWS identity + resource discovery router.
# Mounted at /aws/identity and /aws/resources. Nginx strips the /api/ prefix
# before proxying, so /api/aws/identity (public) becomes /aws/identity inside.
app.include_router(aws_router)

# Phase 4: AI Cost Analyst router (own /ai prefix; Nginx strips /api/).
app.include_router(ai_routes.router)

# Phase 5A: authentication + admin user management routers.  Mounted
# with their own prefixes (/auth, /admin/users); nginx strips /api/.
app.include_router(auth_router)
app.include_router(admin_users_router)

# Phase 5B: conversation + AI history router (own /conversations
# prefix; nginx strips /api/).  Requires AUTH_ENABLED=true; returns
# a controlled ``AuthDisabled`` response otherwise.
app.include_router(conversations_router)

# Phase 5C: realtime conversation WebSocket router (own
# /ws/conversations prefix; nginx strips /api/).  The endpoint
# requires AUTH_ENABLED=true and closes the upgrade with a 1008
# policy-violation code when authentication is disabled.  nginx is
# responsible for the WebSocket upgrade headers.
app.include_router(ws_conversations_router)

# Phase 7C.1: provider-neutral cloud routes mounted under /api/cloud/*.
app.include_router(cloud_providers_router)
# Phase 7C.2B: provider-neutral cloud cost route mounted under /api/cloud/*.
app.include_router(cloud_costs_router)
# Phase 7C.2C: provider-neutral cloud resource route mounted under /api/cloud/*.
app.include_router(cloud_resources_router)
# Phase 7C.2D: provider-neutral cloud recommendations route mounted under /api/cloud/*.
app.include_router(cloud_recommendations_router)
# Phase 7D.4: multi-cloud AI FinOps Analyst route mounted under /api/cloud/*.
app.include_router(cloud_ai_router)


# ---------------------------------------------------------------------------
# Phase 7B: bootstrap the management-account row.
#
# We do this at startup (after the FastAPI app is wired up but
# before it begins serving) so that an existing single-account
# deployment picks up its management row without any operator
# action.  The bootstrap is idempotent — see
# :func:`app.services.aws.account_provider.bootstrap_once`.
# ---------------------------------------------------------------------------


@app.on_event("startup")
def _phase7b_bootstrap_management_account() -> None:
    """Ensure the management AWS account is registered.

    Wrapped in a try/except so a transient STS failure cannot prevent
    the application from starting — the operator will simply see a
    warning in the logs and the registry will be empty until the
    instance profile is granted ``sts:GetCallerIdentity``.
    """
    from app.services.aws.account_provider import bootstrap_once

    try:
        bootstrap_once()
    except Exception as exc:  # noqa: BLE001 — bootstrap must never block startup
        logger.warning(
            "phase7b bootstrap_management_account failed: %s",
            type(exc).__name__,
        )


# ---------------------------------------------------------------------------
# Phase 7C.2A: bootstrap the configured Azure subscriptions.
#
# Subscription IDs are read from runtime configuration
# (``AZURE_SUBSCRIPTION_<N>_*`` env vars, see
# :mod:`app.services.cloud.azure.registrations`) and inserted
# idempotently on first boot.  When the runtime is not
# configured (no Entra tenant / client ids), the bootstrap is
# a silent no-op so a deployment without Azure keeps working.
# ---------------------------------------------------------------------------


@app.on_event("startup")
def _phase7c2a_bootstrap_azure_subscriptions() -> None:
    """Idempotently register the configured Azure subscriptions.

    Wrapped in a try/except so a misconfiguration cannot prevent
    the application from starting.  Operators can always add
    subscriptions later through the standard
    ``POST /api/cloud-accounts`` API.
    """
    try:
        from app.services.cloud.azure.adapter import (
            bootstrap_azure_subscriptions_once,
        )

        bootstrap_azure_subscriptions_once()
    except Exception as exc:  # noqa: BLE001 — bootstrap must never block startup
        logger.warning(
            "phase7c2a bootstrap_azure_subscriptions failed: %s",
            type(exc).__name__,
        )


# ---------------------------------------------------------------------------
# Phase 9B.1: bootstrap the configured Rackspace account.
#
# The single Rackspace Cloud tenant (``RACKSPACE_ACCOUNT_ID``) is
# derived from runtime configuration and inserted idempotently on
# first boot so the canonical cost route can select it without any
# manual database setup.  Only non-secret account metadata is
# stored — the Billing Account Number (RAN) and every credential
# stay in the protected host ``.env``.  When Rackspace is not
# configured the bootstrap is a silent no-op.
# ---------------------------------------------------------------------------


@app.on_event("startup")
def _phase9b1_bootstrap_rackspace_account() -> None:
    """Idempotently register the configured Rackspace account."""
    try:
        from app.services.cloud.rackspace.adapter import (
            bootstrap_rackspace_account_once,
        )

        bootstrap_rackspace_account_once()
    except Exception as exc:  # noqa: BLE001 — bootstrap must never block startup
        logger.warning(
            "phase9b1 bootstrap_rackspace_account failed: %s",
            type(exc).__name__,
        )


# ---------------------------------------------------------------------------
# Flatten HTTPException ``detail`` dicts into the top-level response
# body so auth/RBAC errors share the same envelope as the rest of
# the API (``{status, error_code, message, ...}``).
# ---------------------------------------------------------------------------


@app.exception_handler(HTTPException)
async def _http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail
    if isinstance(detail, dict) and {"status", "error_code", "message"}.issubset(set(detail.keys())):
        body: Dict[str, Any] = {k: v for k, v in detail.items() if k != "headers"}
    else:
        body = {
            "status": "error",
            "error_code": "HTTPError",
            "message": str(detail) if detail is not None else "request failed",
        }
    headers = getattr(exc, "headers", None)
    return JSONResponse(status_code=exc.status_code, content=body, headers=headers)


def _safe_component_status(name: str, exc: Exception) -> str:
    """Return a sanitized status string for a failed dependency check.

    Never leaks the underlying exception message, password, or connection string.
    """
    logger.warning("dependency %s unavailable: %s", name, type(exc).__name__)
    return "error"


def _check_database() -> str:
    """Return 'ok' if SELECT 1 succeeds, else 'error'.

    Phase 2: reuses the process-wide engine from :mod:`app.db.session`
    rather than building a throwaway engine per probe.  The engine is
    created once at import time and shared with the cost-cache layer
    added by Phase 2.  ``engine.dispose()`` is intentionally NOT
    called here — disposing would invalidate the cache layer's
    connection pool.
    """
    try:
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return "ok"
    except (SQLAlchemyError, OSError) as exc:
        return _safe_component_status("database", exc)


async def _check_litellm() -> str:
    """Return 'ok' if LiteLLM /health/readiness indicates healthy, else 'error'.

    LiteLLM's readiness endpoint returns either {"status": "healthy", ...} (ready)
    or a non-200 status when not ready. Older releases used "ready" instead of
    "healthy"; accept either.
    """
    try:
        timeout = httpx.Timeout(3.0, connect=2.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(f"{settings.litellm_base_url}/health/readiness")
        if r.status_code != 200:
            return "error"
        body = r.text.lower()
        if "ready" in body or '"status":"healthy"' in body:
            return "ok"
        return "error"
    except (httpx.HTTPError, OSError) as exc:
        return _safe_component_status("litellm", exc)


@app.get("/health")
def health() -> Dict[str, Any]:
    """Liveness probe — always 200 if the process is alive."""
    return {
        "status": "ok",
        "service": "ai-cloud-cost-detective-backend",
        "version": "0.1.0",
        "phase": 0,
    }


@app.get("/health/ready")
async def health_ready(response: Response) -> Dict[str, Any]:
    """Readiness probe — checks database and LiteLLM (sanitized)."""
    db_status = _check_database()
    llm_status = await _check_litellm()

    components = {
        "backend": "ok",
        "database": db_status,
        "litellm": llm_status,
    }
    # Phase 7D.5 (§6) — only the database is a hard readiness
    # dependency.  LiteLLM being unreachable is a dependency
    # condition (the AI endpoint returns a controlled UNAVAILABLE
    # / DISABLED response) but MUST NOT mark the backend
    # unhealthy.  Likewise, AWS / Azure transient throttling
    # does NOT affect application readiness — those are
    # upstream data sources, not infra dependencies.
    db_ok = components["database"] == "ok"
    if not db_ok:
        response.status_code = 503
        return {
            "status": "not_ready",
            "components": components,
            "reason": "database_unavailable",
        }
    if llm_status != "ok":
        # Database OK + LiteLLM down → still ready, but report
        # the dependency condition honestly.  The AI endpoints
        # surface DISABLED / UNAVAILABLE without making the
        # backend unhealthy.
        return {
            "status": "ready",
            "components": components,
            "dependency_conditions": ["litellm_unavailable"],
        }
    return {
        "status": "ready",
        "components": components,
    }


@app.get("/")
def root() -> Dict[str, Any]:
    return {
        "service": "ai-cloud-cost-detective-backend",
        "phase": 0,
        "docs": "/docs",
    }
