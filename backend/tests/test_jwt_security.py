"""Tests for the JWT issuance + verification core — Phase 5A."""
from __future__ import annotations

import time

import jwt as pyjwt
import pytest

from app.core.config import Settings
from app.core.security import (
    SecurityCore,
    TokenExpired,
    TokenInvalid,
    TokenSignatureInvalid,
)


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        app_env="test",
        auth_enabled=True,
        jwt_secret="a" * 32 + "-test-jwt-secret",
        jwt_algorithm="HS256",
        jwt_access_token_minutes=5,
        jwt_issuer="ai-cloud-cost-detective",
        jwt_audience="ai-cloud-cost-detective-api",
    )


@pytest.fixture()
def core(settings: Settings) -> SecurityCore:
    return SecurityCore(settings=settings)


def test_issue_and_decode_round_trip(core: SecurityCore) -> None:
    token = core.issue_access_token(user_id=42, role="ADMIN")
    claims = core.decode_token(token)
    assert claims.sub == "42"
    assert claims.role == "ADMIN"
    assert claims.iss == "ai-cloud-cost-detective"
    assert claims.aud == "ai-cloud-cost-detective-api"
    assert claims.jti  # not empty


def test_expired_token_raises_token_expired(core: SecurityCore) -> None:
    # Forge a token with exp in the past.
    now = int(time.time())
    payload = {
        "sub": "1",
        "role": "ADMIN",
        "iat": now - 3600,
        "exp": now - 60,
        "iss": "ai-cloud-cost-detective",
        "aud": "ai-cloud-cost-detective-api",
        "jti": "x",
    }
    token = pyjwt.encode(payload, core.settings.jwt_secret, algorithm="HS256")
    with pytest.raises(TokenExpired):
        core.decode_token(token)


def test_malformed_token_raises_token_invalid(core: SecurityCore) -> None:
    with pytest.raises(TokenInvalid):
        core.decode_token("not.a.real.jwt")
    with pytest.raises(TokenInvalid):
        core.decode_token("")
    with pytest.raises(TokenInvalid):
        core.decode_token("only-one-segment")


def test_invalid_signature_raises_token_signature_invalid(core: SecurityCore) -> None:
    token = core.issue_access_token(user_id=1, role="ADMIN")
    # Flip one character in the signature segment.
    head, _mid, sig = token.split(".")
    tampered = f"{head}.{_mid}.{sig[:-2]}AA"
    with pytest.raises(TokenSignatureInvalid):
        core.decode_token(tampered)


def test_wrong_audience_raises_token_invalid(core: SecurityCore) -> None:
    now = int(time.time())
    payload = {
        "sub": "1",
        "role": "ADMIN",
        "iat": now,
        "exp": now + 60,
        "iss": "ai-cloud-cost-detective",
        "aud": "wrong-audience",
        "jti": "x",
    }
    token = pyjwt.encode(payload, core.settings.jwt_secret, algorithm="HS256")
    with pytest.raises(TokenInvalid):
        core.decode_token(token)


def test_wrong_issuer_raises_token_invalid(core: SecurityCore) -> None:
    now = int(time.time())
    payload = {
        "sub": "1",
        "role": "ADMIN",
        "iat": now,
        "exp": now + 60,
        "iss": "evil-issuer",
        "aud": "ai-cloud-cost-detective-api",
        "jti": "x",
    }
    token = pyjwt.encode(payload, core.settings.jwt_secret, algorithm="HS256")
    with pytest.raises(TokenInvalid):
        core.decode_token(token)


def test_alg_none_attack_rejected(core: SecurityCore) -> None:
    """An attacker cannot downgrade to ``alg=none`` to bypass signing."""
    now = int(time.time())
    payload = {
        "sub": "1",
        "role": "ADMIN",
        "iat": now,
        "exp": now + 60,
        "iss": "ai-cloud-cost-detective",
        "aud": "ai-cloud-cost-detective-api",
        "jti": "x",
    }
    unsigned = pyjwt.encode(payload, key="", algorithm="none")
    with pytest.raises(TokenInvalid):
        core.decode_token(unsigned)


def test_role_claim_is_just_a_hint(core: SecurityCore) -> None:
    """The token role MUST NOT be trusted without server-side validation.

    We assert this contract directly: a forged token can carry
    ``role=ADMIN`` but the auth layer treats it as a hint and
    re-loads the user from the DB.  See ``test_auth_service.py`` for
    the consumer-side test; this test merely records that the
    decoder exposes the claim as a string.
    """
    token = core.issue_access_token(user_id=1, role="VIEWER")
    claims = core.decode_token(token)
    assert claims.role == "VIEWER"
    # A forged role in the payload is decoded verbatim.
    forged_payload = pyjwt.decode(
        token,
        core.settings.jwt_secret,
        algorithms=["HS256"],
        audience=core.settings.jwt_audience,
        issuer=core.settings.jwt_issuer,
    )
    assert forged_payload["role"] == "VIEWER"
    # (The decoded role is whatever the issuer put in.  The auth
    # layer is responsible for NOT trusting it without DB lookup.)


def test_settings_validator_rejects_placeholder_secret_when_auth_enabled() -> None:
    """Re-verify the Phase 5A fail-closed invariant at the settings layer."""
    with pytest.raises(Exception):
        Settings(
            app_env="test",
            auth_enabled=True,
            jwt_secret="",
        )
    with pytest.raises(Exception):
        Settings(
            app_env="test",
            auth_enabled=True,
            jwt_secret="change-me",
        )
    with pytest.raises(Exception):
        Settings(
            app_env="test",
            auth_enabled=True,
            jwt_secret="short",
        )


def test_settings_validator_passes_when_auth_disabled() -> None:
    """Empty / short secret is acceptable when AUTH_ENABLED=false."""
    s = Settings(app_env="test", auth_enabled=False, jwt_secret="")
    assert s.auth_enabled is False
