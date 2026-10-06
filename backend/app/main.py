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
# Phase 5A: authentication + admin user management routers.  These
# are mounted with their own prefixes (/auth, /admin/users); nginx
# strips /api/ as usual.
from app.api.auth import router as auth_router
from app.api.admin_users import router as admin_users_router
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
    all_ok = all(v == "ok" for v in components.values())
    if not all_ok:
        response.status_code = 503
        return {
            "status": "degraded",
            "components": components,
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
