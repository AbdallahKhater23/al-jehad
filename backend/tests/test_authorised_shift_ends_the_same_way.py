"""A shift ends four ways, and an answer given while it was running has to survive all four.

WHY THIS EXISTS
---------------
``overtime.apply_authorisation`` is the one place a mid-shift decision is cashed in, and only
the worker's own clock-out called it. The other three ways a shift ends - the quick link, the
offline sync that materialises a queued punch, and an administrator's force clock-out - asked
``shift_hours.overtime_assessment`` and nothing else, so the same shift, closed on the same day
by the same worker, settled differently depending on which button closed it:

* an operator answers a crossing at 19:00 with a 12 h ceiling for a shift already past the line;
* the worker taps the quick link at 21:00 having worked 9.5 paid hours;
* the old arithmetic held ``9.5 - 8 = 1.5 h`` for a second approval nobody could give, because
  the shift had ended and the crossing was no longer a question the queue was asking.

An answer that a later clock-out contradicts is worse than no answer: the operator believes the
extra time is authorised, and the row says it is not. So each path is pinned twice here - once
with a ceiling (it settles at it) and once without (the excess is still held, which is what
stops these tests from passing on a change that approves everything).

Two things follow the close into this file as well. The **auto-close** is the one path with no
human in it, and an answer is a permission to run past the paid day - so the close waits inside
the window it was given instead of ending the day at eight hours anyway, and ends the day at the
ceiling when the window runs out (not never: an answer nothing can bound is how a session runs
for a week). And every settling path **spends** the decision it settled by, so an answer that
has been paid for is no longer live for the worker's next shift.

The guards are the last two tests rather than cases: they derive the set of functions that end a
shift and assert each one asks the decision and spends it, so the *next* path added cannot
quietly skip either half.

    pytest backend/tests/test_authorised_shift_ends_the_same_way.py -q
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta

import pytest
import harness
from database import db
from fastapi.testclient import TestClient
from harness import (
    ADMIN,
    HEAD_ADMIN,
    INSIDE_DOWNTOWN,
    WORKER,
    bearer,
    db_scalar,
    jpeg_bytes,
)

import main
import migrations
import offline_sync
import overtime
import quick_links
import shift_hours

TS = "%Y-%m-%d %H:%M:%S"
SITE = "Downtown Tower A"
NOTE = "the pour ran long - roofers are on the east block until nine"

#: Long enough that the shipped rules hold hours over the line, with the unpaid break included:
#: 10 h on site is 9.5 h paid, which is 1.5 h past the 8 h paid day.
ON_SITE_HOURS = 10.0

#: What the operator authorises in these tests: more than the shift will turn out to be, which
#: is the ordinary shape of an answer given mid-shift ("carry on until this is finished").
CEILING = 12.0


@pytest.fixture(scope="module")
def client(app_module):
    """This module's own client, without the lifespan that boots the deployment.

    Deliberately not the session-scoped ``client``: entering that context manager runs the
    startup gate and the timers again, so a checkout whose face-match bands are mid-experiment
    leaves every file that boots the app unable to run - including the ones about overtime,
    which have nothing to do with faces. What must not happen is this file passing because it
    never sent a request: the force clock-out and the link punch below go through the real ASGI
    stack, the routes' own guards and their status codes.
    """
    instance = TestClient(app_module.app)
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture(autouse=True)
def _no_standing_review_flag():
    """Clear the flagged row the harness leaves for worker 1.

    The seed data carries one ``pending_review`` row as a standing fixture for the approval
    suites, and a flagged account is refused a clock-out on the password path *and* on the
    quick-link one. Without this, the link test would measure that rule instead of the ceiling.
    """
    with db(write=True) as conn:
        conn.execute("DELETE FROM attendance_logs WHERE status = 'pending_review'")


@pytest.fixture(autouse=True)
def _quick_photo_dir(tmp_path_factory):
    """Keep the link punch's selfie out of the checkout."""
    original = quick_links.PHOTOS_DIR
    quick_links.PHOTOS_DIR = str(tmp_path_factory.mktemp("authorised_close_photos"))
    yield
    quick_links.PHOTOS_DIR = original


