"""Phase 9C.1 — Rackspace deterministic optimization engine tests.

Every test in this module is:

* **Deterministic** — no time, randomness, environment, or network.
* **Read-only** — no product write verbs are issued or asserted.
* **Self-contained** — no Rackspace credentials, no Docker, no DB.
* **Provider-isolated** — non-Rackspace resources are silently
  ignored, AWS / Azure behavior is unchanged.

The tests assert the Phase 9C.1 contract:

* detached volumes → ``RS-VOL-001`` review candidate;
* attached volumes → no detached-volume finding;
* ACTIVE servers without metrics → no rightsizing finding;
* non-ACTIVE servers → ``RS-SRV-001`` review candidate;
* ERROR state → review, never automatic delete;
* flavor size alone → no optimization finding;
* empty LB list → zero LB findings;
* empty inventory → ``EMPTY`` status, zero findings;
* same input → same finding IDs and ordering (determinism);
* partial resource inputs → only the valid rules evaluate;
* ``estimated_monthly_savings`` is ``None`` whenever unsupported;
* no API key / token / RAN material in any finding payload.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Dict, List, Optional

import pytest

from app.services.cloud.models import (
    CloudCostSummary,
    CloudProviderKind,
    CloudResource,
)
from app.services.cloud.rackspace.metrics_capability import (
    RightsizingEvidenceStatus,
    probe_metrics_capability_sync,
)
from app.services.cloud.rackspace.optimization import (
    NON_ACTIVE_STATES,
    RackspaceFinding,
    RackspaceFindingConfidence,
    RackspaceFindingSeverity,
    RackspaceOptimizationResult,
    RackspaceOptimizationStatus,
    deterministic_finding_id,
    evaluate_rackspace_optimization,
    rule_rs_lb_001_review,
    rule_rs_srv_001_non_active,
    rule_rs_vol_001_detached,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _vol(
    *,
    volume_id: str,
    region: Optional[str] = "DFW",
    state: str = "available",
    attachment_state: str = "DETACHED",
    size_gb: Optional[int] = 50,
    volume_type: str = "SATA",
    account_id: str = "rack-tenant",
    name: Optional[str] = None,
    created: Optional[str] = None,
    availability_zone: Optional[str] = "DFW1",
) -> CloudResource:
    metadata: Dict[str, Any] = {
        "attachment_state": attachment_state,
        "size_gb": size_gb,
        "volume_type": volume_type,
    }
    if created:
        metadata["created"] = created
    if availability_zone:
        metadata["availability_zone"] = availability_zone
    return CloudResource(
        provider=CloudProviderKind.RACKSPACE,
        account_id=account_id,
        region=region,
        resource_type="VOLUME",
        resource_id=volume_id,
        service="Cloud Block Storage",
        name=name,
        state=state,
        composite_id=f"RACKSPACE|{account_id}|{region}|VOLUME|{volume_id}",
        metadata=metadata,
    )


def _server(
    *,
    server_id: str,
    region: Optional[str] = "DFW",
    state: str = "ACTIVE",
    flavor: str = "performance1-2",
    account_id: str = "rack-tenant",
    name: Optional[str] = None,
    created: Optional[str] = None,
) -> CloudResource:
    metadata: Dict[str, Any] = {
        "flavor_name": flavor,
        "flavor_id": flavor,
        "flavor_ram_mb": 2048,
        "flavor_vcpus": 2,
        "flavor_disk_gb": 40,
    }
    if created:
        metadata["created"] = created
    return CloudResource(
        provider=CloudProviderKind.RACKSPACE,
        account_id=account_id,
        region=region,
        resource_type="SERVER",
        resource_id=server_id,
        service="Cloud Servers",
        name=name,
        state=state,
        composite_id=f"RACKSPACE|{account_id}|{region}|SERVER|{server_id}",
        metadata=metadata,
    )


def _lb(
    *,
    lb_id: str,
    region: Optional[str] = "DFW",
    state: str = "ACTIVE",
    account_id: str = "rack-tenant",
    name: Optional[str] = None,
) -> CloudResource:
    return CloudResource(
        provider=CloudProviderKind.RACKSPACE,
        account_id=account_id,
        region=region,
        resource_type="LOAD_BALANCER",
        resource_id=lb_id,
        service="Cloud Load Balancers",
        name=name,
        state=state,
        composite_id=f"RACKSPACE|{account_id}|{region}|LOAD_BALANCER|{lb_id}",
        metadata={"protocol": "HTTP", "port": 80},
    )


def _aws_volume(volume_id: str) -> CloudResource:
    return CloudResource(
        provider=CloudProviderKind.AWS,
        account_id="111111111111",
        region="us-east-1",
        resource_type="VOLUME",
        resource_id=volume_id,
        service="EBS",
        name=None,
        state="available",
        composite_id=f"AWS|111111111111|us-east-1|VOLUME|{volume_id}",
        metadata={"attachment_state": "DETACHED"},
    )


# ---------------------------------------------------------------------------
# Deterministic ID generation
# ---------------------------------------------------------------------------


def test_deterministic_finding_id_is_stable():
    a = deterministic_finding_id(
        provider="RACKSPACE",
        account_id="rack-tenant",
        rule_id="RS-VOL-001",
        region="DFW",
        resource_type="VOLUME",
        resource_id="vol-1",
    )
    b = deterministic_finding_id(
        provider="RACKSPACE",
        account_id="rack-tenant",
        rule_id="RS-VOL-001",
        region="DFW",
        resource_type="VOLUME",
        resource_id="vol-1",
    )
    assert a == b
    assert a.startswith("rs-det-rs-vol-001-")
    assert len(a) == len("rs-det-rs-vol-001-") + 12


def test_deterministic_finding_id_changes_with_input():
    base = dict(
        provider="RACKSPACE",
        account_id="rack-tenant",
        rule_id="RS-VOL-001",
        region="DFW",
        resource_type="VOLUME",
        resource_id="vol-1",
    )
    ids = {
        deterministic_finding_id(**base),
        deterministic_finding_id(**{**base, "rule_id": "RS-SRV-001"}),
        deterministic_finding_id(**{**base, "region": "ORD"}),
        deterministic_finding_id(**{**base, "resource_id": "vol-2"}),
        deterministic_finding_id(**{**base, "account_id": "other"}),
    }
    assert len(ids) == 5


# ---------------------------------------------------------------------------
# Rule RS-VOL-001
# ---------------------------------------------------------------------------


def test_detached_volume_emits_review_finding():
    findings = rule_rs_vol_001_detached([_vol(volume_id="vol-1")])
    assert len(findings) == 1
    f = findings[0]
    assert f.rule_id == "RS-VOL-001"
    assert f.resource_id == "vol-1"
    assert f.estimated_monthly_savings is None
    assert f.savings_basis == "UNAVAILABLE"
    # Severity is conservative MEDIUM for default 50 GB.
    assert f.severity == RackspaceFindingSeverity.MEDIUM
    assert f.confidence == RackspaceFindingConfidence.MEDIUM
    # No deletion language.
    text = (f.title + " " + f.summary + " " + f.recommended_action).lower()
    assert "delete" not in text or "owner validation" in text
    # Evidence contains size, attachment state, status, region.
    evidence_kinds = {e["kind"] for e in f.evidence}
    assert {"volume_status", "attachment_state", "size_gb", "region"} <= evidence_kinds


def test_attached_volume_produces_no_finding():
    findings = rule_rs_vol_001_detached(
        [_vol(volume_id="vol-1", attachment_state="ATTACHED", state="in-use")]
    )
    assert findings == []


def test_volume_with_attachment_state_detached_but_state_in_use_no_finding():
    """Defense in depth — if the API status contradicts DETACHED, skip."""
    findings = rule_rs_vol_001_detached(
        [_vol(volume_id="vol-1", attachment_state="DETACHED", state="in-use")]
    )
    assert findings == []


def test_detached_volume_severity_escalates_for_large_volumes():
    big = rule_rs_vol_001_detached([_vol(volume_id="big", size_gb=500)])
    assert big[0].severity == RackspaceFindingSeverity.HIGH
    small = rule_rs_vol_001_detached([_vol(volume_id="small", size_gb=80)])
    assert small[0].severity == RackspaceFindingSeverity.MEDIUM


def test_detached_volume_boundary_size_100_is_medium():
    """size_gb == 100 is the inclusive upper boundary for MEDIUM."""
    f = rule_rs_vol_001_detached([_vol(volume_id="boundary-100", size_gb=100)])[0]
    assert f.severity == RackspaceFindingSeverity.MEDIUM


def test_detached_volume_boundary_size_101_is_high():
    """size_gb == 101 crosses the > 100 threshold into HIGH."""
    f = rule_rs_vol_001_detached([_vol(volume_id="boundary-101", size_gb=101)])[0]
    assert f.severity == RackspaceFindingSeverity.HIGH


def test_detached_volume_size_region_created_preserved_in_evidence():
    vol = _vol(
        volume_id="vol-1",
        region="ORD",
        size_gb=250,
        volume_type="SSD",
        created="2024-04-01T00:00:00Z",
    )
    f = rule_rs_vol_001_detached([vol])[0]
    evidence_kinds = {e["kind"] for e in f.evidence}
    assert "region" in evidence_kinds
    assert "volume_type" in evidence_kinds
    assert "created_at" in evidence_kinds
    size = next(e for e in f.evidence if e["kind"] == "size_gb")
    assert size["value"] == 250
    region = next(e for e in f.evidence if e["kind"] == "region")
    assert region["value"] == "ORD"
    created = next(e for e in f.evidence if e["kind"] == "created_at")
    assert created["value"] == "2024-04-01T00:00:00Z"


def test_detached_volume_limitations_make_age_vs_duration_distinction():
    f = rule_rs_vol_001_detached([_vol(volume_id="vol-1", created="2024-04-01T00:00:00Z")])[0]
    text = " ".join(f.limitations).lower()
    assert "detached-now does not imply detached-for-n-days" in text
    assert "resource age" in text or "resource-level price" in text


# ---------------------------------------------------------------------------
# Rule RS-SRV-001
# ---------------------------------------------------------------------------


def test_active_server_without_metrics_produces_no_finding():
    findings = rule_rs_srv_001_non_active([_server(server_id="srv-1", state="ACTIVE")])
    assert findings == []


def test_flavour_size_alone_does_not_trigger_optimization():
    findings = rule_rs_srv_001_non_active(
        [_server(server_id="srv-1", state="ACTIVE", flavor="performance1-15")]
    )
    assert findings == []


@pytest.mark.parametrize("state", ["SHUTOFF", "SUSPENDED", "PAUSED"])
def test_non_active_states_emit_review_finding(state):
    findings = rule_rs_srv_001_non_active([_server(server_id="srv-1", state=state)])
    assert len(findings) == 1
    f = findings[0]
    assert f.rule_id == "RS-SRV-001"
    assert f.estimated_monthly_savings is None
    # Non-ACTIVE states should never carry delete language.
    assert "delete" not in f.recommended_action.lower()
    assert "review" in f.recommended_action.lower()


def test_error_state_does_not_become_delete_recommendation():
    f = rule_rs_srv_001_non_active([_server(server_id="srv-err", state="ERROR")])[0]
    text = (f.title + " " + f.summary + " " + f.recommended_action).lower()
    assert "delete" not in text
    assert "remediation" in " ".join(f.limitations).lower()


def test_server_state_enum_matches_documented_set():
    assert NON_ACTIVE_STATES == ("SHUTOFF", "SUSPENDED", "PAUSED", "ERROR")


# ---------------------------------------------------------------------------
# Rule RS-LB-001
# ---------------------------------------------------------------------------


def test_empty_lb_list_produces_no_finding():
    assert rule_rs_lb_001_review([]) == []


def test_active_lb_produces_no_finding():
    assert rule_rs_lb_001_review([_lb(lb_id="lb-1", state="ACTIVE")]) == []


def test_deleted_lb_is_the_only_signal_for_phase_9c1():
    findings = rule_rs_lb_001_review([_lb(lb_id="lb-1", state="DELETED")])
    assert len(findings) == 1
    assert findings[0].rule_id == "RS-LB-001"


# ---------------------------------------------------------------------------
# Engine orchestration
# ---------------------------------------------------------------------------


def _stable_eval(resources: List[CloudResource], **kwargs: Any) -> RackspaceOptimizationResult:
    return evaluate_rackspace_optimization(
        resources,
        evaluated_at_iso="2025-01-01T00:00:00+00:00",
        **kwargs,
    )


def test_engine_empty_resources_zero_findings_success_status():
    result = _stable_eval([])
    assert result.findings_total == 0
    assert result.findings == ()
    assert result.status == RackspaceOptimizationStatus.EMPTY
    assert result.resources_evaluated == 0
    assert result.estimated_savings_total is None
    assert result.estimated_savings_known_count == 0
    assert result.estimated_savings_unknown_count == 0


def test_engine_summary_counts_reconcile():
    resources = [
        _vol(volume_id="v1"),
        _vol(volume_id="v2", size_gb=300),  # -> HIGH severity
        _vol(volume_id="v3", attachment_state="ATTACHED", state="in-use"),
        _server(server_id="s1", state="SHUTOFF"),
        _server(server_id="s2", state="ACTIVE"),
    ]
    result = _stable_eval(resources)
    assert result.findings_total == len(result.findings)
    by_rule = result.findings_by_rule
    assert by_rule.get("RS-VOL-001") == 2
    assert by_rule.get("RS-SRV-001") == 1
    by_type = result.findings_by_resource_type
    assert by_type.get("VOLUME") == 2
    assert by_type.get("SERVER") == 1
    by_sev = result.findings_by_severity
    # v2 (300 GB) -> HIGH; v1 (50 GB) -> MEDIUM; s1 -> LOW.
    assert by_sev.get("HIGH") == 1
    assert by_sev.get("MEDIUM") == 1
    assert by_sev.get("LOW") == 1


def test_engine_unknown_savings_is_not_zero():
    result = _stable_eval([_vol(volume_id="v1"), _server(server_id="s1", state="SHUTOFF")])
    assert result.estimated_savings_total is None
    assert result.estimated_savings_unknown_count == result.findings_total
    assert result.estimated_savings_known_count == 0


def test_engine_determinism_same_input_same_findings():
    resources = [
        _vol(volume_id="v1"),
        _vol(volume_id="v2", size_gb=200),
        _server(server_id="s1", state="PAUSED"),
    ]
    r1 = _stable_eval(resources)
    r2 = _stable_eval(resources)
    ids1 = [f.finding_id for f in r1.findings]
    ids2 = [f.finding_id for f in r2.findings]
    assert ids1 == ids2
    # Order is also byte-stable.
    payloads1 = [f.to_dict() for f in r1.findings]
    payloads2 = [f.to_dict() for f in r2.findings]
    assert payloads1 == payloads2


def test_engine_ordering_severity_desc_then_size_desc():
    resources = [
        _vol(volume_id="v_small", size_gb=20),
        _vol(volume_id="v_large", size_gb=500),  # HIGH
        _vol(volume_id="v_mid", size_gb=200),  # HIGH (200 > 100)
    ]
    result = _stable_eval(resources)
    severities = [f.severity.value for f in result.findings]
    assert severities == ["HIGH", "HIGH", "MEDIUM"]
    # Among HIGH severities, larger size first.
    high_ids = [f.resource_id for f in result.findings if f.severity == RackspaceFindingSeverity.HIGH]
    assert high_ids == ["v_large", "v_mid"]


def test_engine_provider_isolation_ignores_aws_resources():
    aws_vol = _aws_volume("aws-vol-1")  # ATTACHED state but Rackspace rule would still skip non-RACKSPACE
    resources = [
        aws_vol,
        _vol(volume_id="v1"),
    ]
    result = _stable_eval(resources)
    # Only the Rackspace volume is evaluated.
    assert result.resources_evaluated == 1
    assert all(f.provider == "RACKSPACE" for f in result.findings)


def test_engine_uses_metrics_capability_default():
    report = probe_metrics_capability_sync()
    assert report.status == RightsizingEvidenceStatus.DEFERRED
    assert report.note == "RIGHTSIZING_METRICS_DEFERRED"
    result = _stable_eval([_server(server_id="s1", state="ACTIVE")])
    text = " ".join(result.limitations)
    assert "rightsizing_evidence_status=DEFERRED" in text


def test_engine_partial_resources_only_valid_rules_evaluate():
    """A partial resource list still evaluates every rule on what is present."""
    result = _stable_eval([_server(server_id="s1", state="SHUTOFF")])
    assert any(f.rule_id == "RS-SRV-001" for f in result.findings)
    assert not any(f.rule_id == "RS-VOL-001" for f in result.findings)
    assert not any(f.rule_id == "RS-LB-001" for f in result.findings)


def test_engine_cost_context_appears_in_limitations_only():
    cost = CloudCostSummary(
        provider=CloudProviderKind.RACKSPACE,
        account_id="rack-tenant",
        scope="acct:rack-tenant",
        period_start=date(2026, 10, 19),
        period_end=date(2026, 11, 19),
        currency="USD",
        current_total=Decimal("9.00"),
        by_service=[],
        by_region=[],
    )
    result = _stable_eval([_vol(volume_id="v1")], cost_context=cost)
    text = " ".join(result.limitations)
    assert "account-level current billing cycle cost" in text
    # And the cost context is NOT reflected on individual findings.
    for f in result.findings:
        assert f.estimated_monthly_savings is None


def test_engine_rejects_unrelated_states_with_no_finding():
    # Unknown / brand-new states outside the documented set produce no finding.
    result = _stable_eval(
        [
            _server(server_id="s_unknown", state="BUILD"),
            _vol(
                volume_id="v_unknown",
                state="creating",
                attachment_state="ATTACHED",
            ),
        ]
    )
    assert result.findings_total == 0


# ---------------------------------------------------------------------------
# Security / data-leak guards
# ---------------------------------------------------------------------------


SECRET_FRAGMENTS = (
    "fake-api-key",
    "X-Auth-Token",
    "rackspace-api-key",
    "Bearer abc",
    "RAN123456",
)


@pytest.mark.parametrize("fragment", SECRET_FRAGMENTS)
def test_findings_never_carry_secret_material(fragment):
    result = _stable_eval(
        [
            _vol(volume_id="v1"),
            _server(server_id="s1", state="PAUSED"),
        ]
    )
    for f in result.findings:
        text = f.to_dict()
        # Walk every string value in the JSON form.
        def _walk(node: Any) -> None:
            if isinstance(node, str):
                assert fragment not in node, f"secret fragment leaked in finding: {fragment}"
            elif isinstance(node, dict):
                for v in node.values():
                    _walk(v)
            elif isinstance(node, list):
                for v in node:
                    _walk(v)
        _walk(text)


def test_engine_does_not_issue_product_writes():
    """The optimization module is pure — no HTTP surface is touched.

    A static assertion that the module does not import any HTTP
    client / product surface guarantees the read-only contract even
    if a future refactor accidentally wires the engine to the
    billing client.
    """
    from app.services.cloud.rackspace import optimization as opt_module

    forbidden_names = {
        "RackspaceHttpClient",
        "RackspaceBillingClient",
        "RackspaceIdentity",
        "requests",
        "httpx",
    }
    module_src = open(opt_module.__file__, "r", encoding="utf-8").read()
    for forbidden in forbidden_names:
        assert forbidden not in module_src, (
            f"optimization.py imports forbidden name {forbidden!r}"
        )


def test_engine_finding_ids_have_no_credential_pattern():
    result = _stable_eval([_vol(volume_id="v1")])
    for f in result.findings:
        for forbidden in ("api_key", "x-auth-token", "ran", "secret", "password"):
            assert forbidden not in f.finding_id.lower()
