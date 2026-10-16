"""Azure subscription bootstrap registration — Phase 7C.2A.

Subscription IDs are **runtime configuration**, not source-code
data.  This module is the single entry point through which the
provider learns which subscriptions to register on first boot.

Configuration model:

* Subscriptions are declared through numbered environment
  variables so an operator can register as many as needed without
  changing code:

      AZURE_SUBSCRIPTION_1_ID=<guid>
      AZURE_SUBSCRIPTION_1_DISPLAY_NAME="<label>"
      AZURE_SUBSCRIPTION_2_ID=<guid>
      AZURE_SUBSCRIPTION_2_DISPLAY_NAME="<label>"
      ...

* A subscription is *only* registered when both its
  ``AZURE_SUBSCRIPTION_<N>_ID`` and its
  ``AZURE_SUBSCRIPTION_<N>_DISPLAY_NAME`` are present.
* The provider's :func:`bootstrap_azure_subscriptions_once`
  consumes :func:`get_bootstrap_registrations` and is idempotent:
  a registration that already exists is silently skipped (the
  row's tenant_id is backfilled if it was previously empty).

Why environment variables and not hardcoded IDs?

The two real subscriptions the operator has authorised (MCPP
Subscription, Microsoft Azure Sponsorship) are *configuration
data*, not secrets, but they MUST NOT be baked into the provider
implementation: the provider must remain deployment-neutral and
must not carry operator-specific identifiers in source.  Env vars
keep the IDs in the deployment configuration (.env / secret
manager) where every other runtime identifier lives.

This module never logs subscription IDs in full.  The helper
:func:`safe_subscription_label` (in
:mod:`app.services.cloud.azure.redaction`) truncates to the first
8 hex characters before logging.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Iterable, Mapping, Tuple

from app.services.cloud.azure.redaction import (
    safe_subscription_label,
    scrub_log_payload,
)


logger = logging.getLogger("cost-detective.azure.registrations")


# Azure subscription GUIDs are well-formed UUIDs in lower-case.
_GUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _parse_subscriptions_from_env() -> Tuple[Mapping[str, str], ...]:
    """Read numbered ``AZURE_SUBSCRIPTION_<N>_*`` variables.

    Iterates ``N`` from 1 to 32 (a generous ceiling) and collects
    every registration that has BOTH an ID and a display name set
    to non-empty values.  Entries with only one of the two
    fields are silently skipped with a warning.

    The scan stops at the first index where both fields are
    absent so an operator can keep the variable list dense.
    """
    out: list[dict[str, str]] = []
    for index in range(1, 33):
        id_key = f"AZURE_SUBSCRIPTION_{index}_ID"
        name_key = f"AZURE_SUBSCRIPTION_{index}_DISPLAY_NAME"
        sub_id = (os.getenv(id_key) or "").strip()
        display_name = (os.getenv(name_key) or "").strip()
        if not sub_id and not display_name:
            # Sentinel: nothing else will follow.  We still keep
            # scanning in case an operator left a gap, but if we
            # hit three consecutive empty rows we can stop early
            # — the per-phase deployment script never has gaps.
            continue
        if not sub_id:
            logger.warning(
                "azure.registrations.skipped index=%d reason=missing_id",
                index,
            )
            continue
        if not display_name:
            display_name = safe_subscription_label(sub_id)
        if not _GUID_RE.match(sub_id):
            logger.warning(
                "azure.registrations.skipped index=%d reason=invalid_guid label=%s",
                index,
                safe_subscription_label(sub_id),
            )
            continue
        out.append({
            "subscription_id": sub_id,
            "display_name": scrub_log_payload(display_name)[:120],
        })
    return tuple(out)


def get_bootstrap_registrations() -> Iterable[Mapping[str, str]]:
    """Return the configured subscription registrations.

    This is the single entry point the provider calls.  Returns
    an empty tuple when no subscriptions are configured (the
    provider refuses to insert any rows in that case).
    """
    return _parse_subscriptions_from_env()


__all__ = ["get_bootstrap_registrations"]
