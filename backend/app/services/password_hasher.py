"""Password hashing service — Phase 5A.

Uses Argon2id via :mod:`argon2` (the :mod:`argon2-cffi` package).
Argon2id is the OWASP-recommended password hashing algorithm and
is the right default for a Python 3.11 backend.

Design notes:

* The hashing parameters are intentionally tuned so that one
  ``verify`` call takes ~50-250 ms on modest hardware.  This is
  our application-layer brute-force defence: the rate at which an
  attacker can probe a single account is bounded by the cost of
  one Argon2id verification.  Redis-backed distributed rate
  limiting is documented as a production recommendation but is
  not implemented in Phase 5A.
* Hashes are PHC-format strings (``$argon2id$v=19$m=...,t=...,p=...
  $<salt>$<hash>``).  The encoded parameters are stored with the
  hash so we can re-tune the parameters later without invalidating
  existing hashes — the next successful verify can re-encode.
* ``verify`` NEVER raises on a malformed hash; it always returns
  ``False``.  The :class:`PasswordHasher` is the only component
  allowed to read or compare password material.  Routes never see
  the plaintext or the hash.
* The class is intentionally tiny and stateless.  Tests construct
  fresh instances.
"""
from __future__ import annotations

from dataclasses import dataclass

from argon2 import PasswordHasher as _Argon2PasswordHasher
from argon2.exceptions import (
    InvalidHash,
    VerificationError,
    VerifyMismatchError,
)


# Default Argon2id parameters.  Tuned to be intentionally costly at
# verification time without making legitimate logins feel slow.
# Override in tests by constructing ``PasswordHasher`` directly.
DEFAULT_TIME_COST = 3
DEFAULT_MEMORY_COST = 64 * 1024  # 64 MiB
DEFAULT_PARALLELISM = 2
DEFAULT_HASH_LENGTH = 32
DEFAULT_SALT_LENGTH = 16


@dataclass(frozen=True)
class PasswordHasher:
    """Argon2id password hasher.

    The dataclass is ``frozen`` so the underlying :class:`_Argon2PasswordHasher`
    cannot be swapped at runtime — parameters are validated at construction
    time and remain stable for the lifetime of the instance.
    """

    time_cost: int = DEFAULT_TIME_COST
    memory_cost: int = DEFAULT_MEMORY_COST
    parallelism: int = DEFAULT_PARALLELISM
    hash_length: int = DEFAULT_HASH_LENGTH
    salt_length: int = DEFAULT_SALT_LENGTH

    def __post_init__(self) -> None:
        if self.time_cost < 1:
            raise ValueError("time_cost must be >= 1")
        if self.memory_cost < 8 * 1024:
            raise ValueError("memory_cost must be >= 8 MiB")
        if self.parallelism < 1:
            raise ValueError("parallelism must be >= 1")
        if self.hash_length < 16:
            raise ValueError("hash_length must be >= 16")
        if self.salt_length < 8:
            raise ValueError("salt_length must be >= 8")

    def _engine(self) -> _Argon2PasswordHasher:
        return _Argon2PasswordHasher(
            time_cost=self.time_cost,
            memory_cost=self.memory_cost,
            parallelism=self.parallelism,
            hash_len=self.hash_length,
            salt_len=self.salt_length,
        )

    def hash(self, password: str) -> str:
        """Hash ``password`` and return the encoded Argon2id string.

        The returned string already encodes all parameters so future
        verification works even after we re-tune them.
        """
        if not isinstance(password, str) or not password:
            raise ValueError("password must be a non-empty string")
        return self._engine().hash(password)

    def verify(self, password: str, encoded_hash: str) -> bool:
        """Return ``True`` iff ``password`` matches ``encoded_hash``.

        A malformed hash or wrong password type returns ``False`` —
        this method NEVER raises on bad input.  The route handler
        treats a ``False`` return as a generic invalid-credentials
        error; it does not learn whether the hash was malformed.
        """
        if not isinstance(password, str) or not password:
            return False
        if not isinstance(encoded_hash, str) or not encoded_hash:
            return False
        try:
            self._engine().verify(encoded_hash, password)
            return True
        except (VerifyMismatchError, VerificationError, InvalidHash):
            return False

    def needs_rehash(self, encoded_hash: str) -> bool:
        """Return ``True`` if ``encoded_hash`` uses outdated parameters.

        Callers may use this to opportunistically re-encode the hash
        on the next successful login.  Phase 5A does not exercise the
        re-hash path (no login upgrades yet), but the hook is here
        for Phase 5A's password-change flows and beyond.
        """
        if not isinstance(encoded_hash, str) or not encoded_hash:
            return True
        try:
            return bool(self._engine().check_needs_rehash(encoded_hash))
        except InvalidHash:
            return True


__all__ = [
    "DEFAULT_TIME_COST",
    "DEFAULT_MEMORY_COST",
    "DEFAULT_PARALLELISM",
    "DEFAULT_HASH_LENGTH",
    "DEFAULT_SALT_LENGTH",
    "PasswordHasher",
]