# ---------------------------------------------------------------------------
# helpers: an open shift, an answer to it, and what the close recorded
# ---------------------------------------------------------------------------
def _rules() -> dict:
    with db() as conn:
        return overtime.rules(conn)


def _set_rules(**overrides) -> dict:
    """The shipped rules, *with the automatic close switched on*, and the named ones replaced.

    The close has to be switched on explicitly because it now ships off (see
    ``shift_hours.day_end_rules``): every case in this file's close section asks what the close
    does when it acts, so the baseline here is the close acting. An override of
    ``auto_close_at_regular`` still wins.

    Written as a row rather than through ``POST /admin/shift_rules`` because this file's
    subject is what the *watcher* does with a rule, not the route that stores one - and the
    route is pinned by its own suite.
    """
    values = dict(migrations.DEFAULT_SHIFT_RULES)
    values["auto_close_at_regular"] = 1
    values.update(overrides)
    with db(write=True) as conn:
        cursor = conn.execute(
            "UPDATE shift_rules SET regular_hours = ?, overtime_notify_hours = ?, "
            "break_minutes = ?, break_after_hours = ?, auto_close_at_regular = ? WHERE id = 1",
            (
                values["regular_hours"],
                values["overtime_notify_hours"],
                values["break_minutes"],
                values["break_after_hours"],
                values["auto_close_at_regular"],
            ),
        )
        assert cursor.rowcount == 1, "there is no shift_rules row to set"
    return values


def _authorisation(worker_id: str = WORKER):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM overtime_authorisations WHERE worker_id = ? ORDER BY id DESC LIMIT 1",
            (worker_id,),
        ).fetchone()


def _open_shift(worker_id: str = WORKER) -> int:
    with db() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM active_sessions WHERE worker_id = ?", (worker_id,)
        ).fetchone()["n"]


def _plant_open_shift(worker_id: str = WORKER, *, hours_on_site: float = ON_SITE_HOURS) -> str:
    """An open shift, as the application leaves it mid-shift. Returns the stored clock-in.

    Both rows: the session the close reads, and the clock-in the timesheet is built from. A
    session without one is a state the application never produces, and the reports would show
    the shift as having no beginning.
    """
    clock_in = (datetime.now() - timedelta(hours=hours_on_site)).strftime(TS)
    with db(write=True) as conn:
        conn.execute("DELETE FROM active_sessions WHERE worker_id = ?", (worker_id,))
        conn.execute("DELETE FROM overtime_authorisations WHERE worker_id = ?", (worker_id,))
        conn.execute(
            "INSERT INTO active_sessions (worker_id, site_name, clock_in_time, start_source) "
            "VALUES (?, ?, ?, 'online')",
            (worker_id, SITE, clock_in),
        )
        main._insert_log(
            conn,
            worker_id=worker_id,
            site_name=SITE,
            action=main.ACTION_CLOCK_IN,
            timestamp=clock_in,
            hours=0.0,
            score=1.0,
            status="approved",
            status_code="approved",
            source="online",
        )
    return clock_in


def _authorise(worker_id: str, clock_in: str, hours: float, *, by: str = HEAD_ADMIN) -> None:
    """The operator's answer, written as ``decide_crossing`` writes it: append-only, one live row.

    Raw SQL rather than the endpoint because the *answer* is not what this file is about - the
    queue's own suite proves that - and going through the route would make every case here
    depend on the queue's payload agreeing with the clock-out paths, which is a second subject.
    """
    with db(write=True) as conn:
        conn.execute(
            "INSERT INTO overtime_authorisations (worker_id, clock_in_time, decision, "
            "authorised_hours, recorded_hours_at_decision, decided_by, decided_at, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                worker_id,
                clock_in,
                overtime.DECISION_AUTHORISED,
                hours,
                hours - 2.0,
                by,
                datetime.now().strftime(TS),
                NOTE,
            ),
        )


