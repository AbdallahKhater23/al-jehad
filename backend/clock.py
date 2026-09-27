"""The company clock: the one definition of the zone every timestamp in this app means.

WHY THIS MODULE EXISTS
---------------------
This application runs on a host somewhere (Railway, a US region) while every site, shift
and worker it serves is in Kuwait. Timestamps are stored as **naive** strings
(``%Y-%m-%d %H:%M:%S``) written by ``datetime.now()``, read back by ``datetime.strptime``,
and compared to each other - and SQLite's ``strftime('%Y-%m', 'now', 'localtime')`` is asked
what month it is. Both of those read the *host's* clock. On a server set to Pacific time,
a punch at 06:00 in Kuwait was written as 20:00 the previous day: the shift-window check,
the monthly aggregate, the live elapsed counter and the timesheet all disagreed with the
person standing at the gate, and none of them raised anything.

The application was already written on the assumption that *the host clock is the site
clock* - see ``shift_windows.Window.local``, which converts a naive stamp through the
site's zone, and ``main._seconds_on_site``, which names "a server in UTC and staff in
Kuwait" as the failure. So the fix is not to rewrite every ``datetime.now()``; it is to make
the host clock mean Kuwait.

THE TWO HALVES, AND WHY BOTH
----------------------------
1. **The container** (``Dockerfile``) installs ``tzdata`` and pins ``TZ=Asia/Kuwait`` with
   ``/etc/localtime`` and ``/etc/timezone`` linked at the zoneinfo file. That is what makes
   the C library - and therefore CPython's ``datetime.now()`` *and* SQLite's
   ``'localtime'`` - answer Kuwait time for every process in the image, with no code.
2. **This module** repeats that at the Python level: on import, on POSIX, it sets ``TZ`` and
   calls ``time.tzset()`` (the hook that re-reads the environment into the C library), so a
   run *without* the container - an operator's laptop, a test host, a bare ``uvicorn`` - is
   Kuwait too instead of silently regressing to whatever region the machine sits in.

``tzset`` does not exist on Windows, and the C runtime there reads an IANA name in ``TZ`` as
UTC rather than as the zone; ``install()`` therefore leaves the Windows clock to the OS and
reports what it found rather than pretending. The two places that boundary matters are covered
directly: :func:`now`/:func:`now_str` read Kuwait through ``zoneinfo`` on any platform, and
``main``'s monthly aggregate binds a Python-side month instead of asking SQLite for
``'localtime'``.

WHY ``datetime.now()`` IS LEFT ALONE IN THE CALLING CODE
--------------------------------------------------------
The test suite freezes time by replacing the ``datetime`` symbol in a module's namespace
(``monkeypatch.setattr(app_module, "datetime", _FrozenDatetime)``). Rewriting those call
sites to ``clock.now()`` would route them around that seam and break every frozen-time test.
With the host zone pinned to Kuwait, the existing ``datetime.now()`` calls are already
correct, uniform, and still patchable - which is why the fix lives here and in the
container, not in a hundred edits.

Kuwait has no daylight saving, so "Kuwait" is always exactly UTC+3 - which is why the
``zoneinfo`` failure branch below can fall back to a fixed offset and still be right every
day of the year.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

#: The zone every wall-clock value in this application is written in. Fixed UTC+3: the
#: company is in Kuwait and its staff read the clock on the wall, not the server's region.
TIMEZONE_NAME = "Asia/Kuwait"

#: The zone as a ``tzinfo``. ``zoneinfo`` reads the system's IANA database (the ``tzdata``
#: package the Dockerfile installs); a host without it would otherwise raise *at import* and
#: take the whole application down, so it degrades to the same fixed offset Kuwait has all
#: year round rather than failing.
try:
    KUWAIT_TZ = ZoneInfo(TIMEZONE_NAME)
except Exception:  # noqa: BLE001 - a missing zone database must not stop the app booting
    KUWAIT_TZ = timezone(timedelta(hours=3), TIMEZONE_NAME)

#: The format every stored timestamp uses (see the table columns and ``_parse_ts``). Kept
#: here so a caller that formats "now" and a caller that parses a row cannot drift apart.
TS_FORMAT = "%Y-%m-%d %H:%M:%S"


def now() -> datetime:
    """The current moment as a **naive Kuwait wall-clock** datetime.

    Naive on purpose, and that is a compatibility contract rather than an oversight: every
    stored timestamp in this application is naive and written in ``TS_FORMAT``, ``sqlite3``
    hands them back as strings, and ``datetime.strptime`` compares them as naive values. A
    tz-aware return here would poison each of those comparisons with "can't compare offset-
    naive and offset-aware datetimes", so this returns the same shape ``datetime.now()``
    does - only measured in Kuwait.
    """
    return datetime.now(KUWAIT_TZ).replace(tzinfo=None)


def now_str(fmt: str = TS_FORMAT) -> str:
    """The current Kuwait moment as a string, ``%Y-%m-%d %H:%M:%S`` by default."""
    return now().strftime(fmt)


# ---------------------------------------------------------------------------
# UTC storage helpers
# ---------------------------------------------------------------------------
#: Kuwait is a fixed UTC+3 with no DST, so the offset is a constant. It is written as an
#: hour count here because the *data migration* has to express the same shift in SQL
#: (``datetime(col, '-3 hours')``) and the two must agree exactly.
KUWAIT_UTC_OFFSET_HOURS = 3

#: The stored format for a UTC timestamp - the same string shape as the Kuwait stamps, so an
#: existing column needs no type change and no reader's ``strptime`` has to learn anything new.
UTC = timezone.utc


def utc_now() -> datetime:
    """The current moment as a **naive UTC** datetime, ready to be stored.

    The counterpart to :func:`now`: the database keeps instants in UTC (unambiguous, and
    independent of whatever zone the host runs in), while everything a person reads is
    rendered through :func:`to_kuwait`. Naive UTC - not aware - so it drops into the same
    ``%Y-%m-%d %H:%M:%S`` columns every writer already uses.
    """
    return datetime.now(UTC).replace(tzinfo=None)


def utc_now_str(fmt: str = TS_FORMAT) -> str:
    """The current UTC moment as a string, ``%Y-%m-%d %H:%M:%S`` by default."""
    return utc_now().strftime(fmt)


def to_kuwait(moment: datetime) -> datetime:
    """A stored UTC moment as **naive Kuwait** wall clock, for display and windows.

    A naive input is *assumed to be UTC* - that is the storage contract after the migration.
    An aware input is converted from whatever zone it carries. The return is naive because
    every consumer (SQL string comparisons, ``strftime``, the shift-window minute maths)
    speaks naive Kuwait, not an offset-aware object.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(KUWAIT_TZ).replace(tzinfo=None)


