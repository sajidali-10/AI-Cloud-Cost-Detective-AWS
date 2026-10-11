"""Phase 7B runtime closure regression tests.

These tests pin the two defects caught during live browser
acceptance:

1.  ``NameError: get_default_session_factory is not defined`` raised
    by /aws/optimization/{capabilities,recommendations,summary}.
    The cause was a missing import in ``app.api.aws_optimization``.
    The fix adds the import; this test exercises the actual
    optimization module end-to-end and asserts the import is
    present and the route handlers do not raise ``NameError``.

2.  Frontend mounting of ``AccountSelector`` in the application
    shell.  See ``frontend/src/tests/phase7b-runtime-closure.test.tsx``
    for the frontend side.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app.api import aws_optimization  # noqa: F401 — verify importable
from app.services.aws import session_factory


def test_optimization_module_imports_session_factory():
    """Pin the top-level import so the NameError cannot recur silently.

    The temporary correction that retained the missing
    ``from app.services.aws.session_factory import get_default_session_factory``
    import is the canonical fix.  This test pins the import so a
    future refactor cannot drop it without breaking the test.
    """
    assert hasattr(aws_optimization, "get_default_session_factory")
    # Both references resolve to the same underlying callable.
    assert callable(aws_optimization.get_default_session_factory)
    assert aws_optimization.get_default_session_factory.__module__ == (
        session_factory.get_default_session_factory.__module__
    )


def test_optimization_route_handlers_callable_without_name_error(monkeypatch):
    """The three route handlers must not raise NameError when invoked.

    We patch the underlying AWS-touching dependencies (identity,
    enumerate_all_services, CloudWatch batch, optimizer) so the
    handlers can run end-to-end inside the test process without
    touching AWS.  Any NameError inside the handler would propagate
    out of the function call and fail this test.
    """
    from app.api import aws_optimization as opt_mod
    from app.services.optimization_engine import (
        build_capabilities,
        build_recommendations,
        build_summary,
    )

    fake_identity = {
        "account": "111111111111",
        "arn": "arn:aws:iam::111111111111:role/X",
        "user_id": "AROA",
        "region": "us-east-1",
    }

    monkeypatch.setattr(
        opt_mod,
        "get_caller_identity",
        lambda region, session=None: fake_identity,
    )
    monkeypatch.setattr(opt_mod, "enumerate_all_services", lambda region: {})
    monkeypatch.setattr(
        opt_mod, "_fetch_utilization",
        lambda region, days, phase1_services: {},
    )
    monkeypatch.setattr(opt_mod, "build_capabilities", build_capabilities)
    monkeypatch.setattr(opt_mod, "build_recommendations", build_recommendations)
    monkeypatch.setattr(opt_mod, "build_summary", build_summary)

    from app.db.models import AppUser

    fake_user = AppUser(
        id=1,
        email="x@local.invalid",
        password_hash="",
        display_name="x",
        role="ADMIN",
        is_active=True,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )

    # /api/aws/optimization/capabilities
    caps = opt_mod.get_optimization_capabilities(
        region="us-east-1",
        account_id="all",
        _user=fake_user,
        db=None,
    )
    assert isinstance(caps, dict)

    # /api/aws/optimization/recommendations
    recs = opt_mod.get_optimization_recommendations(
        region="us-east-1",
        days=30,
        account_id="all",
        _user=fake_user,
        db=None,
    )
    assert isinstance(recs, dict)
    assert "status" in recs

    # /api/aws/optimization/summary
    summary = opt_mod.get_optimization_summary(
        region="us-east-1",
        days=30,
        account_id="all",
        _user=fake_user,
        db=None,
    )
    assert isinstance(summary, dict)


def test_session_factory_default_factory_resolves():
    """Process-wide default factory is wired and not None."""
    factory = session_factory.get_default_session_factory()
    assert factory is not None
    # Cleanup.
    session_factory.reset_default_session_factory()
