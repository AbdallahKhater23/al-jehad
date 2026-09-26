"""Site categories: the clock-in window a whole group of sites shares.

WHY THIS SUITE EXISTS
---------------------
A site could already have its own clock-in window, with the company rules as the fallback.
What it could not do was say *which kind of site* it is. A company with four warehouses had to
retune each one separately, and the four copies drifted - which is how one warehouse ends up
measured against hours nobody chose.

A category is the layer between the two: the site's own column, then its category's, then the
company's, resolved **per field** exactly as the site/company pair already was.

Six claims, and they are separable:

1. **The order.** The site wins over its category, and the category wins over the company -
   per field, so a category that names only a start time leaves the end inherited.
2. **One edit, every member.** Retuning a category moves the window of every site inside it
   without writing a row to any of them, so the sites' own columns stay NULL and the answer to
   "why is this arrival late?" stays readable.
3. **No drift for anybody else.** A site that never joined a category resolves exactly as it
   did before categories existed, and a site that has set its own hours is not moved by its
   category at all.
4. **The gate and the timesheet agree.** The punch path and the report grade an arrival with
   the same window, category included, because both fetch the site through
   ``shift_windows.SITE_ROW_SQL``.
5. **A category cannot take hours with it.** Deleting one is refused while sites belong to it,
   rather than silently leaving those sites on the company window.
6. **A hand-edited category is visible.** A zone no runtime can resolve is reported by
   readiness against the *category*, because it moves every site inside it at once.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import harness
import readiness
import shift_windows
from harness import (
    ADMIN,
    DOWNTOWN,
    HEAD_ADMIN,
    INSIDE_DOWNTOWN,
    MOALLEM,
    WORKER,
    ZONE_B,
    bearer,
    clock_in,
    db_rows,
    db_scalar,
)

DB_PATH = harness.DB_PATH

#: The three the migration seeds, in the order Ops says them.
SEEDED = ("مخزن", "مصنع", "مشاريع")

#: The company window a site inherits when neither it nor its category has chosen one.
COMPANY = {
    "clock_in_window_start": "04:00",
    "clock_in_window_end": "06:30",
    "site_timezone": "Asia/Kuwait",
}

#: Where the punch tests stand. Cairo, and the category says so too, so the assertion does not
#: depend on where the machine running the suite happens to be.
CAIRO = "Africa/Cairo"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _site_payload(site_name: str) -> dict:
    lat, lon, radius = {
        DOWNTOWN: (30.05, 31.23, 65.0),
        ZONE_B: (29.98, 31.75, 100.0),
    }[site_name]
    return {"site_name": site_name, "location_input": f"{lat},{lon}", "radius": radius}


def _site(client, site_name: str) -> dict:
    listed = client.get("/api/v1/admin/sites", headers=bearer(ADMIN)).json()
    return next(site for site in listed if site["site_name"] == site_name)


def _categories(client) -> list[dict]:
    response = client.get("/api/v1/admin/site_categories", headers=bearer(ADMIN))
    assert response.status_code == 200, response.text[:300]
    return response.json()


def _category(client, name: str) -> dict:
    return next(row for row in _categories(client) if row["name"] == name)


def _edit_category(client, category_id: int, name: str, **window) -> dict:
    response = client.post(
        "/api/v1/admin/site_categories/edit",
        headers=bearer(ADMIN),
        json={"category_id": category_id, "name": name, **window},
    )
    assert response.status_code == 200, response.text[:300]
    return response.json()


def _tuned(client, name: str, **window) -> dict:
    """Give one of the seeded categories its hours.

    Used instead of creating a new category because the seeded three are the ones Ops will
    actually retune, and because their names are already taken: a second "مخزن" is refused by
    the UNIQUE name, which is a different test's subject.
    """
    existing = _category(client, name)
    _edit_category(client, existing["category_id"], name, **window)
    return _category(client, name)


def _join(client, site_name: str, category_id) -> None:
    """Put a site into a category, or take it out with ``None``, through the site form."""
    response = client.post(
        "/api/v1/admin/sites/edit",
        headers=bearer(ADMIN),
        json={**_site_payload(site_name), "category_id": category_id},
    )
    assert response.status_code == 200, response.text[:300]


def _row(**overrides) -> dict:
    """A site row as one of the shared fetchers returns it: three layers on one row.

    The ``category_*`` names are the contract with ``shift_windows.SITE_ROW_SQL``; a row
    fetched without them is the pre-category world, which one test below pins on purpose.
    """
    base = {
        "site_name": ZONE_B,
        "clock_in_window_start": None,
        "clock_in_window_end": None,
        "site_timezone": None,
        "category_name": None,
        "category_clock_in_window_start": None,
        "category_clock_in_window_end": None,
        "category_site_timezone": None,
    }
    base.update(overrides)
    return base


@contextmanager
def _hand_edit_category(name: str, column: str, value):
    """Write a category field the way a hand-edited database or a restored dump would.

    ``UPDATE`` rather than the API, because the API is what refuses these values - and the
    point of the test is what the application does when one is already in the table. The reset
    between tests rebuilds the database, so there is nothing to undo afterwards.
    """
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute(f"UPDATE site_categories SET {column} = ? WHERE name = ?", (value, name))
        conn.commit()
    finally:
        conn.close()
    yield


class _FrozenClock(datetime):
    """Freezes ``datetime.now`` so a punch can be placed at a chosen hour."""

    FIXED = datetime(2026, 9, 16, 12, 0, tzinfo=ZoneInfo(CAIRO))

    @classmethod
    def now(cls, tz=None):
        return cls.FIXED if tz is None else cls.FIXED.astimezone(tz)


def at_hour(hour: int, minute: int = 0) -> datetime:
    """A fixed instant, at a wall-clock time in the category's zone."""
    return datetime(2026, 9, 16, hour, minute, tzinfo=ZoneInfo(CAIRO))


