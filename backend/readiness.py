"""Readiness reporting and the fail-closed startup gate.

Two surfaces, two trust levels:

* ``GET /api/v1/readiness``            public  - the verdict only: whether the
  deployment is ready, and ``ok``/``tier`` for each check. HTTP 200 when ready,
  **503 when not**, so ``curl -f`` works on the status code alone. The check
  *names* are public vocabulary; nothing else is, because anything this route
  returns is world-readable and a check's ``detail`` string routinely embeds an
  absolute path (the database, the backup directory, the liveness model, the
  biometric directories).
* ``GET /api/v1/admin/readiness``      admin   - the full picture, behind
  ``admin_only``: per-check ``detail``/``value``, the code fingerprint, the
  secret-key fingerprint, the schema/migration inventory, the absolute database
  path, PID/uptime and the row counts; ``?deep=1`` also runs an integrity check.

Readiness self-reporting is *not* a place for self-attestation: a public
``hardened_build_live: true`` is exactly the kind of constant this module exists
to refuse, so the public route answers with evidence-derived booleans and hides
the internals behind the admin surface.

The governing rule: **readiness is evidence read from the running process, never
a constant.** A probe that answers "hardened: true" because someone typed that
string is worse than no probe, because it reports health while the old
unauthenticated process still serves traffic.

``run_startup_gate`` is the same registry used to *refuse to serve* traffic when
a critical check fails. Three tiers, because blocking boot is a blunt instrument:

* FATAL      - serving traffic would be actively wrong; raise (uvicorn exits 3).
* REPAIRABLE - a documented, additive repair is attempted, then re-checked.
* ADVISORY   - boot anyway, report ``degraded``; a legitimate deployment may
  differ from ours (for example serving the SPA from nginx instead).
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import sqlite3
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

import database
import migrations
import notifications
import push
import schema_guard
import shift_hours
import shift_windows
from config import PROJECT_ROOT, settings
from security import CurrentUser, admin_only, create_access_token, decode_access_token, hash_password, verify_password

TIER_FATAL = "fatal"
TIER_REPAIRABLE = "repairable"
TIER_ADVISORY = "advisory"

#: Checks no override may bypass. A missing signing key has no safe degraded mode
#: (the app would either refuse everything or sign forgeable tokens), and nothing
#: works without a database.
NON_OVERRIDABLE = frozenset({"secret_key_configured", "database_reachable"})

_STARTED_AT = time.time()

router = APIRouter()


@dataclass
class Check:
    name: str
    tier: str
    ok: bool
    detail: str = ""
    value: object = None
    repaired: bool = False

    def public(self) -> dict:
        """The anonymous projection of a check: the verdict, never the plumbing.

        ``detail`` is written for an operator reading a startup log - it names
        files, versions and error text - so it stays on the admin-only route.
        """
        return {"ok": self.ok, "tier": self.tier}

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "tier": self.tier,
            "ok": self.ok,
            "detail": self.detail,
            "value": self.value,
            "repaired": self.repaired,
        }


@dataclass
class GateReport:
    checks: list[Check] = field(default_factory=list)
    repaired: list[dict] = field(default_factory=list)
    override_used: bool = False
    duration_ms: float = 0.0

    @property
    def failed(self) -> list[Check]:
        return [check for check in self.checks if not check.ok]

    @property
    def fatal_failures(self) -> list[Check]:
        return [check for check in self.checks if not check.ok and check.tier == TIER_FATAL]

    @property
    def advisory_failures(self) -> list[Check]:
        return [check for check in self.checks if not check.ok and check.tier == TIER_ADVISORY]

    @property
    def degraded(self) -> bool:
        return bool(self.advisory_failures or self.override_used)

    def as_dict(self) -> dict:
        return {
            "ready": not self.fatal_failures,
            "degraded": self.degraded,
            "override_used": self.override_used,
            "duration_ms": round(self.duration_ms, 1),
            "failed_checks": [check.name for check in self.failed],
            "repaired": self.repaired,
            "checks": [check.as_dict() for check in self.checks],
        }


# ---------------------------------------------------------------------------
# individual checks
# ---------------------------------------------------------------------------
def _open(db_path: Path | None = None, *, read_only: bool = False) -> sqlite3.Connection:
    # Resolved past a directory junction/symlink for the same reason ``database.connect``
    # resolves: the test harness's stable path points at a fresh database each reset, and a
    # WAL connection opened through the old link can still contend with the new file. See
    # ``database.resolve_path``.
    target = Path(os.path.realpath(str(Path(db_path or settings.database_path))))
    if read_only:
        return sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    return sqlite3.connect(str(target))


def _check_secret_key(ctx: dict) -> Check:
    return Check(
        "secret_key_configured",
        TIER_FATAL,
        True,
        "signing key present",
        {"fingerprint": settings.secret_key_fingerprint, "algorithm": settings.jwt_algorithm},
    )


def _check_database_reachable(ctx: dict) -> Check:
    try:
        conn = _open(ctx.get("db_path"), read_only=True)
        try:
            conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return Check("database_reachable", TIER_FATAL, False, f"cannot open the database: {exc}")
    return Check("database_reachable", TIER_FATAL, True, "database opened read-only")


def _check_database_writable(ctx: dict) -> Check:
    ok, error = database.writable(db_path=ctx.get("db_path"))
    return Check(
        "database_writable",
        TIER_FATAL,
        ok,
        "write lock acquired and released" if ok else f"write probe failed: {error}",
    )


def _check_schema_version(ctx: dict) -> Check:
    try:
        conn = _open(ctx.get("db_path"), read_only=True)
        try:
            live = migrations.current_version(conn)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return Check("schema_current", TIER_FATAL, False, f"cannot read schema version: {exc}")

    expected = migrations.SCHEMA_VERSION
    if live == expected:
        return Check("schema_current", TIER_FATAL, True, f"schema version {live}", {"live": live, "expected": expected})
    if live > expected:
        return Check(
            "schema_current",
            TIER_FATAL,
            False,
            f"database is at schema {live} but the running code only knows {expected}: code and data "
            "have diverged, most likely an older build was deployed against a migrated database",
            {"live": live, "expected": expected},
        )
    return Check(
        "schema_current",
        TIER_FATAL,
        False,
        f"database is at schema {live}, expected {expected}: migrations have not been applied",
        {"live": live, "expected": expected},
    )


def _check_migrations(ctx: dict) -> Check:
    try:
        conn = _open(ctx.get("db_path"), read_only=True)
        try:
            pending = migrations.pending_migrations(conn)
            applied = migrations.applied_versions(conn)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return Check("migrations_all_applied", TIER_FATAL, False, f"cannot read schema_migrations: {exc}")
    ok = not pending
    detail = "all migrations applied" if ok else f"pending: {[version for version, _ in pending]}"
    return Check(
        "migrations_all_applied",
        TIER_FATAL,
        ok,
        detail,
        {"applied": sorted(applied), "pending": [version for version, _ in pending]},
    )


def _check_schema_version_ahead(ctx: dict) -> Check:
    """Whether this build's ``SCHEMA_VERSION`` is ahead of the migrations the database recorded.

    Advisory, and deliberately *not* the check that decides whether the server starts. The two
    fatal-tier schema checks above - ``schema_current`` and ``migrations_all_applied`` - are the
    ones that refuse to serve when the code expects columns the database has not created yet,
    and they are right to. This check states the same fact as a warning instead of a refusal,
    because there are two places the fatal verdict is easy to miss:

    * the readiness surfaces run *every* check whether or not the startup gate blocked on one
      of them, so an operator reading ``/admin/readiness`` sees the divergence named here too;
    * a deployment started with ``STARTUP_OVERRIDE_REASON`` logs its fatal failures and then
      serves anyway, and "the code is ahead of the data" then reads as one warning among the
      degraded checks rather than as a line in a list of failures already scrolled past.

    It reads ``applied_versions`` rather than ``current_version`` on purpose: the question is
    whether the migration ``SCHEMA_VERSION`` names is really recorded as applied, which is not
    quite the same as the newest number in the ledger being high enough. A gap below the newest
    migration is the fatal ``migrations_all_applied``'s business, not this check's - this one
    only ever warns when the code is ahead of everything the database has applied.
    """
    try:
        conn = _open(ctx.get("db_path"), read_only=True)
        try:
            applied = migrations.applied_versions(conn)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        # An unreadable ledger is the fatal ``migrations_all_applied``'s to report; a warning
        # must never become a second failure stacked on the same problem.
        return Check("schema_version_ahead", TIER_ADVISORY, True, f"could not be checked: {exc}")

    expected = migrations.SCHEMA_VERSION
    database_version = max(applied) if applied else 0
    missing = [version for version, _, _ in migrations.MIGRATIONS if version not in applied]
    value = {
        "code": expected,
        "database": database_version,
        "applied": sorted(applied),
        "missing": missing,
    }
    if database_version >= expected:
        # Level with or ahead of the code: not this check's subject. A database *ahead* of the
        # running code is what the fatal ``schema_current`` reports on its own.
        return Check(
            "schema_version_ahead",
            TIER_ADVISORY,
            True,
            f"the running code expects schema {expected}; the database has applied up to {database_version}",
            value,
        )
    return Check(
        "schema_version_ahead",
        TIER_ADVISORY,
        False,
        f"the running code expects schema {expected} but the database has applied only up to "
        f"{database_version} (missing {missing}): the migrations a newer build needs have not "
        f"run, so the columns it reads may not exist. This is warned here and refused at startup "
        f"by schema_current unless STARTUP_OVERRIDE_REASON is set",
        value,
    )


def _check_schema_drift(ctx: dict) -> Check:
    report = schema_guard.enforce(db_path=ctx.get("db_path"))
    ctx.setdefault("repaired", []).extend(report.repaired)
    if report.error:
        return Check("schema_drift", TIER_FATAL, False, report.error)
    blocking = report.blocking
    if blocking:
        return Check(
            "schema_drift",
            TIER_FATAL,
            False,
            "; ".join(item.detail for item in blocking),
            report.as_dict(),
        )
    return Check(
        "schema_drift",
        TIER_FATAL,
        True,
        f"live schema matches baseline + migrations ({len(report.drift)} informational difference(s))",
        {"severity": report.severity, "differences": [item.name for item in report.drift]},
        repaired=bool(report.repaired),
    )


def _check_shift_rules(ctx: dict) -> Check:
    try:
        conn = _open(ctx.get("db_path"))
        try:
            row = conn.execute("SELECT COUNT(*) FROM shift_rules WHERE id = 1").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return Check("shift_rules_present", TIER_REPAIRABLE, False, f"shift_rules unreadable: {exc}")
    if row and row[0]:
        return Check("shift_rules_present", TIER_REPAIRABLE, True, "shift rules row present")
    return Check(
        "shift_rules_present",
        TIER_REPAIRABLE,
        False,
        "no shift_rules row; the documented defaults will be re-seeded",
    )


def _repair_shift_rules(ctx: dict) -> bool:
    try:
        conn = _open(ctx.get("db_path"))
        try:
            conn.execute(
                "INSERT OR IGNORE INTO shift_rules (id, updated_at) VALUES (1, ?)",
                (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),),
            )
            conn.commit()
        finally:
            conn.close()
        return True
    except sqlite3.Error:
        return False


def _check_site_windows(ctx: dict) -> Check:
    """Every site's stored clock-in window must be a time the application can parse.

    Advisory, not blocking, because the punch path is deliberately total: an unparseable value
    falls back to the global rule rather than refusing a worker's arrival (see
    ``shift_windows``). That fallback is what makes this check necessary - without it, a typo in
    one site's hours is invisible. The site simply stops having its own window, everyone there
    is measured against the company default, and the only symptom is late flags appearing on a
    shift nobody changed.

    The admin API refuses these values where they are typed and the column has a CHECK behind
    it, so a bad value here means it arrived some other way: a hand-edited database, a restored
    dump, or an import that bypassed both.
    """
    try:
        conn = _open(ctx.get("db_path"))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT site_name, clock_in_window_start, clock_in_window_end, site_timezone "
                "FROM construction_sites"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return Check("site_clock_in_windows", TIER_ADVISORY, True, f"could not be checked: {exc}")

    # The rows are re-fetched with names so the problem can be attributed to a site: an
    # operator reading "07" needs to know which site to open.
    broken: list[str] = []
    for row in rows:
        broken.extend(shift_windows.window_problems(row))

    if broken:
        return Check(
            "site_clock_in_windows",
            TIER_ADVISORY,
            False,
            "these sites' windows cannot be applied, so the global rule is used instead: "
            + ", ".join(broken),
            {"sites": len(rows), "unusable": broken},
        )
    return Check(
        "site_clock_in_windows",
        TIER_ADVISORY,
        True,
        f"{len(rows)} site(s) checked; every configured window is usable",
        {"sites": len(rows)},
    )


def _stored_shift_rules(ctx: dict) -> dict | None:
    """The stored shift rules over the shipped defaults, or ``None`` when unreadable.

    Lifted out of the two overtime checks rather than written twice, because they are two
    verdicts about the *same* pair of settings and reading them from two places is how they
    would come to disagree.
    """
    try:
        conn = _open(ctx.get("db_path"))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT * FROM shift_rules WHERE id = 1").fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None

    values = dict(migrations.DEFAULT_SHIFT_RULES)
    if row is not None:
        for key in values:
            try:
                if row[key] is not None:
                    values[key] = row[key]
            except (IndexError, KeyError):
                continue
    return values


def _day_end_fields(day_end: dict) -> dict:
    """The day-end verdict, as the handful of figures both checks report."""
    return {
        "notify_hours": day_end["notify_hours"],
        "regular_hours": day_end["regular_hours"],
        "close_at_paid_hours": day_end["close_at_paid_hours"],
        "close_defers": day_end["close_defers"],
        "day_ended_by": day_end["day_ended_by"],
    }


def _check_overtime_alert(ctx: dict) -> Check:
    """Can the overtime crossing alert actually fire with these rules?

    Advisory, and never a repair: the settings are consistent with each other and with the
    policy - what disagrees is the *pair* of them. A shift the automatic close has ended
    cannot be observed crossing the alert line, so the pair only works when the alert is
    reachable before the close acts. That is decided in ``shift_hours.day_end_rules``, which
    also resolves the conflict by standing the close down when the alert sits above the paid
    day; the one remaining unreachable case is the alert set *on* the paid day, where there
    is nothing to observe and alerting on every full day would page the manager for ordinary
    work.

    That is a configuration an operator chose (or accepted by default), so it is reported
    rather than corrected - and reported here, because the alternative is finding out months
    later that no shift was ever flagged.
    """
    values = _stored_shift_rules(ctx)
    if values is None:
        return Check(
            "overtime_alert_reachable", TIER_ADVISORY, True, "could not be checked: the shift rules could not be read"
        )

    day_end = shift_hours.day_end_rules(values)
    reachable = day_end["alert_reachable"]
    return Check(
        "overtime_alert_reachable",
        TIER_ADVISORY,
        reachable,
        day_end["detail"],
        _day_end_fields(day_end) | {"alert_reachable": reachable},
    )


def _check_overtime_close_deferred(ctx: dict) -> Check:
    """Is the automatic close standing down for the overtime workflow?

    Advisory, and not a fault: the reconciliation the day-end rules perform is exactly this,
    and it is the right answer when the alert line sits above the paid day. But it means the
    setting an operator typed - *close the shift at 8 paid hours* - no longer does that, and
    nothing else in the application would say so: the watcher keeps running, the console
    keeps showing the switch as on, and shifts simply never get closed. Reported here for the
    same reason the unreachable alert is: a rule that has quietly stopped acting is
    indistinguishable from a rule with nothing to act on.
    """
    values = _stored_shift_rules(ctx)
    if values is None:
        return Check(
            "overtime_close_deferred", TIER_ADVISORY, True, "could not be checked: the shift rules could not be read"
        )

    day_end = shift_hours.day_end_rules(values)
    return Check(
        "overtime_close_deferred",
        TIER_ADVISORY,
        not day_end["close_defers"],
        day_end["detail"],
        _day_end_fields(day_end),
    )


def _check_startup_override_acknowledged(ctx: dict) -> Check:
    """Has somebody accepted the last forced start, and said why?

    ``STARTUP_OVERRIDE_REASON`` is the hatch that lets a deployment serve past a failing
    self-test, and by design it demands a reason: an unattributed escape hatch is
    indistinguishable from a permanent bypass. What nothing demanded was the *other* half -
    nobody had to accept it. The alert could be marked read, which records that somebody
    looked, and the audit trail had no acknowledgement at all, so "who decided this was
    acceptable, and why" was answerable only by asking around.

    Advisory, read-only, and answered by ``POST /admin/notifications/{id}/acknowledge``. It
    fails while the newest ``startup_override`` alert has no acknowledgement and passes as soon
    as one is written; a deployment that has never been forced up has nothing to answer and
    passes too, with a detail that says so rather than staying silent.

    The *newest* alert is the one that counts: alerts are keyed per reason, so an
    acknowledgement given for last month's reason must not answer this month's override.
    """
    try:
        conn = _open(ctx.get("db_path"), read_only=True)
        try:
            conn.row_factory = sqlite3.Row
            row = notifications.newest_of_kind(conn, notifications.KIND_STARTUP_OVERRIDE)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return Check(
            "startup_override_acknowledged",
            TIER_ADVISORY,
            False,
            f"could not read the forced-start record: {type(exc).__name__}: {exc}",
            {"measured": False},
        )
    if row is None:
        return Check(
            "startup_override_acknowledged",
            TIER_ADVISORY,
            True,
            "no forced start has been recorded on this deployment",
            {"forced_start": False, "acknowledged": True},
        )
    try:
        payload = json.loads(row["payload"]) if row["payload"] else {}
    except (TypeError, ValueError):  # pragma: no cover - a payload this module wrote
        payload = {}
    value = {
        "forced_start": True,
        "alert_id": int(row["id"]),
        "reason": payload.get("reason"),
        "override_until": payload.get("override_until"),
        "forced_at": row["created_at"],
        "acknowledged": bool(row["acknowledged_at"]),
        "acknowledged_by": row["acknowledged_by"],
        "acknowledged_at": row["acknowledged_at"],
        "note": row["acknowledgement_note"],
    }
    if row["acknowledged_at"]:
        return Check(
            "startup_override_acknowledged",
            TIER_ADVISORY,
            True,
            f"the forced start of {row['created_at']} was accepted by {row['acknowledged_by']} "
            f"on {row['acknowledged_at']}",
            value,
        )
    return Check(
        "startup_override_acknowledged",
        TIER_ADVISORY,
        False,
        f"this deployment was forced up past a failing self-test on {row['created_at']} "
        f"(reason given: {payload.get('reason') or 'not recorded'}) and no administrator has "
        f"accepted it: POST /api/v1/admin/notifications/{int(row['id'])}/acknowledge with a note, "
        "or remove STARTUP_OVERRIDE_REASON and fix the check that failed",
        value,
    )


def _check_worker_push(ctx: dict) -> Check:
    """Can this deployment reach a worker who is not looking at the app?

    Advisory, and it is the *opposite* of a fault: the worker inbox records every event
    whatever this says, and an operator who has not set up Web Push has lost a convenience,
    not correctness. It is reported because the difference is invisible from the outside -
    "the worker is notified when their shift runs long" is true of the inbox and false of the
    phone, and the only place that distinction can be seen is here and
    ``GET /worker/me/push``.

    Switched off on purpose (``PUSH_ENABLED=0``) is a pass, not a warning: an operator who
    decided nobody's phone should ring has got what they asked for. Missing keys or a missing
    ``pywebpush`` is not - that is the state where the app quietly cannot do what the previous
    sentence says it can.
    """
    if not settings.push_enabled:
        return Check(
            "worker_push_delivery",
            TIER_ADVISORY,
            True,
            "push is switched off (PUSH_ENABLED=0); the worker inbox still records every event",
            {"available": False, "enabled": False},
        )
    usable, reason = push.transport_available()
    return Check(
        "worker_push_delivery",
        TIER_ADVISORY,
        usable,
        reason if usable else f"a worker with the app closed is not notified: {reason}",
        {
            "available": usable,
            "enabled": True,
            #: Booleans, never the values: readiness output is logged, mailed and pasted into
            #: issues, and a private key in any of those is a leaked private key.
            "public_key_configured": bool(settings.vapid_public_key),
            "private_key_configured": bool(settings.vapid_private_key),
        },
    )


def _check_worker_notice_backlog(ctx: dict) -> Check:
    """The channel's *output*: notices that passed the push window still undelivered.

    ``worker_push_delivery`` above answers "is this deployment configured to push", which is a
    reading of the settings and says nothing about whether anything is arriving. A push service
    that has started answering 401, a subscription table emptied by a well-meaning script, a
    phone whose permission was revoked - each of those leaves the settings perfectly valid and
    the channel silent. The only evidence of that is the missing ``delivered_at`` stamps, and
    this is where it is read without anybody opening a database.

    Advisory, and deliberately not a second copy of the configuration check: when this
    deployment does not intend to push (no key pair, ``PUSH_ENABLED=0``) the backlog is the
    expected state, and the honest verdict is a pass whose detail says so. The check has teeth
    exactly when the deployment is trying to deliver and cannot - which is the case nobody
    sees from the outside, because from there a quiet channel and a quiet workforce look the
    same.

    Read-only. An operator polling readiness must not be made to write, and the counts here
    are the same ones the alert row and ``push.stranded_notices`` report.
    """
    usable, reason = push.transport_available()
    if not usable:
        return Check(
            "worker_notice_backlog",
            TIER_ADVISORY,
            True,
            f"not measured: {reason}; undelivered notices are expected and the inbox is the record",
            {"measured": False, "reason": reason},
        )
    try:
        conn = _open(ctx.get("db_path"), read_only=True)
        try:
            # ``push`` reads its rows by column name, the way every query in this application
            # does; a bare connection hands back tuples.
            conn.row_factory = sqlite3.Row
            reading = push.stranded_notices(conn)
        finally:
            conn.close()
    except Exception as exc:  # pragma: no cover - a probe must report, never raise
        return Check(
            "worker_notice_backlog",
            TIER_ADVISORY,
            False,
            f"could not read the push backlog: {type(exc).__name__}: {exc}",
            {"measured": False},
        )
    stranded = reading["notices"]
    detail = (
        "no worker notice has been left behind by the push channel"
        if not stranded
        else (
            f"{stranded} worker notification(s) passed the {reading['window_minutes']}-minute push "
            f"window undelivered (oldest {reading['age']}); "
            f"{reading['no_device']} have no live device, {reading['with_device']} have one and were "
            "refused or failed. They are still in the workers' inboxes; the phones are not ringing"
        )
    )
    return Check(
        "worker_notice_backlog",
        TIER_ADVISORY,
        not stranded,
        detail,
        {
            "measured": True,
            "window_minutes": reading["window_minutes"],
            "notices": stranded,
            "workers": reading["workers"],
            "oldest": reading["oldest"],
            "age_seconds": reading["age_seconds"],
            "no_device": reading["no_device"],
            "with_device": reading["with_device"],
            "attempted": reading["attempted"],
            "kinds": reading["kinds"],
        },
    )


def _check_push_endpoint_allowlist(ctx: dict) -> Check:
    """Every host the push allowlist names must be one a subscription could actually use.

    An endpoint is accepted only if its host is on ``PUSH_ENDPOINT_HOSTS``, so an entry there
    that the policy itself refuses is **silently dead**: a worker's browser subscribes and
    reports success, the row is stored, and nothing ever arrives - which reads as a vendor
    outage rather than as a typo in a config file. The entries that cannot work are named here
    at startup, where somebody can still fix them, rather than by a worker who missed an
    overtime alert.

    The check and the request path ask the *same* function (``push.validate_host``), so the two
    cannot drift into disagreeing about what "usable" means.

    Advisory, not fatal: the worker inbox records every event whether or not a phone can be
    reached, and no deployment should refuse to serve payroll over a mistyped push host.
    """
    hosts = push.configured_hosts()
    unusable: list[str] = []
    for host in hosts:
        try:
            push.validate_host(host, allowed=(host,))
        except ValueError as exc:
            unusable.append(f"{host} ({exc})")
    if unusable:
        return Check(
            "push_endpoint_allowlist",
            TIER_ADVISORY,
            False,
            "these PUSH_ENDPOINT_HOSTS entries can never be used: " + "; ".join(unusable),
            {"hosts": len(hosts), "unusable": len(unusable)},
        )
    return Check(
        "push_endpoint_allowlist",
        TIER_ADVISORY,
        True,
        f"{len(hosts)} push service host(s) accepted; every other host is refused",
        {"hosts": len(hosts), "unusable": 0},
    )


def _check_journal_mode(ctx: dict) -> Check:
    try:
        conn = _open(ctx.get("db_path"))
        try:
            mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return Check("journal_mode", TIER_REPAIRABLE, False, f"cannot read journal_mode: {exc}")
    ok = mode == "wal"
    return Check(
        "journal_mode",
        TIER_REPAIRABLE,
        ok,
        f"journal_mode={mode}" + ("" if ok else " (readers can block the writer; will enable WAL)"),
        {"journal_mode": mode},
    )


def _repair_journal_mode(ctx: dict) -> bool:
    result = database.configure(db_path=ctx.get("db_path"), repair=True)
    return str(result.get("journal_mode", "")).lower() == "wal"


def _check_password_hashing(ctx: dict) -> Check:
    sample = "readiness-probe-passphrase"
    try:
        hashed = hash_password(sample)
        ok = verify_password(sample, hashed) and not verify_password(sample + "x", hashed)
    except Exception as exc:  # pragma: no cover - defensive
        return Check("password_hashing_ok", TIER_FATAL, False, f"password hashing is broken: {exc}")
    return Check(
        "password_hashing_ok",
        TIER_FATAL,
        ok,
        "bcrypt round-trip verified" if ok else "the stored hash did not verify against its own password",
    )


def _check_jwt_roundtrip(ctx: dict) -> Check:
    try:
        token, expires = create_access_token("readiness-probe", "worker", 0, ttl_hours=0.01)
        claims = decode_access_token(token)
        ok = claims.get("sub") == "readiness-probe" and claims.get("role") == "worker"
    except Exception as exc:  # pragma: no cover - defensive
        return Check("jwt_roundtrip_ok", TIER_FATAL, False, f"token sign/verify failed: {exc}")
    return Check(
        "jwt_roundtrip_ok",
        TIER_FATAL,
        ok,
        "token signed and verified with the configured secret",
        {"algorithm": settings.jwt_algorithm},
    )


def iter_api_routes(node, prefix: str = ""):
    """Every ``APIRoute`` reachable from ``node``, with its effective path.

    This exists because ``app.routes`` is **not** a flat list any more: an included
    router is stored as a wrapper (``_IncludedRouter``) that holds the original router
    and the prefix it was included with, and the endpoints are only materialised
    elsewhere. Walking it as a flat list therefore found *no* ``/admin/`` route at all -
    which made ``auth_enforced_on_admin_routes``, a FATAL-tier check, pass vacuously:
    it reported "0 admin routes guarded, 0 missing" and the gate believed the whole
    admin surface had been verified when nothing had been looked at.

    The traversal is written against attributes rather than the private classes: an
    ``APIRoute`` yields its path, a wrapper exposes ``original_router`` /
    ``include_context``, and anything with ``routes`` (a plain ``APIRouter``, a
    ``Mount``) is walked through. A future FastAPI that flattens again simply hits the
    first branch.
    """
    from fastapi.routing import APIRoute

    for route in getattr(node, "routes", []) or []:
        if isinstance(route, APIRoute):
            yield prefix + (getattr(route, "path", "") or ""), route
            continue
        inner = getattr(route, "original_router", None)
        if inner is not None:
            context = getattr(route, "include_context", None)
            yield from iter_api_routes(inner, prefix + str(getattr(context, "prefix", "") or ""))
            continue
        yield from iter_api_routes(route, prefix)


def _check_admin_routes_guarded(ctx: dict) -> Check:
    app = ctx.get("app")
    if app is None:  # pragma: no cover - only when called without an app
        return Check("auth_enforced_on_admin_routes", TIER_FATAL, True, "no app supplied; skipped")

    guarded = 0
    missing: list[str] = []
    for path, route in iter_api_routes(app):
        if "/admin/" not in path:
            continue
        enforced = any(
            getattr(dependency.call, "_auth_marker", None) == "require_role"
            and set(getattr(dependency.call, "_allowed_roles", ())) <= {"admin", "head_admin"}
            for dependency in route.dependant.dependencies
        )
        if enforced:
            guarded += 1
        else:
            missing.append(f"{'|'.join(sorted(route.methods))} {path}")
    # "Nothing was found" is not the same as "nothing is wrong": a traversal that stops
    # working must fail the gate, not quietly vouch for a surface it never looked at.
    ok = bool(guarded) and not missing
    if ok:
        detail = f"{guarded} admin route(s) guarded by require_role"
    elif not guarded and not missing:
        detail = (
            "no admin routes could be enumerated from the app, so this check verified "
            "nothing; the route traversal is broken"
        )
    else:
        detail = "admin routes without an admin guard dependency: " + ", ".join(missing)
    return Check(
        "auth_enforced_on_admin_routes",
        TIER_FATAL,
        ok,
        detail,
        {"guarded": guarded, "missing": missing},
    )


# ---------------------------------------------------------------------------
# the whole API surface, not only /admin
# ---------------------------------------------------------------------------
#: Methods and paths that answer **without a session**, each beside the reason that is
#: safe. Keyed by ``"<METHOD> <path>"`` rather than by path alone, so adding a verb to a
#: path that is public today (``DELETE /api/v1/branding``, say) does not inherit the
#: exemption by accident - it has to be declared and explained here, in front of whoever
#: reviews the diff.
PUBLIC_ROUTES: dict[str, str] = {
    "POST /api/v1/auth/login": (
        "sign-in: it is how a session is obtained, so it cannot require one. Rate-limited by "
        "IP and by account, and it answers one generic refusal for both an unknown user and "
        "a wrong password."
    ),
    "GET /api/v1/branding": (
        "the company's own name, lines and mark: the sign-in screen needs them before anybody "
        "has signed in, and the same strings are already on every printed sheet. Setting them "
        "stays admin-only."
    ),
    "GET /api/v1/branding/logo": "the mark the public branding read above points at.",
    "GET /api/v1/enroll/{token}": (
        "self-service enrollment: the invite token in the path is the credential, so a worker "
        "who has no account yet can open the link they were handed."
    ),
    "POST /api/v1/enroll/{token}": "the same invite token; submits the capture.",
    "POST /api/v1/enroll/{token}/register": "the same invite token; claims the link.",
    "GET /api/v1/q/{token}": (
        "one-tap clock link: the link token in the path is the credential. Reusing one does "
        "not authenticate anybody else."
    ),
    "POST /api/v1/q/{token}": "the same link token; records the punch.",
    "GET /api/v1/readiness": (
        "the deployment's verdict, for the load balancer and ``curl -f``: booleans and check "
        "*names* only, never a detail string (those embed absolute paths)."
    ),
    "GET /api/v1/status": "the liveness probe: ``{\"status\": \"active\"}`` and nothing else.",
}

#: Routes that **refuse an anonymous caller themselves**, inside the handler, so they cannot
#: carry a role guard. Declared separately from the list above because they make the opposite
#: promise: ``PUBLIC_ROUTES`` says "this answers without a session", this says "this decides
#: for itself". A test can hold each list to its own claim.
SELF_GATED_ROUTES: dict[str, str] = {
    "GET /api/v1/metrics": (
        "``METRICS_TOKEN`` compared with ``compare_digest`` when configured, otherwise an "
        "admin JWT. It cannot use a ``Depends`` role guard because a scrape token is one of "
        "the two accepted credentials."
    ),
    "GET /metrics": "the same handler, mounted at the bare path a scrape config assumes.",
}

#: Routes that answer without a session **and without any data of ours**: each serves an HTML
#: file from ``frontend/``. Kept apart from the list above so that half stays a list of
#: endpoints that deliberately parse a request.
PAGE_ROUTES: frozenset[str] = frozenset({"GET /", "GET /enroll/{token}", "GET /q/{token}"})


def _check_api_routes_authorised(ctx: dict) -> Check:
    """Every route is either guarded by a role, or declared public with a reason recorded.

    ``auth_enforced_on_admin_routes`` answers "*is the admin surface guarded*"; it says
    nothing about the other eighty-odd routes, which is where the interesting mistakes live.
    An endpoint added under ``/api/v1/worker/`` with no ``Depends`` at all is readable by
    anybody who can reach the port, and until now the gate would not have said a word about
    it. This closes that: the whole surface is enumerated and each route must be
    **accounted for** - carrying a ``require_role`` guard, or declared above (``PUBLIC_ROUTES``
    / ``SELF_GATED_ROUTES`` / ``PAGE_ROUTES``) with the reason beside it.

    Three failure modes beyond the obvious unguarded route, all of which this repository has
    already been bitten by somewhere:

    * the enumeration finding nothing - a traversal that breaks must fail the gate, not
      report ``0 guarded, 0 missing`` and vouch for a surface it never looked at;
    * a **stale** entry - a route renamed or removed while its exemption stayed behind, which
      reads as "public on purpose" for a path nothing serves any more, and quietly covers for
      the new path that is now unaccounted for;
    * a **redundant** entry - a route that is both exempted and guarded, meaning the
      exemption is doing nothing but misinforming the next reader.
    """
    app = ctx.get("app")
    if app is None:  # pragma: no cover - only when called without an app
        return Check("api_routes_authorised", TIER_FATAL, True, "no app supplied; skipped")

    seen: set[str] = set()
    guarded: list[str] = []
    public: list[str] = []
    pages: list[str] = []
    unaccounted: list[str] = []
    for path, route in iter_api_routes(app):
        enforced = any(
            getattr(dependency.call, "_auth_marker", None) == "require_role"
            for dependency in route.dependant.dependencies
        )
        for method in sorted(route.methods or []):
            key = f"{method} {path}"
            seen.add(key)
            if enforced:
                guarded.append(key)
            elif key in PAGE_ROUTES:
                pages.append(key)
            elif key in PUBLIC_ROUTES:
                if PUBLIC_ROUTES[key].strip():
                    public.append(key)
                else:
                    unaccounted.append(f"{key} (declared public with no reason)")
            elif key in SELF_GATED_ROUTES:
                if SELF_GATED_ROUTES[key].strip():
                    public.append(key)
                else:
                    unaccounted.append(f"{key} (self-gated with no reason)")
            else:
                unaccounted.append(key)

    declared = set(PUBLIC_ROUTES) | set(SELF_GATED_ROUTES) | set(PAGE_ROUTES)
    stale = sorted(declared - seen)
    redundant = sorted(set(guarded) & declared)
    ok = bool(seen) and not unaccounted and not stale and not redundant

    if ok:
        detail = (
            f"{len(guarded)} route(s) guarded by require_role, {len(public)} declared open "
            f"with a reason, {len(pages)} page route(s)"
        )
    elif not seen:
        detail = (
            "no routes could be enumerated from the app, so this check verified nothing; the "
            "route traversal is broken"
        )
    else:
        problems = []
        if unaccounted:
            problems.append("routes with no guard and no exemption: " + ", ".join(unaccounted))
        if stale:
            problems.append("exemptions for routes that no longer exist: " + ", ".join(stale))
        if redundant:
            problems.append("exemptions on routes that are already guarded: " + ", ".join(redundant))
        detail = "; ".join(problems)

    return Check(
        "api_routes_authorised",
        TIER_FATAL,
        ok,
        detail,
        {
            "guarded": len(guarded),
            "public": len(public),
            "pages": len(pages),
            "unaccounted": unaccounted,
            "stale_exemptions": stale,
            "redundant_exemptions": redundant,
        },
    )


def _check_no_unprefixed_admin_routes(ctx: dict) -> Check:
    app = ctx.get("app")
    # Effective paths, so this sees a route that was mounted without ``/api/v1`` - which
    # is exactly what it is looking for, and which a top-level walk cannot see at all.
    duplicated = sorted(
        {path for path, _ in iter_api_routes(app) if path.startswith("/admin")}
    ) if app is not None else []
    return Check(
        "no_unprefixed_duplicate_routes",
        TIER_FATAL,
        not duplicated,
        "the admin surface exists only under /api/v1"
        if not duplicated
        else f"the admin API is also mounted without the version prefix: {duplicated}",
        {"duplicates": duplicated},
    )


def _check_static_mounts(ctx: dict) -> Check:
    app = ctx.get("app")
    paths = {getattr(route, "path", "") for route in getattr(app, "routes", [])} if app is not None else set()
    present = {"/static"} & paths
    frontend_present = "/" in paths
    ok = bool(present) and frontend_present
    return Check(
        "static_mounts_present",
        TIER_ADVISORY,
        ok,
        "SPA and legacy /static mounts are registered"
        if ok
        else "a static mount is missing; valid when the SPA is served by a reverse proxy",
        {"mounts": sorted(paths & {"/", "/static"})},
    )


def _check_api_docs_disabled(ctx: dict) -> Check:
    app = ctx.get("app")
    if app is None:  # pragma: no cover
        return Check("api_docs_disabled", TIER_ADVISORY, True, "no app supplied; skipped")
    exposed = [name for name in ("docs_url", "redoc_url", "openapi_url") if getattr(app, name, None)]
    return Check(
        "api_docs_disabled",
        TIER_ADVISORY,
        not exposed,
        "interactive docs and the schema are disabled"
        if not exposed
        else f"the API schema is public through {exposed}",
        {"exposed": exposed, "enabled_by_setting": settings.enable_api_docs},
    )


def _check_network_policy(ctx: dict) -> Check:
    """Whether the network layer is doing what the deployment believes it is.

    Two different verdicts live here, and they are deliberately not the same one:

    * A policy that **will not build** (an allowlist entry that is not an address, an admin
      CORS wildcard) is FATAL. The gate built from it matches nothing, i.e. it fails closed -
      which is safe, and also means every administrator is locked out of the payroll console
      by a typo. Better to refuse to start and say which line to fix, with the existing
      startup override as the escape hatch.
    * A policy that is **valid but weak** - no allowlist, ``TRUSTED_PROXIES`` trusting every
      peer, ``*`` worker origins, ``unsafe-inline`` in the document CSP, or a forwarded
      header that arrived from a peer nobody declared - is advisory. Each one is a decision an
      operator may have made on purpose (a tunnel, a LAN deployment), so the job here is to
      state it plainly rather than to overrule it.
    """
    import netguard

    active = netguard.policy()
    value = active.describe()

    if active.problems:
        return Check(
            "network_policy",
            TIER_FATAL,
            False,
            "network policy is invalid, so the admin gate refuses every client: "
            + "; ".join(active.problems),
            value,
        )

    misuse = netguard.forward_misuse()
    if int(misuse.get("count") or 0) > 0:
        return Check(
            "network_policy",
            TIER_ADVISORY,
            False,
            (
                f"{int(misuse['count'])} request(s) arrived with X-Forwarded-For from an "
                f"undeclared proxy (last peer: {misuse.get('last_peer')}); until TRUSTED_PROXIES "
                "names that proxy, the admin allowlist is checking the proxy's address rather "
                "than the administrator's"
            ),
            {**value, "forward_misuse": misuse},
        )

    detail = (
        f"admin gate {'on for ' + ','.join(active.admin_paths) if active.admin_gate_enabled else 'off'}; "
        f"{len(active.worker_origins)} worker origin(s), {len(active.admin_origins)} admin origin(s); "
        f"security headers {'on' if active.security_headers else 'off'}"
    )
    if active.remarks:
        detail += ". " + ". ".join(active.remarks)
    return Check("network_policy", TIER_ADVISORY, True, detail, value)


#: Every column in the database that holds text a person typed, with the rule it is now
#: held to. Ordered so the tables an administrator reads first come first; the scan is
#: bounded per table (``_TEXT_SCAN_LIMIT``) because this runs at every startup and inside
#: the readiness endpoint, and a check that costs seconds is a check somebody disables.
#: ``None`` as a limit means "whatever the module's own constant says".
TEXT_SURFACES: tuple[tuple[str, str, tuple[tuple[str, str, int | None], ...]], ...] = (
    (
        "users",
        "id",
        (
            ("name", "identifier", None),
            ("email", "contact", None),
            ("phone", "contact", None),
        ),
    ),
    ("construction_sites", "site_name", (("site_name", "identifier", None),)),
    (
        "worker_notes",
        "id",
        (("subject", "prose", 0), ("body", "prose", 0)),  # 0 -> the notes settings
    ),
    (
        "attendance_logs",
        "id",
        (
            ("site_name", "identifier", None),
            ("flag_reason", "prose", None),
        ),
    ),
    (
        "admin_notifications",
        "id",
        (("title", "prose", None), ("body", "prose", None)),
    ),
    (
        "enrollment_invites",
        "id",
        (("name", "identifier", None), ("note", "prose", None)),
    ),
    ("quick_links", "id", (("note", "prose", None),)),
)

#: How many rows of one table this check will look at. The prefilter is a GLOB, so the query
#: is a scan whatever it says; a table larger than this reports the bound rather than the
#: whole truth, and says so.
_TEXT_SCAN_LIMIT = 500


def _check_stored_text_hygiene(ctx: dict) -> Check:
    """Text already in the database that the validators would now refuse.

    This is the honest half of what input validation can promise. From the moment
    ``textguard`` is in the write path, a *new* name, site, note or reason cannot carry
    markup - but a row written by an earlier version can, and it is exactly the row an
    attacker would have planted on purpose. The read path escapes it (see
    ``tests/test_frontend_xss.py``, which renders these values through the real frontend),
    so this is not an open door; it is a list of things an operator may want to look at,
    and the one place where "was this row always like that?" can be answered.

    Advisory, and never repaired automatically: rewriting a worker's name or a note somebody
    wrote is a decision for a person, not for a startup check.
    """
    import textguard
    from config import settings

    def _length(profile: str, column: str, limit: int | None) -> int:
        if limit:
            return limit
        if profile == "contact":
            return textguard.MAX_CONTACT
        if column == "site_name":
            return textguard.MAX_SITE_NAME
        return textguard.MAX_LABEL

    def _refusal(profile: str, column: str, value, length: int) -> str | None:
        try:
            if profile == "identifier":
                textguard.identifier(value, field=column, max_length=length, allow_empty=True)
            elif profile == "contact":
                textguard.contact(value, field=column)
            else:
                textguard.prose(value, field=column, max_length=length, allow_empty=True)
        except ValueError as exc:
            return str(exc).split(".")[0]
        return None

    offenders: list[str] = []
    scanned = 0
    try:
        conn = _open(ctx.get("db_path"), read_only=True)
        try:
            for table, id_column, columns in TEXT_SURFACES:
                selected = [id_column, *(column for column, _, _ in columns)]
                prefilter = " OR ".join(f"{column} GLOB '*[<>&]*'" for column, _, _ in columns)
                try:
                    rows = conn.execute(
                        f"SELECT {', '.join(selected)} FROM {table} "
                        f"WHERE {prefilter} LIMIT {_TEXT_SCAN_LIMIT}"
                    ).fetchall()
                except sqlite3.Error:
                    # A table this check expects and the schema does not have yet is the
                    # migration check's problem, not this one's.
                    continue
                scanned += len(rows)
                for row in rows:
                    # Positional, not by name: ``_open`` hands back plain tuples, and asking
                    # a tuple for a column name is how this check would report itself as
                    # broken instead of reporting the row it exists to find.
                    fields = dict(zip(selected, row))
                    for column, profile, limit in columns:
                        value = fields[column]
                        if value in (None, ""):
                            continue
                        if limit == 0:
                            limit = (
                                settings.notes_max_subject_chars
                                if column == "subject"
                                else settings.notes_max_body_chars
                            )
                        reason = _refusal(profile, column, value, _length(profile, column, limit))
                        if reason is not None:
                            offenders.append(
                                f"{table}.{column} (row {fields[id_column]}): {reason}"
                            )
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return Check("stored_text", TIER_ADVISORY, True, f"stored text not scanned: {exc}")

    value = {
        "rows_scanned": scanned,
        "scan_limit_per_table": _TEXT_SCAN_LIMIT,
        "offenders": offenders[:10],
        "offender_count": len(offenders),
    }
    if not offenders:
        return Check(
            "stored_text",
            TIER_ADVISORY,
            True,
            f"no stored text would be refused by the input rules ({scanned} row(s) with a "
            "tag-like character in a scanned column)",
            value,
        )
    return Check(
        "stored_text",
        TIER_ADVISORY,
        False,
        (
            f"{len(offenders)} stored value(s) would not be accepted today: "
            + "; ".join(offenders[:3])
            + ". These are pre-validation rows - the read path escapes them, so they are not "
            "executed, but they are worth a look before the next report quotes one"
        ),
        value,
    )


def _check_biometric_dirs(ctx: dict) -> Check:
    from main import LOCAL_REFS_DIR, WORKER_PHOTOS_DIR

    problems = []
    for label, directory in (("local_references", LOCAL_REFS_DIR), ("worker_photos", WORKER_PHOTOS_DIR)):
        try:
            Path(directory).mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=directory, prefix=".readiness_", delete=True):
                pass
        except OSError as exc:
            problems.append(f"{label}: {exc}")
    return Check(
        "biometric_dirs_writable",
        TIER_ADVISORY,
        not problems,
        "biometric directories are writable" if not problems else "; ".join(problems),
    )


def _check_biometric_file_naming(ctx: dict) -> Check:
    """Whether any face is still filed under the account id it used to be named by.

    Migration 11 gives every account an immutable id, and ``main.init_db`` renames the
    files already on disk to match. The readers keep a fallback to the old names so that an
    interrupted rename cannot take a worker's clock-in away mid-shift - but that fallback
    is a migration path, not a resting state. While it is in use, a filename still says
    which account a face belongs to, and says it from a number anybody can iterate; zero is
    the only answer that means the change is finished on this deployment.

    Advisory rather than fatal, and it never repairs: deciding what to do with somebody
    else's template - rename it, re-enroll the worker, delete it - is an operator's call.
    """
    try:
        import biometrics

        remaining = biometrics.legacy_files_remaining()
    except Exception as exc:  # noqa: BLE001 - a report must never fail on one check
        return Check("biometric_file_naming", TIER_ADVISORY, True, f"could not be checked: {exc}")
    total = sum(remaining.values())
    return Check(
        "biometric_file_naming",
        TIER_ADVISORY,
        total == 0,
        "every biometric file is named by its account's immutable id"
        if total == 0
        else (
            f"{total} biometric file(s) are still named after their account id; "
            "restart the service to retry the rename, then check again"
        ),
        remaining,
    )


def _check_retention_sweep(ctx: dict) -> Check:
    """Whether the retention policy is actually being enforced, or merely configured.

    This is the check that distinguishes the two. A retention period in ``.env`` with nothing
    running it is the same finding an auditor writes as indefinite retention, and the failure
    is silent by construction - the schedule is in a file, the timer is in a thread, and
    neither leaves evidence when it stops. The evidence that it *is* running is the row the
    sweeper writes at the end of every applied sweep, so this reads it.

    Advisory, never fatal: a deployment that runs ``python -m retention --apply`` from cron
    with ``RETENTION_ENABLED=0`` has no row in this process's first minutes, and a database
    that has just been created has one. Neither is a reason to refuse to serve attendance.

    Overdue is 2x the interval plus the startup delay: one missed pass is noise (a sweep at
    boot, a slow disk), two is a timer that is not running.
    """
    try:
        import retention
    except Exception as exc:  # noqa: BLE001 - a report must never fail on one check
        return Check("retention_sweep", TIER_ADVISORY, True, f"could not be checked: {exc}")

    try:
        last = retention.last_run()
    except Exception as exc:  # noqa: BLE001
        return Check("retention_sweep", TIER_ADVISORY, True, f"could not be checked: {exc}")

    value = {
        "policy": retention.policy().as_dict(),
        "dry_run": bool(settings.retention_dry_run),
        "scheduler": {
            "enabled": bool(settings.retention_enabled),
            "running": retention.watcher_running(),
        },
        "last_run": last,
    }
    if last is None:
        if not settings.retention_enabled:
            # The timer is off and nothing has been recorded from anywhere else. Report it as
            # a decision rather than a fault - but say it, because "off" is the answer that
            # has to be deliberate, and because the alternative is a deployment that believes
            # a retention policy is being enforced by a process that is not running.
            return Check(
                "retention_sweep",
                TIER_ADVISORY,
                True,
                "the in-process sweeper is disabled (RETENTION_ENABLED=0) and no sweep has been "
                "recorded: run 'python -m retention --apply' on a schedule",
                value,
            )
        return Check(
            "retention_sweep",
            TIER_ADVISORY,
            True,
            "no retention sweep has been recorded yet (the first one runs shortly after boot)",
            value,
        )

    overdue_after = 2 * int(settings.retention_interval_seconds) + int(settings.retention_initial_delay_seconds)
    try:
        age = (datetime.now() - datetime.strptime(str(last["started_at"]), "%Y-%m-%d %H:%M:%S")).total_seconds()
    except (TypeError, ValueError):  # pragma: no cover - a hand-edited row
        return Check("retention_sweep", TIER_ADVISORY, True, "the last sweep row has no usable timestamp", value)
    value["seconds_since_last_sweep"] = int(age)
    value["overdue_after_seconds"] = overdue_after

    # ``failures_total`` counts both shapes of failure - an item that could not be removed, and
    # a target that could not run at all - because either one means data is being kept past its
    # period, and the operator's next action is the same: read the compliance event.
    failures = int(last.get("failures_total") or 0)
    if failures:
        return Check(
            "retention_sweep",
            TIER_ADVISORY,
            False,
            f"the last retention sweep reported {failures} failure(s); read the audit event "
            "action=retention_sweep for what could not be removed",
            value,
        )
    if age > overdue_after:
        return Check(
            "retention_sweep",
            TIER_ADVISORY,
            False,
            f"the last retention sweep was {int(age // 3600)}h ago, past the {int(overdue_after // 3600)}h "
            "overdue threshold: data is being retained past its period",
            value,
        )
    return Check(
        "retention_sweep",
        TIER_ADVISORY,
        True,
        f"retention is enforced; the last sweep was {int(age // 60)}m ago and removed "
        f"{int(last.get('deleted_total') or 0)} item(s)",
        value,
    )


def _check_retention_residue(ctx: dict) -> Check:
    """Whether anything is still on disk that the policy says should be gone.

    Counted from the directories, not from the database, which is the entire value of it: a
    face whose row is gone is invisible to a query and perfectly visible to ``os.listdir``.
    The classes are a template for an account that no longer exists, a quarantined legacy
    file, a half-written staging file, and a punch selfie nothing references.

    Advisory: residue is nearly always a *failure to delete* - a file held open by a backup,
    a permissions problem - and the next sweep retries it. What must not happen is that it is
    never mentioned, since "we deleted it" and "we believe we deleted it" differ by exactly
    this count.
    """
    try:
        import retention

        found = retention.residue()
    except Exception as exc:  # noqa: BLE001
        return Check("retention_residue", TIER_ADVISORY, True, f"could not be checked: {exc}")
    if found.get("error"):
        return Check("retention_residue", TIER_ADVISORY, True, f"could not be checked: {found['error']}")

    total = sum(
        int(found.get(key) or 0)
        for key in (
            "biometric_files_for_deactivated_accounts",
            "orphaned_biometric_files",
            "biometric_staging_files",
            "orphaned_punch_photos",
        )
    )
    return Check(
        "retention_residue",
        TIER_ADVISORY,
        total == 0,
        "no biometric files or punch selfies are still on disk past their retention window"
        if total == 0
        else f"{total} file(s) are past their retention window and still on disk: {found.get('listed')}",
        found,
    )


def _check_metrics(ctx: dict) -> Check:
    """Whether this process can be monitored, and whether that is exposed safely.

    Advisory, never fatal, for a reason worth stating: Prometheus metrics are an operational
    extra, and a deployment that has none is *unmonitored*, not broken. Refusing to serve
    attendance because a monitoring library is missing would be the tail wagging the dog.

    What it does report is the thing that actually goes wrong here, which is a **silent**
    gap: a scrape that lands on one of several uvicorn workers reports a fraction of the
    traffic, and an alert threshold tuned to that number fires late or not at all. The mode
    is therefore stated in the check's value rather than left to be discovered.
    """
    import telemetry

    detail = telemetry.describe()
    if not detail["available"]:
        return Check(
            "metrics",
            TIER_ADVISORY,
            True,
            "Prometheus metrics are not installed; GET /metrics answers 501 with the install "
            f"instruction ({detail['import_error']})",
            detail,
        )
    if not detail["enabled"]:
        return Check(
            "metrics",
            TIER_ADVISORY,
            True,
            "metrics are disabled (METRICS_ENABLED=0); GET /metrics answers 404",
            detail,
        )
    if settings.metrics_token:
        return Check(
            "metrics",
            TIER_ADVISORY,
            True,
            "metrics are served at /metrics to the configured scrape token"
            + (
                " (single-process registry; set PROMETHEUS_MULTIPROC_DIR before running more "
                "than one uvicorn worker)"
                if detail["single_process_registry"]
                else f" (multiprocess registry: {detail['multiprocess_dir']})"
            ),
            detail,
        )
    return Check(
        "metrics",
        TIER_ADVISORY,
        True,
        "metrics are served at /metrics to administrators only: no METRICS_TOKEN is set, so a "
        "scrape job cannot authenticate. Set it (or disable metrics) before pointing "
        "Prometheus at this host.",
        detail,
    )


def _check_face_engine(ctx: dict) -> Check:
    """How loaded the face-verification pool is, and whether it is refusing work.

    Advisory, and it never stops the server: a saturated queue is a *load* condition, not a
    broken deployment, and refusing to start because a whole site arrived at once would be
    worse than being slow. What this is for is the question an operator actually asks when
    punches crawl - "what is it doing?" - which the snapshot answers in one line (two
    workers, 64 waiting, twelve refused since boot) instead of leaving it to be guessed at.
    """
    try:
        import face_engine

        snapshot = face_engine.stats()
    except Exception as exc:  # noqa: BLE001 - a report must never fail on one check
        return Check("face_engine", TIER_ADVISORY, True, f"could not be checked: {exc}")
    refused = int(snapshot.get("refused") or 0)
    detail = (
        f"{snapshot.get('in_flight', 0)}/{snapshot.get('capacity', 0)} verifying, "
        f"{snapshot.get('queued', 0)} waiting"
    )
    if refused:
        detail += f", {refused} refused since start"
    return Check("face_engine", TIER_ADVISORY, not snapshot.get("busy"), detail, snapshot)


def _check_face_detector(ctx: dict) -> Check:
    """Which face detector is live, and how many templates it has invalidated.

    Advisory, and it never stops the server - a checkout without the model runs on the
    previous detector, which is correct, just thirty times slower on the detection half of
    every punch. That is worth one line an operator can see.

    The second half is the migration: replacing the detector changed the crop an embedding
    is computed from, so every template written before it has to be re-enrolled. The count
    is the size of that worklist, and the fix is a photograph rather than a config change -
    so it is reported here and acted on through ``/admin/enroll`` (see
    ``biometrics.stale_references``).
    """
    try:
        import face_detector

        described = face_detector.describe()
    except Exception as exc:  # noqa: BLE001 - a report must never fail on one check
        return Check("face_detector", TIER_ADVISORY, True, f"could not be checked: {exc}")

    detail = (
        f"{described['detector']} ({described['pipeline']}) is the face detector"
        if described["available"]
        else f"YuNet is unavailable ({described.get('error') or 'no reason given'}) and "
        "verification is running on the previous detector"
    )
    ok = bool(described["available"])

    try:
        import biometrics

        worklist = biometrics.stale_references()
    except Exception as exc:  # noqa: BLE001
        return Check("face_detector", TIER_ADVISORY, ok, f"{detail}; templates could not be checked: {exc}")

    stale = int(worklist.get("count") or 0)
    if stale:
        detail += (
            f"; {stale} of {worklist.get('enrolled_checked', 0)} enrolled templates were made "
            "by the previous pipeline and need re-enrollment (/api/v1/admin/enroll/needs_reenrollment)"
        )
    return Check("face_detector", TIER_ADVISORY, ok, detail, worklist)


def _check_face_match_band(ctx: dict) -> Check:
    """Whether the pipeline that would really run has decision lines that were measured.

    FATAL, and the only face check here that is. Every other face check is advisory because
    its failure has a correct degraded mode: a missing detector model runs the previous
    crop, which is slower and has *its own* band. This one has no degraded mode at all. The
    lines that turn a distance into approved / review / refused are derived per pipeline
    from measured boundaries (see ``face_detector.MatchBand``), and a build that can run a
    crop without them can only answer in one of two wrong ways - approve against numbers
    measured on a different crop, or refuse every punch for everybody. Refusing to open the
    port until the lines exist is the honest version of "this build cannot verify a face".

    Reported healthy with the derivation attached, because the number an operator will want
    when a review queue moves is not the line but what the line was measured against.
    """
    import face_detector
    import face_engine

    pipeline = face_detector.active_pipeline()
    try:
        band = face_detector.band_for(pipeline)
    except face_detector.UnknownPipelineError as exc:
        return Check(
            "face_match_band",
            TIER_FATAL,
            False,
            f"{exc}; every punch would be refused until the lines are derived (or the detector "
            "model is restored, which selects the previous pipeline and its own band)",
            {"pipeline": pipeline, "model": face_engine.FACE_MODEL},
        )
    return Check(
        "face_match_band",
        TIER_FATAL,
        True,
        f"{pipeline} / {face_engine.FACE_MODEL}: {band.basis()}",
        {
            "pipeline": pipeline,
            "model": face_engine.FACE_MODEL,
            "approve": band.approve,
            "review": band.review,
            "genuine_ceiling": band.genuine_ceiling,
            "impostor_floor": band.impostor_floor,
        },
    )


def _check_calibration_corpus(ctx: dict) -> Check:
    """How far the calibration corpus is from supporting a band derivation.

    Advisory, and deliberately so in both directions: capture being off is a configuration
    a deployment may legitimately keep (the shipped band serves), and a corpus still filling
    is not a fault but a countdown. What must not happen is silence - the provisional band
    was derived from public portraits because nothing said "your own traffic is the better
    corpus", and the operator who asks "has it been a few days yet?" deserves an answer at
    every boot rather than a shell session.

    The numbers are the ones ``corpus.stats()`` computes in the terms the derivation tool
    actually consumes - genuine pairs, impostor pairs, identities - so this check and a
    ``derive_facenet_band.py`` run can never disagree about what the corpus holds.
    """
    import corpus
    from config import settings as live_settings

    if not live_settings.calibration_capture_enabled:
        return Check(
            "calibration_corpus",
            TIER_ADVISORY,
            True,
            "capture is off; set CALIBRATION_CAPTURE_ENABLED=true with CALIBRATION_CORPUS_DIR "
            "on the volume to collect the gate selfies a real band is measured from",
            {"capture_enabled": False},
        )
    try:
        summary = corpus.stats()
    except Exception as exc:  # noqa: BLE001 - an unreadable store is reported, never fatal
        return Check("calibration_corpus", TIER_ADVISORY, True, f"could not be checked: {exc}")

    details = {
        "capture_enabled": True,
        "captures": summary["captures"],
        "labelled": summary["labelled"],
        "unlabelled": summary["unlabelled"],
        "identities": summary["identities"],
        "genuine_pairs": summary["genuine_pairs"],
        "impostor_pairs": summary["impostor_pairs"],
        "can_support_floor": summary["can_support_floor"],
    }
    if summary["can_support_floor"]:
        return Check(
            "calibration_corpus",
            TIER_ADVISORY,
            True,
            f"ready to derive: {summary['labelled']} labelled capture(s) across "
            f"{summary['identities']} identity/ies give {summary['genuine_pairs']} genuine and "
            f"{summary['impostor_pairs']} impostor pair(s) - run "
            "tools/derive_facenet_band.py per docs/RUNBOOK_FACE_BAND.md, then reinstall the "
            "printed band in face_detector.BANDS",
            details,
        )
    return Check(
        "calibration_corpus",
        TIER_ADVISORY,
        True,
        f"collecting: {summary['captures']} capture(s) so far ({summary['labelled']} labelled, "
        f"{summary['unlabelled']} awaiting a label) - a derivation needs at least two capture(s) "
        "per identity and 100 impostor pair(s) across identities; keep the site running "
        "normally and check back in a few days",
        details,
    )


def _check_clock_sanity(ctx: dict) -> Check:
    try:
        conn = _open(ctx.get("db_path"), read_only=True)
        try:
            row = conn.execute("SELECT MAX(timestamp) FROM attendance_logs").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return Check("clock_sanity", TIER_ADVISORY, True, f"no attendance history to compare: {exc}")
    newest = row[0] if row else None
    if not newest:
        return Check("clock_sanity", TIER_ADVISORY, True, "no attendance history yet")
    try:
        newest_dt = datetime.strptime(str(newest), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return Check("clock_sanity", TIER_ADVISORY, True, f"unparseable newest timestamp {newest!r}")
    skew_hours = (newest_dt - datetime.now()).total_seconds() / 3600.0
    ok = skew_hours < 24
    return Check(
        "clock_sanity",
        TIER_ADVISORY,
        ok,
        f"newest attendance record is {skew_hours:.1f}h ahead of this host's clock"
        if not ok
        else "host clock is consistent with the attendance history",
        {"skew_hours": round(skew_hours, 2), "newest": str(newest)},
    )


def _check_database_path(ctx: dict) -> Check:
    """Whether the database sits at the canonical project-root file.

    A **configured** ``DATABASE_PATH`` is not the state this check is looking for. The
    deployment this check was written for points the database at a mounted volume
    (``/data/times.db`` on Railway) by declaring ``DATABASE_PATH`` in the image - so
    "somewhere other than the project root" is the declared design, and reporting it
    degraded makes the report red on every boot for a decision that was made on purpose.
    The accident worth surfacing is the opposite one: a path that arrives from somewhere
    nobody configured - a leftover from an old shell, a stray environment probe - while
    the operator believes the project-root file is in use.
    """
    expected = (PROJECT_ROOT / "times.db").resolve()
    actual = Path(ctx.get("db_path") or settings.database_path).resolve()
    configured = bool((os.environ.get("DATABASE_PATH") or "").strip())
    ok = actual == expected or configured
    return Check(
        "database_path_is_project_root",
        TIER_ADVISORY,
        ok,
        "database resolves to the canonical project-root file"
        if actual == expected
        else (
            f"database resolves to {actual} via a configured DATABASE_PATH"
            if configured
            else (
                f"database resolves to {actual} rather than {expected} with no DATABASE_PATH "
                "configured (valid for a migration or a test run)"
            )
        ),
        {"actual": str(actual), "expected": str(expected), "configured": configured},
    )


def _check_head_admin_exists(ctx: dict) -> Check:
    try:
        conn = _open(ctx.get("db_path"), read_only=True)
        try:
            count = conn.execute("SELECT COUNT(*) FROM users WHERE role = 'head_admin'").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return Check("database_has_head_admin", TIER_ADVISORY, True, f"users unreadable: {exc}")
    return Check(
        "database_has_head_admin",
        TIER_ADVISORY,
        count > 0,
        "a head admin account exists" if count else "no head admin account: nobody can administer this installation",
        {"count": count},
    )


def _check_liveness(ctx: dict) -> Check:
    """Anti-spoofing posture.

    ``enforce`` with no usable MiniFASNet model is **fatal**: it would reject every
    genuine punch, which is a self-inflicted outage far worse than the spoofing it
    is trying to stop. A missing model in ``advisory``/``off`` is only advisory,
    because the model is an optional extra and must never stop the API from serving
    attendance.
    """
    import liveness

    report = liveness.readiness()
    tier = {"fatal": TIER_FATAL, "advisory": TIER_ADVISORY}.get(report.get("severity"), TIER_ADVISORY)
    if tier == TIER_FATAL and report.get("ready"):
        tier = TIER_ADVISORY
    return Check(
        "liveness_anti_spoofing",
        tier,
        bool(report.get("ready")),
        str(report.get("reason")),
        {
            "mode": report.get("mode"),
            "available": report.get("available"),
            "model_path": report.get("model_path"),
            "error": report.get("error"),
        },
    )


#: Registry order is the order presented in the report.
CHECKS = (
    _check_secret_key,
    _check_database_reachable,
    _check_database_writable,
    _check_schema_version,
    _check_migrations,
    _check_schema_version_ahead,
    _check_startup_override_acknowledged,
    _check_schema_drift,
    _check_shift_rules,
    _check_site_windows,
    _check_overtime_alert,
    _check_overtime_close_deferred,
    _check_worker_push,
    _check_worker_notice_backlog,
    _check_push_endpoint_allowlist,
    _check_journal_mode,
    _check_password_hashing,
    _check_jwt_roundtrip,
    _check_admin_routes_guarded,
    _check_api_routes_authorised,
    _check_no_unprefixed_admin_routes,
    _check_static_mounts,
    _check_api_docs_disabled,
    _check_network_policy,
    _check_stored_text_hygiene,
    _check_biometric_dirs,
    _check_biometric_file_naming,
    _check_retention_sweep,
    _check_retention_residue,
    _check_metrics,
    _check_face_engine,
    _check_face_detector,
    _check_face_match_band,
    _check_calibration_corpus,
    _check_liveness,
    _check_clock_sanity,
    _check_database_path,
    _check_head_admin_exists,
)

REPAIRS = {
    "shift_rules_present": _repair_shift_rules,
    "journal_mode": _repair_journal_mode,
}


def run_checks(app=None, *, db_path: Path | None = None) -> tuple[list[Check], list[dict]]:
    ctx: dict = {"app": app, "db_path": db_path, "repaired": []}
    checks: list[Check] = []
    for function in CHECKS:
        try:
            checks.append(function(ctx))
        except Exception as exc:  # pragma: no cover - a broken check must not hide the others
            checks.append(
                Check(function.__name__.removeprefix("_check_"), TIER_ADVISORY, False, f"check raised: {exc}")
            )
    return checks, list(ctx.get("repaired", []))


# ---------------------------------------------------------------------------
# the startup gate
# ---------------------------------------------------------------------------
def run_startup_gate(app, *, db_path: Path | None = None, log=None) -> GateReport:
    """Verify the deployment *before* serving traffic.

    Raises ``RuntimeError`` on an unoverridden FATAL failure. Under uvicorn that
    surfaces as ``SystemExit(3)`` with the port never opened - a one-glance
    symptom for the runbook.
    """
    started = time.time()
    log = log or (lambda message: print(message, file=sys.stderr))

    checks, repaired = run_checks(app, db_path=db_path)
    for check in [item for item in checks if not item.ok and item.tier == TIER_REPAIRABLE]:
        repair = REPAIRS.get(check.name)
        if repair is None:
            continue
        applied = repair({"db_path": db_path, "app": app})
        repaired.append({"check": check.name, "applied": applied})

    if any(item.get("applied") for item in repaired):
        checks, _ = run_checks(app, db_path=db_path)

    report = GateReport(checks=checks, repaired=repaired, duration_ms=(time.time() - started) * 1000)

    for check in report.failed:
        log(f"[startup] {check.tier.upper()}: {check.name}: {check.detail}")

    fatal = report.fatal_failures
    if fatal:
        overridable = [check for check in fatal if check.name not in NON_OVERRIDABLE]
        blocked_irrespective = [check for check in fatal if check.name in NON_OVERRIDABLE]
        if settings.startup_override_active and not blocked_irrespective:
            report.override_used = True
            log(
                "[startup] OVERRIDE ACTIVE: serving despite "
                f"{[check.name for check in overridable]} - reason: {settings.startup_override_reason}"
            )
            alert_id = _record(
                notifications.KIND_STARTUP_OVERRIDE,
                notifications.SEVERITY_CRITICAL,
                "Startup gate overridden",
                f"The server started with failing checks {[check.name for check in overridable]}. "
                f"Reason given: {settings.startup_override_reason}",
                # One alert per *reason*, not one for ever. An acknowledgement accepts the
                # override it was written about, and an operator who accepted last week's
                # reason must not have silently accepted this week's - so a different reason
                # reopens the question (``_check_startup_override_acknowledged``).
                dedupe_key=f"startup_override:{settings.startup_override_reason}",
                payload={
                    "reason": settings.startup_override_reason,
                    "override_until": settings.startup_override_until,
                    "checks": [check.as_dict() for check in overridable],
                },
            )
            # ...and every *start* is an audit event, whether or not the alert row already
            # existed: the alert is one row per reason, so this is the only record of how many
            # times the deployment was forced up.
            _audit_system(
                "startup_override",
                entity="admin_notifications",
                entity_id=alert_id,
                after={
                    "reason": settings.startup_override_reason,
                    "override_until": settings.startup_override_until,
                    "failed_checks": [check.name for check in overridable],
                    "blocked_irrespective": [check.name for check in blocked_irrespective],
                    "schema_expected": migrations.SCHEMA_VERSION,
                },
            )
        else:
            names = ", ".join(f"{check.name} ({check.detail})" for check in fatal)
            raise RuntimeError(
                "startup self-test failed, refusing to serve traffic: "
                f"{names}. Fix the failure, or set STARTUP_OVERRIDE_REASON (with an optional "
                "STARTUP_OVERRIDE_UNTIL) to start anyway; the override is audited and never "
                "reports the deployment as healthy."
            )

    if report.advisory_failures:
        degraded_names = [check.name for check in report.advisory_failures]
        log(f"[startup] DEGRADED: {degraded_names}")
        _record(
            notifications.KIND_STARTUP_DEGRADED,
            notifications.SEVERITY_WARNING,
            "Server started degraded",
            f"Advisory checks failed: {degraded_names}",
            dedupe_key="startup_degraded:" + ",".join(sorted(degraded_names)),
            payload={"checks": [check.as_dict() for check in report.advisory_failures]},
        )

    log(
        f"[startup] self-test complete in {report.duration_ms:.0f}ms: "
        f"{len(report.checks) - len(report.failed)}/{len(report.checks)} checks passed"
        + (" (degraded)" if report.degraded else "")
    )
    return report


def _record(
    kind: str,
    severity: str,
    title: str,
    body: str,
    *,
    dedupe_key: str,
    payload: dict | None = None,
) -> int | None:
    """Write an operator alert and return its row id, or None when it could not be written.

    The id is what lets a system event be attributed to the alert that describes it: the
    forced start appends an audit row pointing at the alert an operator is asked to answer,
    rather than at nothing.
    """
    try:
        with database.db(write=True) as conn:
            notifications.notify(
                conn,
                kind=kind,
                severity=severity,
                title=title,
                body=body,
                dedupe_key=dedupe_key,
                payload=payload,
            )
            row = conn.execute(
                "SELECT id FROM admin_notifications WHERE dedupe_key = ?", (dedupe_key,)
            ).fetchone()
            return int(row[0]) if row else None
    except sqlite3.Error:
        return None


def _audit_system(action: str, *, entity: str, entity_id: str | int | None, after: dict) -> None:
    """Append a system-attributed audit event: the startup gate has no session and no actor.

    ``audit_log`` is append-only in the database, so every forced start is its own row rather
    than an update of the previous one. "How often has this deployment been started past a
    failing self-test" is a question an incident review asks, and a single overwritten row
    could not answer it - especially now that the alert itself is one row per *reason*.
    """
    try:
        with database.db(write=True) as conn:
            conn.execute(
                "INSERT INTO audit_log (actor_id, actor_role, action, entity, entity_id, "
                "after_json, created_at) VALUES (NULL, 'system', ?, ?, ?, ?, ?)",
                (
                    action,
                    entity,
                    str(entity_id) if entity_id is not None else None,
                    json.dumps(after, default=str),
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
    except sqlite3.Error:
        pass


# ---------------------------------------------------------------------------
# HTTP surfaces
# ---------------------------------------------------------------------------
def _code_fingerprint() -> str:
    try:
        from main import __file__ as main_file

        return hashlib.sha256(Path(main_file).read_bytes()).hexdigest()[:8]
    except Exception:  # pragma: no cover - defensive
        return "unknown"


def _migration_table(db_path: Path | None) -> list[dict]:
    try:
        conn = _open(db_path, read_only=True)
        try:
            applied = {
                int(row[0]): {"applied_at": row[1]}
                for row in conn.execute("SELECT version, applied_at FROM schema_migrations")
            }
        finally:
            conn.close()
    except sqlite3.Error:
        applied = {}
    return [
        {
            "version": version,
            "name": name,
            "applied": version in applied,
            "applied_at": applied.get(version, {}).get("applied_at"),
        }
        for version, name, _ in migrations.MIGRATIONS
    ]


def build_report(app, *, deep: bool = False, db_path: Path | None = None) -> GateReport:
    checks, repaired = run_checks(app, db_path=db_path)
    report = GateReport(checks=checks, repaired=repaired)
    if deep:
        report.checks.append(_deep_check(db_path))
    return report


def _deep_check(db_path: Path | None) -> Check:
    try:
        conn = _open(db_path, read_only=True)
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            counts = {}
            for (table,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ):
                counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return Check("deep_integrity", TIER_ADVISORY, False, f"integrity check failed: {exc}")
    ok = str(integrity).lower() == "ok"
    return Check(
        "deep_integrity",
        TIER_ADVISORY,
        ok,
        f"integrity_check={integrity}",
        {"integrity": integrity, "row_counts": counts},
    )


def _live_schema_version(db_path: Path | None) -> int:
    try:
        conn = _open(db_path, read_only=True)
        try:
            return migrations.current_version(conn)
        finally:
            conn.close()
    except sqlite3.Error:
        return 0


def _verdict(report: GateReport) -> dict:
    """The part of a readiness run that is safe to hand to an anonymous caller."""
    return {
        "ok": not report.fatal_failures,
        "degraded": report.degraded,
        "checks": {check.name: check.public() for check in report.checks},
        "failed_checks": [check.name for check in report.fatal_failures],
        "degraded_checks": [check.name for check in report.advisory_failures],
    }


def _public_body(app, db_path: Path | None) -> dict:
    """What ``GET /api/v1/readiness`` may return: the verdict and nothing else.

    A probe that anyone can call must not be a map of the host. The app/schema
    inventory, the fingerprints and every ``detail``/``value`` a check produced
    (they name absolute paths) are served by ``/api/v1/admin/readiness``.
    """
    return _verdict(build_report(app, db_path=db_path))


def _internal_body(report: GateReport, db_path: Path | None) -> dict:
    """The verdict plus the internals, for the admin-only surface."""
    body = _verdict(report)
    body.update(
        {
            "hardened_build_live": not report.fatal_failures,
            "app": {
                "name": "site-attendance",
                "version": settings.app_version,
                "python": platform.python_version(),
                "code_fingerprint": _code_fingerprint(),
                "started_at": datetime.fromtimestamp(_STARTED_AT).strftime("%Y-%m-%d %H:%M:%S"),
            },
            "schema": {
                "current": _live_schema_version(db_path),
                "expected": migrations.SCHEMA_VERSION,
            },
            "migrations": _migration_table(db_path),
            "secret_key_fingerprint": settings.secret_key_fingerprint,
            "token_ttl_hours": settings.jwt_ttl_hours,
        }
    )
    return body


@router.get("/readiness")
def readiness(response: Response, request: Request):
    body = _public_body(request.app, None)
    response.status_code = 200 if body["ok"] else 503
    return body


@router.get("/admin/readiness")
def admin_readiness(
    response: Response,
    request: Request,
    deep: int = 0,
    current: CurrentUser = Depends(admin_only),
):
    report = build_report(request.app, deep=bool(deep))
    body = _internal_body(report, None)
    body["admin"] = {
        "database_path": str(settings.database_path),
        "backup_dir": str(settings.backup_dir),
        "pid": os.getpid(),
        "uptime_seconds": round(time.time() - _STARTED_AT, 1),
        "allowed_origins": settings.allowed_origins,
        "schema_guard_mode": settings.schema_guard_mode,
        "startup_override_active": settings.startup_override_active,
        # The full per-check detail/value/name, which the public route drops.
        "checks": [check.as_dict() for check in report.checks],
        "repaired": report.repaired,
    }
    response.status_code = 200 if body["ok"] else 503
    return body
