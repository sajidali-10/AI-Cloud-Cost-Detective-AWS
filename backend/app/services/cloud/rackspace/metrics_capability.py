"""Rackspace metrics capability probe — Phase 9C.1.

The Phase 9C.1 engine deliberately does NOT consume server
utilization metrics.  A rightsizing rule would require a reliable
CPU / memory / network history (with sample counts, observation
windows, and observer-role permissions) — building that is a
larger subphase than 9C.1.

This module exists for two reasons:

1.  The optimization engine records a structured
    ``rightsizing_evidence_status`` so a future subphase can flip
    metrics on without changing the engine's surface.
2.  The capability probe scaffolding is intentionally read-only:
    it touches only the Rackspace Identity service catalog (the
    same catalog used by Phase 9B.2) and never issues
    ``PUT``/``PATCH``/``DELETE``.  When 9C.x enables real metrics
    integration, this is the seam.

In 9C.1 the probe is hard-wired to ``DEFERRED`` so rightsizing
remains an evidence gate, not a wire call.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional

logger = logging.getLogger("cost-detective.cloud.rackspace.metrics_capability")


# Catalog identifiers that signal a metrics-style service.  These
# match the Rackspace public catalog as documented at Phase 9A POC.
METRICS_SERVICE_IDENTIFIERS: tuple = (
    "rax:cloudmetrics",
    "rax:monitor",
    "cloudMonitoring",
    "cloudMetrics",
)


class RightsizingEvidenceStatus(str, Enum):
    """Status of the rightsizing evidence layer.

    * ``AVAILABLE`` — utilization metrics are accessible with the
      configured role; the engine MAY emit rightsizing candidates
      for ACTIVE servers.
    * ``INSUFFICIENT`` — metrics are accessible but the observation
      window is too short to support a verdict.
    * ``PERMISSION_DENIED`` — the role cannot read metrics; rightsizing
      must stay gated off.
    * ``SERVICE_UNAVAILABLE`` — the metrics service is absent from
      the catalog or unreachable.
    * ``DEFERRED`` — Phase 9C.1 default.  The engine does not emit
      rightsizing candidates regardless of any other signal.
    """

    AVAILABLE = "AVAILABLE"
    INSUFFICIENT = "INSUFFICIENT"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    DEFERRED = "DEFERRED"


# Environment flag.  When ``RACKSPACE_METRICS_PROBE_ENABLED=1`` the
# probe will consult the service catalog.  Otherwise the deferred
# default is returned without any network call.  This keeps 9C.1
# network-free at boot.
_PROBE_ENABLED_ENV = "RACKSPACE_METRICS_PROBE_ENABLED"


@dataclass(frozen=True)
class MetricsCapabilityReport:
    """The structured result of a capability probe."""

    status: RightsizingEvidenceStatus
    note: str
    catalog_services_observed: tuple = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status.value,
            "note": self.note,
            "catalog_services_observed": list(self.catalog_services_observed),
        }


async def probe_metrics_capability() -> MetricsCapabilityReport:
    """Return the current rightsizing evidence status.

    Phase 9C.1 default: ``DEFERRED`` with the documented note
    ``RIGHTSIZING_METRICS_DEFERRED`` so the engine can keep its
    contract even before any real metrics integration ships.
    """
    enabled = (os.getenv(_PROBE_ENABLED_ENV, "").strip().lower() in {"1", "true", "yes"})
    if not enabled:
        return MetricsCapabilityReport(
            status=RightsizingEvidenceStatus.DEFERRED,
            note="RIGHTSIZING_METRICS_DEFERRED",
        )
    # Defensive: if a future subphase flips the env flag on, do not
    # silently make a destructive call.  Resolve the catalog via the
    # Phase 9A identity client and report whatever it observes.
    try:
        from app.services.rackspace import get_identity_client  # noqa: WPS433
    except Exception:  # pragma: no cover - defensive
        return MetricsCapabilityReport(
            status=RightsizingEvidenceStatus.DEFERRED,
            note="RIGHTSIZING_METRICS_DEFERRED",
        )
    try:
        session = await get_identity_client().current_session()
        catalog = session.service_catalog or {}
        observed = tuple(
            name
            for name in catalog
            if name in METRICS_SERVICE_IDENTIFIERS
        )
        if not observed:
            return MetricsCapabilityReport(
                status=RightsizingEvidenceStatus.SERVICE_UNAVAILABLE,
                note="metrics service not present in the catalog",
            )
        return MetricsCapabilityReport(
            status=RightsizingEvidenceStatus.INSUFFICIENT,
            note=(
                "metrics catalog entries observed; rightsizing still "
                "gated off until observation window + role are verified"
            ),
            catalog_services_observed=observed,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "rackspace.metrics_capability.probe_failed code=%s",
            type(exc).__name__,
        )
        return MetricsCapabilityReport(
            status=RightsizingEvidenceStatus.SERVICE_UNAVAILABLE,
            note="metrics probe failed",
        )


def probe_metrics_capability_sync() -> MetricsCapabilityReport:
    """Synchronous variant for tests and the live validator.

    Mirrors :func:`probe_metrics_capability` but never touches the
    network — 9C.1's only callers are deterministic tests and the
    in-process validation script, neither of which can drive an
    event loop.  Future subphases that wire real metrics should
    move the sync path behind a thin ``asyncio.run`` wrapper.
    """
    return MetricsCapabilityReport(
        status=RightsizingEvidenceStatus.DEFERRED,
        note="RIGHTSIZING_METRICS_DEFERRED",
    )


__all__ = [
    "METRICS_SERVICE_IDENTIFIERS",
    "MetricsCapabilityReport",
    "RightsizingEvidenceStatus",
    "probe_metrics_capability",
    "probe_metrics_capability_sync",
]
