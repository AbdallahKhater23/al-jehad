"""Per-site clock-in windows, including the ones that cross midnight.

WHY THIS SUITE EXISTS
---------------------
One window applied to every site, and it was evaluated as ``start <= now <= end`` in minutes
since midnight. A site running 21:30-05:30 therefore had ``start > end``, the comparison was
never true, and **every** arrival there - the on-time ones included - was flagged "outside the
clock-in window" and pushed to an administrator for review. The punch still succeeded, which is
what made it quiet: the only symptom was a queue of late flags with nothing wrong in it.

So there are three claims to hold up, and they are separable:

1. **The predicate.** ``start > end`` is not a range, it is a union of two ranges. It is
   exercised here with no clock, no database and no timezone, because a rule that is wrong on
   one day of the year is far easier to see as arithmetic than through a request.
2. **The resolution.** The window comes from the site the phone is inside of, falling back
   *per field* to ``shift_rules`` - so a night site changes only its hours and keeps the
   company timezone, and a site that has configured nothing behaves exactly as it did before
   this feature existed.
3. **The integration.** The handler actually uses it. A helper that is correct and unwired is
   the same outage with better unit tests, so the punch path is asserted end to end: a night
   site is on time at 23:15 and flagged at noon, the site next door is the other way round, and
   the notification names the window that was really applied rather than a hardcoded default.

Times are passed as **aware** datetimes wherever the intent is "this instant at this site".
A naive datetime means "this wall clock on this server", which depends on where the test
machine sits; asserting on that would make the suite pass in Cairo and fail in London.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import harness
import shift_windows
from harness import (
    ADMIN,
    DOWNTOWN,
    INSIDE_DOWNTOWN,
    INSIDE_ZONE_B,
    MOALLEM,
    ZONE_B,
    bearer,
    clock_in,
    db_rows,
    db_scalar,
    with_site_windows,
)

CAIRO = ZoneInfo("Africa/Cairo")
DB_PATH = harness.DB_PATH

#: The two shifts this whole feature exists for, taken from the operations brief: a day site
#: and an overnight one.
DAY_SHIFT = ("05:00", "07:30")
NIGHT_SHIFT = ("21:30", "05:30")


def at(hour: int, minute: int = 0, *, zone: str = "Africa/Cairo") -> datetime:
    """A fixed instant, at a wall-clock time in ``zone``. Ambiguity belongs in the test name."""
    return datetime(2026, 9, 16, hour, minute, tzinfo=ZoneInfo(zone))


def window(start, end, timezone="Africa/Cairo", site_name=None, *, site=True):
    """A window as ``effective_window`` builds one, without going through the database."""
    source = shift_windows.SOURCE_SITE if site else shift_windows.SOURCE_GLOBAL
    return shift_windows.Window(
        start=start,
        end=end,
        timezone=timezone,
        site_name=site_name,
        start_source=source,
        end_source=source,
        timezone_source=source,
    )


# ---------------------------------------------------------------------------
# 1. parsing and validation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "value,expected",
    [
        ("00:00", 0),
        ("04:00", 240),
        ("09:07", 547),
        ("22:00", 1320),
        ("23:59", 1439),
        (" 06:30 ", 390),  # whitespace has one obvious meaning
    ],
)
def test_valid_times_parse_to_minutes_since_midnight(value, expected):
    assert shift_windows.parse_hhmm(value) == expected
    assert shift_windows.is_valid_hhmm(value) is True


@pytest.mark.parametrize(
    "value",
    [
        "7:30",       # must be zero-padded: the column's CHECK is this shape
        "24:00",      # there is no 24th hour
        "9:60",
        "22:00:00",   # a time, but not this field's
        "2200",
        "22-00",
        "ten",
        "",
        None,
        22,
        "25:00",
        "1:2",
    ],
)
def test_anything_that_is_not_strict_hhmm_is_refused(value):
    assert shift_windows.parse_hhmm(value) is None
    assert shift_windows.is_valid_hhmm(value) is False


def test_formatting_round_trips_every_minute_of_the_day():
    """The inverse must be exact, or a message can name a window that was never configured."""
    for minutes in range(shift_windows.MINUTES_PER_DAY):
        rendered = shift_windows.format_hhmm(minutes)
        assert shift_windows.is_valid_hhmm(rendered), rendered
        assert shift_windows.parse_hhmm(rendered) == minutes


def test_an_unknown_timezone_is_not_accepted():
    assert shift_windows.is_known_timezone("Africa/Cairo") is True
    assert shift_windows.is_known_timezone("Asia/Riyadh") is True
    for bad in ("Africa/Cario", "EET-2", "Cairo", "", None, 3):
        assert shift_windows.is_known_timezone(bad) is False, bad


def test_an_unknown_timezone_degrades_to_the_documented_default():
    """A bad zone must not turn into an exception at the gate - it falls back, and is reported."""
    assert shift_windows.resolve_timezone("Nowhere/Nothing").key == shift_windows.DEFAULT_TIMEZONE


# ---------------------------------------------------------------------------
# 2. the predicate, both shapes
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("moment,expected", [("05:00", True), ("06:00", True), ("07:30", True)])
def test_a_same_day_window_contains_its_own_hours(moment, expected):
    assert window(*DAY_SHIFT).contains_moment(at(*map(int, moment.split(":")))) is expected


@pytest.mark.parametrize(
    "moment,expected",
    [
        ("04:59", False),  # the minute before it opens
        ("07:31", False),  # the minute after it closes
        ("23:15", False),  # the middle of the night is not the middle of the day
        ("00:00", False),
    ],
)
def test_a_same_day_window_excludes_the_minutes_around_it(moment, expected):
    assert window(*DAY_SHIFT).contains_moment(at(*map(int, moment.split(":")))) is expected


@pytest.mark.parametrize(
    "moment,expected",
    [
        ("21:30", True),   # opens
        ("23:15", True),   # after midnight is *after* the start, not before it
        ("23:59", True),
        ("00:00", True),   # the seam itself
        ("02:00", True),
        ("04:30", True),
        ("05:30", True),   # closes
        ("21:29", False),
        ("05:31", False),
        ("12:00", False),  # midday is not in a night shift
    ],
)
def test_a_cross_midnight_window_contains_both_sides_of_midnight(moment, expected):
    """The case the old ``start <= now <= end`` could not express at all."""
    assert window(*NIGHT_SHIFT).contains_moment(at(*map(int, moment.split(":")))) is expected


def test_a_cross_midnight_window_is_identified_as_such():
    assert window(*NIGHT_SHIFT).crosses_midnight is True
    assert window(*DAY_SHIFT).crosses_midnight is False
    assert "overnight" in window(*NIGHT_SHIFT).label()
    assert "overnight" not in window(*DAY_SHIFT).label()


@pytest.mark.parametrize(
    "start,end,moment,expected",
    [
        # Exactly midnight, on a window that ends there and one that starts there.
        ("22:00", "00:00", "00:00", True),
        ("00:00", "06:00", "00:00", True),
        ("00:00", "00:00", "00:00", True),
        # The last minute of the day belongs to an overnight window and not to a day one.
        ("22:00", "06:00", "23:59", True),
        ("04:00", "06:30", "23:59", False),
        # Both ends are inclusive, to the minute - see the module docstring.
        ("04:00", "06:30", "04:00", True),
        ("04:00", "06:30", "06:30", True),
        # start == end is one minute, not a whole day: the misreading that flags nobody is
        # the dangerous one.
        ("06:00", "06:00", "06:00", True),
        ("06:00", "06:00", "06:01", False),
        ("06:00", "06:00", "05:59", False),
        # A site open around the clock expresses it as the full range.
        ("00:00", "23:59", "13:37", True),
    ],
)
def test_midnight_adjacent_boundaries(start, end, moment, expected):
    hours, minutes = (int(part) for part in moment.split(":"))
    assert window(start, end).contains_moment(at(hours, minutes)) is expected


def test_the_pure_predicate_agrees_with_the_window_that_wraps_it():
    """``contains`` is the whole rule; the rest is timezone and provenance around it."""
    for start in (0, 240, 1320):
        for end in (60, 390, 0, 1439):
            for moment in range(0, shift_windows.MINUTES_PER_DAY, 7):
                assert window(
                    shift_windows.format_hhmm(start), shift_windows.format_hhmm(end)
                ).contains_moment(moment_at(moment)) is shift_windows.contains(start, end, moment)


def moment_at(minutes: int) -> datetime:
    return at(minutes // 60, minutes % 60)


# ---------------------------------------------------------------------------
# 3. the timezone decides, not the server
# ---------------------------------------------------------------------------
def test_the_window_is_evaluated_in_the_sites_timezone():
    """One instant, two sites, two answers - which is what ``site_timezone`` is for.

    Kolkata (UTC+5:30, no daylight saving) and Cairo are chosen so the same instant genuinely
    lands in a different hour in each: 21:30 UTC is 03:00 in Kolkata and 00:00 in Cairo in
    September. The expected minutes are computed from ``zoneinfo`` rather than hardcoded, so a
    change to either zone's rules cannot quietly invert this test.
    """
    instant = at(21, 30, zone="UTC")
    kolkata = window("03:00", "04:00", "Asia/Kolkata")
    cairo = window("03:00", "04:00", "Africa/Cairo")

    kolkata_local = instant.astimezone(ZoneInfo("Asia/Kolkata"))
    cairo_local = instant.astimezone(ZoneInfo("Africa/Cairo"))
    assert (kolkata_local.hour, cairo_local.hour) == (3, 0), "pick zones that actually disagree"

    assert kolkata.contains_moment(instant) is True
    assert cairo.contains_moment(instant) is False
    assert kolkata.contains_moment(instant) != cairo.contains_moment(instant)


def test_a_naive_timestamp_means_local_wall_clock_time_on_this_server():
    """Every stored timestamp in this app is naive (``datetime.now()``), so it has to mean
    something specific. It means "the clock on this machine", which is then converted to the
    site's zone - *not* "already the site's local time" and *not* UTC. Pinned by construction
    because the alternative (asserting a fixed answer) only holds where the suite machine is.
    """
    window_riyadh = window("12:00", "12:59", "Asia/Riyadh")
    naive_midnight = datetime(2026, 9, 16, 0, 0)
    riyadh = naive_midnight.astimezone(ZoneInfo("Asia/Riyadh"))
    assert window_riyadh.contains_moment(naive_midnight) is (riyadh.hour == 12)


def test_a_timestamp_that_cannot_be_converted_is_not_a_late_arrival():
    """A device clock set to year 9999 is a bad timestamp, not a disciplinary matter."""
    assert window(*DAY_SHIFT).contains_moment(datetime.max) is True


# ---------------------------------------------------------------------------
# 4. resolution: site -> global -> documented default
# ---------------------------------------------------------------------------
GLOBAL = {"clock_in_window_start": "04:00", "clock_in_window_end": "06:30", "site_timezone": "Africa/Cairo"}


def test_a_site_with_no_overrides_inherits_the_global_window_field_by_field():
    resolved = shift_windows.effective_window({"site_name": "Legacy", **{k: None for k in GLOBAL}}, GLOBAL)
    assert (resolved.start, resolved.end, resolved.timezone) == ("04:00", "06:30", "Africa/Cairo")
    assert resolved.is_site_specific is False
    assert resolved.source_dict()["clock_in_window_start"] == shift_windows.SOURCE_GLOBAL


def test_a_site_overrides_only_what_it_sets():
    """Per field, not per row: a night site keeps the company timezone it never changed."""
    resolved = shift_windows.effective_window(
        {"site_name": ZONE_B, "clock_in_window_start": "21:30", "clock_in_window_end": "05:30", "site_timezone": None},
        GLOBAL,
    )
    assert (resolved.start, resolved.end, resolved.timezone) == ("21:30", "05:30", "Africa/Cairo")
    assert resolved.has_site_hours is True
    assert resolved.is_site_specific is True
    assert resolved.source_dict()["site_timezone"] == shift_windows.SOURCE_GLOBAL


def test_a_site_may_set_only_the_timezone():
    resolved = shift_windows.effective_window(
        {"site_name": "Riyadh", "clock_in_window_start": None, "clock_in_window_end": None, "site_timezone": "Asia/Riyadh"},
        GLOBAL,
    )
    assert (resolved.start, resolved.end, resolved.timezone) == ("04:00", "06:30", "Asia/Riyadh")
    assert resolved.has_site_hours is False


def test_no_site_row_falls_back_to_the_global_rules():
    """A punch with no geofence match, or a site an administrator deleted mid-request."""
    resolved = shift_windows.effective_window(None, GLOBAL)
    assert (resolved.start, resolved.end, resolved.timezone) == ("04:00", "06:30", "Africa/Cairo")
    assert resolved.site_name is None
    assert shift_windows.describe(resolved) == "outside the 04:00-06:30 window"


def test_a_row_missing_the_window_columns_entirely_is_tolerated():
    """``sqlite3.Row`` raises IndexError for a column a query did not select."""
    resolved = shift_windows.effective_window({"site_name": "Narrow"}, GLOBAL)
    assert (resolved.start, resolved.end) == ("04:00", "06:30")


def test_with_no_rules_at_all_the_window_cannot_make_anybody_late():
    """Nothing configured anywhere. Falling back to a closed window would flag a whole site."""
    resolved = shift_windows.effective_window({}, {})
    assert resolved.contains_moment(at(13, 0)) is True
    assert resolved.contains_moment(at(3, 0)) is True


def test_an_unparseable_stored_value_falls_back_instead_of_being_used():
    """Second line of defence: the API and the column CHECK come first, this catches the rest."""
    resolved = shift_windows.effective_window(
        {"site_name": "Typo", "clock_in_window_start": "25:00", "clock_in_window_end": "bogus"},
        GLOBAL,
    )
    assert (resolved.start, resolved.end) == ("00:00", "23:59")
    assert resolved.contains_moment(at(13, 0)) is True


def test_the_helper_loaders_own_signature_works_on_a_plain_mapping():
    """``is_within_site_window(site_rules, current_time)`` - the shape callers hold."""
    rules = {"clock_in_window_start": "21:30", "clock_in_window_end": "05:30", "site_timezone": "Africa/Cairo"}
    assert shift_windows.is_within_site_window(rules, at(23, 15)) is True
    assert shift_windows.is_within_site_window(rules, at(4, 30)) is True
    assert shift_windows.is_within_site_window(rules, at(12, 0)) is False
    # An explicit zone wins over whatever the rules carry.
    assert shift_windows.is_within_site_window(rules, at(12, 0, zone="UTC"), timezone="UTC") is False


def test_the_defaults_cannot_make_anybody_late_by_accident():
    assert shift_windows.DEFAULT_TIMEZONE == "Africa/Cairo", (
        "the fallback zone is a documented value: changing it silently moves every window at "
        "a site that has not configured one"
    )


# ---------------------------------------------------------------------------
# 5. the migration
# ---------------------------------------------------------------------------
def _columns(table: str) -> dict[str, dict]:
    conn = sqlite3.connect(str(DB_PATH))
    try:
        return {
            row[1]: {"type": row[2], "notnull": row[3], "default": row[4]}
            for row in conn.execute(f"PRAGMA table_info({table})")
        }
    finally:
        conn.close()


@contextmanager
def _hand_edit(app_module, column: str, value):
    """Write a window field the way a hand-edited database or a restored dump would.

    ``UPDATE`` rather than the API, because the API is what refuses these values - and the
    point of the test is what the application does when one is already in the table.
    """
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute(
            f"UPDATE construction_sites SET {column} = ? WHERE site_name = ?", (value, DOWNTOWN)
        )
        conn.commit()
    finally:
        conn.close()
    yield
    # ``reset_database`` rebuilds the sites before the next test, so there is nothing to undo
    # here beyond leaving the connection closed.


def test_the_window_columns_are_additive_nullable_and_undefaulted():
    """NULL is the encoding of "this site has not chosen", and it is load-bearing.

    A column default of 'Africa/Cairo' would have been written into every existing site by the
    migration, and a *set* timezone overrides the global one - so an installation whose global
    zone is not Cairo would have had every site silently moved there on upgrade.
    """
    columns = _columns("construction_sites")
    for name in ("clock_in_window_start", "clock_in_window_end", "site_timezone"):
        assert name in columns, f"{name} was not added"
        assert columns[name]["notnull"] == 0, f"{name} must stay nullable"
        assert columns[name]["default"] is None, (
            f"{name} must have no default: a default is a value written into every site that "
            "had already been configured with something else"
        )


def test_a_migrated_database_needs_no_backfill_to_keep_its_old_behaviour():
    """The seeded sites predate the feature, and they still run the global window."""
    rows = db_rows(
        "SELECT site_name, clock_in_window_start, clock_in_window_end, site_timezone "
        "FROM construction_sites"
    )
    assert rows, "the harness seeds sites"
    for row in rows:
        assert tuple(row[1:]) == (None, None, None), row


def test_the_database_itself_refuses_a_time_that_is_not_hhmm():
    """The API is not the only writer: a restored dump or an operator's session reaches here.

    The column CHECK is the same shape as ``HHMM_PATTERN``, which makes this field the one
    place a stored value cannot be unparseable - the application's fallback for an unreadable
    *time* is therefore unreachable through the database, and the reachable case (an unknown
    timezone, which only tzdata can judge) is covered on the punch path instead.
    """
    conn = sqlite3.connect(str(DB_PATH))
    try:
        for bad in ("25:00", "7:30", "22:60", "22:00:00", "22:00 ", " 22:00", "22:00\n"):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO construction_sites (site_name, lat, lon, radius, clock_in_window_start) "
                    "VALUES (?, 1.0, 1.0, 10.0, ?)",
                    (f"bad-{bad.strip()}", bad),
                )
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE construction_sites SET clock_in_window_end = ? WHERE site_name = ?",
                    (bad, DOWNTOWN),
                )
        # ...and accepts the values it is supposed to, including NULL (inherit).
        for good in ("00:00", "09:05", "23:59"):
            conn.execute(
                "INSERT INTO construction_sites (site_name, lat, lon, radius, clock_in_window_start) "
                "VALUES (?, 1.0, 1.0, 10.0, ?)",
                (f"good-{good}", good),
            )
        conn.execute(
            "UPDATE construction_sites SET clock_in_window_start = NULL, clock_in_window_end = NULL "
            "WHERE site_name = ?",
            (DOWNTOWN,),
        )
    finally:
        conn.close()


def test_the_schema_version_names_the_newest_migration():
    """A migration added without bumping this is a server that refuses to boot.

    ``readiness`` compares the database's version against ``SCHEMA_VERSION``, so the failure
    lands on deployment rather than here - and it would not reproduce on the machine of whoever
    forgot, whose database was already migrated. Hence a test rather than a convention.
    """
    import migrations

    assert migrations.SCHEMA_VERSION == max(version for version, _, _ in migrations.MIGRATIONS)
    # Pinned as a number so that bumping the schema is a deliberate act with a test to update,
    # rather than something that happens on the way past. (13 added ``retention_runs``.)
    assert migrations.SCHEMA_VERSION == 13


def test_the_migration_is_replayable_and_idempotent():
    """The drift guard compiles its expectation by replaying the migrations in memory."""
    import migrations

    conn = sqlite3.connect(":memory:")
    try:
        migrations.ensure_schema(conn)
        migrations.run_migrations(conn)
        assert migrations.run_migrations(conn) == []
        columns = {row[1] for row in conn.execute("PRAGMA table_info(construction_sites)")}
        assert {"clock_in_window_start", "clock_in_window_end", "site_timezone"} <= columns
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 6. the admin API
# ---------------------------------------------------------------------------
SITE_PAYLOAD = {
    "site_name": "Night Works",
    "location_input": "30.10,31.40",
    "radius": 80.0,
}


def test_an_admin_can_create_a_site_with_an_overnight_window(client):
    created = client.post(
        "/api/v1/admin/sites/add",
        headers=bearer(ADMIN),
        json={**SITE_PAYLOAD, "clock_in_window_start": "21:30", "clock_in_window_end": "05:30", "site_timezone": "Africa/Cairo"},
    )
    assert created.status_code == 200, created.text[:300]

    row = db_rows(
        "SELECT clock_in_window_start, clock_in_window_end, site_timezone FROM construction_sites "
        "WHERE site_name = ?",
        (SITE_PAYLOAD["site_name"],),
    )[0]
    assert tuple(row) == ("21:30", "05:30", "Africa/Cairo")


def test_creating_a_site_without_window_fields_leaves_them_inheriting(client):
    """Every site that existed before this feature, and every one the console creates."""
    created = client.post("/api/v1/admin/sites/add", headers=bearer(ADMIN), json=SITE_PAYLOAD)
    assert created.status_code == 200, created.text[:300]
    row = db_rows(
        "SELECT clock_in_window_start, clock_in_window_end, site_timezone FROM construction_sites "
        "WHERE site_name = ?",
        (SITE_PAYLOAD["site_name"],),
    )[0]
    assert tuple(row) == (None, None, None)


def test_the_site_list_reports_both_what_is_configured_and_what_is_in_force(client):
    """An administrator needs the resolved window to answer "why was this arrival flagged?".

    Reading only the configured columns leaves a site with no overrides looking unconfigured,
    which is precisely the case where the global rule is the one that applied.
    """
    listed = client.get("/api/v1/admin/sites", headers=bearer(ADMIN))
    assert listed.status_code == 200, listed.text[:200]
    by_name = {site["site_name"]: site for site in listed.json()}
    for name in (DOWNTOWN, ZONE_B):
        site = by_name[name]
        assert site["clock_in_window_start"] is None
        window_info = site["window"]
        assert window_info["clock_in_window_start"] == "04:00"
        assert window_info["clock_in_window_end"] == "06:30"
        assert window_info["site_timezone"] == "Africa/Cairo"
        assert window_info["site_specific"] is False
        assert window_info["source"]["clock_in_window_start"] == "global"


def test_an_admin_can_set_a_window_on_an_existing_site(client):
    edited = client.post(
        "/api/v1/admin/sites/edit",
        headers=bearer(ADMIN),
        json={
            "site_name": ZONE_B,
            "location_input": "29.98,31.75",
            "radius": 100.0,
            "clock_in_window_start": "21:30",
            "clock_in_window_end": "05:30",
        },
    )
    assert edited.status_code == 200, edited.text[:300]
    window_info = _site(client, ZONE_B)["window"]
    assert window_info["clock_in_window_start"] == "21:30"
    assert window_info["clock_in_window_end"] == "05:30"
    assert window_info["crosses_midnight"] is True
    assert window_info["site_specific"] is True


def test_an_edit_that_omits_the_window_leaves_it_alone(client):
    """The console's site form posts four fields; it must not erase a shift on every save.

    This is the whole reason ``edit_site`` consults ``model_fields_set``: an absent key means
    "not mentioned", an explicit ``null`` means "clear it", and a form written before this
    feature existed can only ever do the former.
    """
    client.post(
        "/api/v1/admin/sites/edit",
        headers=bearer(ADMIN),
        json={**_site_payload(ZONE_B), "clock_in_window_start": "21:30", "clock_in_window_end": "05:30"},
    )
    assert _site(client, ZONE_B)["clock_in_window_start"] == "21:30"

    # The old form's payload, to the letter: the three window keys are simply not there.
    moved = client.post("/api/v1/admin/sites/edit", headers=bearer(ADMIN), json=_site_payload(ZONE_B, radius=120.0))
    assert moved.status_code == 200, moved.text[:200]
    site = _site(client, ZONE_B)
    assert site["radius"] == 120.0
    assert site["clock_in_window_start"] == "21:30", "an untouched field was cleared by a save"
    assert site["clock_in_window_end"] == "05:30"


def test_an_override_is_cleared_by_sending_null_explicitly(client):
    client.post(
        "/api/v1/admin/sites/edit",
        headers=bearer(ADMIN),
        json={**_site_payload(ZONE_B), "clock_in_window_start": "21:30", "clock_in_window_end": "05:30"},
    )
    cleared = client.post(
        "/api/v1/admin/sites/edit",
        headers=bearer(ADMIN),
        json={**_site_payload(ZONE_B), "clock_in_window_start": None, "clock_in_window_end": None},
    )
    assert cleared.status_code == 200, cleared.text[:200]
    site = _site(client, ZONE_B)
    assert site["clock_in_window_start"] is None and site["clock_in_window_end"] is None
    assert site["window"]["clock_in_window_start"] == "04:00", "and it inherits again"


@pytest.mark.parametrize("bad", ["7:30", "25:00", "9:60", "22:00:00", "2200", "twenty-two", "21-30"])
def test_a_time_that_is_not_hhmm_is_refused_where_it_is_typed(client, bad):
    response = client.post(
        "/api/v1/admin/sites/add",
        headers=bearer(ADMIN),
        json={**SITE_PAYLOAD, "site_name": f"bad-{bad.strip()}", "clock_in_window_start": bad},
    )
    assert response.status_code == 422, response.text[:300]
    assert "HH:MM" in response.text, response.text[:300]
    # Nothing was written: a rejected configuration must not half-apply.
    assert db_scalar("SELECT COUNT(*) FROM construction_sites WHERE site_name = ?", (f"bad-{bad.strip()}",)) == 0


def test_an_empty_string_means_inherit_rather_than_an_error(client):
    """What an HTML form sends for a field nobody filled in."""
    response = client.post(
        "/api/v1/admin/sites/add",
        headers=bearer(ADMIN),
        json={
            **SITE_PAYLOAD,
            "site_name": "Blank Fields",
            "clock_in_window_start": "",
            "clock_in_window_end": "   ",
            "site_timezone": "",
        },
    )
    assert response.status_code == 200, response.text[:300]
    row = db_rows(
        "SELECT clock_in_window_start, clock_in_window_end, site_timezone FROM construction_sites "
        "WHERE site_name = 'Blank Fields'"
    )[0]
    assert tuple(row) == (None, None, None)


def test_an_unknown_timezone_is_refused(client):
    """Stored, it would silently fall back at every punch and move the whole site's window."""
    response = client.post(
        "/api/v1/admin/sites/add",
        headers=bearer(ADMIN),
        json={**SITE_PAYLOAD, "site_name": "Bad Zone", "site_timezone": "Africa/Cario"},
    )
    assert response.status_code == 422, response.text[:300]
    assert "Africa/Cario" in response.text
    assert db_scalar("SELECT COUNT(*) FROM construction_sites WHERE site_name = 'Bad Zone'") == 0


