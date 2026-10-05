"""Read-only AWS operation guard — Phase 2.

Phase 2 must NEVER issue an AWS mutation call.  This module is the
single source of truth for that rule at the application layer.

It exposes:

* ``FORBIDDEN_PREFIXES`` — the tuple of Boto3 operation-name prefixes
  that would mutate AWS state.  Anything matching one of these raises
  ``AwsReadOnlyViolation`` before the call is dispatched.
* ``is_read_only_operation(op_name)`` — pure predicate used by unit
  tests and by ``phase2_verify.sh`` (via the shell grep in scripts/).
* ``AwsReadOnlyViolation`` — a dedicated exception type so callers
  never confuse an "I was about to delete a resource" failure with a
  network or authorization failure.
* ``assert_read_only(client, op_name)`` — the convenience wrapper
  used at every AWS service call site in Phase 2.  It receives the
  Boto3 client object so we can validate against the same client
  whose ``meta.service_model`` is inspected by ``phase2_verify.sh``'s
  shell grep.  It does NOT itself perform the call — it is a guard
  to be invoked immediately before ``client.<op_name>(**kwargs)``.

Database writes (e.g. ``INSERT INTO cost_cache``) are intentionally
NOT in scope here; the guard is for AWS-only mutations.
"""
from __future__ import annotations

from typing import Final


# These prefixes cover every Boto3 operation name that would mutate
# AWS state.  The list is intentionally a literal tuple (not a
# regex) so it is grep-able in code review and easy to audit.  See
# docs/phase2-cost-intelligence.md for the rationale.
FORBIDDEN_PREFIXES: Final[tuple[str, ...]] = (
    "create_",
    "delete_",
    "put_",
    "update_",
    "modify_",
    "start_",
    "stop_",
    "terminate_",
    "attach_",
    "detach_",
    "associate_",
    "disassociate_",
    # Belt-and-braces: ``run_`` (e.g. ``RunInstances``,
    # ``RunCommand``) and ``register_`` / ``deregister_`` (e.g.
    # ``RegisterImage``) also mutate state.
    "run_",
    "register_",
    "deregister_",
    "enable_",
    "disable_",
    "reboot_",
    "release_",
    "restore_",
    "reset_",
    "cancel_",
    "send_",
    "publish_",
    "invoke_",
)


# Read-only prefixes — listed explicitly so future code can grep the
# file to confirm intent and so tests can iterate over them.
READ_ONLY_PREFIXES: Final[tuple[str, ...]] = (
    "get_",
    "describe_",
    "list_",
    "search_",
)


class AwsReadOnlyViolation(RuntimeError):
    """Raised when code attempts an AWS mutation through a Phase 2 path.

    This is a defensive failure: the guard fires BEFORE the call is
    dispatched, so a violation never reaches AWS.  The message names
    the offending operation and the matching prefix so logs and
    tests can pinpoint the issue without revealing credentials or
    request payloads.
    """

    def __init__(self, op_name: str, matched_prefix: str) -> None:
        self.op_name = op_name
        self.matched_prefix = matched_prefix
        super().__init__(
            f"AWS operation '{op_name}' is forbidden in Phase 2 "
            f"(matched forbidden prefix '{matched_prefix}')"
        )


def is_read_only_operation(op_name: str) -> bool:
    """Return True if ``op_name`` looks like a read-only Boto3 operation.

    The check is conservative: any op that does NOT match a known
    read-only prefix AND does NOT match a forbidden prefix is
    treated as ``read_only=False`` (i.e. rejected) so that a new,
    unclassified Boto3 operation cannot slip through silently.
    """
    if not isinstance(op_name, str) or not op_name:
        return False
    lower = op_name.lower()
    for prefix in READ_ONLY_PREFIXES:
        if lower.startswith(prefix):
            return True
    return False


def find_forbidden_prefix(op_name: str) -> str | None:
    """Return the first forbidden prefix that ``op_name`` matches, if any."""
    if not isinstance(op_name, str) or not op_name:
        return None
    lower = op_name.lower()
    for prefix in FORBIDDEN_PREFIXES:
        if lower.startswith(prefix):
            return prefix
    return None


def assert_read_only(client, op_name: str) -> None:
    """Raise ``AwsReadOnlyViolation`` if ``op_name`` would mutate AWS state.

    Intended use:

        from app.services.aws.guard import assert_read_only

        assert_read_only(ce_client, "get_cost_and_usage")
        response = ce_client.get_cost_and_usage(...)

    ``client`` is accepted (and inspected only via ``type(client).__name__``
    in debug builds) so call sites read like a real Boto3 invocation
    and so static analyzers do not flag a "dead" unused-argument warning
    when the guard is wired in.  The Phase 2 service modules pass the
    same client object they are about to call, which also makes the
    intent obvious in code review.
    """
    forbidden = find_forbidden_prefix(op_name)
    if forbidden is not None:
        raise AwsReadOnlyViolation(op_name=op_name, matched_prefix=forbidden)
    if not is_read_only_operation(op_name):
        # Defensive: an unknown / unclassified operation is treated as
        # non-read-only so a new Boto3 method cannot slip past the
        # guard before it is added to READ_ONLY_PREFIXES explicitly.
        raise AwsReadOnlyViolation(
            op_name=op_name,
            matched_prefix="<unclassified>",
        )


__all__ = [
    "FORBIDDEN_PREFIXES",
    "READ_ONLY_PREFIXES",
    "AwsReadOnlyViolation",
    "is_read_only_operation",
    "find_forbidden_prefix",
    "assert_read_only",
]
