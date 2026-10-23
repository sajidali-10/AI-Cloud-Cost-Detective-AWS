"""Rackspace cloud provider sub-package — Phase 9B.1.

This sub-package hosts the Rackspace side of the cloud-provider
abstraction.  It is the canonical cost integration promoted from
the Phase 9A read-only POC:

* :mod:`app.services.cloud.rackspace.costs` — normalizes Billing V2
  ``estimated_charges`` payloads into provider-neutral cost
  snapshots with explicit failure / period semantics.
* :mod:`app.services.cloud.rackspace.cost_cache` — the Rackspace
  read-through cache wrapper over the shared ``cost_cache``
  table (FRESH + STALE-LKG + single-flight).
* :mod:`app.services.cloud.rackspace.adapter` —
  :class:`RackspaceCloudProvider`, the concrete
  :class:`~app.services.cloud.base.CloudProvider` implementation
  for Rackspace.

All transport / authentication work is delegated to the Phase 9A
modules in :mod:`app.services.rackspace` (shared HTTP client,
in-memory token cache, Billing client).  There is exactly ONE
Rackspace authentication implementation in the codebase.

Phase 9B.1 scope: COSTS ONLY.  Resources, optimization, and AI
FinOps surfaces are deliberately unimplemented (Phase 9B.2+).
"""
from app.services.cloud.rackspace.adapter import RackspaceCloudProvider

__all__ = ["RackspaceCloudProvider"]