def test_the_site_edit_form_the_console_sends_still_works(client):
    """A regression guard on the payload the console actually posts - four fields, no window."""
    response = client.post(
        "/api/v1/admin/sites/edit",
        headers=bearer(ADMIN),
        json={**_site_payload(DOWNTOWN), "admin_id": "1000"},
    )
    assert response.status_code == 200, response.text[:300]


def test_the_window_change_is_audited(client):
    """The audit log is where "who changed the shift, and to what" has to be answerable."""
    client.post(
        "/api/v1/admin/sites/edit",
        headers=bearer(ADMIN),
        json={**_site_payload(ZONE_B), "clock_in_window_start": "21:30", "clock_in_window_end": "05:30"},
    )
    entry = db_rows(
        "SELECT after_json FROM audit_log WHERE action = 'site_edit' AND entity_id = ? ORDER BY id DESC",
        (ZONE_B,),
    )[0]
    assert "21:30" in entry[0] and "05:30" in entry[0]


def _site_payload(site_name: str, *, radius: float | None = None) -> dict:
    lat, lon, default_radius = {
        DOWNTOWN: (30.05, 31.23, 65.0),
        ZONE_B: (29.98, 31.75, 100.0),
    }[site_name]
    return {"site_name": site_name, "location_input": f"{lat},{lon}", "radius": radius or default_radius}