@pytest.fixture
def frozen_clock(monkeypatch, app_module):
    def freeze(moment: datetime):
        _FrozenClock.FIXED = moment
        monkeypatch.setattr(app_module, "datetime", _FrozenClock)

    return freeze


# ---------------------------------------------------------------------------
# 1. the layer order, with no database and no clock
# ---------------------------------------------------------------------------
def test_a_category_sets_the_window_for_a_site_that_has_not_chosen_one():
    window = shift_windows.effective_window(
        _row(
            category_name="مخزن",
            category_clock_in_window_start="07:00",
            category_clock_in_window_end="15:00",
        ),
        COMPANY,
    )
    assert (window.start, window.end) == ("07:00", "15:00")
    assert window.start_source == shift_windows.SOURCE_CATEGORY
    assert window.end_source == shift_windows.SOURCE_CATEGORY
    assert window.category_name == "مخزن"
    assert window.is_category_specific is True
    assert window.is_site_specific is False, "the site itself has chosen nothing"


def test_a_site_that_has_chosen_its_own_hours_keeps_them():
    """"This site is different" has to survive a category edit, which is the whole point."""
    window = shift_windows.effective_window(
        _row(
            clock_in_window_start="21:30",
            clock_in_window_end="05:30",
            category_name="مخزن",
            category_clock_in_window_start="07:00",
            category_clock_in_window_end="15:00",
        ),
        COMPANY,
    )
    assert (window.start, window.end) == ("21:30", "05:30")
    assert window.start_source == shift_windows.SOURCE_SITE
    assert window.is_category_specific is False


def test_the_fallback_is_per_field_not_per_layer():
    """A category that changes only the opening time leaves the end where it was.

    This is the shape the retune actually takes - "the warehouses open at 07:00 now" - and a
    rule that fell back a whole layer at a time would silently replace the other two fields
    with the company's.
    """
    window = shift_windows.effective_window(
        _row(category_name="مخزن", category_clock_in_window_start="07:00"), COMPANY
    )
    assert window.start == "07:00" and window.start_source == shift_windows.SOURCE_CATEGORY
    assert window.end == "06:30" and window.end_source == shift_windows.SOURCE_GLOBAL
    assert window.timezone == "Asia/Kuwait"
    assert window.timezone_source == shift_windows.SOURCE_GLOBAL


def test_a_site_with_no_category_resolves_exactly_as_it_did_before():
    window = shift_windows.effective_window(_row(), COMPANY)
    assert (window.start, window.end) == ("04:00", "06:30")
    assert window.start_source == shift_windows.SOURCE_GLOBAL
    assert window.is_category_specific is False
    assert window.category_name is None


