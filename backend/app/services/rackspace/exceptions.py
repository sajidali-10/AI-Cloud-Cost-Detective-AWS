"""Typed Rackspace exceptions — Phase 9A POC.

Every public Rackspace method raises one of the subclasses defined
here.  The hierarchy is intentionally narrow:

* :class:`RackspaceError` — base; never raised directly.
* :class:`RackspaceConfigError` — credentials missing / invalid
  configuration.  Callers translate this to a 503 / NOT_CONFIGURED
  result so the rest of the application keeps working when Rackspace
  has not been wired up yet (Phase 9A ships with Rackspace as an
  optional, non-required provider).
* :class:`RackspaceAuthError` — Identity rejected the credentials
  (HTTP 401).  Distinct from :class:`RackspacePermissionError`
  (HTTP 403) so the validator script can tell them apart.
* :class:`RackspacePermissionError` — Identity accepted the
  credentials but the role set is insufficient for a specific
  product API call (HTTP 403).
* :class:`RackspaceNotFoundError` — HTTP 404.  Used both for
  unrecognised endpoints and for "feature not enabled on this
  account" responses that Rackspace returns as 404s for some
  legacy billing surfaces.
* :class:`RackspaceElevatedRoleError` — Identity returned a role
  set that includes an unexpected elevated role (e.g. ``admin``,
  ``billing:admin``).  The POC fails closed on this case because
  the read-only guarantee is contractually important.
* :class:`RackspaceTransportError` — network / timeout / non-JSON
  responses.  No token / credential material is ever attached to
  these exceptions.

The ``code`` attribute is a stable string identifier suitable for
machine consumption; ``message`` is a *human-readable* description
that MUST NOT contain tokens, X-Auth-Token values, API keys, or
other secrets.
"""

from __future__ import annotations

from typing import Optional


class RackspaceError(Exception):
    """Base class for every Rackspace exception."""

    code: str = "rackspace_error"

    def __init__(self, message: str = "") -> None:
        super().__init__(message)
        self.message = message


class RackspaceConfigError(RackspaceError):
    """Credentials are missing or the configuration is unusable."""

    code = "rackspace_not_configured"


class RackspaceAuthError(RackspaceError):
    """Identity rejected the supplied credentials (HTTP 401)."""

    code = "rackspace_auth_failed"


class RackspacePermissionError(RackspaceError):
    """The account is authenticated but lacks the required role (HTTP 403)."""

    code = "rackspace_permission_denied"


class RackspaceNotFoundError(RackspaceError):
    """Endpoint or resource not found / not enabled (HTTP 404)."""

    code = "rackspace_not_found"


class RackspaceElevatedRoleError(RackspaceError):
    """Identity returned an unexpectedly privileged role.

    The POC fails closed on this condition — if the authenticated
    user has ``admin``, ``billing:admin``, ``creator``, or any of
    the identity-write roles, the call site is considered unsafe
    for a read-only validator.  The validator script treats this
    as a hard failure.
    """

    code = "rackspace_elevated_role_detected"


class RackspaceTransportError(RackspaceError):
    """Network failure, timeout, or malformed JSON response."""

    code = "rackspace_transport_error"


class RackspaceThrottledError(RackspaceTransportError):
    """Rackspace throttled the request (HTTP 429).

    Phase 9B.1 — introduced so the canonical cost layer can
    distinguish throttling from generic transport failures while
    remaining a subclass of :class:`RackspaceTransportError`
    (Phase 9A catch-blocks that handle transport errors keep
    working unchanged).
    """

    code = "rackspace_throttled"

    def __init__(self, message: str = "", retry_after: Optional[float] = None) -> None:
        super().__init__(message)
        #: Seconds Rackspace asked us to wait (parsed from the
        #: ``Retry-After`` header when present).  ``None`` when the
        #: response did not carry a usable value.
        self.retry_after = retry_after


__all__ = [
    "RackspaceAuthError",
    "RackspaceConfigError",
    "RackspaceElevatedRoleError",
    "RackspaceError",
    "RackspaceNotFoundError",
    "RackspacePermissionError",
    "RackspaceThrottledError",
    "RackspaceTransportError",
]