def _site(client, site_name: str) -> dict:
    listed = client.get("/api/v1/admin/sites", headers=bearer(ADMIN)).json()
    return next(site for site in listed if site["site_name"] == site_name)


# ---------------------------------------------------------------------------
# 7. the punch path
# ---------------------------------------------------------------------------
class _FrozenClock(datetime):
    """Freezes ``datetime.now`` so a punch can be placed at a chosen hour.

    Only ``now`` is frozen. ``strptime`` and arithmetic keep working, which matters because the
    handler parses stored timestamps in the same request.
    """

    FIXED = at(12, 0)

    @classmethod
    def now(cls, tz=None):
        return cls.FIXED if tz is None else cls.FIXED.astimezone(tz)


@pytest.fixture
def frozen_clock(monkeypatch, app_module):
    def freeze(moment: datetime):
        _FrozenClock.FIXED = moment
        monkeypatch.setattr(app_module, "datetime", _FrozenClock)

    return freeze


def _late_arrivals(worker_id: str) -> list[str]:
    """The arrival notifications for one worker, oldest first.

    Listed rather than counted because this suite runs against a clone of the live database,
    which may already hold late arrivals of its own: an absolute count would fail on data the
    test never wrote. Reading the *list* before and after a punch asserts the same thing and
    cannot be broken by somebody else's history.
    """
    return [
        row[0]
        for row in db_rows(
            "SELECT body FROM admin_notifications WHERE kind = 'late_arrival' AND worker_id = ? "
            "ORDER BY id",
            (worker_id,),
        )
    ]


