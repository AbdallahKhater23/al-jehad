"""Is the Railway deployment's own self-test clean? One command, one exit code.

WHY THIS EXISTS
---------------
The application publishes its startup self-test at ``GET /api/v1/readiness`` - the same checks
the gate runs before it serves traffic - and answers **200** even when checks are failing, because
"the app is up" and "the app is configured" are different questions. A deployment therefore looks
healthy to every uptime monitor while carrying advisories that mean a feature nobody will notice
is missing: a worker whose phone never rings, an auto-close that quietly stands down, an edge the
app does not trust.

Reading those by hand is a curl and a judgement call about which of the three tiers matter. This
does that in one run and exits non-zero when anything is failing, so "is the deploy clean?" has
an answer a script can act on:

    0   every check passed
    1   an advisory is failing - the app serves, and a feature is not what it says it is
    2   a fatal check is failing - the gate refused to serve, or the deployment is degraded
    3   the endpoint could not be read (down, DNS, not JSON, or behind a login)

The public route is what this reads by default, and it carries only a verdict - check names,
tiers and two booleans - because a probe anybody can call must not be a map of the host. The
*reasons* live on the admin route, which needs an administrator's token; pass ``--token`` to
fetch it and the failing checks are printed with their detail. No token, no guesses: the script
says which checks are failing and where to read why.

Usage::

    python deploy/railway/verify_readiness.py
    python deploy/railway/verify_readiness.py --origin https://al-jehad-production.up.railway.app
    python deploy/railway/verify_readiness.py --token "$(cat /tmp/admin.jwt)"

The token is read from ``$ADMIN_TOKEN`` when it is set, so it does not have to appear in a shell
history or a process list.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping

#: This deployment's backend, as the Cloudflare Worker is pointed at it. Named here rather than
#: required, so the common run is one command with nothing to mistype; ``--origin`` overrides it
#: for a tunnel or a laptop.
DEFAULT_ORIGIN = "https://al-jehad-production.up.railway.app"

PUBLIC_PATH = "/api/v1/readiness"
ADMIN_PATH = "/api/v1/admin/readiness"

CLEAN = 0
ADVISORY_FAILED = 1
FATAL_FAILED = 2
UNREADABLE = 3


@dataclass(frozen=True)
class Verdict:
    """What the deployment said about itself, without interpreting it yet."""

    origin: str
    status: int
    ok: bool
    degraded: bool
    total: int
    failing: tuple[str, ...]
    tiers: Mapping[str, str]

    @property
    def fatal(self) -> tuple[str, ...]:
        return tuple(name for name in self.failing if self.tiers.get(name) == "fatal")

    @property
    def advisory(self) -> tuple[str, ...]:
        return tuple(name for name in self.failing if self.tiers.get(name) != "fatal")

    @property
    def exit_code(self) -> int:
        if self.fatal:
            return FATAL_FAILED
        if self.advisory:
            return ADVISORY_FAILED
        return CLEAN


def verdict_from(origin: str, status: int, payload: Any) -> Verdict:
    """Read the verdict out of the body the endpoint publishes.

    The shape is the app's own (``readiness._verdict``): ``checks`` keyed by name, each with
    ``ok`` and ``tier``. A body that is not that - an edge's HTML error page, a proxy's JSON, a
    route that moved - is refused here rather than half-read, because "0 failing checks" and
    "this is not a readiness report" must never be the same answer.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("checks"), dict):
        raise ValueError("the body is not a readiness report (no 'checks' mapping)")

    checks: dict[str, Any] = payload["checks"]
    tiers: dict[str, str] = {}
    failing: list[str] = []
    for name, check in checks.items():
        if not isinstance(check, dict):
            raise ValueError(f"check {name!r} is not an object")
        tiers[str(name)] = str(check.get("tier") or "")
        if not check.get("ok"):
            failing.append(str(name))
    return Verdict(
        origin=origin,
        status=int(status),
        ok=bool(payload.get("ok")),
        degraded=bool(payload.get("degraded")),
        total=len(checks),
        failing=tuple(sorted(failing)),
        tiers=tiers,
    )


def _get(url: str, *, token: str | None, timeout: float) -> tuple[int, bytes]:
    """One GET, with the token only when there is one. Returns ``(status, body)``."""
    request = urllib.request.Request(url, method="GET")
    request.add_header("accept", "application/json")
    # A browser-ish agent: a bare urllib client can meet an edge's bot rules, and a health check
    # refused for being a script says nothing about the app (see verify_live.py).
    request.add_header(
        "user-agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0 Safari/537.36",
    )
    if token:
        request.add_header("authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as answer:  # noqa: S310 - an operator's URL
            return int(answer.status), answer.read()
    except urllib.error.HTTPError as exc:
        # 503 is this app's own answer for a failing *fatal* check, and it carries the report.
        return int(exc.code), exc.read()


def _details(origin: str, *, token: str, timeout: float) -> dict[str, str]:
    """The failing checks' ``detail`` from the admin route, or ``{}`` when it cannot be read.

    Deliberately best-effort: the verdict is already in hand, and an expired token must not turn
    a real answer into a crash. The detail is the *why*, and it is admin-only because it names
    absolute paths.
    """
    try:
        status, body = _get(f"{origin}{ADMIN_PATH}", token=token, timeout=timeout)
        payload = json.loads(body.decode("utf-8", "replace"))
    except Exception:
        return {}
    checks = (payload.get("admin") or {}).get("checks") if isinstance(payload, dict) else None
    if not isinstance(checks, list):
        return {}
    return {
        str(check.get("name")): str(check.get("detail") or "")
        for check in checks
        if isinstance(check, dict) and check.get("name") and not check.get("ok")
    }


def report(verdict: Verdict, details: Mapping[str, str]) -> None:
    print(f"origin  : {verdict.origin}")
    print(
        f"verdict : ok={verdict.ok} degraded={verdict.degraded} "
        f"({verdict.total} checks, {len(verdict.failing)} failing)"
    )
    if not verdict.failing:
        print("clean   : no check is failing; every advertised feature is configured")
        return
    for name in verdict.failing:
        tier = verdict.tiers.get(name) or "unknown"
        print(f"FAIL    : {name} (tier {tier})")
        detail = details.get(name)
        if detail:
            print(f"          {detail}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--origin", default=DEFAULT_ORIGIN, help="the backend's https address")
    parser.add_argument(
        "--token",
        default=os.environ.get("ADMIN_TOKEN"),
        help="an administrator's JWT, to print the failing checks' reasons (default: $ADMIN_TOKEN)",
    )
    parser.add_argument("--timeout", type=float, default=20.0, help="seconds per request")
    args = parser.parse_args(argv)

    origin = str(args.origin).rstrip("/")
    try:
        status, body = _get(f"{origin}{PUBLIC_PATH}", token=None, timeout=float(args.timeout))
    except Exception as exc:
        print(f"cannot read {origin}{PUBLIC_PATH}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return UNREADABLE

    try:
        verdict = verdict_from(origin, status, json.loads(body.decode("utf-8", "replace")))
    except ValueError as exc:
        print(
            f"{origin}{PUBLIC_PATH} answered {status} and is not a readiness report: {exc}",
            file=sys.stderr,
        )
        return UNREADABLE

    details = _details(origin, token=str(args.token), timeout=float(args.timeout)) if args.token else {}
    report(verdict, details)
    if verdict.failing and not args.token:
        print(f"note    : reasons are admin-only; re-run with --token to read them from {ADMIN_PATH}")
    return verdict.exit_code


if __name__ == "__main__":  # pragma: no cover - an operator's script
    raise SystemExit(main())
