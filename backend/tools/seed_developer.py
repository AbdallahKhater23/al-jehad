"""Mint the root DEVELOPER account. Idempotent, credential-free by default.

Why this is a tool and not an endpoint
--------------------------------------
Every other account in this application is created through the API, which is what makes an
administrator able to hand out access at all. The root tier is the one account that must not
be: an administrator who could create one could create it for themselves, and the tier exists
precisely to be unreachable from below. So the account is minted here, deliberately, by
somebody who already has shell access to the deployment - and every API path that could write
a ``users`` row refuses the role by name (``security.refuse_developer_role``) instead of
relying on a validation range happening not to include it.

Usage
-----
    # the password comes from the environment, and is never an argument
    DEVELOPER_PASSWORD='...' python backend/tools/seed_developer.py

    # or let the tool invent one and print it once, to be stored in a password manager
    python backend/tools/seed_developer.py --generate

    # rotate the credential of an account that already exists (revokes its live tokens)
    DEVELOPER_PASSWORD='...' python backend/tools/seed_developer.py --rotate-password

    # a different id or name, and a machine-readable answer for a deploy script
    DEVELOPER_PASSWORD='...' python backend/tools/seed_developer.py --id 309010401073 --json

What it will not do
-------------------
* It will not write a default password. There is no ``--password`` flag on purpose: an
  argument lands in shell history, in ``ps`` output and in a CI log, and a default credential
  in a seeder is a backdoor with a known key.
* It will not overwrite a credential that already exists unless ``--rotate-password`` says so.
  Re-running the seed is safe, and a deploy pipeline that runs it on every release cannot
  silently restore a password an operator has since rotated.
* It will not promote an existing business account into the root band.

Exit codes
----------
    0  the account exists and the credential is in place (created, rotated, or already set)
    1  refused or failed - nothing was written, or the answer is in the output above

Notes
-----
The id is a 64-bit value (default ``309010401073``) and that is safe rather than merely large:
``users.id`` is ``TEXT PRIMARY KEY`` and every consumer parses it with Python's
arbitrary-precision ``int``, so no schema change is needed and no AUTOINCREMENT sequence is
touched - the sequences in this database belong to ``attendance_logs``, not to ``users``.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import string
import sys
from pathlib import Path

_BACKEND_DIR = str(Path(__file__).resolve().parent.parent)
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

import database  # noqa: E402 - the bootstrap above has to run first
import developer  # noqa: E402
import migrations  # noqa: E402
from fastapi import HTTPException  # noqa: E402

#: The alphabet for a generated password. No ambiguous glyphs (``0``/``O``, ``1``/``l``): this
#: value is read off a screen and typed into a console exactly once.
_ALPHABET = string.ascii_letters + string.digits + "!@#%^&*-_=+"

#: Long enough that the deployment's own policy (``security.validate_password_strength``) is
#: satisfied by construction rather than by luck, and short enough to be typed.
GENERATED_LENGTH = 24


def generate_password(length: int = GENERATED_LENGTH) -> str:
    """A password from ``secrets``, not ``random``: this one protects the whole deployment."""
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))


def _prepare_schema() -> None:
    """Bring the database up to the current schema before writing to it.

    The seed is run on a fresh deployment as often as on a live one, so it must not depend on
    the application having started once. ``migrations.initialize`` is the same idempotent entry
    point the app's own startup uses, which is what makes "run it in any order" true.
    """
    with database.db(write=True) as conn:
        migrations.initialize(conn)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="seed_developer",
        description="Create the root DEVELOPER account (idempotent).",
    )
    parser.add_argument(
        "--id",
        dest="user_id",
        default=developer.DEVELOPER_ID_DEFAULT,
        help=f"the account id; must be in the root band (default {developer.DEVELOPER_ID_DEFAULT})",
    )
    parser.add_argument("--name", default=developer.DEVELOPER_NAME_DEFAULT)
    parser.add_argument("--email", default=None)
    parser.add_argument("--phone", default=None)
    parser.add_argument(
        "--password-env",
        default="DEVELOPER_PASSWORD",
        help="the environment variable holding the password (default DEVELOPER_PASSWORD)",
    )
    parser.add_argument(
        "--generate",
        action="store_true",
        help="invent a password, print it once, and store only its hash",
    )
    parser.add_argument(
        "--rotate-password",
        action="store_true",
        help="reset the credential of an existing account (this revokes its live tokens)",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable answer")
    parser.add_argument(
        "--skip-migrations",
        action="store_true",
        help="do not run migrations first; only correct if this database is already current",
    )
    args = parser.parse_args(argv)

    generated = False
    if args.generate:
        password = generate_password()
        generated = True
    else:
        password = os.environ.get(args.password_env) or ""
        if not password:
            print(
                f"refusing to seed: set the password in ${args.password_env}, or pass "
                "--generate to have one invented (and printed once).",
                file=sys.stderr,
            )
            return 1

    try:
        if not args.skip_migrations:
            _prepare_schema()
        result = developer.seed_developer_account(
            password=password,
            user_id=args.user_id,
            name=args.name,
            email=args.email,
            phone=args.phone,
            rotate_password=args.rotate_password,
            actor=f"{os.environ.get('USER') or os.environ.get('USERNAME') or 'operator'}"
            f"@{os.uname().nodename if hasattr(os, 'uname') else 'host'}"
            ":tools/seed_developer.py",
        )
    except ValueError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    except HTTPException as exc:
        # Raised by ``hash_password`` when the supplied password fails the deployment's own
        # policy. Reported verbatim: the operator needs the rule, not a generic failure.
        print(f"refused: {exc.detail}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps({**result, "generated": generated}, indent=2))
    else:
        state = (
            "created"
            if result["created"]
            else ("credential rotated" if result["rotated"] else "already present, left as it is")
        )
        print(f"root account {result['id']} ({result['role']}): {state}")
        if not result["created"] and not result["rotated"]:
            print(
                "  the stored credential was not changed - pass --rotate-password to reset it",
            )
    if generated:
        # Printed last, and only when it was invented here: this is the one moment the value
        # exists outside the hash, and there is nowhere to recover it from afterwards.
        print("\nPASSWORD (store this now; it is not recoverable):")
        print(f"  {password}")
    return 0


if __name__ == "__main__":  # pragma: no cover - the entry point
    raise SystemExit(main())