def test_a_night_site_is_on_time_at_2315(client, app_module, frozen_clock):
    """The bug this feature exists to fix: an overnight window flagged every arrival."""
    with with_site_windows(app_module, {ZONE_B: ("21:30", "05:30", None)}):
        frozen_clock(at(23, 15))
        before = _late_arrivals(MOALLEM)
        response = clock_in(client, MOALLEM, headers=bearer(MOALLEM), coordinates=INSIDE_ZONE_B)

        assert response.status_code == 200, response.text[:300]
        assert db_scalar("SELECT late_flag FROM active_sessions WHERE worker_id = ?", (MOALLEM,)) is None
        assert _late_arrivals(MOALLEM) == before, "an on-time arrival produced a late notification"
        assert "outside the standard window" not in response.json()["message"]


def test_a_night_site_is_late_at_midday(client, app_module, frozen_clock):
    with with_site_windows(app_module, {ZONE_B: ("21:30", "05:30", None)}):
        frozen_clock(at(12, 0))
        before = _late_arrivals(MOALLEM)
        response = clock_in(client, MOALLEM, headers=bearer(MOALLEM), coordinates=INSIDE_ZONE_B)

        assert response.status_code == 200, response.text[:300]
        flag = db_scalar("SELECT late_flag FROM active_sessions WHERE worker_id = ?", (MOALLEM,))
        assert flag, "midday is not a night shift"
        assert "21:30" in flag and "05:30" in flag, flag
        assert "overnight" in flag, "the flag should say which shape of window it missed"

        after = _late_arrivals(MOALLEM)
        assert len(after) == len(before) + 1
        # The notification names the window that was actually applied. It used to hardcode the
        # global 04:00-06:30, which sent an administrator to change a setting that was not the
        # one in force at the site the worker was standing on.
        assert "21:30" in after[-1] and ZONE_B in after[-1]


