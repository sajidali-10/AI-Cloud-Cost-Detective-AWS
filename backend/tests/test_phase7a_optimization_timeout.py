"""Phase 7A — Optimization timeout resilience tests.

These tests pin the behavior the Phase 6B dashboard relies on:

* A slow AWS-native source (Compute Optimizer or Cost Optimization
  Hub) does NOT blank the dashboard.
* The deterministic engine always contributes; even when both AWS
  sources time out, deterministic recommendations remain present.
* The orchestrator emits a TIMED_OUT ``OptimizationNotice`` so the
  UI can attribute the gap.

The tests use monkey-patched slow fetcher functions; no AWS calls.
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional

import pytest

from app.services import optimization_engine


def _slow_sleep(_seconds: float) -> None:
    time.sleep(_seconds)


def _make_inputs(
    *, utilization: Optional[Dict[str, List[Any]]] = None
) -> optimization_engine.OptimizationInputs:
    return optimization_engine.OptimizationInputs(
        region="us-east-1",
        account_id="123456789012",
        days=30,
        phase1_services={},
        utilization_by_resource=utilization or {},
        include_compute_optimizer=True,
        include_cost_optimization_hub=True,
        include_deterministic=False,
    )


def test_short_budget_completes_normally(monkeypatch: pytest.MonkeyPatch) -> None:
    """Within-budget fetcher returns the candidates."""
    # Force short budgets so the test stays fast.
    monkeypatch.setitem(optimization_engine.PROVIDER_TIMEOUT_SECONDS, "compute_optimizer", 1.0)
    monkeypatch.setitem(optimization_engine.PROVIDER_TIMEOUT_SECONDS, "cost_optimization_hub", 1.0)

    # Patch the in-module fetchers to return quickly.
    monkeypatch.setattr(
        optimization_engine,
        "_fetch_compute_optimizer_candidates",
        lambda **_kw: [],
    )
    monkeypatch.setattr(
        optimization_engine,
        "_fetch_cost_optimization_hub_candidates",
        lambda **_kw: [],
    )
    # Capability gate: ACTIVE so the fetchers actually run.
    monkeypatch.setattr(
        optimization_engine,
        "_co_capability",
        lambda **_kw: (optimization_engine.ServiceCapability(status="ACTIVE"), None),
    )
    monkeypatch.setattr(
        optimization_engine,
        "_coh_capability",
        lambda **_kw: (optimization_engine.ServiceCapability(status="ACTIVE"), None),
    )

    recs, warnings = optimization_engine.build_recommendations(_make_inputs())
    assert recs == []
    assert warnings == []


def test_compute_optimizer_timeout_emits_timed_out_notice(monkeypatch: pytest.MonkeyPatch) -> None:
    """A slow Compute Optimizer call should produce a TIMED_OUT warning."""
    monkeypatch.setitem(optimization_engine.PROVIDER_TIMEOUT_SECONDS, "compute_optimizer", 0.05)
    monkeypatch.setitem(optimization_engine.PROVIDER_TIMEOUT_SECONDS, "cost_optimization_hub", 1.0)

    def _slow_co(**_kw: Any) -> List[Any]:
        _slow_sleep(0.5)
        return []

    monkeypatch.setattr(
        optimization_engine,
        "_fetch_compute_optimizer_candidates",
        _slow_co,
    )
    monkeypatch.setattr(
        optimization_engine,
        "_fetch_cost_optimization_hub_candidates",
        lambda **_kw: [],
    )
    monkeypatch.setattr(
        optimization_engine,
        "_co_capability",
        lambda **_kw: (optimization_engine.ServiceCapability(status="ACTIVE"), None),
    )
    monkeypatch.setattr(
        optimization_engine,
        "_coh_capability",
        lambda **_kw: (optimization_engine.ServiceCapability(status="ACTIVE"), None),
    )

    _, warnings = optimization_engine.build_recommendations(_make_inputs())
    timed_out = [w for w in warnings if w.code == "TIMED_OUT"]
    assert len(timed_out) == 1
    assert timed_out[0].source == "compute_optimizer"


def test_cost_optimization_hub_timeout_emits_timed_out_notice(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(optimization_engine.PROVIDER_TIMEOUT_SECONDS, "compute_optimizer", 1.0)
    monkeypatch.setitem(optimization_engine.PROVIDER_TIMEOUT_SECONDS, "cost_optimization_hub", 0.05)

    monkeypatch.setattr(
        optimization_engine,
        "_fetch_compute_optimizer_candidates",
        lambda **_kw: [],
    )

    def _slow_coh(**_kw: Any) -> List[Any]:
        _slow_sleep(0.5)
        return []

    monkeypatch.setattr(
        optimization_engine,
        "_fetch_cost_optimization_hub_candidates",
        _slow_coh,
    )
    monkeypatch.setattr(
        optimization_engine,
        "_co_capability",
        lambda **_kw: (optimization_engine.ServiceCapability(status="ACTIVE"), None),
    )
    monkeypatch.setattr(
        optimization_engine,
        "_coh_capability",
        lambda **_kw: (optimization_engine.ServiceCapability(status="ACTIVE"), None),
    )

    _, warnings = optimization_engine.build_recommendations(_make_inputs())
    timed_out = [w for w in warnings if w.code == "TIMED_OUT"]
    assert len(timed_out) == 1
    assert timed_out[0].source == "cost_optimization_hub"


def test_deterministic_runs_even_when_aws_sources_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    """When both AWS sources fail, the deterministic engine must still
    produce recommendations and the overall status is PARTIAL_SUCCESS."""
    monkeypatch.setitem(optimization_engine.PROVIDER_TIMEOUT_SECONDS, "compute_optimizer", 1.0)
    monkeypatch.setitem(optimization_engine.PROVIDER_TIMEOUT_SECONDS, "cost_optimization_hub", 1.0)
    monkeypatch.setattr(
        optimization_engine,
        "_fetch_compute_optimizer_candidates",
        lambda **_kw: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    monkeypatch.setattr(
        optimization_engine,
        "_fetch_cost_optimization_hub_candidates",
        lambda **_kw: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    monkeypatch.setattr(
        optimization_engine,
        "_co_capability",
        lambda **_kw: (optimization_engine.ServiceCapability(status="ACTIVE"), None),
    )
    monkeypatch.setattr(
        optimization_engine,
        "_coh_capability",
        lambda **_kw: (optimization_engine.ServiceCapability(status="ACTIVE"), None),
    )

    # Deterministic engine produces a deterministic candidate for a
    # hand-rolled unattached EBS volume.
    from app.services.optimization_rules import (
        DeterministicCandidate,
        rule_unattached_ebs,
    )

    class _Item:
        def __init__(self, vid: str, size: int) -> None:
            self.volume_id = vid
            self.size_gb = size
            self.state = "available"
            self.attachments: List[Any] = []
            self.tags: Dict[str, str] = {}

    det = rule_unattached_ebs(
        account_id="123",
        region="us-east-1",
        ebs_volumes=[_Item("vol-aaaa", 100)],
    )
    assert len(det) == 1

    monkeypatch.setattr(
        optimization_engine,
        "_fetch_deterministic_candidates",
        lambda **_kw: det,
    )

    inputs = optimization_engine.OptimizationInputs(
        region="us-east-1",
        account_id="123",
        days=30,
        phase1_services={},
        utilization_by_resource={},
        include_compute_optimizer=True,
        include_cost_optimization_hub=True,
        include_deterministic=True,
    )
    recs, warnings = optimization_engine.build_recommendations(inputs)
    assert len(recs) >= 1
    assert any(w.source == "compute_optimizer" for w in warnings)
    assert any(w.source == "cost_optimization_hub" for w in warnings)
