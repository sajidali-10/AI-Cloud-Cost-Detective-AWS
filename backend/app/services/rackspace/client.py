"""Shared async HTTP client for the Rackspace POC — Phase 9A.

A single :class:`httpx.AsyncClient` is constructed lazily on first
use and reused across requests.  The client honours bounded
timeouts (the Phase 9A default is 15 s for connect / read / write)
and never follows redirects — Rackspace product endpoints do not
issue them, and a redirect from an Identity-authenticated request
would be a smuggling red flag.

Token handling
--------------

The client itself never holds the X-Auth-Token.  The
:class:`~app.services.rackspace.identity.RackspaceIdentity` module
owns token issuance and caching; once a token has been issued, the
shared client receives it via the per-call ``token_provider``
callback.  The callback returns the *current* token without
logging it; the shared client never persists it.

The client is deliberately small — it is *not* a wrapper around
:mod:`httpx` that pretends to be a Rackspace SDK.  Callers pass
the resolved base URL, the path, the verb, and (for write-like
requests) a JSON body.  The shared client enforces:

* a bounded timeout,
* ``Content-Type: application/json`` for JSON requests,
* a sanitized error model that translates HTTP 401 / 403 / 404 /
  5xx into the corresponding :mod:`exceptions` subclass without
  attaching the response body (which may contain the X-Auth-Token
  echoed by misbehaving intermediaries).

Read-only enforcement
---------------------

The public surface of :class:`RackspaceHttpClient` exposes only
:func:`get` (plus the implicit :func:`post` used by Identity to
authenticate).  Other verbs (``put`` / ``patch`` / ``delete``) are
not exposed — Phase 9A is a read-only POC and the codebase should
not silently grow write capabilities.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional

import httpx

from app.services.rackspace.exceptions import (
    RackspaceAuthError,
    RackspaceNotFoundError,
    RackspacePermissionError,
    RackspaceTransportError,
)


#: Default request timeout, in seconds.  Chosen to be generous
#: enough for a slow Identity round-trip plus one product call,
#: but bounded so a hung Rackspace endpoint cannot wedge the POC
#: validator forever.
DEFAULT_TIMEOUT_SECONDS: float = 15.0


@dataclass(frozen=True)
class RackspaceResponse:
    """A sanitized HTTP response wrapper.

    Holds only the parsed JSON body, the HTTP status code, and the
    response headers *minus* any ``X-Auth-Token`` echo.  Callers
    receive the body verbatim because Rackspace product endpoints
    are well-behaved and the validator only consumes sanitized
    summary fields; the dataclass itself never exposes the raw
    request headers (so a future caller cannot accidentally log
    the X-Auth-Token).
    """

    status_code: int
    body: Dict[str, Any]
    headers: Dict[str, str]


TokenProvider = Callable[[], Awaitable[Optional[str]]]


def _redact_headers(headers: Dict[str, str]) -> Dict[str, str]:
    """Drop headers that may carry authentication material.

    Rackspace does not normally echo the X-Auth-Token in responses,
    but defensive coding costs nothing and prevents an accidental
    log line from leaking the token if a future proxy or load
    balancer is added in front of the Rackspace API.
    """

    forbidden = {
        "x-auth-token",
        "x-auth-key",
        "x-server-management-url",
        "set-cookie",
    }
    lower = {k.lower(): v for k, v in headers.items()}
    return {k: v for k, v in headers.items() if k.lower() not in forbidden and lower[k.lower()] not in forbidden}


class RackspaceHttpClient:
    """Reusable async HTTP client for Rackspace product APIs.

    Construct one instance per process.  The underlying
    :class:`httpx.AsyncClient` is lazily created on the first
    request and closed by :meth:`aclose` (the validator script
    always calls :meth:`aclose` from a ``finally`` block).
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        user_agent: str = "ai-cloud-cost-detective-phase9a/0.1",
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._user_agent = user_agent
        self._client: Optional[httpx.AsyncClient] = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout_seconds),
                follow_redirects=False,
                headers={"User-Agent": self._user_agent},
            )
        return self._client

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""

        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def get(
        self,
        base_url: str,
        path: str,
        *,
        token_provider: TokenProvider,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> RackspaceResponse:
        """Issue an authenticated ``GET`` request.

        ``token_provider`` is awaited on every call so the caller
        can refresh the token on demand (after expiry / 401).
        Returns a :class:`RackspaceResponse`.  Raises:

        * :class:`RackspaceAuthError` on HTTP 401,
        * :class:`RackspacePermissionError` on HTTP 403,
        * :class:`RackspaceNotFoundError` on HTTP 404,
        * :class:`RackspaceTransportError` on network failures.
        """

        token = await token_provider()
        if not token:
            # The token provider chose not to issue a token (e.g.
            # because credentials are absent).  Surface as an
            # auth failure so callers translate it consistently.
            raise RackspaceAuthError("no X-Auth-Token available")

        request_headers = {
            "X-Auth-Token": token,
            "Accept": "application/json",
        }
        if headers:
            request_headers.update(headers)

        client = await self._ensure_client()
        url = _join_url(base_url, path)
        try:
            response = await client.get(
                url, params=params, headers=request_headers,
            )
        except httpx.HTTPError as exc:
            raise RackspaceTransportError(
                f"Rackspace GET {path} failed: {type(exc).__name__}"
            ) from exc
        return _parse_response(response)

    async def post_json(
        self,
        base_url: str,
        path: str,
        body: Dict[str, Any],
        *,
        headers: Optional[Dict[str, str]] = None,
    ) -> RackspaceResponse:
        """Issue an *unauthenticated* ``POST`` with a JSON body.

        This is the only write-like verb exposed by the POC.  It
        is reserved for Rackspace Identity authentication — the
        product APIs are read-only.
        """

        request_headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if headers:
            request_headers.update(headers)

        client = await self._ensure_client()
        url = _join_url(base_url, path)
        try:
            response = await client.post(url, json=body, headers=request_headers)
        except httpx.HTTPError as exc:
            raise RackspaceTransportError(
                f"Rackspace POST {path} failed: {type(exc).__name__}"
            ) from exc
        return _parse_response(response)


def _join_url(base_url: str, path: str) -> str:
    """Join a base URL and a path without breaking the trailing slash.

    Rackspace product endpoints vary in whether they include the
    trailing ``/`` on the catalog.  We normalise here so callers
    can pass either form safely.
    """

    if not base_url:
        return path
    if not path:
        return base_url
    if base_url.endswith("/") and path.startswith("/"):
        return base_url + path[1:]
    if not base_url.endswith("/") and not path.startswith("/"):
        return base_url + "/" + path
    return base_url + path


def _parse_response(response: httpx.Response) -> RackspaceResponse:
    """Translate a raw :class:`httpx.Response` into a sanitized wrapper."""

    status = response.status_code
    body: Dict[str, Any] = {}
    text = response.text
    if text:
        try:
            parsed = response.json()
        except ValueError:
            # Non-JSON body — surface as transport error so the
            # validator reports an unambiguous failure.
            raise RackspaceTransportError(
                f"Rackspace returned non-JSON body (HTTP {status})"
            )
        if isinstance(parsed, dict):
            body = parsed
        else:
            # Some legacy endpoints may return a JSON array at the
            # top level.  Wrap into a dict so callers never see a
            # bare list.
            body = {"data": parsed}

    if status == 401:
        raise RackspaceAuthError(
            f"Rackspace rejected the X-Auth-Token (HTTP 401) at {response.url}"
        )
    if status == 403:
        raise RackspacePermissionError(
            f"Rackspace denied access (HTTP 403) at {response.url}"
        )
    if status == 404:
        raise RackspaceNotFoundError(
            f"Rackspace endpoint not available (HTTP 404) at {response.url}"
        )
    if status >= 500:
        raise RackspaceTransportError(
            f"Rackspace server error (HTTP {status}) at {response.url}"
        )

    return RackspaceResponse(
        status_code=status,
        body=body,
        headers=_redact_headers(dict(response.headers)),
    )


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "RackspaceHttpClient",
    "RackspaceResponse",
    "TokenProvider",
]