def test_the_site_next_door_keeps_the_global_window(client, app_module, frozen_clock):
    """Proof that the window is resolved per *site*, not per request or per process.

    Same instant, same handler, same global rules - the only difference is which geofence the
    phone is inside of, and the answers are opposite.
    """
    with with_site_windows(app_module, {ZONE_B: ("21:30", "05:30", None)}):
        frozen_clock(at(23, 15))
        response = clock_in(client, MOALLEM, headers=bearer(MOALLEM), coordinates=INSIDE_DOWNTOWN)

        assert response.status_code == 200, response.text[:300]
        flag = db_scalar("SELECT late_flag FROM active_sessions WHERE worker_id = ?", (MOALLEM,))
        assert flag, "Downtown still runs the company window, and 23:15 is outside it"
        assert "04:00" in flag and "06:30" in flag, flag
        assert "overnight" not in flag


def test_a_site_window_narrower_than_the_global_one_still_flags_inside_the_global_hours(
    client, app_module, frozen_clock
):
    """A site that opens later than the company default: 04:30 is on time globally, late there."""
    with with_site_windows(app_module, {DOWNTOWN: ("05:00", "07:30", None)}):
        frozen_clock(at(4, 30))
        response = clock_in(client, MOALLEM, headers=bearer(MOALLEM), coordinates=INSIDE_DOWNTOWN)

        assert response.status_code == 200, response.text[:300]
        flag = db_scalar("SELECT late_flag FROM active_sessions WHERE worker_id = ?", (MOALLEM,))
        assert flag and "05:00" in flag and "07:30" in flag, flag


