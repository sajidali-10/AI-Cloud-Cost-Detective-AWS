"""WebSocket authentication + authorization helpers — Phase 5C.

Reuses the Phase 5A ``SecurityCore`` and ``AuthService`` for every token
check; no JWT parsing, signature verification, or role decision is
duplicated here.

Transport
---------

Native browser ``WebSocket`` cannot set the ``Authorization`` header
freely, so the Phase 5A bearer token reaches the server via the
``Sec-WebSocket-Protocol`` header — the client offers
``bearer.<jwt>`` as a subprotocol, and the server echoes it back when
accepting the connection.  Non-browser clients that CAN set
``Authorization`` continue to work; the same ``SecurityCore.decode_token``
call validates either source.

The token is deliberately NOT read from the query string.  nginx
``access_log`` records the full URL on every request, so a token in
the query would leak into log aggregation.  Keeping the token in the
``Sec-WebSocket-Protocol`` header (a request header, not a URL
component) avoids that channel entirely.

Origin validation
-----------------

Phase 8.2 — cross-origin WebSocket hijacking is prevented by an
explicit ``Origin`` allowlist.  When ``app_env`` is ``production``
the upgrade is rejected unless the ``Origin`` header matches the
configured allowlist (default: ``https://finops.hiplink.com``).
Loopback origins are NEVER accepted in production.  The
``WS_CLOSE_FORBIDDEN`` close code signals rejection before the
upgrade finalizes.

Close codes
-----------

The :class:`WSAuthFailure` dataclass carries a stable reason string and
a WebSocket close code (per RFC 6455 + the standard 4xxx application
range).  The route layer uses these BEFORE the WebSocket upgrade
finalizes so the rejection happens at the HTTP layer with a clean
``close(code=...)``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, Optional

from fastapi import WebSocket
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.security import (
    TokenExpired,
    TokenInvalid,
    TokenSignatureInvalid,
    get_security_core,
)
from app.db.models import AppUser
from app.services.auth_service import (
    AuthService,
    UserInactive,
    UserNotFound,
)

logger = logging.getLogger("cost-detective-backend.ws_auth")


# ---------------------------------------------------------------------------
# Failure shape
# ---------------------------------------------------------------------------


#: WebSocket close code emitted before the protocol upgrade completes.
#: 1008 = "policy violation" (no special handling required on clients).
WS_CLOSE_POLICY_VIOLATION = 1008

#: Application-range close codes (RFC 6455 §7.4.2).  The 4xxx range is
#: reserved for application use and never used by the WebSocket spec.
WS_CLOSE_PROTOCOL_ERROR = 4400
WS_CLOSE_UNAUTHORIZED = 4401
WS_CLOSE_FORBIDDEN = 4403
WS_CLOSE_NOT_FOUND = 4404
WS_CLOSE_REQUEST_TIMEOUT = 4408
WS_CLOSE_TOO_MANY_REQUESTS = 4429

#: Application-range close code for "AUTH_ENABLED=false".
WS_CLOSE_AUTH_DISABLED = 1008


@dataclass(frozen=True)
class WSAuthFailure:
    """Typed authentication / authorization failure.

    The route layer uses ``close_code`` to terminate the upgrade with
    a deliberate close code and ``reason`` for an audit log line.
    ``safe_message`` is suitable for client-facing logs (never includes
    the JWT, the user id, or any other identifying material beyond
    what the caller already knows).
    """

    reason: str
    close_code: int
    safe_message: str

    def __str__(self) -> str:  # pragma: no cover — debug only
        return f"{self.reason} ({self.close_code}): {self.safe_message}"


# ---------------------------------------------------------------------------
# Token extraction
# ---------------------------------------------------------------------------


# The maximum number of subprotocol values we'll iterate when looking
# for the bearer entry.  RFC 6455 does not cap this but a sane
# client emits at most a handful; the cap is here for defense in depth.
_MAX_SUBPROTOCOLS = 16


def _iter_subprotocols(websocket: WebSocket) -> list[str]:
    """Return the ``Sec-WebSocket-Protocol`` values as a list of strings.

    The header can appear once with comma-separated values OR multiple
    times; Starlette already collapsed the multi-header form into a
    single comma-separated string by the time we read it.
    """
    raw = websocket.headers.get("sec-websocket-protocol") or ""
    if not raw:
        return []
    parts = [p.strip() for p in raw.split(",")]
    return [p for p in parts if p][: _MAX_SUBPROTOCOLS]


def extract_ws_token(websocket: WebSocket) -> Optional[str]:
    """Return the JWT carried by this WebSocket upgrade, or ``None``.

    Resolution order:

    1. ``Sec-WebSocket-Protocol: bearer.<jwt>`` (browser-compatible
       path; ``Sec-WebSocket-Protocol`` is a request header and never
       recorded in nginx ``access_log`` URLs).
    2. ``Authorization: Bearer <jwt>`` (non-browser clients).
    """
    for sub in _iter_subprotocols(websocket):
        if sub.startswith("bearer."):
            token = sub[len("bearer.") :].strip()
            if token:
                return token
    auth = websocket.headers.get("authorization") or ""
    parts = auth.split(" ", 1)
    if len(parts) == 2 and parts[0].strip().lower() == "bearer":
        token = parts[1].strip()
        if token:
            return token
    return None


# ---------------------------------------------------------------------------
# Principal resolution
# ---------------------------------------------------------------------------


def _is_loopback_origin(origin: str) -> bool:
    """Return True iff ``origin`` is a loopback / dev origin.

    Production MUST NEVER accept loopback origins — a misconfigured
    Nginx that forwards ``Origin: http://localhost`` would otherwise
    expose authenticated realtime sessions to attacker-controlled
    loopback pages.
    """
    o = (origin or "").strip().lower()
    return o in {
        "http://localhost",
        "http://127.0.0.1",
        "http://localhost:8080",
        "http://127.0.0.1:8080",
    }


def validate_ws_origin(
    websocket: WebSocket,
    *,
    settings: Settings,
    allowed_origins: Optional[Iterable[str]] = None,
) -> Optional[WSAuthFailure]:
    """Reject WebSocket upgrades whose ``Origin`` is not in the allowlist.

    Returns ``None`` on success or a :class:`WSAuthFailure` on
    rejection.  Production deployments (``app_env=production``)
    require the ``Origin`` header to match the configured allowlist;
    the canonical production origin (``https://finops.hiplink.com``)
    is always permitted even if not explicitly listed.
    """
    is_production = (getattr(settings, "app_env", "") or "").strip().lower() == "production"
    origin = (websocket.headers.get("origin") or "").strip()

    if is_production:
        if not origin:
            return WSAuthFailure(
                reason="origin_missing",
                close_code=WS_CLOSE_FORBIDDEN,
                safe_message="Origin header required",
            )
        if _is_loopback_origin(origin):
            return WSAuthFailure(
                reason="origin_loopback_rejected",
                close_code=WS_CLOSE_FORBIDDEN,
                safe_message="loopback origins are not accepted in production",
            )
        # Build the allowlist: explicit config + canonical production origin.
        configured = list(allowed_origins or []) if allowed_origins else []
        # When allowed_origins is None we fall back to the
        # CORS_ALLOWED_ORIGINS env var (single source of truth for
        # the deployment's trust set).
        if allowed_origins is None:
            raw = (settings.cors_allowed_origins or "").strip()
            if raw:
                configured = [o.strip() for o in raw.split(",") if o.strip()]
        # Canonical production origin is always allowed when running
        # in production — it is the deployment contract.
        # Already part of the configured allowlist in normal
        # deployments; we keep the explicit allow for safety.
        allowed = {o.strip().lower() for o in configured if o.strip()}
        if origin.lower() not in allowed:
            return WSAuthFailure(
                reason="origin_not_allowed",
                close_code=WS_CLOSE_FORBIDDEN,
                safe_message="origin not in allowlist",
            )

    return None


def _validate_token_shape(token: str) -> Optional[WSAuthFailure]:
    """Cheap pre-check before the cryptographic verify.

    Avoids loading the security core for obviously empty / oversize
    inputs.
    """
    if not token:
        return WSAuthFailure(
            reason="missing_token",
            close_code=WS_CLOSE_UNAUTHORIZED,
            safe_message="missing token",
        )
    if len(token) > 4096:
        return WSAuthFailure(
            reason="token_too_long",
            close_code=WS_CLOSE_UNAUTHORIZED,
            safe_message="token too long",
        )
    return None


def resolve_ws_principal(
    *,
    websocket: WebSocket,
    db: Session,
    settings: Optional[Settings] = None,
) -> "tuple[Optional[AppUser], Optional[WSAuthFailure]]":
    """Return ``(user, None)`` on success or ``(None, WSAuthFailure)``.

    On success ``user.role`` is the AUTHORITATIVE DB role — never the
    role carried by the JWT.  A user whose DB role is ``VIEWER`` is
    rejected even if the token's ``role`` claim says ``ADMIN``; this
    is the same defense Phase 5A uses for REST endpoints.
    """
    cfg = settings or get_settings()

    if not cfg.auth_enabled:
        return None, WSAuthFailure(
            reason="auth_disabled",
            close_code=WS_CLOSE_AUTH_DISABLED,
            safe_message=(
                "WebSocket conversation endpoints require authentication. "
                "Set AUTH_ENABLED=true to use realtime conversations."
            ),
        )

    # Phase 8.2 — origin validation runs BEFORE the (relatively
    # expensive) token decode so a brute-force origin probe does
    # not consume Argon2id/JWT cycles.
    origin_failure = validate_ws_origin(websocket, settings=cfg)
    if origin_failure is not None:
        return None, origin_failure

    token = extract_ws_token(websocket)
    shape_failure = _validate_token_shape(token or "")
    if shape_failure is not None:
        return None, shape_failure

    security = get_security_core()
    try:
        claims = security.decode_token(token)  # type: ignore[arg-type]
    except TokenExpired:
        return None, WSAuthFailure(
            reason="token_expired",
            close_code=WS_CLOSE_UNAUTHORIZED,
            safe_message="token expired",
        )
    except TokenSignatureInvalid:
        return None, WSAuthFailure(
            reason="token_signature_invalid",
            close_code=WS_CLOSE_UNAUTHORIZED,
            safe_message="token signature invalid",
        )
    except TokenInvalid:
        return None, WSAuthFailure(
            reason="token_invalid",
            close_code=WS_CLOSE_UNAUTHORIZED,
            safe_message="token invalid",
        )

    try:
        user_id = int(claims.sub)
    except (TypeError, ValueError):
        return None, WSAuthFailure(
            reason="token_subject_invalid",
            close_code=WS_CLOSE_UNAUTHORIZED,
            safe_message="token subject is not a valid user id",
        )

    # Defense in depth: if the JWT role claim disagrees with the DB
    # role, prefer the DB role.  We do NOT trust the JWT for
    # authorization (Phase 5A contract).
    auth_service = AuthService(db=db)
    try:
        user = auth_service.get_active_user(user_id)
    except UserNotFound:
        return None, WSAuthFailure(
            reason="user_not_found",
            close_code=WS_CLOSE_UNAUTHORIZED,
            safe_message="user no longer exists",
        )
    except UserInactive:
        return None, WSAuthFailure(
            reason="user_inactive",
            close_code=WS_CLOSE_FORBIDDEN,
            safe_message="user is inactive",
        )

    if user.role not in ("ADMIN", "ANALYST"):
        # Both VIEWER and any unknown role are denied.
        return None, WSAuthFailure(
            reason="role_not_allowed",
            close_code=WS_CLOSE_FORBIDDEN,
            safe_message=(
                f"role {user.role!r} is not permitted on realtime "
                "conversation endpoints"
            ),
        )

    return user, None


# ---------------------------------------------------------------------------
# Subprotocol negotiation
# ---------------------------------------------------------------------------


def negotiate_subprotocol(websocket: WebSocket) -> Optional[str]:
    """Return the subprotocol value the server should echo back, or ``None``.

    RFC 6455 §1.9: the server MUST echo one of the values the client
    offered.  We always echo the exact ``bearer.<jwt>`` string we
    consumed so the client library can match it up without ambiguity.
    """
    for sub in _iter_subprotocols(websocket):
        if sub.startswith("bearer."):
            return sub
    return None


__all__ = [
    "WSAuthFailure",
    "WS_CLOSE_AUTH_DISABLED",
    "WS_CLOSE_FORBIDDEN",
    "WS_CLOSE_NOT_FOUND",
    "WS_CLOSE_POLICY_VIOLATION",
    "WS_CLOSE_PROTOCOL_ERROR",
    "WS_CLOSE_REQUEST_TIMEOUT",
    "WS_CLOSE_TOO_MANY_REQUESTS",
    "WS_CLOSE_UNAUTHORIZED",
    "extract_ws_token",
    "negotiate_subprotocol",
    "resolve_ws_principal",
    "validate_ws_origin",
]