def test_a_row_fetched_without_the_join_has_no_category_layer():
    """The layer is read off columns, so a query that does not select them is not misled.

    ``shift_windows._field`` answers ``None`` for a column that is not in the result set, which
    is the same thing as "not configured" here - so a path that fetches the site by hand gets
    the window it got before categories existed rather than a wrong one.
    """
    unjoined = {"site_name": ZONE_B, "clock_in_window_start": None, "clock_in_window_end": None}
    window = shift_windows.effective_window(unjoined, COMPANY)
    assert (window.start, window.end) == ("04:00", "06:30")
    assert window.start_source == shift_windows.SOURCE_GLOBAL


def test_the_resolved_window_says_which_category_it_came_through():
    info = shift_windows.effective_window(
        _row(category_name="مصنع", category_site_timezone=CAIRO), COMPANY
    ).as_dict()
    assert info["category"] == "مصنع"
    assert info["category_specific"] is True
    assert info["source"]["site_timezone"] == shift_windows.SOURCE_CATEGORY
    assert info["site_specific"] is False


# ---------------------------------------------------------------------------
# 2. the migration and what it seeds
# ---------------------------------------------------------------------------
def test_the_three_categories_ops_names_are_seeded():
    names = {row[0] for row in db_rows("SELECT name FROM site_categories")}
    assert names == set(SEEDED), names


def test_the_seeded_categories_have_no_hours_of_their_own():
    """Seeding must not move a single window: NULL means "inherit", as it does on a site."""
    assert db_rows(
        "SELECT name FROM site_categories WHERE clock_in_window_start IS NOT NULL "
        "OR clock_in_window_end IS NOT NULL OR site_timezone IS NOT NULL"
    ) == []


def test_the_site_column_is_additive_nullable_and_undefaulted():
    """NULL is the encoding of "this site belongs to no category", and it is load-bearing."""
    columns = {row[1]: row for row in db_rows("PRAGMA table_info(construction_sites)")}
    assert "category_id" in columns, "migration 25 did not land"
    assert columns["category_id"][3] == 0, "a NOT NULL would have refused every existing site"
    assert columns["category_id"][4] is None, "a default would have joined them to something"
    assert db_rows("SELECT site_name FROM construction_sites WHERE category_id IS NOT NULL") == []


def test_the_seeding_is_idempotent(app_module):
    """The migrator runs on every reset and on every boot; the seed must not duplicate."""
    app_module.init_db()
    app_module.init_db()
    assert len(db_rows("SELECT name FROM site_categories")) == len(SEEDED)


# ---------------------------------------------------------------------------
# 3. the API: one edit, every member site
# ---------------------------------------------------------------------------
def test_a_category_is_created_and_listed_with_its_hours(client):
    response = client.post(
        "/api/v1/admin/site_categories/add",
        headers=bearer(ADMIN),
        json={"name": "مصنع الشمال", "clock_in_window_start": "07:00", "clock_in_window_end": "15:00"},
    )
    assert response.status_code == 200, response.text[:300]
    created = _category(client, "مصنع الشمال")
    assert (created["clock_in_window_start"], created["clock_in_window_end"]) == ("07:00", "15:00")
    assert created["site_count"] == 0
    assert created["site_timezone"] is None, "an hour without a zone inherits the company's"


@pytest.mark.parametrize("name", ["", "   ", "مخزن"])
def test_a_category_name_must_be_new_and_not_empty(client, name):
    response = client.post(
        "/api/v1/admin/site_categories/add", headers=bearer(ADMIN), json={"name": name}
    )
    assert response.status_code in (400, 422), response.text[:300]


def test_a_site_joins_a_category_and_reports_it(client):
    warehouse = _tuned(client, "مخزن", clock_in_window_start="07:00", clock_in_window_end="15:00")
    _join(client, ZONE_B, warehouse["category_id"])

    site = _site(client, ZONE_B)
    assert site["category_id"] == warehouse["category_id"]
    assert site["category"] == "مخزن"
    assert (site["clock_in_window_start"], site["clock_in_window_end"]) == (None, None), (
        "nothing was copied onto the site"
    )
    assert site["window"]["source"]["clock_in_window_start"] == "category"
    assert site["window"]["category"] == "مخزن"
    assert site["window"]["clock_in_window_start"] == "07:00"


def test_an_unknown_category_is_refused_where_it_is_typed(client):
    response = client.post(
        "/api/v1/admin/sites/edit",
        headers=bearer(ADMIN),
        json={**_site_payload(ZONE_B), "category_id": 999999},
    )
    assert response.status_code == 404, response.text[:300]
    assert _site(client, ZONE_B)["category_id"] is None, "and nothing was written"


