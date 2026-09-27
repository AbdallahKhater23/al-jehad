#!/usr/bin/env python
"""Prove the application's clock is Kuwait time, not the host region's.

Run it from ``backend/`` (or anywhere; it fixes ``sys.path`` itself):

    python tools/verify_timezone.py

It checks the three things the timezone fix is about, and exits non-zero if a hard check
fails:

1. ``datetime.datetime.now()`` matches ``Asia/Kuwait`` wall-clock time. This is the check
   that catches a deployment whose host is in another region: on the container it passes
   because ``Dockerfile`` pins ``TZ`` and links ``/etc/localtime``; on a bare host it passes
   because importing ``clock`` (which ``config`` does) calls ``time.tzset()``. On Windows
   there is no ``tzset`` and the operating system owns the clock, so the script says so
   instead of pretending the container guarantee was tested.
2. ``SELECT datetime('now', 'localtime')`` is the same Kuwait timestamp - the query that
   SQLite runs on the C library's clock, which is why the host zone has to be right.
3. A geofence clock-in window is judged on **Kuwait** wall-clock minutes: the verdict a
   punch gets is the one ``shift_windows`` computes from the Kuwait time of day.

A fourth check is the regression the request named: the monthly aggregate in ``main.py``
must not ask SQLite for ``'localtime'``.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import clock  # noqa: E402  (after the sys.path bootstrap)
import shift_windows  # noqa: E402

#: How far two "now" readings may drift and still be the same wall clock. The checks read the
#: clock twice in quick succession; a couple of seconds covers the work in between without
#: hiding a real hour-scale mismatch.
TOLERANCE_SECONDS = 5.0

_results: list[tuple[bool, str, str]] = []


def _record(ok: bool, name: str, detail: str) -> None:
    _results.append((ok, name, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}\n        {detail}")


def check_install() -> None:
    info = clock.INSTALLED
    print("clock.install() ->")
    for key, value in info.items():
        print(f"        {key}: {value}")
    print(f"        TZ (live): {os.environ.get('TZ')!r}")
    print(f"        zoneinfo backend: {type(clock.KUWAIT_TZ).__name__} ({clock.KUWAIT_TZ})")


def check_datetime_now() -> None:
    """``datetime.now()`` (host clock) versus ``clock.now()`` (Kuwait, via zoneinfo)."""
    host_now = datetime.now()
    kuwait_now = clock.now()
    tzset_available = bool(clock.INSTALLED.get("tzset_available"))
    drift = abs((host_now - kuwait_now).total_seconds())
    ok = drift <= TOLERANCE_SECONDS
    if ok:
        detail = (
            f"datetime.now() = {host_now.strftime('%Y-%m-%d %H:%M:%S')} "
            f"== Kuwait wall clock {kuwait_now.strftime('%Y-%m-%d %H:%M:%S')} "
            f"(drift {drift:.3f}s, tolerance {TOLERANCE_SECONDS:.0f}s)"
        )
    elif not tzset_available:
        detail = (
            f"datetime.now() = {host_now.strftime('%Y-%m-%d %H:%M:%S')} but Kuwait is "
            f"{kuwait_now.strftime('%Y-%m-%d %H:%M:%S')} (drift {drift / 3600:.2f}h). This "
            f"platform has no time.tzset (Windows), so the OS owns datetime.now(); the "
            f"container's TZ=Asia/Kuwait and /etc/localtime are what pin it in production."
        )
    else:
        detail = (
            f"datetime.now() = {host_now.strftime('%Y-%m-%d %H:%M:%S')} but Kuwait is "
            f"{kuwait_now.strftime('%Y-%m-%d %H:%M:%S')} (drift {drift / 3600:.2f}h): the "
            f"process clock was not pinned. Check clock.install() and the container TZ."
        )
    _record(ok, "1. datetime.now() is Kuwait time", detail)


def check_sqlite_localtime() -> None:
    """SQLite's own ``'now','localtime'`` must agree with Kuwait."""
    conn = sqlite3.connect(":memory:")
    try:
        local, utc = conn.execute(
            "SELECT datetime('now','localtime'), datetime('now')"
        ).fetchone()
    finally:
        conn.close()
    kuwait = clock.now().strftime("%Y-%m-%d %H:%M:%S")
    parsed = datetime.strptime(local, "%Y-%m-%d %H:%M:%S")
    drift = abs((parsed - clock.now()).total_seconds())
    ok = drift <= TOLERANCE_SECONDS
    detail = (
        f"SELECT datetime('now','localtime') = {local} (UTC {utc}), Kuwait = {kuwait}"
    )
    if not ok:
        detail += (
            f" -- differs by {drift / 3600:.2f}h; SQLite reads the C library's zone, so set "
            f"TZ / /etc/localtime (the container does) or run on a POSIX host where "
            f"clock.install() calls tzset()."
        )
    _record(ok, "2. SQLite datetime('now','localtime') is Kuwait time", detail)


