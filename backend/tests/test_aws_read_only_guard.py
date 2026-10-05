"""Unit tests for the read-only AWS operation guard.

These tests are pure-Python: they exercise the guard module directly
and never touch boto3, moto, or any network.  They are intentionally
written so they pass under both the live container environment and
the local-host pytest invocation in `make test`.
"""
from __future__ import annotations

import pytest

from app.services.aws.guard import (
    FORBIDDEN_PREFIXES,
    READ_ONLY_PREFIXES,
    AwsReadOnlyViolation,
    assert_read_only,
    find_forbidden_prefix,
    is_read_only_operation,
)


class _FakeClient:
    """Stand-in for a Boto3 client.

    The guard never calls methods on the client — it only inspects
    the operation name passed by the caller — so a bare object is
    enough to exercise the wrapper's signature path.
    """

    def __repr__(self) -> str:  # pragma: no cover - cosmetic only
        return "<FakeClient>"


# ---------------------------------------------------------------------------
# Predicate tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("op_name", ["get_cost_and_usage", "describe_instances", "list_buckets", "search_index"])
def test_read_only_prefixes_are_accepted(op_name: str) -> None:
    assert is_read_only_operation(op_name) is True
    assert find_forbidden_prefix(op_name) is None


@pytest.mark.parametrize(
    "op_name",
    [
        "create_volume",
        "delete_bucket",
        "put_object",
        "update_function_code",
        "modify_instance_attribute",
        "start_instances",
        "stop_instances",
        "terminate_instances",
        "attach_volume",
        "detach_volume",
        "associate_address",
        "disassociate_address",
        "run_instances",
        "register_image",
        "deregister_image",
        "enable_metrics_collection",
        "disable_metrics_collection",
        "reboot_instances",
        "release_address",
        "restore_table_from_snapshot",
        "reset_instance_attribute",
        "cancel_export_task",
        "send_command",
        "publish_message",
        "invoke_function",
    ],
)
def test_forbidden_prefixes_are_rejected(op_name: str) -> None:
    assert is_read_only_operation(op_name) is False
    assert find_forbidden_prefix(op_name) is not None


def test_empty_or_non_string_op_name_is_rejected() -> None:
    assert is_read_only_operation("") is False
    assert is_read_only_operation(None) is False  # type: ignore[arg-type]
    assert find_forbidden_prefix("") is None
    assert find_forbidden_prefix(None) is None  # type: ignore[arg-type]


def test_unclassified_op_name_is_rejected_by_assert_read_only() -> None:
    # A brand-new boto3 operation that the guard has not been told
    # about must not silently pass.
    with pytest.raises(AwsReadOnlyViolation) as excinfo:
        assert_read_only(_FakeClient(), "do_something_brand_new")
    assert excinfo.value.op_name == "do_something_brand_new"
    assert excinfo.value.matched_prefix == "<unclassified>"


# ---------------------------------------------------------------------------
# assert_read_only wrapper tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("op_name", list(READ_ONLY_PREFIXES))
def test_assert_read_only_accepts_known_read_only_ops(op_name: str) -> None:
    # ``assert_read_only`` only inspects the operation name, so the
    # client arg can be any object — we use a bare sentinel here.
    assert_read_only(_FakeClient(), op_name + "_cost_and_usage")


@pytest.mark.parametrize(
    "op_name",
    [
        "create_user",
        "delete_user",
        "put_role_policy",
        "update_access_key",
        "modify_db_instance",
        "start_db_instance",
        "stop_db_cluster",
        "terminate_db_instance",
        "attach_role_policy",
        "detach_user_policy",
        "associate_route_table",
        "disassociate_route_table",
        "run_instances",
    ],
)
def test_assert_read_only_rejects_forbidden_ops(op_name: str) -> None:
    with pytest.raises(AwsReadOnlyViolation) as excinfo:
        assert_read_only(_FakeClient(), op_name)
    assert excinfo.value.op_name == op_name
    assert excinfo.value.matched_prefix in FORBIDDEN_PREFIXES


def test_assert_read_only_exception_message_sanitized() -> None:
    """The exception message must never echo credentials or payload data."""
    with pytest.raises(AwsReadOnlyViolation) as excinfo:
        assert_read_only(_FakeClient(), "terminate_instances")
    msg = str(excinfo.value)
    # It names the operation and the matched prefix but nothing else.
    assert "terminate_instances" in msg
    assert "terminate_" in msg
    # Sanity: nothing that looks like a credential is present.
    for needle in ("AKIA", "ASIA", "secret", "password", "session"):
        assert needle.lower() not in msg.lower()


def test_forbidden_prefixes_constant_is_a_tuple() -> None:
    # Belt-and-braces: a future contributor who tries to "extend" the
    # list with a set or list must update the assert_read_only call
    # sites too.  The guard relies on tuple iteration order to pick
    # the *first* matching prefix deterministically.
    assert isinstance(FORBIDDEN_PREFIXES, tuple)
    assert len(FORBIDDEN_PREFIXES) >= 12  # all the documented mutating prefixes


def test_read_only_prefixes_constant_is_a_tuple() -> None:
    assert isinstance(READ_ONLY_PREFIXES, tuple)
    for prefix in ("get_", "describe_", "list_", "search_"):
        assert prefix in READ_ONLY_PREFIXES