def _admin_notification(kind: str):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM admin_notifications WHERE kind = ? ORDER BY id DESC LIMIT 1", (kind,)
        ).fetchone()


def _last_clock_out(worker_id: str = WORKER):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM attendance_logs WHERE worker_id = ? AND action = ? "
            "ORDER BY id DESC LIMIT 1",
            (worker_id, main.ACTION_CLOCK_OUT),
        ).fetchone()


def _queue_clock_out(worker_id: str, at: datetime) -> None:
    """A queued clock-out as the phone hands it over: verified, undecided, and hours old.

    Inserted rather than signed into the queue because the signature is not this file's
    subject - ``test_offline_client_signing`` owns it - and what matters here is the *effective
    time*, which is what the materialiser reads to price the shift.
    """
    stamp = at.strftime(TS)
    with db(write=True) as conn:
        conn.execute(
            "INSERT INTO punch_queue (client_punch_id, device_id, worker_id, action, "
            "client_timestamp, lat, lon, anchor_server_time, monotonic_offset_s, signature, "
            "received_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'accepted')",
            (
                f"authorised-close-{worker_id}-{stamp}",
                "authorised-close-device",
                worker_id,
                main.ACTION_CLOCK_OUT,
                stamp,
                30.05,
                31.23,
                stamp,
                0.0,
                "not-this-file's-subject",
                stamp,
            ),
        )


def _issue_link(client, worker_id: str = WORKER) -> str:
    response = client.post(
        "/api/v1/admin/quick_links", headers=bearer(HEAD_ADMIN), json={"worker_id": worker_id}
    )
    assert response.status_code == 200, response.text
    return response.json()["token"]


def _tap_link(client, token: str):
    lat, lon = INSIDE_DOWNTOWN.split(",")
    return client.post(
        f"/api/v1/q/{token}",
        data={"lat": lat, "lon": lon, "confirm_early_checkout": "1"},
        files={"selfie": ("selfie.jpg", jpeg_bytes(), "image/jpeg")},
    )


def _force_clock_out(client, worker_id: str = WORKER):
    return client.post(
        "/api/v1/admin/force_clock_out", headers=bearer(ADMIN), json={"worker_id": worker_id}
    )


# ---------------------------------------------------------------------------
# 1. the administrator's force clock-out
# ---------------------------------------------------------------------------
def test_a_force_clock_out_settles_at_the_ceiling(client, app_module):
    """The override is about *who* decides, not about re-deciding what was already answered."""
    clock_in = _plant_open_shift()
    _authorise(WORKER, clock_in, CEILING)

    response = _force_clock_out(client)
    assert response.status_code == 200, response.text

    row = _last_clock_out()
    assert row is not None, "the force clock-out wrote no row"
    assert row["status_code"] == "approved", row
    assert row["hours"] == pytest.approx(9.5, abs=0.01), row
    assert not row["overtime_hours"], (
        "the shift was closed by an administrator hours after somebody authorised it, and the "
        f"row still holds hours for a second approval: {dict(row)}"
    )
    assert _authorisation()["consumed_by_log_id"] == row["id"], (
        "the force clock-out settled the shift without spending the answer it settled it by"
    )
    assert db_scalar("SELECT COUNT(*) FROM active_sessions WHERE worker_id = ?", (WORKER,)) == 0


def test_a_force_clock_out_without_an_answer_still_holds_the_excess(client, app_module):
    """The control: nothing was decided, so the hours past the paid day are still held.

    Without this the test above would pass on a change that simply stopped costing overtime on
    this path - which would be a much worse bug than the one being fixed.
    """
    _plant_open_shift()

    response = _force_clock_out(client)
    assert response.status_code == 200, response.text

    row = _last_clock_out()
    assert row["overtime_hours"] == pytest.approx(1.5, abs=0.01), row


