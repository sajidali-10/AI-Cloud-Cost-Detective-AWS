"""Provider registry — Phase 7C.

The registry is the single source of truth for which cloud
providers are active in this deployment.  It owns no AWS-specific
code — adapters register themselves by kind, and the rest of the
application looks them up through ``ProviderRegistry.current()``.

Safety invariants:

1. **Unknown / unregistered providers fail explicitly.**  Calling
   :meth:`ProviderRegistry.require` for a kind that has no adapter
   raises :class:`ProviderNotFound`.  This is a programming bug,
   not a runtime condition — providers are statically wired at
   process boot, so a missing one is a deployment defect.

2. **No dynamic arbitrary imports.**  Adapters are imported at
   module load time via :func:`_register_known_providers` and
   stored in a process-wide dict.  There is no
   ``importlib.import_module(provider_name)`` path that would let
   user input drive a Python import.

3. **Disabled providers fail explicitly.**  The registry accepts
   a ``disabled`` set so an operator can turn off a provider
   without uninstalling its adapter (useful for staged rollouts
   of Azure/Rackspace).  A request for a disabled provider raises
   :class:`ProviderNotFound` with a distinct message.

4. **Process-wide singleton.**  ``ProviderRegistry.current()``
   returns the same instance for every caller in the process;
   the registry is created lazily on first access.
"""
from __future__ import annotations

import logging
import threading
from typing import Dict, Iterable, Mapping, Optional

from app.services.cloud.base import CloudProvider
from app.services.cloud.models import CloudProviderKind

logger = logging.getLogger("cost-detective.cloud.registry")


class ProviderNotFound(KeyError):
    """Raised when a provider kind is not registered (or is disabled).

    Inherits ``KeyError`` so callers that already handle ``KeyError``
    from a dict-style lookup continue to work; the dedicated class
    lets the API layer return a precise 404 / 503 envelope.
    """

    def __init__(self, kind: CloudProviderKind, *, disabled: bool = False):
        suffix = "is registered but disabled in this deployment" if disabled else (
            "is not registered in this deployment"
        )
        super().__init__(f"cloud provider {kind.value} {suffix}")
        self.kind = kind
        self.disabled = disabled


class ProviderAlreadyRegistered(ValueError):
    """Raised when two adapters try to register the same kind."""


class ProviderRegistry:
    """Process-wide registry of cloud provider adapters.

    Constructed via :meth:`current` so the singleton is shared
    across the entire backend.  Tests that need an isolated
    registry can use :meth:`empty` to mint a fresh instance.
    """

    def __init__(self) -> None:
        self._adapters: Dict[CloudProviderKind, CloudProvider] = {}
        self._disabled: set[CloudProviderKind] = set()
        self._lock = threading.Lock()

    # ---- registration -----------------------------------------------------

    def register(self, adapter: CloudProvider) -> None:
        """Register ``adapter`` for its declared ``provider_kind``.

        Raises :class:`ProviderAlreadyRegistered` if the kind is
        already bound; raises :class:`ValueError` if the adapter
        has no ``provider_kind``.  Re-registering with ``replace=True``
        is allowed (used by tests).
        """
        kind = adapter.provider_kind
        if kind is None:
            raise ValueError(
                f"adapter {adapter!r} does not declare provider_kind"
            )
        with self._lock:
            existing = self._adapters.get(kind)
            if existing is not None and existing is not adapter:
                raise ProviderAlreadyRegistered(
                    f"provider {kind.value} is already registered with {existing!r}"
                )
            self._adapters[kind] = adapter
            self._disabled.discard(kind)
            logger.info("cloud.provider.registered kind=%s adapter=%s", kind.value, type(adapter).__name__)

    def disable(self, kind: CloudProviderKind) -> None:
        """Disable a provider without unregistering it.

        Disabled providers appear in :meth:`available_kinds` filtered
        out but still appear in :meth:`known_kinds`.  The intent is
        staged rollout: an Azure adapter may be present but disabled
        in regions where Entra ID is not yet wired.
        """
        with self._lock:
            self._disabled.add(kind)
            logger.info("cloud.provider.disabled kind=%s", kind.value)

    def enable(self, kind: CloudProviderKind) -> None:
        with self._lock:
            self._disabled.discard(kind)
            logger.info("cloud.provider.enabled kind=%s", kind.value)

    # ---- lookup -----------------------------------------------------------

    def require(self, kind: CloudProviderKind) -> CloudProvider:
        """Return the adapter for ``kind`` or raise :class:`ProviderNotFound`.

        Distinguishes "not registered" from "registered but
        disabled" so callers can surface the right operator
        message.
        """
        with self._lock:
            if kind in self._disabled:
                raise ProviderNotFound(kind, disabled=True)
            adapter = self._adapters.get(kind)
        if adapter is None:
            raise ProviderNotFound(kind)
        return adapter

    def try_get(self, kind: CloudProviderKind) -> Optional[CloudProvider]:
        """Return the adapter for ``kind`` or ``None`` if not registered.

        Distinct from :meth:`require` — this is the non-raising
        variant used by routes that want to gracefully degrade when
        a provider is unavailable.
        """
        try:
            return self.require(kind)
        except ProviderNotFound:
            return None

    def is_registered(self, kind: CloudProviderKind) -> bool:
        with self._lock:
            return kind in self._adapters and kind not in self._disabled

    def known_kinds(self) -> Iterable[CloudProviderKind]:
        """Return every kind that has an adapter registered.

        Includes disabled adapters so an admin endpoint can surface
        them with their state.
        """
        with self._lock:
            return tuple(self._adapters.keys())

    def available_kinds(self) -> Iterable[CloudProviderKind]:
        """Return every kind that is registered and not disabled."""
        with self._lock:
            return tuple(
                kind for kind in self._adapters.keys() if kind not in self._disabled
            )

    def snapshot(self) -> Mapping[CloudProviderKind, CloudProvider]:
        """Return a snapshot of registered providers (live, not disabled)."""
        with self._lock:
            return {
                kind: adapter
                for kind, adapter in self._adapters.items()
                if kind not in self._disabled
            }

    # ---- singleton --------------------------------------------------------

    _current: Optional["ProviderRegistry"] = None
    _current_lock = threading.Lock()

    @classmethod
    def current(cls) -> "ProviderRegistry":
        """Return the process-wide registry, creating it on first access."""
        if cls._current is not None:
            return cls._current
        with cls._current_lock:
            if cls._current is None:
                registry = cls()
                # Assign BEFORE importing adapter modules so an
                # adapter's self-registration (which calls
                # ``ProviderRegistry.current()``) does not recurse
                # back through this factory.
                cls._current = registry
                _register_known_providers(registry)
        return cls._current

    @classmethod
    def reset_current(cls) -> None:
        """Drop the singleton.  Tests only."""
        with cls._current_lock:
            cls._current = None

    @classmethod
    def empty(cls) -> "ProviderRegistry":
        """Return a fresh empty registry.  Tests only."""
        return cls()


def _register_known_providers(registry: ProviderRegistry) -> None:
    """Register every adapter bundled with the application.

    Phase 7C.1: AWS only.  Importing the module is enough to wire
    the adapter — :func:`register` is invoked from inside the
    module's import side effects so we never instantiate a
    provider here.
    """
    # Import for the side-effect of registering the AWS adapter.
    # No AWS work happens at import time beyond declaring class
    # attributes.
    from app.services.cloud.aws import adapter as _aws_adapter_module  # noqa: F401


__all__ = [
    "ProviderAlreadyRegistered",
    "ProviderNotFound",
    "ProviderRegistry",
]
