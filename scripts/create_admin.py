#!/usr/bin/env python3
"""Bootstrap administrator creation script — Phase 5A.

This is the only supported way to create the first application
admin.  It is intentionally NOT run on application startup: the
Phase 5A spec forbids automatic default-credential creation.

Behavior:

* Refuses to run when ``AUTH_ENABLED=false`` — creating an admin
  with auth disabled would be meaningless and dangerous.
* Refuses to create a user with a well-known default email
  (``admin@admin``, ``admin@localhost``, ``admin@example.com``)
  or password (``admin``, ``admin123``, ``password``).
* Prompts for email, display_name, and password (with confirmation)
  using :mod:`getpass`.  Passwords are never echoed.
* Rejects duplicate emails with a non-zero exit code and a clear
  message; idempotent re-runs are safe.
* Uses the same :class:`AuthService` the API uses, so the new
  admin immediately works for the first login.

Usage (inside the backend container)::

    docker compose exec backend python scripts/create_admin.py

Optional flags:

* ``--email`` / ``--display-name`` / ``--password`` for non-
  interactive use in CI.  ``--password`` reads from
  ``ADMIN_BOOTSTRAP_PASSWORD`` env var if not passed.
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

# Make ``app`` importable when this script is invoked directly.
_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent / "backend"
sys.path.insert(0, str(_BACKEND))

from app.core.config import get_settings  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.services.auth_service import (  # noqa: E402
    AuthService,
    UserAlreadyExists,
    normalise_email,
)


# Well-known default credentials we will never create.  These
# match the Phase 5A spec explicitly.
_FORBIDDEN_DEFAULT_EMAILS = frozenset({
    "admin@admin",
    "admin@localhost",
    "admin@example.com",
    "admin@example.org",
    "admin@test.com",
    "root@localhost",
})

_FORBIDDEN_DEFAULT_PASSWORDS = frozenset({
    "",
    "admin",
    "admin123",
    "password",
    "changeme",
    "letmein",
    "test",
})


def _prompt(label: str, *, default: str | None = None, hidden: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    prompt = f"{label}{suffix}: "
    if hidden:
        value = getpass.getpass(prompt)
    else:
        value = input(prompt)
    if not value and default:
        return default
    return value


def _check_default_credentials(email: str, password: str) -> str | None:
    norm = normalise_email(email)
    if norm in _FORBIDDEN_DEFAULT_EMAILS:
        return (
            f"refusing to create admin with default email {norm!r}; "
            "use a unique operator email"
        )
    if password.lower() in _FORBIDDEN_DEFAULT_PASSWORDS:
        return (
            "refusing to create admin with a default / weak password; "
            "choose a unique password of at least 12 characters"
        )
    if len(password) < 12:
        return (
            "password must be at least 12 characters (Argon2id cost "
            "is wasted on trivially-short passwords)"
        )
    return None


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create the first application admin (Phase 5A).",
    )
    parser.add_argument("--email", help="admin email (prompted if omitted)")
    parser.add_argument(
        "--display-name",
        help="admin display name (prompted if omitted)",
    )
    parser.add_argument(
        "--password",
        help=(
            "admin password (prompted if omitted; "
            "falls back to ADMIN_BOOTSTRAP_PASSWORD env var)"
        ),
    )
    parser.add_argument(
        "--role",
        default="ADMIN",
        choices=("ADMIN",),
        help="role (Phase 5A only supports ADMIN here)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(argv) if argv is not None else sys.argv[1:])

    # Load settings BEFORE any DB work so the validator fires if
    # auth is supposed to be enabled but the secret is missing.
    try:
        settings = get_settings()
    except Exception as exc:  # pragma: no cover — defensive
        print(f"error: failed to load settings: {type(exc).__name__}", file=sys.stderr)
        return 2

    if not settings.auth_enabled:
        print(
            "error: AUTH_ENABLED is false.  Bootstrap admin creation "
            "is only meaningful when authentication is enabled.  Set "
            "AUTH_ENABLED=true (and provide a strong JWT_SECRET) in "
            "your environment, then re-run this script.",
            file=sys.stderr,
        )
        return 2

    # Collect credentials (prompted unless provided).
    email = (args.email or _prompt("admin email")).strip()
    if not email:
        print("error: email is required", file=sys.stderr)
        return 2

    display_name = args.display_name or _prompt("display name")
    if not display_name:
        print("error: display name is required", file=sys.stderr)
        return 2

    if args.password:
        password = args.password
    elif os.environ.get("ADMIN_BOOTSTRAP_PASSWORD"):
        password = os.environ["ADMIN_BOOTSTRAP_PASSWORD"]
    else:
        pw1 = _prompt("password", hidden=True)
        if not pw1:
            print("error: password is required", file=sys.stderr)
            return 2
        pw2 = _prompt("confirm password", hidden=True)
        if pw1 != pw2:
            print("error: passwords do not match", file=sys.stderr)
            return 2
        password = pw1

    rejection = _check_default_credentials(email, password)
    if rejection is not None:
        print(f"error: {rejection}", file=sys.stderr)
        return 2

    # Create the admin via the AuthService so hashing and audit
    # logging go through the same code paths as the API.
    db = SessionLocal()
    try:
        auth = AuthService(db=db)
        user = auth.create_user(
            email=email,
            password=password,
            display_name=display_name,
            role=args.role,
        )
    except UserAlreadyExists as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()

    print(f"ok: created admin user_id={user.id} email={user.email} role={user.role}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