def test_a_force_clock_out_can_record_the_hours_the_administrator_names(client, app_module):
    """The forgotten clock-out: the clock says 12 h, the administrator authorises 10.

    The session has run on for hours the worker was not on site, so the clock's own figure is
    nobody's estimate of the shift. The named figure is what the row records - and the audit
    event carries the clock's figure beside it, so the gap between the two stays answerable.
    Hours past the paid day are held exactly as an answered crossing holds them, which keeps
    this override and the Approvals queue writing the same row for the same answer.
    """
    ON_SITE_HOURS
    _plant_open_shift(hours_on_site=12.5)

    response = client.post(
        "/api/v1/admin/force_clock_out",
        headers=bearer(ADMIN),
        json={"worker_id": WORKER, "hours": 10.0},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["hours"] == pytest.approx(10.0, abs=0.01), body
    assert "10.00" in body["message"] and "12" in body["message"], body

    row = _last_clock_out()
    assert row["hours"] == pytest.approx(10.0, abs=0.01), row
    # 10 h named against an 8 h paid day: 2 h of overtime, held for the ordinary approval.
    assert row["overtime_hours"] == pytest.approx(2.0, abs=0.01), row
    assert row["status_code"] == "approved", row


def test_a_force_clock_out_refuses_an_hours_figure_that_cannot_be_meant(client, app_module):
    _plant_open_shift(hours_on_site=1.0)
    before = db_scalar("SELECT COUNT(*) FROM attendance_logs WHERE action = ?", (main.ACTION_CLOCK_OUT,))

    response = client.post(
        "/api/v1/admin/force_clock_out",
        headers=bearer(ADMIN),
        json={"worker_id": WORKER, "hours": overtime.MAX_AUTHORISED_HOURS + 1},
    )
    assert response.status_code == 400, response.text
    assert "between 0 and" in response.json()["detail"], response.text
    # Nothing was written: the shift is still open and no new clock-out row exists.
    assert db_scalar("SELECT COUNT(*) FROM active_sessions WHERE worker_id = ?", (WORKER,)) == 1
    after = db_scalar("SELECT COUNT(*) FROM attendance_logs WHERE action = ?", (main.ACTION_CLOCK_OUT,))
    assert after == before, "the refused override wrote a clock-out row"


def test_the_hours_worked_decide_what_is_held_not_the_ceiling(client, app_module):
    """The ceiling is a limit, not a figure to be paid: the hours decide what is held past it.

    The same answer has to end two different shifts differently, or the number the operator
    typed is decoration. 12 h authorised against a 9.5 h shift holds nothing; against a 13 h
    shift it holds only the half hour past the ceiling - not the 4.5 h the answer mentions.
    """
    clock_in = _plant_open_shift()
    _authorise(WORKER, clock_in, CEILING)
    assert _force_clock_out(client).status_code == 200
    assert not _last_clock_out()["overtime_hours"]

    clock_in = _plant_open_shift(hours_on_site=13.0)
    _authorise(WORKER, clock_in, CEILING)
    assert _force_clock_out(client).status_code == 200
    row = _last_clock_out()
    assert row["overtime_hours"] == pytest.approx(0.5, abs=0.02), (
        f"a shift past the ceiling it was given holds the hours past it: {dict(row)}"
    )


# ---------------------------------------------------------------------------
# 2. the offline sync that materialises a queued clock-out
# ---------------------------------------------------------------------------
def test_a_queued_clock_out_settles_at_the_ceiling_but_still_wants_a_human(app_module):
    """Two questions, two answers: the *hours* are settled, the *punch* is not verified.

    A materialised punch has nobody's word for it - no face, no frame, no site check at the
    moment it was taken - so it is in review whichever way the money falls. What the ceiling
    changes is the hold *reason*: the queue should not ask about hours somebody already
    authorised.
    """
    clock_in = _plant_open_shift()
    _authorise(WORKER, clock_in, CEILING)
    _queue_clock_out(WORKER, datetime.strptime(clock_in, TS) + timedelta(hours=ON_SITE_HOURS))

    with db(write=True) as conn:
        offline_sync.materialize_worker(conn, WORKER)

    row = _last_clock_out()
    assert row is not None, "the queued clock-out was never materialised"
    assert row["status_code"] == offline_sync.STATUS_CODE_PENDING_REVIEW, (
        f"a punch that arrived with nobody watching is not in review: {dict(row)}"
    )
    assert not row["overtime_hours"], (
        "the queued clock-out asked for approval of hours that had already been authorised "
        f"while the shift was running: {dict(row)}"
    )
    assert row["hours"] == pytest.approx(9.5, abs=0.01), row
    assert _authorisation()["consumed_by_log_id"] == row["id"], (
        "the materialised clock-out spent no answer, so the decision is still live"
    )


def test_a_queued_clock_out_without_an_answer_is_held_for_overtime(app_module):
    """The control, and the case the offline suite already pins: no answer, so it is a hold."""
    clock_in = _plant_open_shift()
    _queue_clock_out(WORKER, datetime.strptime(clock_in, TS) + timedelta(hours=ON_SITE_HOURS))

    with db(write=True) as conn:
        offline_sync.materialize_worker(conn, WORKER)

    row = _last_clock_out()
    assert row["status_code"] == offline_sync.STATUS_CODE_PENDING_OVERTIME, row
    assert row["overtime_hours"] == pytest.approx(1.5, abs=0.01), row


def test_an_answer_for_an_earlier_shift_does_not_authorise_this_one(app_module):
    """The pairing is the shift, not the worker: a decision left live for an old clock-in is not
    an answer to the question this shift is asking.

    That state is reachable - nothing consumes an authorisation when a shift ends, deliberately,
    because the clock-in it names is what identifies the shift - so a generous decision for a
    shift months ago must not quietly pay for tonight's hours.
    """
    clock_in = _plant_open_shift()
    _authorise(WORKER, "2026-01-01 06:00:00", CEILING)
    _queue_clock_out(WORKER, datetime.strptime(clock_in, TS) + timedelta(hours=ON_SITE_HOURS))

    with db(write=True) as conn:
        offline_sync.materialize_worker(conn, WORKER)

    row = _last_clock_out()
    assert row["status_code"] == offline_sync.STATUS_CODE_PENDING_OVERTIME, (
        f"an answer for a shift that ended months ago authorised a new one: {dict(row)}"
    )
    assert row["overtime_hours"] == pytest.approx(1.5, abs=0.01), row


# ---------------------------------------------------------------------------
# 3. the quick link
# ---------------------------------------------------------------------------
def test_a_quick_link_clock_out_settles_at_the_ceiling(client, app_module):
    """A link is a second way to close the same shift, not a second policy."""
    clock_in = _plant_open_shift()
    _authorise(WORKER, clock_in, CEILING)
    token = _issue_link(client)

    response = _tap_link(client, token)
    assert response.status_code == 200, response.text

    row = _last_clock_out()
    assert row["source"] == "quick_link", row
    assert row["status_code"] == "approved", (
        "a shift authorised while it was running came out of a link punch held for approval "
        f"anyway: {dict(row)}"
    )
    assert not row["overtime_hours"], row
    assert _authorisation()["consumed_by_log_id"] == row["id"], (
        "the link punch settled the shift without spending the answer it settled it by"
    )


def test_a_quick_link_clock_out_without_an_answer_is_held_for_overtime(client, app_module):
    """The control: the same tap, with nobody having answered, still holds the excess."""
    _plant_open_shift()
    token = _issue_link(client)

    response = _tap_link(client, token)
    assert response.status_code == 200, response.text

    row = _last_clock_out()
    assert row["status_code"] == "pending_overtime", row
    assert row["overtime_hours"] == pytest.approx(1.5, abs=0.01), row


# ---------------------------------------------------------------------------
# 4. the auto-close: the end of the day moves to the ceiling
# ---------------------------------------------------------------------------
def test_the_auto_close_waits_while_the_shift_is_inside_the_window(app_module):
    """The automatic close is the one path with no human in it, so it is the one that decides
    unilaterally whether a shift may run past the paid day. An answer is a permission to run on,
    and the close waits inside it instead of ending the day at eight hours anyway.

    The rules here are the ones that give the close the end of the day at all (the switch on,
    and ``7.5`` below the 8 h paid day, so ``close_defers`` is false) - with the close off, or
    with its alert line above the paid day, the close stands down and this window would never be
    consulted.
    """
    _set_rules(overtime_notify_hours=7.5)
    clock_in = _plant_open_shift(hours_on_site=9.5)
    _authorise(WORKER, clock_in, CEILING)

    summary = overtime.scan_auto_close()
    assert summary["deferred"] is False, summary
    assert summary["closed"] == 0, "the close ended a day the operator said could run on"
    assert summary["holding"] == 1, summary
    assert summary["authorised"][0]["authorised_hours"] == CEILING, summary["authorised"]
    assert summary["authorised"][0]["paid_hours"] == pytest.approx(9.0, abs=0.01)
    assert _open_shift() == 1, "the shift has to stay open for the worker to clock out of"
    assert (
        db_scalar(
            "SELECT COUNT(*) FROM attendance_logs WHERE worker_id = ? AND source = 'auto_close'",
            (WORKER,),
        )
        == 0
    ), "the close wrote the clock-out it was supposed to be waiting on"


def test_the_auto_close_ends_the_day_at_the_paid_day_without_an_answer(app_module):
    """The control: the same shift, nobody answering, and the close behaves as it always has."""
    _set_rules(overtime_notify_hours=7.5)
    _plant_open_shift(hours_on_site=9.5)

    summary = overtime.scan_auto_close()
    assert summary["closed"] == 1, summary
    assert summary["holding"] == 0, summary
    assert _last_clock_out()["hours"] == pytest.approx(8.0, abs=0.01)


def test_the_auto_close_ends_the_day_at_the_ceiling_it_was_given(app_module):
    """The window has a far edge, and it is the ceiling: past it, the day still ends - at the
    moment the authorised hours ran out, so the timesheet carries the whole authorised day and
    nothing beyond it. An answer that nothing can ever bound is how a session runs for a week.
    """
    rules = _set_rules(overtime_notify_hours=7.5)
    clock_in = _plant_open_shift(hours_on_site=13.0)
    _authorise(WORKER, clock_in, CEILING)
    boundary = shift_hours.paid_limit_at(datetime.strptime(clock_in, TS), CEILING, rules)

    summary = overtime.scan_auto_close(now=boundary)
    assert summary["closed"] == 1, summary
    assert summary["holding"] == 0, summary

    row = _last_clock_out()
    assert row["status_code"] == migrations.STATUS_CODE_AUTO_CLOSED_8H, row
    assert row["hours"] == pytest.approx(CEILING, abs=0.01), row
    assert row["timestamp"] == boundary.strftime(TS), (
        "the day was not closed at the moment the authorised hours ran out"
    )
    assert row["flag_reason"] is None, row
    assert _open_shift() == 0
    assert _authorisation()["consumed_by_log_id"] == row["id"], (
        "the close ended the shift on an answer and left that answer live"
    )

    # ... and the administrator is told which limit it was, rather than being left with a
    # close at "8 paid hours" that recorded twelve.
    alert = _admin_notification("shift_auto_closed")
    assert alert is not None, "the close wrote no administrator alert"
    assert str(int(CEILING)) in alert["title"], alert["title"]
    assert "authorised" in alert["body"].lower(), alert["body"]


def test_a_declined_crossing_ends_the_day_at_the_paid_day(app_module):
    """A refusal is the paid day, which is the boundary the close always used - so declining a
    crossing must not move the end of anybody's day."""
    rules = _set_rules(overtime_notify_hours=7.5)
    clock_in = _plant_open_shift(hours_on_site=9.5)
    with db(write=True) as conn:
        conn.execute(
            "INSERT INTO overtime_authorisations (worker_id, clock_in_time, decision, "
            "authorised_hours, recorded_hours_at_decision, decided_by, decided_at, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                WORKER,
                clock_in,
                overtime.DECISION_DECLINED,
                rules["regular_hours"],
                rules["regular_hours"],
                HEAD_ADMIN,
                datetime.now().strftime(TS),
                "no overtime was agreed",
            ),
        )

    summary = overtime.scan_auto_close()
    assert summary["closed"] == 1, summary
    assert summary["holding"] == 0, summary
    assert _last_clock_out()["hours"] == pytest.approx(rules["regular_hours"], abs=0.01)


