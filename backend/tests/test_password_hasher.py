"""Tests for the Argon2id password hasher — Phase 5A."""
from __future__ import annotations

import inspect

import pytest

from app.services.password_hasher import PasswordHasher


@pytest.fixture()
def hasher() -> PasswordHasher:
    # Tuned down so the test suite stays snappy.  Defaults are still
    # safe for production runs in the actual container.
    return PasswordHasher(
        time_cost=1,
        memory_cost=8 * 1024,
        parallelism=1,
        hash_length=16,
        salt_length=8,
    )


def test_hash_is_argon2id_string(hasher: PasswordHasher) -> None:
    encoded = hasher.hash("correct horse battery staple")
    assert encoded.startswith("$argon2id$")
    # PHC format: $argon2id$v=19$m=...,t=...,p=...$<salt>$<hash>
    parts = encoded.split("$")
    assert parts[1] == "argon2id"
    assert parts[2].startswith("v=19")


def test_verify_correct_password(hasher: PasswordHasher) -> None:
    encoded = hasher.hash("password-1")
    assert hasher.verify("password-1", encoded) is True


def test_verify_wrong_password(hasher: PasswordHasher) -> None:
    encoded = hasher.hash("password-1")
    assert hasher.verify("password-2", encoded) is False


def test_verify_malformed_hash_returns_false(hasher: PasswordHasher) -> None:
    # Malformed hashes must NEVER raise — they always return False
    # so the auth layer collapses them with normal wrong-password
    # failures (no oracle distinguishing the two cases).
    assert hasher.verify("password-1", "not-an-argon2id-hash") is False
    assert hasher.verify("password-1", "") is False
    assert hasher.verify("password-1", "$argon2id$garbage") is False


def test_verify_empty_password_returns_false(hasher: PasswordHasher) -> None:
    encoded = hasher.hash("password-1")
    assert hasher.verify("", encoded) is False
    assert hasher.verify(None, encoded) is False  # type: ignore[arg-type]


def test_hash_produces_different_output_for_same_input(hasher: PasswordHasher) -> None:
    """Argon2id must salt; two hashes of the same password differ."""
    a = hasher.hash("same-password")
    b = hasher.hash("same-password")
    assert a != b
    assert hasher.verify("same-password", a)
    assert hasher.verify("same-password", b)


def test_hash_rejects_empty_or_non_string(hasher: PasswordHasher) -> None:
    with pytest.raises(ValueError):
        hasher.hash("")
    with pytest.raises(ValueError):
        hasher.hash(None)  # type: ignore[arg-type]


def test_constructor_validates_parameters() -> None:
    with pytest.raises(ValueError):
        PasswordHasher(time_cost=0)
    with pytest.raises(ValueError):
        PasswordHasher(memory_cost=1024)
    with pytest.raises(ValueError):
        PasswordHasher(parallelism=0)
    with pytest.raises(ValueError):
        PasswordHasher(hash_length=8)
    with pytest.raises(ValueError):
        PasswordHasher(salt_length=4)


def test_needs_rehash_changes_when_params_change() -> None:
    cheap = PasswordHasher(time_cost=1, memory_cost=8 * 1024, parallelism=1)
    expensive = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=2)
    encoded = cheap.hash("password")
    assert expensive.needs_rehash(encoded) is True
    assert cheap.needs_rehash(encoded) is False


def test_password_hasher_is_dataclass_frozen() -> None:
    sig = inspect.signature(PasswordHasher.__init__)
    # dataclass(frozen=True) sets __dataclass_params__.frozen
    assert getattr(PasswordHasher, "__dataclass_params__").frozen is True
    # Sanity: parameters exist.
    for name in ("time_cost", "memory_cost", "parallelism", "hash_length", "salt_length"):
        assert name in sig.parameters