def test_a_site_with_no_overrides_is_judged_by_the_global_rule(client, app_module, frozen_clock):
    """The behaviour that existed before per-site windows, unchanged."""
    frozen_clock(at(5, 0))
    response = clock_in(client, MOALLEM, headers=bearer(MOALLEM), coordinates=INSIDE_DOWNTOWN)
    assert response.status_code == 200, response.text[:300]
    assert db_scalar("SELECT late_flag FROM active_sessions WHERE worker_id = ?", (MOALLEM,)) is None


def test_a_site_window_is_read_on_that_sites_clock(client, app_module, frozen_clock):
    """A window is a wall clock at the site, so the same instant can be in one and out of another.

    23:30-01:30 on an Indian site (UTC+5:30) contains 19:00 UTC; the same instant is 22:00 in
    Cairo, so a handler that ignored ``site_timezone`` and used the (global) Cairo clock would
    flag this arrival, and one that used UTC would flag it too.
    """
    with with_site_windows(app_module, {ZONE_B: ("23:30", "01:30", "Asia/Kolkata")}):
        instant = at(19, 0, zone="UTC")
        local = instant.astimezone(ZoneInfo("Asia/Kolkata"))
        assert (local.hour, local.minute) == (0, 30), "in the site's window, and not near its edge"
        assert instant.astimezone(CAIRO).hour == 22, "and well outside it on the global clock"

        frozen_clock(instant)
        response = clock_in(client, MOALLEM, headers=bearer(MOALLEM), coordinates=INSIDE_ZONE_B)

        assert response.status_code == 200, response.text[:300]
        flag = db_scalar("SELECT late_flag FROM active_sessions WHERE worker_id = ?", (MOALLEM,))
        assert flag is None, f"the site's own timezone was not used: {flag}"