def to_utc(moment: datetime) -> datetime:
    """A **naive Kuwait** wall-clock moment as naive UTC, for storage.

    A naive input is assumed to be Kuwait time - the reverse of :func:`to_kuwait`, and the
    direction the data migration needs: every pre-migration row is a Kuwait wall-clock stamp
    that has to become the instant it names. ``clock.to_utc(clock.now())`` therefore equals
    ``clock.utc_now()`` to the second.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=KUWAIT_TZ)
    return moment.astimezone(UTC).replace(tzinfo=None)


def to_utc_str(moment: datetime, fmt: str = TS_FORMAT) -> str:
    """A naive Kuwait moment as a stored UTC string."""
    return to_utc(moment).strftime(fmt)


def kuwait_str_to_utc_str(value: str, fmt: str = TS_FORMAT) -> str:
    """Re-read a Kuwait wall-clock string as the UTC string for the same instant.

    This is the Python twin of the migration's ``datetime(col, '-3 hours')``, so a row that
    the migration handled in SQL and a row this function handles in code are converted by the
    same rule. ``value`` that will not parse is returned unchanged rather than raising: a
    hand-written or foreign-format row must not abort a backfill.
    """
    try:
        return to_utc(datetime.strptime(value, fmt)).strftime(fmt)
    except (TypeError, ValueError):
        return value


def install(force: bool = True) -> dict:
    """Point the process's C-level local time at Kuwait, and report what happened.

    On POSIX, sets ``TZ`` and calls ``time.tzset()`` so ``datetime.now()``, ``time.strftime``
    and SQLite's ``'localtime'`` all answer Kuwait time even when the container's environment
    did not set ``TZ``. Idempotent: the container's own ``TZ`` and these lines agree, and a
    second call is a no-op.

    On Windows there is no ``tzset``, and the C runtime there does **not** understand an IANA
    name in ``TZ`` - it reads an unrecognised value as UTC, so setting the variable without a
    ``tzset`` to consume it would push ``datetime.now()`` three hours the wrong way. Windows
    therefore keeps its OS-configured clock and this function only reports whether that clock
    is already Kuwait; the container's ``TZ``/``/etc/localtime`` is what guarantees the zone
    in production.

    ``force=False`` leaves an operator's explicit ``TZ`` untouched on POSIX. The return value
    is a small dict - not a bare ``True`` - because "the variable is set" and "the clock is
    actually Kuwait" are different states, and a caller (the verification script, a startup
    self-test) has to be able to tell them apart.
    """
    before = os.environ.get("TZ")
    tzset = getattr(time, "tzset", None)
    applied = False
    if tzset is not None and force:
        os.environ["TZ"] = TIMEZONE_NAME
        try:
            tzset()
            applied = True
        except Exception:  # noqa: BLE001 - a platform without tzset must not fail the boot
            applied = False
    # Whether the clock this process will actually read is Kuwait, measured rather than
    # inferred: the variable being set proves nothing if the platform ignored it.
    drift = abs((datetime.now() - now()).total_seconds())
    return {
        "timezone": TIMEZONE_NAME,
        "tz_before": before,
        "tz_env": os.environ.get("TZ"),
        "tzset_available": tzset is not None,
        "tzset_applied": applied,
        "local_time_is_kuwait": drift <= 5.0,
    }


#: Applied once, at import. ``config`` imports this module before it builds anything, so
#: every entrypoint that loads configuration - the server, the tools, the tests - gets a
#: process whose clock is Kuwait before the first ``datetime.now()`` runs.
INSTALLED = install()