# ---------------------------------------------------------------------------
# 5. spending the answer: a decision settles one shift
# ---------------------------------------------------------------------------
def test_the_clock_out_that_settled_the_shift_spends_the_decision(client, app_module):
    """``consumed_by_log_id`` was a column nothing ever wrote, so a decision stayed live for
    ever - read as authorisation by anything that matched on the worker and the clock-in, and
    showing a payroll query an answer that looks unanswered.
    """
    clock_in = _plant_open_shift()
    _authorise(WORKER, clock_in, CEILING)

    assert _force_clock_out(client).status_code == 200
    row = _last_clock_out()
    decision = _authorisation()
    assert decision["consumed_by_log_id"] == row["id"], (
        f"the decision is still live after the shift it answered was paid: {dict(decision)}"
    )
    with db() as conn:
        assert overtime.live_decision(conn, WORKER, clock_in) is None, (
            "a spent decision is still being read as the standing answer"
        )


def test_the_workers_own_punch_spends_the_answer_too(client, app_module):
    """The one path that already settled at the ceiling, asserted to spend it as well.

    Driven through ``/attendance/verify`` rather than through the module: this is the path a
    worker actually uses, and the stamp has to be written by the request, not by a helper the
    test happens to call.
    """
    clock_in = _plant_open_shift()
    _authorise(WORKER, clock_in, CEILING)

    response = harness.clock_in(client, WORKER, action="Clock Out", headers=bearer(WORKER))
    assert response.status_code == 200, response.text[:300]
    assert response.json()["hours"] == pytest.approx(9.5, abs=0.05), response.text[:300]

    row = _last_clock_out()
    assert row["status_code"] == "approved", row
    assert _authorisation()["consumed_by_log_id"] == row["id"], (
        "the worker's own clock-out left the answer that settled it still live"
    )


