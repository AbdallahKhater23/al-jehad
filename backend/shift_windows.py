"""Which clock-in window applies to a punch, and whether the arrival is inside it.

WHY THIS EXISTS
---------------
The installation had one clock-in window for every site (``shift_rules``), and the check was
``start <= now <= end`` in minutes since midnight. Both halves of that broke as soon as a site
ran a shift that started the previous evening:

* **An overnight window could not be expressed at all.** A site working 21:30 to 05:30 needs
  ``start > end``, and the comparison above is then never true - so every arrival, including
  the ones that were exactly on time, was flagged "outside the clock-in window" and routed to
  an administrator. The window was not merely wrong; it was inverted, and silently so: the
  punch still succeeded (a late flag is an exception to review, not a refusal), so the only
  symptom was a queue of late flags with nothing wrong in it.
* **Two sites could not run different shifts.** A day site and a night site shared one window,
  so whichever was configured were the hours the other was measured against.

The window an arrival is measured against now comes from the site the punch was inside of,
falling back per field to the global rules - so a site may override only the hours and keep
the global timezone, or the other way round.

THE THREE THINGS THAT ARE EASY TO GET WRONG
-------------------------------------------
1. **Cross-midnight is a different predicate, not a different comparison.** ``start <= end``
   means one interval on one day; ``start > end`` means the union of ``[start, 24:00)`` and
   ``[00:00, end]``. ``contains`` is the only place that distinction exists, and it is a pure
   function of two minutes-since-midnight values so it can be tested without a clock, a
   database or a timezone.
2. **The comparison happens in the site's timezone, not the server's.** A site is a place; the
   server's clock is where the machine happens to sit. A punch is resolved into the site's
   zone first, then compared - otherwise moving the deployment host changes who is late.
3. **Boundaries are inclusive on both ends, to the minute.** A window of 04:00-06:30 contains
   04:00:00 and 06:30:59. The alternative - exclusive ends - turns the minute an administrator
   typed into a minute that does not exist, which is exactly the kind of off-by-one that shows
   up as one worker flagged every single day.

THE SAME ANSWER, ONE SCREEN EARLIER
------------------------------------
The window is also shown to the worker *before* the punch (``Window.arrival``), because the
verdict used to be delivered only to an administrator, as a notification about an arrival that
had already happened. "You are late" after the shutter is a statement about the past; the same
arithmetic a tap earlier is something a person can still act on. It is one more caller of
:func:`contains`, not a second rule - two implementations of this predicate is how a card ends
up saying "on time" while the record is flagged late.

AN UNPARSEABLE WINDOW IS NOT A LATE ARRIVAL
-------------------------------------------
Every function here is deliberately total: a missing, malformed or unknown-zone value falls
back to the documented default rather than raising. That is a policy, not laziness. The caller
is a punch handler, and a ``500`` there means a worker standing at a gate cannot record their
hours because an administrator typed ``7:30`` instead of ``07:30``. Falling *back* keeps the
site recording; ``readiness`` reports the bad value so it is not invisible.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping
from zoneinfo import ZoneInfo

#: Used when neither the site nor the global rules name a zone. It is the value
#: ``shift_rules`` was seeded with, so an untouched installation behaves exactly as before.
#: ``Asia/Kuwait`` is the company clock: a fixed UTC+3, with no DST rule to move the
#: clock-in window by an hour twice a year.
DEFAULT_TIMEZONE = "Asia/Kuwait"

#: 24-hour ``HH:MM``, exactly - and strict on purpose. This value decides whether a worker's
#: arrival is flagged for review, so "4:00" or "07:5" must be rejected where an administrator
#: can see the problem (the admin API) rather than accepted and then silently ignored.
HHMM_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
_HHMM_RE = re.compile(HHMM_PATTERN)

MINUTES_PER_DAY = 24 * 60

#: Where a value in an effective window came from. Surfaced to the console so "inherited" and
#: "configured here" are distinguishable - an administrator looking at 04:00 needs to know
#: whether editing the site will change it.
SOURCE_SITE = "site"
SOURCE_GLOBAL = "global"
SOURCE_DEFAULT = "default"


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------
def normalise_hhmm(value: Any) -> str | None:
    """A canonical ``HH:MM`` string, or ``None`` if ``value`` is not one.

    Surrounding whitespace is tolerated: these values arrive from a form, a CSV import and an
    administrator's ``sqlite3`` session, and " 22:00" has one obvious meaning. Everything else
    is rejected, including ``"7:30"``, ``"24:00"`` and ``"22:00:00"``.
    """
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    return candidate if _HHMM_RE.match(candidate) else None


def parse_hhmm(value: Any) -> int | None:
    """Minutes since midnight for a strict ``HH:MM`` string, or ``None`` if it is not one."""
    candidate = normalise_hhmm(value)
    if candidate is None:
        return None
    hours, minutes = candidate.split(":")
    return int(hours) * 60 + int(minutes)


def format_hhmm(minutes: int) -> str:
    """The inverse of :func:`parse_hhmm`, for building messages."""
    wrapped = int(minutes) % MINUTES_PER_DAY
    return f"{wrapped // 60:02d}:{wrapped % 60:02d}"


def is_valid_hhmm(value: Any) -> bool:
    """Whether ``value`` is a strict ``HH:MM`` string. Used by the admin API and readiness."""
    return normalise_hhmm(value) is not None


def resolve_timezone(name: Any) -> ZoneInfo:
    """The zone called ``name``, or the documented default if there is no such zone.

    ``ZoneInfo`` raises for an unknown key, and an unknown key is a configuration mistake that
    must not stop a punch from being recorded - so it degrades to the default, which is where
    the value would have come from anyway if nobody had configured a site.
    """
    try:
        return ZoneInfo(str(name))
    except Exception:
        return ZoneInfo(DEFAULT_TIMEZONE)


def is_known_timezone(name: Any) -> bool:
    """Whether ``name`` is a zone the runtime can actually resolve.

    Checked at the API boundary so ``Asia/Kuwait `` (a trailing space) or ``EET`` are refused
    where they are typed. A timezone that silently resolves to something else would move every
    window at that site, and the symptom - the wrong people flagged late - looks like a policy
    decision rather than a typo.
    """
    try:
        ZoneInfo(str(name))
    except Exception:
        return False
    return True


# ---------------------------------------------------------------------------
# the window
# ---------------------------------------------------------------------------
def contains(start_minutes: int, end_minutes: int, moment_minutes: int) -> bool:
    """Whether ``moment_minutes`` (minutes since midnight) is inside the window.

    The whole cross-midnight problem is these four lines. ``start <= end`` is a single interval
    on one day. ``start > end`` is the window that wraps: it runs from ``start`` to midnight and
    from midnight to ``end``, so the test becomes a union rather than a range.

    ``start == end`` is a one-minute window (the minute it names), not a 24-hour one: the
    dangerous misreading of an ambiguous pair is the one that makes every arrival on time. A
    site that genuinely operates around the clock is expressed as ``00:00``-``23:59``.
    """
    if start_minutes <= end_minutes:
        return start_minutes <= moment_minutes <= end_minutes
    return moment_minutes >= start_minutes or moment_minutes <= end_minutes


#: Where an arrival falls relative to a window. Published to the worker's punch card and
#: computed from the same predicate the punch itself is judged by, so the two cannot disagree
#: about the one question a person standing at a gate is asking.
VERDICT_ON_TIME = "on_time"
VERDICT_EARLY = "early"
VERDICT_LATE = "late"


def classify(start_minutes: int, end_minutes: int, moment_minutes: int) -> tuple[str, int]:
    """``(verdict, minutes_off)`` for an arrival outside or inside a window.

    ``minutes_off`` is how early or how late, in minutes, and is 0 exactly when the arrival is
    inside the window - the two travel together, so a caller can never print "late" without a
    number, or a number without a direction.

    Inside the window the answer is "on time". Outside it, the window is *closed* from ``end``
    until ``start``, and that gap is the only thing left to describe: the arrival is "late" when
    it is nearer the close (the shift that has just gone) and "early" when it is nearer the
    open (the shift that is coming). At 12:00 on a 22:00-06:00 site, six hours after it closed
    and ten before it opens again, the honest answer is late; at 21:00, an hour before it opens,
    it is early.

    That is one rule for both shapes of window, and deliberately so. The old ``start <= now <=
    end`` bug came from treating a wrapping window as a special case, and a *second* branch here
    - "before the start is early, after the end is late" for a day window - would make 23:00 on
    a 04:00-06:30 site read as "late by 16 h 30 m" instead of "early, opens in 5 h". Both are
    true statements about the clock; only one is worth reading on the way into a shift.

    ``start == end`` is a one-minute window (see :func:`contains`): before that minute is
    early, after it is late, and the whole rest of the day is one or the other.

    ``minutes_off`` is never 0 outside the window: an arrival exactly on an edge is *inside*
    (both ends are inclusive), so the distances below are at least a minute.
    """
    if contains(start_minutes, end_minutes, moment_minutes):
        return VERDICT_ON_TIME, 0
    since_close = (moment_minutes - end_minutes) % MINUTES_PER_DAY
    until_open = (start_minutes - moment_minutes) % MINUTES_PER_DAY
    if since_close <= until_open:
        return VERDICT_LATE, since_close
    return VERDICT_EARLY, until_open


@dataclass(frozen=True)
class Arrival:
    """Where one instant falls relative to one window, as a punch card can print it.

    A third field rather than a sentence: the frontend speaks three languages and composes the
    wording itself, so the server's job is the arithmetic and the site's local clock - never
    the phrasing.
    """

    verdict: str
    minutes_off: int
    #: ``HH:MM`` on the *site's* clock at the moment asked about, or ``None`` for a timestamp
    #: that could not be converted at all (see :meth:`Window.arrival`).
    local_time: str | None = None

    @property
    def is_on_time(self) -> bool:
        return self.verdict == VERDICT_ON_TIME

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "minutes_off": self.minutes_off,
            "site_time": self.local_time,
        }


@dataclass(frozen=True)
class Window:
    """An effective clock-in window: the hours, the zone, and where each part came from."""

    start: str
    end: str
    timezone: str = DEFAULT_TIMEZONE
    site_name: str | None = None
    start_source: str = SOURCE_DEFAULT
    end_source: str = SOURCE_DEFAULT
    timezone_source: str = SOURCE_DEFAULT

    # -- derived -----------------------------------------------------------
    @property
    def start_minutes(self) -> int:
        parsed = parse_hhmm(self.start)
        return parsed if parsed is not None else 0

    @property
    def end_minutes(self) -> int:
        parsed = parse_hhmm(self.end)
        return parsed if parsed is not None else MINUTES_PER_DAY - 1

    @property
    def crosses_midnight(self) -> bool:
        """True when the window runs past 00:00 - the case the old helper could not express."""
        return self.start_minutes > self.end_minutes

    @property
    def is_site_specific(self) -> bool:
        """True when any part of this window was configured on the site rather than inherited."""
        return SOURCE_SITE in (self.start_source, self.end_source, self.timezone_source)

    @property
    def has_site_hours(self) -> bool:
        """True when the site overrode the hours, whatever it did with the timezone."""
        return SOURCE_SITE in (self.start_source, self.end_source)

    # -- behaviour ---------------------------------------------------------
    def local(self, moment: datetime) -> datetime:
        """``moment`` expressed in this window's timezone.

        A naive datetime is converted as local time on this server, which is how every
        timestamp in this application is written (``datetime.now()``, no zone) - including the
        effective time an offline punch is replayed at. Making the conversion explicit here is
        what keeps "who is late" a property of the site rather than of the deployment host.
        """
        return moment.astimezone(resolve_timezone(self.timezone))

    def contains_moment(self, moment: datetime) -> bool:
        """Whether ``moment`` falls in this window, resolved through the site's timezone."""
        try:
            local = self.local(moment)
        except (OverflowError, OSError, ValueError):
            # A timestamp so far out that it cannot be converted (a device clock reading year
            # 9999). Not a late arrival: it is a bad timestamp, and the punch path has its own
            # policy for those (``offline_sync`` compares against a clock-skew allowance).
            return True
        return contains(self.start_minutes, self.end_minutes, local.hour * 60 + local.minute)

    def arrival(self, moment: datetime) -> Arrival:
        """Where ``moment`` falls in this window, on the site's clock.

        The counterpart of :meth:`contains_moment`, and deliberately built on the same two
        inputs: a window that says "inside" here and "flagged" there is the bug this module
        already exists to prevent, one screen earlier.

        An unconvertible timestamp is on time, exactly as ``contains_moment`` treats it - it is
        a bad device clock, not a late worker, and the punch path has its own policy for those.
        """
        try:
            local = self.local(moment)
        except (OverflowError, OSError, ValueError):
            return Arrival(VERDICT_ON_TIME, 0, None)
        minutes = local.hour * 60 + local.minute
        verdict, minutes_off = classify(self.start_minutes, self.end_minutes, minutes)
        return Arrival(verdict, minutes_off, format_hhmm(minutes))

    def label(self) -> str:
        """``06:00-08:00``, or ``22:00-06:00 (overnight)``, for messages a person reads."""
        return f"{self.start}-{self.end}" + (" (overnight)" if self.crosses_midnight else "")

    def source_dict(self) -> dict[str, str]:
        """Where each field came from, for the admin API and for these tests."""
        return {
            "clock_in_window_start": self.start_source,
            "clock_in_window_end": self.end_source,
            "site_timezone": self.timezone_source,
        }

    def as_dict(self) -> dict[str, Any]:
        """The shape the admin API and readiness publish."""
        return {
            "site_name": self.site_name,
            "clock_in_window_start": self.start,
            "clock_in_window_end": self.end,
            "site_timezone": self.timezone,
            "window": self.label(),
            "crosses_midnight": self.crosses_midnight,
            "site_specific": self.is_site_specific,
            "source": self.source_dict(),
        }