def test_editing_a_category_moves_every_site_inside_it(client):
    warehouse = _tuned(client, "مخزن", clock_in_window_start="07:00", clock_in_window_end="15:00")
    _join(client, ZONE_B, warehouse["category_id"])
    _join(client, DOWNTOWN, warehouse["category_id"])

    _edit_category(
        client,
        warehouse["category_id"],
        "مخزن",
        clock_in_window_start="08:00",
        clock_in_window_end="16:00",
    )

    for site_name in (ZONE_B, DOWNTOWN):
        window = _site(client, site_name)["window"]
        assert (window["clock_in_window_start"], window["clock_in_window_end"]) == (
            "08:00",
            "16:00",
        ), site_name
        assert window["source"]["clock_in_window_start"] == "category"
    assert db_scalar(
        "SELECT COUNT(*) FROM construction_sites WHERE clock_in_window_start IS NOT NULL"
    ) == 0, "one row changed, not two sites"


def test_a_site_with_its_own_hours_is_not_moved_by_its_category(client):
    warehouse = _tuned(client, "مخزن", clock_in_window_start="07:00", clock_in_window_end="15:00")
    client.post(
        "/api/v1/admin/sites/edit",
        headers=bearer(ADMIN),
        json={
            **_site_payload(ZONE_B),
            "clock_in_window_start": "21:30",
            "clock_in_window_end": "05:30",
        },
    )
    _join(client, ZONE_B, warehouse["category_id"])

    _edit_category(
        client,
        warehouse["category_id"],
        "مخزن",
        clock_in_window_start="09:00",
        clock_in_window_end="17:00",
    )

    window = _site(client, ZONE_B)["window"]
    assert (window["clock_in_window_start"], window["clock_in_window_end"]) == ("21:30", "05:30")
    assert window["source"]["clock_in_window_start"] == "site"
    assert window["category"] == "مخزن", "it still belongs to the category; it just overrides it"


def test_a_site_outside_every_category_still_follows_the_company_rules(client):
    """The regression floor: the sites that exist today must not move."""
    window = _site(client, DOWNTOWN)["window"]
    assert window["source"]["clock_in_window_start"] == "global"
    assert window["category"] is None
    assert window["category_specific"] is False


def test_editing_a_site_without_sending_a_category_leaves_membership_alone(client):
    """Moving a pin on the map is not a reason to empty a site's warehouse.

    The site form posts the category only when the control is on screen, so an absent key must
    mean "leave it" - the same rule the window fields already follow (``model_fields_set``).
    """
    warehouse = _tuned(client, "مخزن", clock_in_window_start="07:00")
    _join(client, ZONE_B, warehouse["category_id"])

    response = client.post(
        "/api/v1/admin/sites/edit", headers=bearer(ADMIN), json=_site_payload(ZONE_B)
    )
    assert response.status_code == 200, response.text[:300]
    assert _site(client, ZONE_B)["category_id"] == warehouse["category_id"]


def test_a_site_can_be_taken_out_of_its_category_with_an_explicit_null(client):
    warehouse = _tuned(client, "مخزن", clock_in_window_start="07:00")
    _join(client, ZONE_B, warehouse["category_id"])
    _join(client, ZONE_B, None)

    site = _site(client, ZONE_B)
    assert site["category_id"] is None
    assert site["window"]["clock_in_window_start"] == "04:00"
    assert site["window"]["source"]["clock_in_window_start"] == "global"


def test_a_category_holding_sites_cannot_be_deleted(client):
    warehouse = _tuned(client, "مخزن", clock_in_window_start="07:00")
    _join(client, ZONE_B, warehouse["category_id"])

    response = client.post(
        "/api/v1/admin/site_categories/delete",
        headers=bearer(ADMIN),
        data={"category_id": warehouse["category_id"]},
    )
    assert response.status_code == 409, response.text[:300]
    assert "1 site" in response.json()["detail"], response.json()["detail"]
    assert _category(client, "مخزن")["site_count"] == 1, "and it is still there"

    _join(client, ZONE_B, None)
    assert client.post(
        "/api/v1/admin/site_categories/delete",
        headers=bearer(ADMIN),
        data={"category_id": warehouse["category_id"]},
    ).status_code == 200