def test_an_arrival_settles_nothing_and_spends_nothing(client, app_module):
    """One row is written for both punches, so the arrival is the case the guard exists for.

    A clock-in ends no shift, and the answer sitting in the table belongs to a shift that has
    already started - spending it here would silence a question about a shift somebody is still
    working. The arrival is also the half that has no clock-in of its own to pair an answer
    with, which is why asking unconditionally is a crash and not merely a wrong stamp.
    """
    clock_in = _plant_open_shift()
    _authorise(WORKER, clock_in, CEILING)
    with db(write=True) as conn:  # nobody is on site: the next tap is an arrival
        conn.execute("DELETE FROM active_sessions WHERE worker_id = ?", (WORKER,))

    response = harness.clock_in(client, WORKER, action="Clock In", headers=bearer(WORKER))
    assert response.status_code == 200, response.text[:300]
    assert _open_shift() == 1
    assert _authorisation()["consumed_by_log_id"] is None, (
        "clocking in spent an answer that belonged to an earlier shift"
    )


def test_a_spent_decision_cannot_authorise_the_workers_next_shift(client, app_module):
    """The point of the stamp, at the level of money: the next shift is a new question.

    The worker clocks in again after a closed shift and works past the paid day; the answer
    given for the shift before it must not settle that one, or one approval quietly covers
    every late night after it.
    """
    clock_in = _plant_open_shift()
    _authorise(WORKER, clock_in, CEILING)
    assert _force_clock_out(client).status_code == 200

    _plant_open_shift()  # a new shift, with a clock-in of its own
    assert _force_clock_out(client).status_code == 200
    row = _last_clock_out()
    assert row["overtime_hours"] == pytest.approx(1.5, abs=0.01), (
        "an answer spent on a closed shift authorised the next one"
    )