# ---------------------------------------------------------------------------
# resolving a site's window
# ---------------------------------------------------------------------------
def _field(row: Any, key: str) -> Any:
    """Read ``key`` from a sqlite3.Row, a dict or an object, or ``None``.

    ``sqlite3.Row`` raises ``IndexError`` for a column that is not in the result set and a dict
    raises ``KeyError``; both mean "this query did not select it", which is the same thing as
    "not configured" for our purpose. Tolerating that is what lets a caller pass a row fetched
    for another reason without the query having to be kept in step with this module.
    """
    if row is None:
        return None
    try:
        return row[key]
    except (IndexError, KeyError, TypeError):
        return None


def _pick(site_row: Any, global_rules: Mapping[str, Any] | None, key: str) -> tuple[Any, str]:
    """The effective value of ``key``: the site's, else the global rules', else the default."""
    site_value = _field(site_row, key)
    if site_value is not None and str(site_value).strip() != "":
        return site_value, SOURCE_SITE
    global_value = (global_rules or {}).get(key)
    if global_value is not None and str(global_value).strip() != "":
        return global_value, SOURCE_GLOBAL
    return None, SOURCE_DEFAULT


def effective_window(
    site_row: Any = None, global_rules: Mapping[str, Any] | None = None
) -> Window:
    """The window that applies to a punch at ``site_row``, falling back per field.

    Per field, not per row, and that is the useful granularity: a site that runs the company's
    timezone but its own hours configures only the hours, and a site that follows the company
    hours in a different zone configures only the zone. A single ``has the site configured a
    window?`` flag would force an administrator to retype values they are not changing - which
    is how the two copies drift apart.

    A row with nothing set, or no row at all (an administrator deleted the site, a punch
    outside every geofence), yields the global rules unchanged.
    """
    start, start_source = _pick(site_row, global_rules, "clock_in_window_start")
    end, end_source = _pick(site_row, global_rules, "clock_in_window_end")
    timezone, timezone_source = _pick(site_row, global_rules, "site_timezone")
    return Window(
        # An unparseable *stored* value falls back rather than becoming a window nobody meant.
        # The admin API refuses one; this is the second line, for a value written by hand or
        # by a tool that skipped the API.
        start=normalise_hhmm(start) or "00:00",
        end=normalise_hhmm(end) or "23:59",
        timezone=str(timezone) if is_known_timezone(timezone) else DEFAULT_TIMEZONE,
        site_name=(str(_field(site_row, "site_name")) if _field(site_row, "site_name") else None),
        start_source=start_source,
        end_source=end_source,
        timezone_source=timezone_source,
    )


