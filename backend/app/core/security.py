"""JWT issuance + verification — Phase 5A.

Stateless bearer-token authentication for the application backend.
Tokens are signed with a symmetric key (``JWT_SECRET``) supplied via
the environment; verification rejects tokens whose issuer or
audience does not match what the server expects.

Design notes:

* No refresh-token DB.  Phase 5A ships short-lived access tokens
  only; refresh tokens are explicitly deferred.
* The signing key is never logged.  The ``SecurityCore`` class
  reads it from :func:`app.core.config.get_settings` and caches it
  in memory; the secret value is not exposed by any public
  attribute.
* Three typed exceptions are surfaced by ``decode_token``:
    - :class:`TokenExpired`
    - :class:`TokenInvalid`            (malformed / wrong issuer/audience)
    - :class:`TokenSignatureInvalid`   (bad signature)
  The auth layer collapses them to a generic ``InvalidToken`` so
  callers never learn which check failed.
* Claims are intentionally minimal: ``sub`` (user id), ``role``
  (best-effort hint), ``iat``, ``exp``, ``iss``, ``aud``, ``jti``.
  The role claim is treated as a HINT only; the auth layer
  re-loads the user from the database and uses the DB role for
  authorization.  This closes the "user tampers with role claim"
  hole described in the Phase 5A spec.
"""
from __future__ import annotations

import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

import jwt

from app.core.config import Settings, get_settings

logger = logging.getLogger("cost-detective-backend.security")


class TokenError(Exception):
    """Base class for all token failures.  Never raised directly."""


class TokenExpired(TokenError):
    """Raised when the token's ``exp`` is in the past."""


class TokenInvalid(TokenError):
    """Raised when the token is malformed, wrong issuer, or wrong audience."""


class TokenSignatureInvalid(TokenError):
    """Raised when signature verification fails."""


@dataclass(frozen=True)
class TokenClaims:
    """Decoded JWT claims.

    ``sub`` is the user id (string-encoded so JWT serialization is
    stable across large ids).  ``role`` is the role carried in the
    token; consumers MUST re-validate it against the database row
    before using it for authorization.
    """

    sub: str
    role: str
    jti: str
    iat: datetime
    exp: datetime
    iss: str
    aud: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sub": self.sub,
            "role": self.role,
            "jti": self.jti,
            "iat": int(self.iat.timestamp()),
            "exp": int(self.exp.timestamp()),
            "iss": self.iss,
            "aud": self.aud,
        }


@dataclass(frozen=True)
class SecurityCore:
    """Stateless JWT issuer + verifier.

    A single instance is shared process-wide.  Tests construct fresh
    instances with explicit settings to keep them isolated from
    environment variables.
    """

    settings: Settings

    # ----- issuance -----

    def issue_access_token(
        self,
        *,
        user_id: int,
        role: str,
        now: datetime | None = None,
    ) -> str:
        """Issue a signed access token for ``user_id``.

        ``role`` is included as a hint only; the auth layer
        re-validates against the DB.
        """
        iat_dt = now or datetime.now(timezone.utc)
        exp_dt = iat_dt + timedelta(minutes=self.settings.jwt_access_token_minutes)
        claims = {
            "sub": str(user_id),
            "role": role,
            "iat": int(iat_dt.timestamp()),
            "exp": int(exp_dt.timestamp()),
            "iss": self.settings.jwt_issuer,
            "aud": self.settings.jwt_audience,
            "jti": secrets.token_hex(16),
        }
        # jwt.encode returns str on PyJWT 2.x.
        token = jwt.encode(
            claims,
            self.settings.jwt_secret,
            algorithm=self.settings.jwt_algorithm,
        )
        return token

    # ----- verification -----

    def decode_token(self, token: str) -> TokenClaims:
        """Decode and verify ``token``.

        Raises:
            TokenExpired: ``exp`` is in the past.
            TokenSignatureInvalid: signature does not verify.
            TokenInvalid: malformed / wrong issuer / wrong audience /
                unexpected algorithm / missing required claims.
        """
        if not isinstance(token, str) or not token:
            raise TokenInvalid("token is empty")

        try:
            # We intentionally constrain ``algorithms`` so an attacker
            # cannot downgrade the algorithm by sending ``alg=none``.
            payload = jwt.decode(
                token,
                self.settings.jwt_secret,
                algorithms=[self.settings.jwt_algorithm],
                audience=self.settings.jwt_audience,
                issuer=self.settings.jwt_issuer,
                options={
                    "require": ["exp", "iat", "sub", "iss", "aud"],
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_iat": True,
                    "verify_aud": True,
                    "verify_iss": True,
                },
            )
        except jwt.ExpiredSignatureError as exc:
            raise TokenExpired("token expired") from exc
        except jwt.InvalidSignatureError as exc:
            raise TokenSignatureInvalid("signature invalid") from exc
        except jwt.InvalidTokenError as exc:
            # PyJWT raises this for: InvalidAudienceError,
            # InvalidIssuerError, MissingRequiredClaimError,
            # DecodeError, ImmatureSignatureError, ...
            raise TokenInvalid("token invalid") from exc

        try:
            sub = str(payload["sub"])
            role = str(payload.get("role", ""))
            jti = str(payload.get("jti", uuid.uuid4().hex))
            iat = datetime.fromtimestamp(int(payload["iat"]), tz=timezone.utc)
            exp = datetime.fromtimestamp(int(payload["exp"]), tz=timezone.utc)
        except (KeyError, TypeError, ValueError) as exc:
            raise TokenInvalid("token claims malformed") from exc

        return TokenClaims(
            sub=sub,
            role=role,
            jti=jti,
            iat=iat,
            exp=exp,
            iss=str(payload.get("iss", self.settings.jwt_issuer)),
            aud=str(payload.get("aud", self.settings.jwt_audience)),
        )


# ----- module-level singleton wired to the current Settings -----


_security_core_singleton: SecurityCore | None = None


def get_security_core() -> SecurityCore:
    """Return the process-wide :class:`SecurityCore`.

    The settings object is re-read on every call so changes to
    environment variables in tests take effect immediately.
    """
    global _security_core_singleton
    if _security_core_singleton is None:
        _security_core_singleton = SecurityCore(get_settings())
    return _security_core_singleton


def reset_security_core_for_tests() -> None:
    """Reset the module-level singleton.

    Test fixtures call this after patching settings to ensure the
    next ``get_security_core`` call reads the patched settings.
    """
    global _security_core_singleton
    _security_core_singleton = None


__all__ = [
    "SecurityCore",
    "TokenClaims",
    "TokenError",
    "TokenExpired",
    "TokenInvalid",
    "TokenSignatureInvalid",
    "get_security_core",
    "reset_security_core_for_tests",
]
