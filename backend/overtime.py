"""What happens to a shift while it is still open: alert at 8.1 h, close at 8 h paid.

TWO JOBS, ONE TIMER
-------------------
``scan_overtime()`` is the *alert*: it walks ``active_sessions`` and, for each shift
whose elapsed time has reached ``overtime_notify_hours`` (8.1 h by default), stamps
``overtime_notified_at`` once, inserts an ``overtime_exceeded`` notification carrying
the exact crossing moment, and appends a system action to the append-only
``audit_log``.

``scan_auto_close()`` is the *rule*: a day is ``regular_hours`` of paid work plus an
unpaid break (see ``shift_hours.py``), so when an open shift reaches that many paid
hours the system writes the clock-out itself - at the boundary, not at the moment the
watcher happened to run - marks it ``auto_closed_8h``, and tells the administrator.

Which one fires first depends on the settings: with the shipped numbers the close
happens at 8.5 h on site and the alert threshold (8.1 h *paid*, i.e. 8.6 h on site)
is never reached, so the close is what an administrator actually sees. Turn
``auto_close_at_regular`` off and the alert is the only thing watching a forgotten
shift, which is what the alert was written for. Neither job touches a session that is
under its limit.

WHAT THE CLOSE COSTS, SAID OUT LOUD
-----------------------------------
A shift that is still open when the watcher runs *after* the boundary loses the time
past it - that is what "the day ends at 8 paid hours" means, and it is why this
writes an alert rather than closing quietly. The alert names the excess ("still open
1.2 h past the limit"), and the log row keeps the exact closing moment, so the
administrator can see that a worker was on site longer than the recorded hours. The
closed shift's hours are **payable** (``auto_closed_8h``), unlike the 11 h-era
``auto_closed`` rows, which are not: nobody ever decided what those were worth.

IDEMPOTENCE
-----------
``overtime_notified_at IS NULL`` guards the alert, and the notification additionally
carries a ``dedupe_key`` on ``admin_notifications`` (UNIQUE). Both matter: the watcher
runs on a timer, several ASGI workers may each run one, and a restart must not
re-alert the same shift. Re-alerting is not harmless - an administrator who is paged
twice an hour for the same worker stops reading the alerts. The close needs no such
guard: it deletes the session it closed, so there is nothing left to close twice.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime, timedelta

import migrations
import notifications
import shift_hours
from config import settings
from database import db

log = logging.getLogger("attendance.overtime")

_TS_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f")

_stop_event = threading.Event()
_thread: threading.Thread | None = None


def _parse_ts(value) -> datetime | None:
    if isinstance(value, datetime):
        return value
    for fmt in _TS_FORMATS:
        try:
            return datetime.strptime(str(value), fmt)
        except (TypeError, ValueError):
            continue
    return None


def rules(conn: sqlite3.Connection | None = None) -> dict:
    """Current shift rules, falling back to the shipped defaults.

    Read directly here rather than imported from ``main`` so this module has no
    dependency on the application module (``main`` imports *this*)."""
    values = dict(migrations.DEFAULT_SHIFT_RULES)
    owns = conn is None
    handle = conn or db()
    try:
        try:
            row = handle.execute("SELECT * FROM shift_rules WHERE id = 1").fetchone()
        except sqlite3.Error:
            row = None
        if row is not None:
            for key in values:
                try:
                    value = row[key]
                except (IndexError, KeyError):
                    continue
                if value is not None:
                    values[key] = value
    finally:
        if owns:
            handle.close()
    return values


def _number(values: dict, key: str, default: float) -> float:
    try:
        return float(values[key])
    except (KeyError, TypeError, ValueError):
        return default


def _audit_system(conn: sqlite3.Connection, *, action: str, entity_id: str, after: dict) -> None:
    """Append a system-attributed event (no human actor) to the audit trail."""
    try:
        import json

        conn.execute(
            "INSERT INTO audit_log (actor_id, actor_role, action, entity, entity_id, after_json, created_at) "
            "VALUES (NULL, 'system', ?, 'active_sessions', ?, ?, ?)",
            (action, str(entity_id), json.dumps(after, default=str), datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
    except sqlite3.Error:
        pass


def scan_overtime(*, now: datetime | None = None) -> dict:
    """Notify once per open shift that has crossed the overtime threshold.

    Returns a summary; never raises, because it runs from a timer thread where an
    exception would silently stop all future monitoring.
    """
    moment = now or datetime.now()
    summary = {"scanned": 0, "notified": 0, "already_notified": 0, "errors": 0, "notifications": []}
    try:
        with db(write=True) as conn:
            values = rules(conn)
            regular = _number(values, "regular_hours", 8.0)
            threshold = _number(values, "overtime_notify_hours", 8.1)

            sessions = conn.execute(
                "SELECT worker_id, site_name, clock_in_time, overtime_notified_at FROM active_sessions"
            ).fetchall()
            for session in sessions:
                summary["scanned"] += 1
                clock_in = _parse_ts(session["clock_in_time"])
                if clock_in is None:
                    continue
                elapsed = (moment - clock_in).total_seconds() / 3600.0
                # The threshold is in *paid* hours, because that is the number payroll
                # acts on: 8.6 h on site with a 30-minute unpaid break is 8.1 h of work,
                # and a normal full day (8.5 h on site) must never page anybody.
                paid, break_taken = shift_hours.paid_hours(elapsed, values)
                if paid < threshold:
                    continue
                if session["overtime_notified_at"]:
                    summary["already_notified"] += 1
                    continue

                crossing = shift_hours.paid_limit_at(clock_in, threshold, values)
                crossing_str = crossing.strftime("%Y-%m-%d %H:%M:%S")
                name = conn.execute("SELECT name FROM users WHERE id = ?", (session["worker_id"],)).fetchone()
                display = name["name"] if name else session["worker_id"]

                created = notifications.notify(
                    conn,
                    kind=notifications.KIND_OVERTIME_EXCEEDED,
                    severity=notifications.SEVERITY_WARNING,
                    title=f"Worker exceeded {threshold:g} hours — overtime needs sign-off",
                    body=(
                        f"{display} (id {session['worker_id']}) passed {threshold:g} h of paid "
                        f"work at {crossing_str} on '{session['site_name']}'. "
                        f"{shift_hours.describe(round(elapsed, 4), paid, break_taken)} so far; "
                        "the shift is still open and tracking. Review whether the extra "
                        f"{max(0.0, paid - regular):.2f} h past the regular day is authorised."
                    ),
                    worker_id=session["worker_id"],
                    site_name=session["site_name"],
                    payload={
                        "threshold_hours": threshold,
                        "regular_hours": regular,
                        "elapsed_hours": round(elapsed, 4),
                        "paid_hours": paid,
                        "break_hours": break_taken,
                        "crossed_at": crossing_str,
                        "clock_in_time": session["clock_in_time"],
                    },
                    dedupe_key=f"overtime_live:{session['worker_id']}:{session['clock_in_time']}",
                )

                # Stamped even when the notification was deduped, so the watcher does
                # not re-evaluate this shift every interval.
                conn.execute(
                    "UPDATE active_sessions SET overtime_notified_at = ? WHERE worker_id = ?",
                    (crossing_str, session["worker_id"]),
                )
                _audit_system(
                    conn,
                    action="overtime_detected",
                    entity_id=session["worker_id"],
                    after={
                        "threshold_hours": threshold,
                        "elapsed_hours": round(elapsed, 4),
                        "paid_hours": paid,
                        "break_hours": break_taken,
                        "crossed_at": crossing_str,
                        "site": session["site_name"],
                        "notification_created": bool(created),
                    },
                )
                summary["notified"] += 1
                summary["notifications"].append(
                    {
                        "worker_id": session["worker_id"],
                        "crossed_at": crossing_str,
                        "elapsed_hours": round(elapsed, 4),
                        "created": bool(created),
                    }
                )
    except Exception as exc:  # pragma: no cover - defensive: keep the timer alive
        log.warning("overtime scan failed: %s", exc)
        summary["errors"] += 1
        summary["error"] = f"{type(exc).__name__}: {exc}"
    return summary


#: How late the watcher may be before a close is worth flagging on the shift. The
#: timer runs every minute, so a few seconds of lateness is the timer working normally;
#: quarter of an hour means the shift was left open long enough that somebody might have
#: been on site past the recorded hours, which is the case the administrator should see.
LATE_TOLERANCE_HOURS = 0.25

_TS = "%Y-%m-%d %H:%M:%S"


def _insert_auto_close_log(
    conn: sqlite3.Connection,
    *,
    worker_id: str,
    site_name: str,
    timestamp: str,
    hours: float,
    break_hours: float,
    flag_reason: str | None,
) -> int:
    """Write the clock-out the system is making on the worker's behalf.

    Its own INSERT rather than ``main._insert_log`` because ``main`` imports *this*
    module, and a real clock-out row - not a marker - is the point: the timesheet, the
    monthly report and the payroll query all read one table, and a synthetic "the system
    closed this" row somewhere else would have to be joined back into all three.
    """
    cursor = conn.execute(
        """
        INSERT INTO attendance_logs
            (worker_id, site_name, action, timestamp, hours, score, status, status_code,
             source, flag_reason, break_hours)
        VALUES (?, ?, 'Clock Out', ?, ?, 0.0, ?, ?, 'auto_close', ?, ?)
        """,
        (
            worker_id,
            site_name,
            timestamp,
            hours,
            migrations.STATUS_AUTO_CLOSED_LABEL,
            migrations.STATUS_CODE_AUTO_CLOSED_8H,
            flag_reason,
            break_hours or None,
        ),
    )
    return int(cursor.lastrowid or 0)


def scan_auto_close(*, now: datetime | None = None) -> dict:
    """End every open shift whose paid hours have reached the regular limit.

    Returns a summary; never raises, for the same reason ``scan_overtime`` does not: it
    runs in a timer thread, and an exception would stop the monitoring for good. The
    clock-out is written at the *boundary* - the moment the paid day ran out - rather
    than at the moment this happened to run, so a server restarted at lunchtime does not
    record an eight-hour shift as ending at noon.
    """
    moment = now or datetime.now()
    summary = {"enabled": True, "scanned": 0, "closed": 0, "skipped": 0, "errors": 0, "shifts": []}
    try:
        with db(write=True) as conn:
            values = rules(conn)
            if not shift_hours.auto_close_enabled(values):
                summary["enabled"] = False
                return summary

            regular = shift_hours.regular_hours(values)
            sessions = conn.execute(
                "SELECT worker_id, site_name, clock_in_time FROM active_sessions"
            ).fetchall()
            for session in sessions:
                summary["scanned"] += 1
                clock_in = _parse_ts(session["clock_in_time"])
                if clock_in is None:
                    continue
                elapsed = max(0.0, (moment - clock_in).total_seconds() / 3600.0)
                paid_now, _ = shift_hours.paid_hours(elapsed, values)
                if paid_now < regular:
                    summary["skipped"] += 1
                    continue

                boundary = shift_hours.closes_at(clock_in, values)
                closed_at = min(boundary, moment)
                close_elapsed = max(0.0, (closed_at - clock_in).total_seconds() / 3600.0)
                paid, break_taken = shift_hours.paid_hours(close_elapsed, values)
                late_by = max(0.0, round((moment - boundary).total_seconds() / 3600.0, 4))
                closed_str = closed_at.strftime(_TS)

                name = conn.execute(
                    "SELECT name FROM users WHERE id = ?", (session["worker_id"],)
                ).fetchone()
                display = name["name"] if name else session["worker_id"]

                flag = None
                if late_by > LATE_TOLERANCE_HOURS:
                    flag = (
                        f"shift auto-closed at the {regular:g}h paid limit; still open "
                        f"{late_by:.2f}h later when the watcher ran, so time past "
                        f"{closed_str} is not recorded"
                    )

                conn.execute(
                    "DELETE FROM active_sessions WHERE worker_id = ?", (session["worker_id"],)
                )
                log_id = _insert_auto_close_log(
                    conn,
                    worker_id=session["worker_id"],
                    site_name=session["site_name"],
                    timestamp=closed_str,
                    hours=paid,
                    break_hours=break_taken,
                    flag_reason=flag,
                )
                notifications.notify(
                    conn,
                    kind=notifications.KIND_SHIFT_AUTO_CLOSED,
                    severity=notifications.SEVERITY_INFO,
                    title=f"Shift closed automatically at {regular:g} paid hours",
                    body=(
                        f"{display} (id {session['worker_id']}) reached {regular:g}h paid on "
                        f"'{session['site_name']}' and the shift was closed at {closed_str} "
                        f"({shift_hours.describe(close_elapsed, paid, break_taken)}). "
                        + (
                            f"It was still open {late_by:.2f}h past that limit when the watcher "
                            "ran, so any time after it is not recorded - adjust the shift if it "
                            "was worked."
                            if late_by > LATE_TOLERANCE_HOURS
                            else "Nothing more will be recorded for it."
                        )
                    ),
                    worker_id=session["worker_id"],
                    site_name=session["site_name"],
                    log_id=log_id,
                    payload={
                        "log_id": log_id,
                        "regular_hours": regular,
                        "paid_hours": paid,
                        "break_hours": break_taken,
                        "elapsed_hours": round(close_elapsed, 4),
                        "closed_at": closed_str,
                        "clock_in_time": session["clock_in_time"],
                        "late_by_hours": late_by,
                    },
                    dedupe_key=f"auto_close:{session['worker_id']}:{session['clock_in_time']}",
                )
                _audit_system(
                    conn,
                    action="shift_auto_closed",
                    entity_id=session["worker_id"],
                    after={
                        "log_id": log_id,
                        "site": session["site_name"],
                        "paid_hours": paid,
                        "break_hours": break_taken,
                        "elapsed_hours": round(close_elapsed, 4),
                        "closed_at": closed_str,
                        "late_by_hours": late_by,
                    },
                )
                summary["closed"] += 1
                summary["shifts"].append(
                    {
                        "worker_id": session["worker_id"],
                        "log_id": log_id,
                        "closed_at": closed_str,
                        "paid_hours": paid,
                        "break_hours": break_taken,
                        "late_by_hours": late_by,
                    }
                )
    except Exception as exc:  # pragma: no cover - defensive: keep the timer alive
        log.warning("auto-close scan failed: %s", exc)
        summary["errors"] += 1
        summary["error"] = f"{type(exc).__name__}: {exc}"
    return summary


def _loop(interval: int) -> None:
    """Timer body: closes shifts that reached the paid limit, then alerts on the rest."""
    while not _stop_event.is_set():
        try:
            closed = scan_auto_close()
            if closed.get("closed"):
                log.info("auto-close ended %s shift(s) at the paid limit", closed["closed"])
            summary = scan_overtime()
            if summary.get("notified"):
                log.info("overtime watcher raised %s notification(s)", summary["notified"])
        except Exception as exc:  # pragma: no cover
            log.warning("overtime watcher pass failed: %s", exc)
        _stop_event.wait(max(5, int(interval)))


def start_watcher(*, interval: int | None = None, enabled: bool | None = None) -> bool:
    """Start the daemon timer. Returns whether it is running.

    Skipped when ``OVERTIME_WATCHER_ENABLED=0``, which is what the test suite and a
    read-only replica want: the watcher writes to the database, and a test run must
    not mutate state on a timer.
    """
    global _thread
    on = settings.overtime_watcher_enabled if enabled is None else enabled
    if not on:
        return False
    if _thread is not None and _thread.is_alive():
        return True
    _stop_event.clear()
    seconds = int(interval if interval is not None else settings.overtime_watcher_interval_seconds)
    _thread = threading.Thread(target=_loop, args=(seconds,), name="overtime-watcher", daemon=True)
    _thread.start()
    log.info("overtime watcher started (interval %ss)", seconds)
    return True


def stop_watcher(*, timeout: float = 2.0) -> None:
    """Signal the timer to stop and wait briefly for it (used at shutdown)."""
    global _thread
    _stop_event.set()
    if _thread is not None:
        _thread.join(timeout=timeout)
        _thread = None


def watcher_running() -> bool:
    return _thread is not None and _thread.is_alive()