@pytest.mark.parametrize(
    "path",
    (
        main.verify_worker,
        main.force_clock_out,
        quick_links.submit_quick_punch,
        offline_sync.materialize_worker,
        overtime.scan_auto_close,
    ),
    ids=lambda path: path.__name__,
)
def test_every_path_that_ends_a_shift_spends_the_answer(path):
    """Settling and spending are the same event, so every settling path is guarded together.

    The auto-close is in this list rather than exempt: it is the path that reads a decision to
    find the end of the day, so it is the one with the answer most obviously in hand.
    """
    source = inspect.getsource(inspect.unwrap(path))
    assert "consume_authorisation(" in source, (
        f"{path.__name__} ends a shift without spending the decision that settled it, so the "
        "answer stays live and reads as authorisation for whatever comes next"
    )


# ---------------------------------------------------------------------------
# 6. the guard: every path that ends a shift faces the decision
# ---------------------------------------------------------------------------
#: Every function that ends a shift, resolved from the application rather than from a list
#: somebody has to remember to update. Each is a real entry point: the worker's own punch, the
#: administrator's override, the public link and the offline materialiser.
SHIFT_ENDING_PATHS = (
    main.verify_worker,
    main.force_clock_out,
    quick_links.submit_quick_punch,
    offline_sync.materialize_worker,
)