def check_geofence_window() -> None:
    """A clock-in window is judged on Kuwait wall-clock minutes."""
    # The shipped default hours, in the site's zone. ``force`` a zone so this does not depend
    # on what the database currently says.
    window = shift_windows.Window(
        start="04:00",
        end="06:30",
        timezone=shift_windows.DEFAULT_TIMEZONE,
    )
    moment = clock.now()
    kuwait_minutes = moment.hour * 60 + moment.minute
    expected = shift_windows.contains(window.start_minutes, window.end_minutes, kuwait_minutes)
    actual = window.contains_moment(moment)
    arrival = window.arrival(moment)
    local_matches = arrival.local_time == moment.strftime("%H:%M")
    ok = (actual == expected) and local_matches
    detail = (
        f"window {window.label()} ({window.timezone}); now {moment.strftime('%H:%M')} "
        f"Kuwait ({kuwait_minutes} min since midnight) -> contains_moment={actual}, "
        f"expected={expected}, verdict={arrival.verdict}, site_time={arrival.local_time}"
    )
    if not ok:
        detail += " -- the window was not evaluated on the Kuwait time of day"
    _record(ok, "3. Geofence window judged on Kuwait wall clock", detail)


def check_aggregate_uses_app_clock() -> None:
    """The monthly aggregate must bind a Python month, not SQLite's ``'localtime'``."""
    source = (BACKEND_DIR / "main.py").read_text(encoding="utf-8")
    dangling = "strftime('%Y-%m', 'now', 'localtime')" in source
    # Reproduce the shipped query against an in-memory table: only the row in the current
    # *Kuwait* month must come back.
    month = clock.now_str("%Y-%m")
    this_month = clock.now().strftime("%Y-%m-%d %H:%M:%S")
    last_month = (clock.now() - timedelta(days=40)).strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute(
            "CREATE TABLE attendance_logs "
            "(worker_id TEXT, action TEXT, timestamp TEXT)"
        )
        conn.executemany(
            "INSERT INTO attendance_logs VALUES (?, 'Clock Out', ?)",
            [("w1", this_month), ("w1", last_month)],
        )
        rows = conn.execute(
            "SELECT timestamp FROM attendance_logs "
            "WHERE worker_id = ? AND action = ? AND strftime('%Y-%m', timestamp) = ?",
            ("w1", "Clock Out", month),
        ).fetchall()
    finally:
        conn.close()
    ok = (not dangling) and len(rows) == 1 and rows[0][0] == this_month
    detail = (
        f"'{'now','localtime'}' still in main.py: {dangling}; "
        f"month bound = {month}; rows returned = {[r[0] for r in rows]} "
        f"(expected only this-month {this_month})"
    )
    _record(ok, "4. Monthly aggregate uses the application clock", detail)


def main() -> int:
    print(f"Python {sys.version.split()[0]} on {sys.platform}; backend={BACKEND_DIR}\n")
    check_install()
    print()
    check_datetime_now()
    check_sqlite_localtime()
    check_geofence_window()
    check_aggregate_uses_app_clock()
    failures = [name for ok, name, _ in _results if not ok]
    print()
    if failures:
        print(f"FAILED ({len(failures)}): " + "; ".join(failures))
        return 1
    print(f"All {len(_results)} checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