def test_the_punch_still_records_when_a_sites_timezone_is_unusable(client, app_module, frozen_clock):
    """A punch is a worker's hours: a configuration mistake must not cost them the punch.

    The timezone has no column CHECK (only the runtime can resolve a tzdata key), so this is the
    one window field that can really reach the database unusable - a typo, or a dump restored on
    a host with an older tzdata. The arrival is then judged on the default clock and still
    recorded; ``readiness`` is what makes the bad value visible, not a 500 at the gate.
    """
    with _hand_edit(app_module, "site_timezone", "Africa/Cario"):
        frozen_clock(at(5, 0))
        response = clock_in(client, MOALLEM, headers=bearer(MOALLEM), coordinates=INSIDE_DOWNTOWN)

        assert response.status_code == 200, response.text[:300]
        assert db_scalar("SELECT COUNT(*) FROM active_sessions WHERE worker_id = ?", (MOALLEM,)) == 1
        assert db_scalar("SELECT late_flag FROM active_sessions WHERE worker_id = ?", (MOALLEM,)) is None


# ---------------------------------------------------------------------------
# 8. the offline path
# ---------------------------------------------------------------------------
def test_the_offline_replay_uses_the_same_predicate():
    """A phone that was offline at 23:15 must not be judged by a different rule in the morning.

    The same arrival, replayed, goes through ``offline_sync`` rather than the request handler -
    and it has to reach the same verdict, or the exception an administrator sees depends on
    whether the phone had signal.
    """
    import offline_sync

    night = window(*NIGHT_SHIFT)
    assert offline_sync._within_clock_in_window(night, at(23, 15)) is True
    assert offline_sync._within_clock_in_window(night, at(4, 30)) is True
    assert offline_sync._within_clock_in_window(night, at(12, 0)) is False
    # And the global window is still the global window.
    day = window(*("04:00", "06:30"), site=False)
    assert offline_sync._within_clock_in_window(day, at(5, 0)) is True
    assert offline_sync._within_clock_in_window(day, at(23, 15)) is False