@pytest.mark.parametrize("path", SHIFT_ENDING_PATHS, ids=lambda path: path.__name__)
def test_every_path_that_ends_a_shift_consults_the_decision(path):
    """The five paths disagreeing about one shift is the bug this feature keeps re-learning.

    ``inspect.unwrap`` because one of these carries the rate limiter's wrapper, whose source
    lives in slowapi - the assertion is about *this* application's function. Stated as "asks
    both resolvers" rather than as one of them: the line decides whether a shift crossed, and
    the decision decides what is still held past it, and a path that asks only the first is
    the state this guard was written for.
    """
    source = inspect.getsource(inspect.unwrap(path))
    assert "overtime_assessment" in source, (
        f"{path.__name__} does not ask shift_hours whether the shift crossed the line"
    )
    assert "apply_authorisation(" in source, (
        f"{path.__name__} ends a shift without asking whether somebody answered the crossing "
        "while it was running - so an approval taken in the Approvals queue is ignored by this "
        "way of closing it"
    )


def test_the_auto_close_moves_the_end_of_the_day_rather_than_settling_hours(app_module):
    """Why this path reads the decision, spends it, and still never calls
    ``apply_authorisation`` - the distinction, pinned so a later tidy-up does not blur it.

    ``apply_authorisation`` answers "what is *held past* the ceiling". The auto-close ends the
    day exactly at the limit it was given - the paid day, or the ceiling somebody authorised -
    so the hours it records are never past the limit and there is nothing to hold: the same
    arithmetic that makes it safe when nobody answered. It reads the decision for the limit and
    spends it when it closes; it does not need to settle anything.
    """
    source = inspect.getsource(overtime.scan_auto_close)
    assert "live_decision(" in source, "the close ignores the ceiling it was given"
    assert "consume_authorisation(" in source, "the close leaves the answer live"
    assert "apply_authorisation(" not in source, (
        "the auto-close is settling hours now; that means it is recording time past a limit it "
        "was given, which this test exists to make somebody look at"
    )
    assert "paid_limit_at(clock_in, limit" in source, (
        "the close no longer ends the day at the limit it resolved"
    )
    assert shift_hours.overtime_assessment(8 * 3600, _rules())["overtime_hours"] == 0.0, (
        "a shift closed exactly at its limit holds nothing, which is what makes this safe"
    )
