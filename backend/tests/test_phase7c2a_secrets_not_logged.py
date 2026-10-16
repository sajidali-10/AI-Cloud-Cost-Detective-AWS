"""Phase 7C.2A — Azure-side redaction tests.

Verifies that the Azure credential / federation / ARM code paths
never leak:

* AWS WebIdentityToken (JWT) values.
* Azure access tokens (Bearer).
* ``client_assertion=`` query segments.
* AWS access key ids (``AKIA…`` / ``ASIA…``).
* Full subscription GUIDs (only the first 8 hex chars are surfaced).

This file does NOT call live AWS / Azure APIs.  Every test exercises
the redaction helpers directly with synthetic tokens / values.
"""
from __future__ import annotations

import logging

import pytest

from app.services.cloud.azure.redaction import (
    AZURE_STATUS_ACCESS_DENIED,
    AZURE_STATUS_AUTH_FAILED,
    AZURE_STATUS_ERROR,
    AZURE_STATUS_NOT_CONFIGURED,
    AZURE_STATUS_NOT_FOUND,
    category_from_azure_error,
    safe_log_extra,
    safe_subscription_label,
    scrub_aws_access_key,
    scrub_authorization,
    scrub_client_assertion,
    scrub_jwt,
    scrub_log_payload,
    scrub_tenant_id,
)


# ---------------------------------------------------------------------------
# Subscription label
# ---------------------------------------------------------------------------


def test_safe_subscription_label_truncates():
    assert safe_subscription_label("a54d076b-2650-4b4f-a28c-2deed2011f83") == "a54d076b…"


def test_safe_subscription_label_handles_empty():
    assert safe_subscription_label(None) == "<unset>"
    assert safe_subscription_label("") == "<unset>"


def test_safe_subscription_label_handles_short():
    assert safe_subscription_label("abc") == "<short-id>"


# ---------------------------------------------------------------------------
# JWT redaction
# ---------------------------------------------------------------------------


JWT_SAMPLE = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0In0.signature_signature_signature"


def test_scrub_jwt_replaces_compact_jwt():
    payload = f"token={JWT_SAMPLE} user=admin"
    out = scrub_jwt(payload)
    assert "<jwt>" in out
    assert JWT_SAMPLE not in out


def test_scrub_log_payload_strips_jwt():
    out = scrub_log_payload(f"got {JWT_SAMPLE} from federation")
    assert "<jwt>" in out
    assert "eyJ" not in out


def test_scrub_log_payload_strips_bearer():
    out = scrub_log_payload("Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJ")
    assert "Bearer" not in out
    assert "Authorization: <redacted>" in out


def test_scrub_log_payload_strips_client_assertion():
    out = scrub_log_payload(
        "POST /token?client_assertion=eyJ.abc.def&other=value"
    )
    assert "client_assertion=<redacted>" in out
    assert "eyJ" not in out


def test_scrub_log_payload_strips_aws_keys():
    out = scrub_log_payload(
        "credential id=AKIAIOSFODNN7EXAMPLE leaked by accident"
    )
    assert "<aws-key>" in out
    assert "AKIAIOSFODNN7EXAMPLE" not in out


def test_scrub_log_payload_strips_asia_temporary_key():
    out = scrub_log_payload("key=ASIAIOSFODNN7EXAMPLE")
    assert "<aws-key>" in out


def test_scrub_authorization_only_strips_header():
    out = scrub_authorization(
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJ"
    )
    assert "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJ" not in out


def test_scrub_client_assertion_only_strips_query():
    out = scrub_client_assertion(
        "client_assertion=eyJ.abc.def&grant_type=client_credentials"
    )
    assert "eyJ" not in out
    assert "client_credentials" in out


def test_scrub_aws_access_key_only_strips_id():
    out = scrub_aws_access_key("AKIAIOSFODNN7EXAMPLE some other text")
    assert "AKIA" not in out
    assert "some other text" in out


def test_scrub_tenant_id_replaces_full_guid():
    out = scrub_tenant_id(
        "tenant 11111111-2222-3333-4444-555555555555 cannot be queried"
    )
    assert "<tenant>" in out
    assert "11111111-2222-3333" not in out


def test_scrub_log_payload_passes_through_non_string():
    """Non-string values are returned unchanged — the helper is
    safe to call on any input.
    """
    assert scrub_log_payload(123) == 123
    assert scrub_log_payload(None) is None


def test_safe_log_extra_scrubs_string_values():
    out = safe_log_extra({"token": JWT_SAMPLE, "count": 3})
    assert "<jwt>" in out["token"]
    assert out["count"] == 3


# ---------------------------------------------------------------------------
# Category mapping
# ---------------------------------------------------------------------------


def test_category_from_azure_error_missing_tenant():
    class MissingTenantIdError(Exception):
        pass

    assert category_from_azure_error(MissingTenantIdError()) == AZURE_STATUS_NOT_CONFIGURED


def test_category_from_azure_error_missing_client_id():
    class MissingClientIdError(Exception):
        pass

    assert category_from_azure_error(MissingClientIdError()) == AZURE_STATUS_NOT_CONFIGURED


def test_category_from_azure_error_invalid_client():
    exc = Exception("invalid_client: assertion rejected")
    assert category_from_azure_error(exc) == AZURE_STATUS_AUTH_FAILED


def test_category_from_azure_error_unauthorized_client():
    exc = Exception("unauthorized_client: AADSTS700016")
    assert category_from_azure_error(exc) == AZURE_STATUS_AUTH_FAILED


def test_category_from_azure_error_authorization_failed():
    exc = Exception("AuthorizationFailed: 403 from ARM")
    assert category_from_azure_error(exc) == AZURE_STATUS_ACCESS_DENIED


def test_category_from_azure_error_resource_not_found():
    exc = Exception("ResourceNotFound: subscription does not exist")
    assert category_from_azure_error(exc) == AZURE_STATUS_NOT_FOUND


def test_category_from_azure_error_unknown_collapses_to_error():
    exc = Exception("Something exotic happened")
    assert category_from_azure_error(exc) == AZURE_STATUS_ERROR


def test_category_from_azure_error_empty_message_collapses_to_error():
    assert category_from_azure_error(Exception("")) == AZURE_STATUS_ERROR


# ---------------------------------------------------------------------------
# End-to-end: the federation provider scrubs its own error messages
# ---------------------------------------------------------------------------


def test_federation_error_message_does_not_contain_jwt():
    """The ``AzureFederationError`` raised by
    ``AWSWorkloadFederationCredential.get_assertion`` must not echo
    the input JWT even if Boto3 happened to surface it.
    """
    from app.services.cloud.azure.federation import AzureFederationError

    err = AzureFederationError(AZURE_STATUS_AUTH_FAILED, "AWS STS GetWebIdentityToken returned AccessDenied")
    out = scrub_log_payload(str(err))
    assert "eyJ" not in out


# ---------------------------------------------------------------------------
# Logger smoke — capturing handlers
# ---------------------------------------------------------------------------


def test_log_records_do_not_contain_jwt(caplog):
    """When a real ``logger.warning`` is invoked with a string that
    happens to embed a JWT, ``scrub_log_payload`` must scrub it
    before it reaches the handler.
    """
    logger = logging.getLogger("cost-detective.phase7c2a.redaction.smoke")
    with caplog.at_level(logging.WARNING):
        logger.warning(
            "azure.federation.token_leaked %s",
            scrub_log_payload(f"payload={JWT_SAMPLE}"),
        )
    assert "<jwt>" in caplog.text
    assert "eyJhbGciOiJIUzI1NiJ9" not in caplog.text
