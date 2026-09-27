#!/usr/bin/env python
"""Mint one long-lived bearer token per roster account, ahead of a load run.

WHY THIS EXISTS
---------------
A 30-day access token (``config.ACCESS_TOKEN_EXPIRE_DAYS``) means a worker's phone holds a
session across days, so opening the app in the morning never calls ``/auth/login`` - and
therefore never pays bcrypt, the ~0.21 s of single-core CPU that caps this deployment at
~4.75 logins/s. A load test that wants to measure *that* steady state has to start the way a
phone does: already signed in. This tool does the signing in **once, out of band**, and writes
the tokens back into the roster so the Locust run never touches ``/auth/login``.

    cd backend
    LOCUST_HOST=https://al-jehad-production.up.railway.app \
      python tools/mint_tokens.py --roster /path/locust_roster50.json

Writes ``<roster>.tokens.json`` (override with ``--out``) - the same entries plus ``token``
and ``expires_at``. The locustfile reads ``token`` and skips the login entirely.

THE LOGIN LIMITER IS THE REAL COST
----------------------------------
``LOGIN_RATE_LIMIT`` defaults to ``10/minute`` per IP, and this tool runs from one IP, so
minting 50 tokens takes roughly five minutes. That is the deployment protecting itself, not a
fault: 429 is honoured with the server's ``Retry-After`` (or an exponential backoff) rather than
hammered. Run it once and reuse the file for many test runs; the tokens are good for 30 days.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests

LOGIN_PATH = "/api/v1/auth/login"
ME_PATH = "/api/v1/auth/me"


def _login(session: requests.Session, host: str, entry: dict, attempts: int = 6) -> tuple[str | None, str | None, str | None]:
    """One account's login. Returns ``(token, expires_at, error)`` and never raises for a 429.

    A 429 is the per-IP limiter, so it is waited out rather than counted as a failure: honour
    ``Retry-After`` when the server sends one, otherwise back off exponentially. Any other
    status is a real error and is returned as the third value.
    """
    payload = {
        "user_id": str(entry["user_id"]),
        "email_or_phone": str(entry.get("login") or entry.get("email_or_phone") or ""),
        "password": str(entry["password"]),
    }
    for attempt in range(attempts):
        try:
            response = session.post(f"{host}{LOGIN_PATH}", json=payload, timeout=30)
        except requests.RequestException as exc:
            return None, None, f"network: {exc}"
        if response.status_code == 200:
            body = response.json()
            token = body.get("token") or body.get("access_token")
            if not token:
                return None, None, "login answered 200 with no token"
            return token, body.get("expires_at"), None
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            wait = float(retry_after) if retry_after and retry_after.isdigit() else 2.0 * (attempt + 1)
            time.sleep(min(wait, 30.0))
            continue
        return None, None, f"{response.status_code}: {response.text[:120]}"
    return None, None, "rate limiter never cleared"


def _verify(session: requests.Session, host: str, token: str) -> bool:
    """Prove a minted token authenticates a real request, with no password."""
    try:
        response = session.get(
            f"{host}{ME_PATH}", headers={"Authorization": f"Bearer {token}"}, timeout=30
        )
    except requests.RequestException:
        return False
    return response.status_code == 200


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("LOCUST_HOST"),
                        help="deployment base URL (default: LOCUST_HOST)")
    parser.add_argument("--roster", required=True, help="the roster JSON to mint tokens for")
    parser.add_argument("--out", default=None, help="where to write the token roster")
    parser.add_argument("--verify", action="store_true",
                        help="call /auth/me with the first token to prove it works")
    args = parser.parse_args(argv)

    if not args.host:
        print("no --host and no LOCUST_HOST set")
        return 1
    host = args.host.rstrip("/")
    roster_path = Path(args.roster)
    roster = json.loads(roster_path.read_text(encoding="utf-8"))
    if not isinstance(roster, list) or not roster:
        print(f"no entries in {roster_path}")
        return 1

    session = requests.Session()
    minted: list[dict] = []
    failures: list[str] = []
    for index, entry in enumerate(roster, start=1):
        token, expires_at, error = _login(session, host, entry)
        if token:
            row = dict(entry)
            row["token"] = token
            if expires_at:
                row["expires_at"] = expires_at
            minted.append(row)
            print(f"  {index:>3}/{len(roster)}  {entry.get('user_id')}  ok  (expires {expires_at or '?'})")
        else:
            failures.append(str(entry.get("user_id")))
            print(f"  {index:>3}/{len(roster)}  {entry.get('user_id')}  FAIL  {error}")

    out = Path(args.out) if args.out else roster_path.with_name(roster_path.name + ".tokens.json")
    out.write_text(json.dumps(minted, indent=2), encoding="utf-8")
    print(f"\ntoken roster : {out} ({len(minted)} accounts)")
    if failures:
        print(f"no token for : {failures}")

    if args.verify and minted:
        ok = _verify(session, host, minted[0]["token"])
        print(f"verify       : /auth/me with the first token -> {'ok' if ok else 'FAILED'}")
        if not ok:
            return 2
    return 0 if not failures else 2


if __name__ == "__main__":
    sys.exit(main())