def test_the_offline_sync_accepts_a_window_object_and_a_plain_rules_mapping():
    """Two callers, two shapes: the replay holds a resolved window, older helpers hold a dict."""
    import offline_sync

    rules = {"clock_in_window_start": "21:30", "clock_in_window_end": "05:30", "site_timezone": "Africa/Cairo"}
    assert offline_sync._within_clock_in_window(rules, at(2, 0)) is True
    assert offline_sync._within_clock_in_window(rules, at(12, 0)) is False


def test_a_queued_punch_is_measured_against_the_site_it_was_taken_at():
    """``_detect_site_row`` returns the row, so the replay has the site's own window."""
    import offline_sync

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(
            "UPDATE construction_sites SET clock_in_window_start = '21:30', "
            "clock_in_window_end = '05:30' WHERE site_name = ?",
            (ZONE_B,),
        )
        conn.commit()
        row = offline_sync._detect_site_row(conn, 29.98, 31.75)
        assert row is not None and row["site_name"] == ZONE_B

        resolved = shift_windows.effective_window(row, GLOBAL)
        assert resolved.contains_moment(at(23, 15)) is True
        assert resolved.contains_moment(at(12, 0)) is False

        # Outside every geofence: no site, so the global rules - which is what the punch path
        # assumes when it flags the punch for review.
        assert offline_sync._detect_site_row(conn, 51.5074, -0.1278) is None
        fallback = shift_windows.effective_window(None, GLOBAL)
        assert fallback.contains_moment(at(5, 0)) is True
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 9. what the operator can see
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "row,expected",
    [
        ({"site_name": "A"}, []),
        ({"site_name": "A", "clock_in_window_start": "04:00", "clock_in_window_end": "06:30"}, []),
        ({"site_name": "A", "clock_in_window_start": None, "site_timezone": "Asia/Riyadh"}, []),
        ({"site_name": "A", "clock_in_window_start": "07"}, ["A.clock_in_window_start='07'"]),
        ({"site_name": "A", "clock_in_window_end": "25:00"}, ["A.clock_in_window_end='25:00'"]),
        ({"site_name": "A", "site_timezone": "Africa/Cario"}, ["A.site_timezone='Africa/Cario'"]),
        (
            {"site_name": "A", "clock_in_window_start": "7:30", "site_timezone": "EET-2"},
            ["A.clock_in_window_start='7:30'", "A.site_timezone='EET-2'"],
        ),
    ],
)
def test_the_checker_flags_every_field_it_could_not_apply(row, expected):
    """The rule lives with the fallback, so the report cannot disagree with what happened."""
    assert shift_windows.window_problems(row) == expected


def test_readiness_reports_a_site_window_it_cannot_apply(app_module):
    """The fallback is what makes the check necessary: without it a typo is invisible.

    The site simply stops having its own window, everyone there is measured against the company
    default, and the only symptom is late flags appearing on a shift nobody changed.
    """
    import readiness

    with _hand_edit(app_module, "site_timezone", "Africa/Cario"):
        check = readiness._check_site_windows({"db_path": str(DB_PATH)})
        assert check.name == "site_clock_in_windows"
        assert check.ok is False, "a window that cannot be applied is worth showing"
        assert check.tier == readiness.TIER_ADVISORY, "and not worth refusing to start for"
        assert DOWNTOWN in check.detail and "Africa/Cario" in check.detail


def test_readiness_is_quiet_when_every_window_is_usable(app_module):
    import readiness

    check = readiness._check_site_windows({"db_path": str(DB_PATH)})
    assert check.ok is True, check.detail
    assert check.tier == readiness.TIER_ADVISORY


def test_the_configured_and_effective_windows_agree_for_a_freshly_configured_site(client):
    """End to end: what an administrator sets is what the gate applies, and what is reported."""
    client.post(
        "/api/v1/admin/sites/edit",
        headers=bearer(ADMIN),
        json={**_site_payload(ZONE_B), "clock_in_window_start": "21:30", "clock_in_window_end": "05:30"},
    )
    window_info = _site(client, ZONE_B)["window"]
    assert (window_info["clock_in_window_start"], window_info["clock_in_window_end"]) == ("21:30", "05:30")
    assert window_info["site_specific"] is True

    import readiness

    assert readiness._check_site_windows({"db_path": str(DB_PATH)}).ok is True


def test_the_old_helper_still_answers_the_way_its_callers_expect(app_module, frozen_clock):
    """``main._within_clock_in_window`` is the name the punch paths use; it must stay honest."""
    frozen_clock(at(5, 0))
    assert app_module._within_clock_in_window(GLOBAL) is True
    frozen_clock(at(12, 0))
    assert app_module._within_clock_in_window(GLOBAL) is False
    # A resolved site window: overnight, and in it.
    frozen_clock(at(23, 15))
    assert app_module._within_clock_in_window(window(*NIGHT_SHIFT)) is True
    # An explicit moment wins over the clock.
    assert app_module._within_clock_in_window(GLOBAL, at(5, 0)) is True