def test_renaming_a_category_is_one_row_not_a_rewrite_of_its_sites(client):
    """The id is the key; the name is a label. A typo must not be a migration."""
    warehouse = _tuned(client, "مخزن", clock_in_window_start="07:00")
    _join(client, ZONE_B, warehouse["category_id"])

    _edit_category(client, warehouse["category_id"], "المخزن")

    site = _site(client, ZONE_B)
    assert site["category_id"] == warehouse["category_id"]
    assert site["category"] == "المخزن"
    assert site["window"]["clock_in_window_start"] == "07:00", "a rename is not a retune"


def test_a_head_admin_can_read_the_categories_and_a_worker_cannot(client):
    assert client.get("/api/v1/admin/site_categories", headers=bearer(HEAD_ADMIN)).status_code == 200
    assert client.get("/api/v1/admin/site_categories", headers=bearer(WORKER)).status_code in (401, 403)


# ---------------------------------------------------------------------------
# 4. the gate resolves through the same layers
# ---------------------------------------------------------------------------
def test_a_warehouse_arrival_inside_its_category_window_is_on_time(
    client, app_module, frozen_clock
):
    """The integration claim: an unwired layer is correct and useless."""
    warehouse = _tuned(
        client,
        "مخزن",
        clock_in_window_start="21:30",
        clock_in_window_end="05:30",
        site_timezone=CAIRO,
    )
    _join(client, DOWNTOWN, warehouse["category_id"])

    frozen_clock(at_hour(23, 15))
    response = clock_in(client, MOALLEM, headers=bearer(MOALLEM), coordinates=INSIDE_DOWNTOWN)

    assert response.status_code == 200, response.text[:300]
    assert db_scalar("SELECT late_flag FROM active_sessions WHERE worker_id = ?", (MOALLEM,)) is None


def test_the_same_warehouse_flags_an_arrival_outside_its_category_window(
    client, app_module, frozen_clock
):
    warehouse = _tuned(
        client,
        "مخزن",
        clock_in_window_start="21:30",
        clock_in_window_end="05:30",
        site_timezone=CAIRO,
    )
    _join(client, DOWNTOWN, warehouse["category_id"])

    frozen_clock(at_hour(12, 0))
    assert clock_in(
        client, MOALLEM, headers=bearer(MOALLEM), coordinates=INSIDE_DOWNTOWN
    ).status_code == 200

    flag = db_scalar("SELECT late_flag FROM active_sessions WHERE worker_id = ?", (MOALLEM,))
    assert flag, "midday is not this warehouse's shift"
    assert "21:30" in flag and "05:30" in flag, flag


def test_the_timesheet_marks_the_category_a_shift_was_worked_under(client, app_module):
    """So the board can group and search by it without a request per row."""
    warehouse = _tuned(
        client,
        "مخزن",
        clock_in_window_start="00:00",
        clock_in_window_end="23:59",
        site_timezone=CAIRO,
    )
    _join(client, DOWNTOWN, warehouse["category_id"])

    assert clock_in(client, MOALLEM, headers=bearer(MOALLEM), coordinates=INSIDE_DOWNTOWN).status_code == 200
    assert clock_in(
        client, MOALLEM, action="Clock Out", headers=bearer(MOALLEM), confirmed=True
    ).status_code == 200

    today = datetime.now().date()
    report = client.get(
        "/api/v1/admin/reports/shifts",
        headers=bearer(ADMIN),
        params={
            "start": str(today),
            "end": str(today + timedelta(days=1)),
        },
    ).json()
    rows = [
        row
        for row in report["rows"]
        if row["site_name"] == DOWNTOWN and row["worker_id"] == MOALLEM
    ]
    assert rows, "the punch pair produced no timesheet row"
    assert rows[0]["site_category"] == "مخزن"


# ---------------------------------------------------------------------------
# 5. a hand-edited category is visible
# ---------------------------------------------------------------------------
def test_readiness_reports_a_category_zone_it_cannot_apply(app_module):
    """A category's bad zone moves every site inside it, so it is named as the category."""
    with _hand_edit_category("مصنع", "site_timezone", "Africa/Cario"):
        check = readiness._check_site_windows({"db_path": str(DB_PATH)})

    assert check.name == "site_clock_in_windows"
    assert check.ok is False, check.detail
    assert check.tier == readiness.TIER_ADVISORY, "and not worth refusing to start for"
    assert "category مصنع" in check.detail, check.detail
    assert "Africa/Cario" in check.detail


def test_readiness_is_quiet_when_no_category_has_been_tuned(app_module):
    assert readiness._check_site_windows({"db_path": str(DB_PATH)}).ok is True