def is_within_site_window(
    site_rules: Any, current_time: datetime, *, timezone: str | None = None
) -> bool:
    """Whether ``current_time`` is inside the window described by ``site_rules``.

    ``site_rules`` may be a :class:`Window` (the resolved form), or any mapping with the three
    window keys - which is what the callers that already hold a row or a rules dict pass. The
    timezone is taken from the rules unless ``timezone`` overrides it, so a caller that has
    resolved the site's zone separately does not have to copy it into the mapping.
    """
    window = (
        site_rules
        if isinstance(site_rules, Window)
        else effective_window(None, site_rules if isinstance(site_rules, Mapping) else None)
    )
    if timezone is not None:
        window = Window(
            start=window.start,
            end=window.end,
            timezone=timezone,
            site_name=window.site_name,
            start_source=window.start_source,
            end_source=window.end_source,
            timezone_source=window.timezone_source,
        )
    return window.contains_moment(current_time)


def window_problems(site_row: Any) -> list[str]:
    """Every window field on a stored site row that this module cannot apply, described.

    The rule lives here rather than in the caller that reports it (``readiness``) because this
    is the module that knows what "usable" means, and a report that disagreed with the
    fallback it describes would be worse than no report at all.
    """
    if site_row is None:
        return []
    name = _field(site_row, "site_name") or "?"
    problems: list[str] = []
    for column in ("clock_in_window_start", "clock_in_window_end"):
        value = _field(site_row, column)
        if value is not None and not is_valid_hhmm(value):
            problems.append(f"{name}.{column}={value!r}")
    zone = _field(site_row, "site_timezone")
    if zone is not None and not is_known_timezone(zone):
        problems.append(f"{name}.site_timezone={zone!r}")
    return problems


def describe(window: Window) -> str:
    """One sentence naming the window an arrival missed, for a notification body."""
    if window.site_name:
        return f"outside {window.site_name}'s {window.label()} window"
    return f"outside the {window.label()} window"
