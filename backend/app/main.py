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
from fastapi import FastAPI, Response
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import get_settings

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
    description="Phase 0 platform foundation. No AWS / no LLM calls.",
)


def _safe_component_status(name: str, exc: Exception) -> str:
    """Return a sanitized status string for a failed dependency check.

    Never leaks the underlying exception message, password, or connection string.
    """
    logger.warning("dependency %s unavailable: %s", name, type(exc).__name__)
    return "error"


def _check_database() -> str:
    """Return 'ok' if SELECT 1 succeeds, else 'error'."""
    try:
        engine = create_engine(settings.database_url, pool_pre_ping=True)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
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
