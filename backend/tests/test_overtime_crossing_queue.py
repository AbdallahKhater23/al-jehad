"""A shift past the overtime line while it is *still running* is a question, not a notice.

WHY THIS EXISTS
---------------
The crossing used to be an ``admin_notifications`` row (``overtime_exceeded``), which put a live
question in the tab built for notices. The two differ in the only way that matters here: a notice
is *read* and stops existing, and a question is *answered* and stops being asked. Reading the
crossing row changed nothing - the shift stayed open, kept accumulating past the line, and the
answer it needed (is this extra time paid for?) had nowhere to be recorded, so the clock-out
asked again hours later, by which time the person who could answer it had gone home.

So the crossing is derived from the open shifts (``overtime.open_crossings``) and the *answer*
is stored (``overtime_authorisations``, migration 21). What this file pins:

* **the move.** No administrator notification is written for a crossing any more, and the Alerts
  list does not carry one; the worker still learns they crossed the line, and the event is still
  audited. Moving a queue is not the same as deleting a channel, and this is where that is said.
* **the queue.** ``GET /admin/overtime/crossings`` lists the open shift with the figures the
  gate itself computed - asserted *equal to* ``overtime_assessment``, because a queue that
  disagrees with the gate about the same second is how an operator authorises the wrong number.
* **the answers.** Accept records a ceiling and a reason; Decline records a refusal as the
  regular paid day. Both are append-only, and the first answer is the one kept - until the shift
  works past the ceiling it named, when the hours beyond it become a second question and the new
  answer supersedes the old one without erasing it.
* **the consequence.** ``apply_authorisation`` settles the clock-out at the ceiling instead of
  the regular day - and returns the assessment untouched when nobody has answered, which is the
  property that lets every pre-existing path keep the arithmetic it had.

The last one is testable here only as the resolver (a real clock-out needs a camera, a face and
a liveness model), so the *wiring* of the punch path is asserted at the source instead - stated
as such rather than dressed up as an end-to-end proof.

    pytest backend/tests/test_overtime_crossing_queue.py -q
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from database import db
from fastapi.testclient import TestClient
import harness
from harness import ADMIN, HEAD_ADMIN, MOALLEM, WORKER, bearer, current_db_path

import main
import migrations
import notifications
import overtime
import shift_hours

TS = "%Y-%m-%d %H:%M:%S"
NOTE = "Roofer on the east block: the pour ran long, twenty more minutes."

#: Long enough on site to be past the shipped line (8.1 h *paid*, which is 8.6 h on site with
#: the unpaid break). Nine hours leaves a deliberate margin so the test is not about the
#: boundary - the boundary has its own suite.
HOURS_ON_SITE = 9.0


@pytest.fixture(scope="module")
def client(app_module):
    """This module's own client, deliberately without the lifespan that boots the deployment.

    Deliberately not the session-scoped ``client``, and for the same reason
    ``test_admin_readiness_over_http`` does not use it: entering the context manager runs the
    startup gate and the timers again, so a checkout whose *face-match bands* are mid-experiment
    (a detector/model pair with no derived thresholds) leaves every file that boots the app
    unable to run - including the files about overtime, which have nothing to do with faces.
    What must not happen is this file passing because it never sent a request: those assertions
    go through the real ASGI stack, the routes' own guards and their status codes.
    """
    instance = TestClient(app_module.app)
    try:
        yield instance
    finally:
        instance.close()


def _rules() -> dict:
    with db() as conn:
        return overtime.rules(conn)


def _set_rules(**overrides) -> dict:
    """The shipped rules with the named ones replaced, written where the queue reads them.

    Written as a row rather than through ``POST /admin/shift_rules`` because the subject here is
    what the *queue* reports about a rule, not the route that stores one - and that route is
    pinned by its own suite.
    """
    values = dict(migrations.DEFAULT_SHIFT_RULES)
    values.update(overrides)
    with db(write=True) as conn:
        cursor = conn.execute(
            "UPDATE shift_rules SET overtime_notify_hours = ?, auto_close_at_regular = ? "
            "WHERE id = 1",
            (values["overtime_notify_hours"], values["auto_close_at_regular"]),
        )
        assert cursor.rowcount == 1, "there is no shift_rules row to set"
    return values


def _plant_open_shift(worker_id: str = WORKER, *, hours_on_site: float = HOURS_ON_SITE) -> str:
    """An open shift, as the application leaves it mid-shift: the session row and the clock-in.

    The clock-in row is written because the queue and every report read one table for a shift's
    history, and a session without one would be a state the application never produces.
    """
    clock_in = datetime.now() - timedelta(hours=hours_on_site)
    stamp = clock_in.strftime(TS)
    with db(write=True) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO active_sessions (worker_id, site_name, clock_in_time) "
            "VALUES (?, ?, ?)",
            (worker_id, "Downtown Tower A", stamp),
        )
        conn.execute(
            "INSERT INTO attendance_logs "
            "(worker_id, site_name, action, timestamp, hours, score, status, status_code, source) "
            "VALUES (?, ?, 'Clock In', ?, 0.0, 0.0, 'Present', 'ok', 'online')",
            (worker_id, "Downtown Tower A", stamp),
        )
    return stamp


def _rows(sql: str, params: tuple = ()) -> list[dict]:
    conn = sqlite3.connect(str(current_db_path()))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _alerts() -> list[dict]:
    return _rows("SELECT * FROM admin_notifications ORDER BY id")


def _decisions() -> list[dict]:
    return _rows("SELECT * FROM overtime_authorisations ORDER BY id")


def _audit(action: str) -> list[dict]:
    return _rows("SELECT * FROM audit_log WHERE action = ? ORDER BY id", (action,))


def _assessment(hours_on_site: float = HOURS_ON_SITE) -> dict:
    """What the gate itself says about this shift - the number the queue must agree with."""
    return shift_hours.overtime_assessment(int(hours_on_site * 3600), _rules())


# ---------------------------------------------------------------------------
# 1. the move: out of Alerts, and the channels that stay
# ---------------------------------------------------------------------------
def test_a_crossing_no_longer_writes_an_administrator_notification(client, app_module):
    _plant_open_shift()
    before = len(_alerts())

    overtime.scan_overtime()

    after = _alerts()
    crossings = [
        row for row in after if row["kind"] == notifications.KIND_OVERTIME_EXCEEDED
    ]
    assert not crossings, (
        "the crossing is still arriving in the Alerts tab, so it is still a notice an operator "
        f"can read without answering anything: {crossings}"
    )
    assert len(after) == before, "the scan wrote an administrator notification of some other kind"
    # ...and the two channels that are *not* the Alerts tab are untouched, which is the half of
    # this that must not regress: moving the question must not quiet the two people who need it.
    assert _rows(
        "SELECT id FROM worker_notifications WHERE kind = ?",
        (notifications.KIND_WORKER_OVERTIME_CROSSED,),
    ), "the worker is no longer told they crossed the line"
    detected = _audit("overtime_detected")
    assert detected, "the crossing is no longer recorded in the audit trail"
    assert detected[-1]["actor_role"] == "system"
    # The alert queue is still served - it is simply not where the crossing is. Asked as the
    # root tier, which is the only reader it has now; an administrator is refused the route
    # entirely, and that refusal is asserted in ``test_notification_acknowledgement``.
    listed = client.get("/api/v1/developer/notifications", headers=harness.root_bearer())
    assert listed.status_code == 200, listed.text
    # Asked as text rather than by walking the payload: the list's own shape is not this file's
    # subject, and an assertion that enumerates it would be the thing that drifts.
    assert notifications.KIND_OVERTIME_EXCEEDED not in listed.text, (
        "the alert queue still serves the crossing"
    )


def test_the_scan_still_reports_what_it_saw(client, app_module):
    """The watcher's own summary, which the metrics and the readiness check read."""
    _plant_open_shift()
    summary = overtime.scan_overtime()
    assert summary["scanned"] == 1, summary
    assert summary["notified"] == 1, summary
    assert summary["worker_notified"] == 1, summary
    # The stamp the watcher keeps so it does not re-evaluate this shift every minute: still
    # written, because the crossing happened once and the summary should say so.
    session = _rows("SELECT overtime_notified_at FROM active_sessions WHERE worker_id = ?", (WORKER,))
    assert session[0]["overtime_notified_at"], "the crossing was not stamped on the open shift"


# ---------------------------------------------------------------------------
# 2. the queue: derived from the shift, agreeing with the gate
# ---------------------------------------------------------------------------
def test_the_approvals_queue_lists_the_open_crossing_with_the_gates_own_figures(client, app_module):
    _plant_open_shift()

    response = client.get("/api/v1/admin/overtime/crossings", headers=bearer(ADMIN))
    assert response.status_code == 200, response.text
    items = response.json()
    assert len(items) == 1, items
    item = items[0]
    assert item["worker_id"] == WORKER
    assert item["worker_name"], "the queue does not say who is on the shift"
    assert item["clock_in_time"], item

    assessment = _assessment()
    # The figures are asserted *equal* to the one resolver's, not merely plausible: the queue and
    # the gate disagreeing about the same second is how an operator authorises the wrong number.
    # To the nearest hundredth of an hour - the clock-in is stamped to the second and the two
    # readings happen at slightly different instants - which is still far tighter than any
    # *different* formula could land: reading the recorded hours instead of the paid ones, or
    # forgetting the unpaid break, is off by half an hour, not by a rounding error.
    assert item["paid_hours"] == pytest.approx(float(assessment["paid_hours"]), abs=0.01), item
    assert item["elapsed_hours"] == pytest.approx(float(assessment["elapsed_hours"]), abs=0.01), item
    assert item["overtime_hours"] == pytest.approx(
        float(assessment["overtime_hours"]), abs=0.01
    ), item
    assert item["regular_hours"] == assessment["regular_hours"], item
    assert item["threshold_hours"] == _rules()["overtime_notify_hours"], item
    assert item["crossed_at"] <= datetime.now().strftime(TS), item
    assert item["decision"] is None, "the queue reports a decision nobody has made"
    # Nothing else will end this shift under the shipped rules - the close is off, and it would
    # stand down under the 8.1-over-8 line even if it were on - so the queue has to say so, or
    # the operator leaves the shift open.
    assert item["close_defers"] is True, item

    # A worker or a lead worker cannot see the queue at all, and an anonymous caller cannot
    # either: this is the console's own surface, next to the review queue.
    for role in (WORKER, MOALLEM):
        assert client.get("/api/v1/admin/overtime/crossings", headers=bearer(role)).status_code == 403
    assert client.get("/api/v1/admin/overtime/crossings").status_code == 401


def test_the_queue_says_whether_anything_automatic_will_end_the_shift(client):
    """``close_defers`` on a crossing is the queue's question, and it answers it both ways.

    The field was written for the deferral - the close standing down under an alert line above
    the paid day - and a deployment now ships with the close *off*, which leaves the shift just as
    free of automatic ends. Read as "will anything but a human end this shift?", the two states
    have one answer, and the note the console draws from it ("clock it out when they finish, or
    use Force clock out") is owed in both. Read as the resolver's own flag, a fresh deployment
    would go quiet on every crossing and lose the warning it was added for.
    """
    _plant_open_shift()

    def will_something_close_it() -> bool:
        items = client.get("/api/v1/admin/overtime/crossings", headers=bearer(ADMIN)).json()
        return next(item for item in items if item["worker_id"] == WORKER)["close_defers"]

    # The shipped rules: the close is off, so nothing ends this shift by itself.
    _set_rules(auto_close_at_regular=0)
    assert will_something_close_it() is True

    # Switched back on above its own alert line, it stands down - and the answer is the same.
    _set_rules(auto_close_at_regular=1)
    assert will_something_close_it() is True

    # With the line below the paid day the close is coming, so the note must not appear.
    _set_rules(auto_close_at_regular=1, overtime_notify_hours=7.5)
    assert will_something_close_it() is False

    # And the two readings are not the same reading: with the close off, the queue says "nothing
    # will end this" while the resolver says "not deferring" - the flag that means the *latter*
    # stays on the day-end verdict, where the shift-rules panel reads it.
    _set_rules(auto_close_at_regular=0)
    day_end = shift_hours.day_end_rules(_rules())
    assert day_end["close_defers"] is False and day_end["close_at_paid_hours"] is None, day_end
    assert will_something_close_it() is True


def test_a_shift_past_the_line_only_appears_while_it_is_open(client, app_module):
    """Derived, so the item cannot outlive the shift - the property a stored row cannot have."""
    _plant_open_shift(hours_on_site=4.0)  # well inside the day: no crossing
    assert client.get("/api/v1/admin/overtime/crossings", headers=bearer(ADMIN)).json() == []

    _plant_open_shift()  # ... and now past the line
    assert len(client.get("/api/v1/admin/overtime/crossings", headers=bearer(ADMIN)).json()) == 1


def test_the_count_endpoint_answers_the_nav_badge_without_the_queue_payload(client, app_module):
    """The badge count: what the queue's own ``needs_answer`` flag says, and nothing else.

    The console paints this on screens where the tab is *not* open, on a poll - so the
    endpoint must answer one small number rather than the queue's decision payloads and
    hour arithmetic. Counted from ``needs_answer`` (the same guard the tab's cards use to
    decide whether they are a question) rather than from raw queue length, so a shift that
    has been answered and is still covered is information, and must not light a badge.
    """
    # No crossing, no badge.
    assert client.get("/api/v1/admin/overtime/crossings/count", headers=bearer(ADMIN)).json() == {"count": 0}

    _plant_open_shift()  # 9 h on site, past the 8.1 h line, nobody has answered
    body = client.get("/api/v1/admin/overtime/crossings/count", headers=bearer(ADMIN)).json()
    assert body == {"count": 1}, body

    # The queue agrees with the badge, from the same flag: one card asking, one badge.
    queue = client.get("/api/v1/admin/overtime/crossings", headers=bearer(ADMIN)).json()
    assert [item for item in queue if item["needs_answer"]] and len(queue) == 1

    # Answer it, and the badge falls with the question - the accept that covers the shift
    # leaves the crossing on the queue as information, but nothing for a person to do.
    client.post(
        f"/api/v1/admin/overtime/crossings/{WORKER}/accept",
        headers=bearer(ADMIN), json={"authorised_hours": 9.5, "note": "within the badge test"},
    )
    assert client.get("/api/v1/admin/overtime/crossings/count", headers=bearer(ADMIN)).json() == {"count": 0}

    # The same audience guard the queue answers with: workers and leads get 403, an
    # anonymous caller 401 - a badge is a read of a console surface, no matter how small.
    for role in (WORKER, MOALLEM):
        assert client.get("/api/v1/admin/overtime/crossings/count", headers=bearer(role)).status_code == 403
    assert client.get("/api/v1/admin/overtime/crossings/count").status_code == 401

    with db(write=True) as conn:  # the shift ends
        conn.execute("DELETE FROM active_sessions WHERE worker_id = ?", (WORKER,))
    assert client.get("/api/v1/admin/overtime/crossings", headers=bearer(ADMIN)).json() == [], (
        "the queue kept an item for a shift that is over; an answered question is not the only "
        "reason an item should leave this list"
    )


# ---------------------------------------------------------------------------
# 3. the answers
# ---------------------------------------------------------------------------
def test_accepting_records_a_ceiling_the_evidence_and_the_actor(client, app_module):
    clock_in = _plant_open_shift()
    assessment = _assessment()

    response = client.post(
        f"/api/v1/admin/overtime/crossings/{WORKER}/accept",
        json={"note": NOTE},
        headers=bearer(HEAD_ADMIN),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["decision"] == overtime.DECISION_AUTHORISED
    assert body["already_decided"] is False
    # No number given, so the ceiling is what has been worked: approving what there is evidence
    # for. Anything larger is stated deliberately by the operator.
    # Within a hundredth of a second's worth of rounding: the endpoint reads the shift at its
    # own instant, and this file's clock-in was stamped to the second. Tighter than that would
    # make the test about the clock rather than about the ceiling.
    assert body["authorised_hours"] == pytest.approx(float(assessment["paid_hours"]), abs=0.01), body
    assert body["recorded_hours_at_decision"] == pytest.approx(
        float(assessment["paid_hours"]), abs=0.01
    ), body

    stored = _decisions()
    assert len(stored) == 1, stored
    assert stored[0]["clock_in_time"] == clock_in, "the decision is not tied to this shift"
    assert stored[0]["decided_by"] == HEAD_ADMIN
    assert stored[0]["authorised_hours"] == body["authorised_hours"]
    assert stored[0]["recorded_hours_at_decision"] == body["recorded_hours_at_decision"]
    assert stored[0]["consumed_by_log_id"] is None, (
        "the decision is marked as spent before any clock-out has settled under it"
    )

    events = _audit("overtime_authorise")
    assert len(events) == 1, f"the decision is not on the record: {events}"
    event = events[0]
    assert event["actor_id"] == HEAD_ADMIN and event["actor_role"] == "head_admin"
    assert event["entity_id"] == WORKER
    assert event["user_agent"], "the trail does not say where the decision came from"
    assert NOTE in (event["after_json"] or ""), "the reason the operator gave was not recorded"

    # The queue shows the answer instead of asking again - it does not vanish, because the shift
    # is still running and somebody should be able to see what was decided about it.
    item = client.get("/api/v1/admin/overtime/crossings", headers=bearer(ADMIN)).json()[0]
    assert item["decision"] is not None, item
    assert item["decision"]["authorised_hours"] == body["authorised_hours"], item
    assert item["decision"]["decided_by"] == HEAD_ADMIN, item
    # The console prints this answer to an operator, so the id arrives beside the name resolved
    # for it. "Answered by 5000" is another lookup, and not needing one is the point of a queue.
    assert item["decision"]["decided_by_name"] == _rows(
        "SELECT name FROM users WHERE id = ?", (HEAD_ADMIN,)
    )[0]["name"], item


def test_the_first_answer_is_the_one_kept(client, app_module):
    _plant_open_shift()
    first = client.post(
        f"/api/v1/admin/overtime/crossings/{WORKER}/accept",
        json={"authorised_hours": 10.0, "note": NOTE},
        headers=bearer(HEAD_ADMIN),
    )
    assert first.status_code == 200, first.text

    again = client.post(
        f"/api/v1/admin/overtime/crossings/{WORKER}/decline",
        json={"note": "changed my mind"},
        headers=bearer(ADMIN),
    )
    assert again.status_code == 200, again.text
    body = again.json()
    assert body["already_decided"] is True, (
        "a second answer overwrote the first, so the queue and the trail describe a decision "
        "somebody else made"
    )
    assert body["decision"] == overtime.DECISION_AUTHORISED, body
    assert body["authorised_hours"] == 10.0, body
    assert len(_decisions()) == 1, "a second decision row was appended for one shift"
    assert not _audit("overtime_decline"), "a decision that was refused was audited as if it happened"


def test_declining_records_a_refusal_rather_than_leaving_the_question_open(client, app_module):
    _plant_open_shift()
    rules = _rules()

    response = client.post(
        f"/api/v1/admin/overtime/crossings/{WORKER}/decline",
        json={"note": "No overtime was agreed for this site today."},
        headers=bearer(ADMIN),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["decision"] == overtime.DECISION_DECLINED, body
    # A refusal is a ceiling too - the regular paid day - so the clock-out reads one column for
    # both answers instead of branching. "Nobody answered" and "the answer is no" are different
    # states of one queue, and only one of them should keep asking.
    assert body["authorised_hours"] == float(rules["regular_hours"]), body
    stored = _decisions()
    assert len(stored) == 1 and stored[0]["decision"] == overtime.DECISION_DECLINED, stored
    assert _audit("overtime_decline"), "the refusal is not on the record"


def test_the_answer_refuses_what_cannot_be_meant(client, app_module):
    _plant_open_shift()
    assessment = _assessment()
    path = f"/api/v1/admin/overtime/crossings/{WORKER}/accept"

    # A ceiling BELOW the hours already worked is now a legal answer, not a refusal: the
    # operator authorises the day up to the figure they named, and the hours past it stay
    # on the shift as the unauthorised excess (kept counting, settled at the clock-out).
    # A shift at 12 h can be authorised for the agreed 10. What is still refused is a
    # figure that cannot be meant at all: one shift past 24 h is a stray digit.
    below = client.post(
        path, json={"authorised_hours": 1.0}, headers=bearer(ADMIN)
    )
    assert below.status_code == 200, below.text
    assert below.json()["authorised_hours"] == 1.0, below.text
    stored = _decisions()
    assert len(stored) == 1 and stored[0]["authorised_hours"] == 1.0, stored
    # A refusal covers the shift for its whole length, so the next question cannot be asked
    # through the API - the standing decision is cleared the way nothing in production does,
    # purely to put the bound check within reach.
    with db(write=True) as conn:
        conn.execute("DELETE FROM overtime_authorisations")
    above = client.post(
        path, json={"authorised_hours": overtime.MAX_AUTHORISED_HOURS + 1}, headers=bearer(ADMIN)
    )
    assert above.status_code == 400, above.text

    # A shift that has not crossed the line is not a question, and a worker with no open shift
    # is not one either: both answer 404 rather than writing an authorisation for nothing.
    with db(write=True) as conn:
        conn.execute("DELETE FROM active_sessions WHERE worker_id = ?", (WORKER,))
    missing = client.post(path, json={}, headers=bearer(ADMIN))
    assert missing.status_code == 404, missing.text

    _plant_open_shift(hours_on_site=3.0)
    early = client.post(path, json={}, headers=bearer(ADMIN))
    assert early.status_code == 404, early.text
    assert assessment["paid_hours"] > 0


# ---------------------------------------------------------------------------
# 3b. the second question: a ceiling the shift has outgrown
# ---------------------------------------------------------------------------
def _plant_standing_decision(
    ceiling: float, recorded: float, *, decision: str = overtime.DECISION_AUTHORISED
) -> int:
    """An earlier answer, planted as the writer would have left it.

    The state these tests need is "a decision the shift has outgrown", and reaching it through two
    calls would mean waiting for the paid figure to pass a ceiling the first call had just set -
    the passage of time is part of what is being tested, so the earlier answer is supplied rather
    than slept through. The real sequence, with the clock injected instead of waited on, is
    ``test_a_ceiling_is_outgrown_by_the_passage_of_time_and_answered_again`` below.
    """
    with db(write=True) as conn:
        cursor = conn.execute(
            "INSERT INTO overtime_authorisations (worker_id, clock_in_time, decision, "
            "authorised_hours, recorded_hours_at_decision, decided_by, decided_at, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                WORKER,
                _clock_in(),
                decision,
                ceiling,
                recorded,
                ADMIN,
                datetime.now().strftime(TS),
                "the pour ran long",
            ),
        )
        return int(cursor.lastrowid)


def test_the_covering_rule_reads_both_answers_the_same_way():
    """``authorisation_covers``: an amount runs out, a refusal does not.

    A unit test, because this is the rule the queue and the writer share and its two failure modes
    are opposite: too eager and the tab re-asks a question somebody answered (the operator answers
    it again and the queue does not change), too reluctant and hours nobody authorised have nothing
    anywhere asking about them.
    """
    tolerance = overtime.COVER_TOLERANCE_HOURS
    authorised = {"decision": overtime.DECISION_AUTHORISED, "authorised_hours": 10.0}
    declined = {"decision": overtime.DECISION_DECLINED, "authorised_hours": 8.0}

    assert overtime.authorisation_covers(None, 9.0) is False, "nobody answered - that is not a cover"
    assert overtime.authorisation_covers(authorised, 9.9) is True
    assert overtime.authorisation_covers(authorised, 10.0 + tolerance) is True, (
        "the ceiling covers the instant it was measured at and the drift either side of it"
    )
    assert overtime.authorisation_covers(authorised, 10.0 + tolerance + 0.01) is False, (
        "the shift has worked past the ceiling and those hours are nobody's authorisation"
    )
    # A refusal is a decision about the day, and a day does not run out: however long the shift
    # runs, the answer to "is this overtime authorised" is the same, and it is no.
    assert overtime.authorisation_covers(declined, 8.5) is True
    assert overtime.authorisation_covers(declined, 14.0) is True


def test_a_covered_ceiling_is_not_asked_about_again(client, app_module):
    """The queue stops asking while the answer still authorises what has been worked."""
    _plant_open_shift()
    paid = round(float(_assessment()["paid_hours"]), 4)
    accepted = client.post(
        f"/api/v1/admin/overtime/crossings/{WORKER}/accept",
        json={"authorised_hours": paid + 1.0, "note": NOTE},
        headers=bearer(HEAD_ADMIN),
    )
    assert accepted.status_code == 200, accepted.text

    item = client.get("/api/v1/admin/overtime/crossings", headers=bearer(ADMIN)).json()[0]
    assert item["needs_answer"] is False, f"the queue is asking again about an answer just given: {item}"
    # ``None`` rather than 0.0: nothing is being asked about, which is a different statement from
    # "the excess is zero" - and on a first question the excess is not a figure at all.
    assert item["unauthorised_hours"] is None, item
    assert item["decision"]["authorised_hours"] == round(paid + 1.0, 4), item


def test_the_hours_past_the_ceiling_become_the_question_again(client, app_module):
    """What a *ceiling* means: the answer covers what it named, not the rest of the shift."""
    _plant_open_shift()
    paid = round(float(_assessment()["paid_hours"]), 4)
    ceiling = round(paid - 0.5, 4)
    _plant_standing_decision(ceiling=ceiling, recorded=round(ceiling - 0.1, 4))

    item = client.get("/api/v1/admin/overtime/crossings", headers=bearer(ADMIN)).json()[0]
    assert item["needs_answer"] is True, (
        f"the shift has worked past the ceiling and nothing is asking about those hours: {item}"
    )
    # The figure the operator is being asked about is the *excess*, not the crossing: the hours
    # under the ceiling were answered already, and asking about them again is how an operator
    # authorises the same time twice.
    assert item["unauthorised_hours"] == pytest.approx(item["paid_hours"] - ceiling, abs=0.01), item
    assert item["unauthorised_hours"] == pytest.approx(0.5, abs=0.02), item
    # The answer is still shown: an outgrown ceiling is not a forgotten one, and it is what the next
    # number will be measured against.
    assert item["decision"]["authorised_hours"] == ceiling, item


def test_the_second_answer_supersedes_the_first_rather_than_overwriting_it(client, app_module):
    """Append-only: the earlier answer stands down and stays on the record."""
    _plant_open_shift()
    paid = round(float(_assessment()["paid_hours"]), 4)
    ceiling = round(paid - 0.5, 4)
    first = _plant_standing_decision(ceiling=ceiling, recorded=round(ceiling - 0.1, 4))

    second = client.post(
        f"/api/v1/admin/overtime/crossings/{WORKER}/accept",
        json={"authorised_hours": paid + 2.0, "note": NOTE},
        headers=bearer(HEAD_ADMIN),
    )
    assert second.status_code == 200, second.text
    body = second.json()
    assert body["already_decided"] is False, (
        "the shift outgrew the ceiling and the answer was refused as a repeat, so the extra hours "
        f"have no way to be authorised: {body}"
    )
    assert body["superseded"] == {
        "id": first,
        "decision": overtime.DECISION_AUTHORISED,
        "authorised_hours": ceiling,
        "recorded_hours_at_decision": round(ceiling - 0.1, 4),
        "decided_by": ADMIN,
    }, body

    rows = _decisions()
    assert len(rows) == 2, f"the earlier answer was replaced instead of kept: {rows}"
    assert rows[0]["superseded_by"] == rows[1]["id"], rows
    assert rows[1]["superseded_by"] is None, rows
    assert rows[1]["authorised_hours"] == round(paid + 2.0, 4), rows

    with db() as conn:
        live = overtime.live_decision(conn, WORKER, _clock_in())
    assert live is not None and int(live["id"]) == rows[1]["id"], (
        "the superseded answer is still the one the clock-out would settle at"
    )

    events = _audit("overtime_authorise")
    assert len(events) == 1, events
    recorded_before = json.loads(events[0]["before_json"])
    assert recorded_before["superseded"]["id"] == first, (
        f"the trail does not say which decision this one replaced: {recorded_before}"
    )
    assert recorded_before["superseded"]["authorised_hours"] == ceiling, recorded_before


def test_a_refusal_is_not_asked_again_however_long_the_shift_runs(client, app_module):
    """A refusal answers the only question it can answer, and the answer stands.

    The hours it leaves are not lost: they are priced after the fact by the ordinary review, which
    is exactly what "no overtime is authorised for this shift" says should happen. What they must
    not do is re-open the *mid-shift* queue - a refused shift whose paid figure is past the regular
    day would ask again every few minutes, and an operator who just answered would read that as the
    system not having heard them.
    """
    _plant_open_shift()
    rules = _rules()
    declined = client.post(
        f"/api/v1/admin/overtime/crossings/{WORKER}/decline",
        json={"note": "No overtime was agreed for this site today."},
        headers=bearer(ADMIN),
    )
    assert declined.status_code == 200, declined.text
    assert float(declined.json()["authorised_hours"]) == float(rules["regular_hours"])
    paid = float(_assessment()["paid_hours"])
    assert paid > float(rules["regular_hours"]), "premise: this shift is past the day it was refused"

    item = client.get("/api/v1/admin/overtime/crossings", headers=bearer(ADMIN)).json()[0]
    assert item["needs_answer"] is False, f"the queue is asking again about a refusal: {item}"
    assert item["unauthorised_hours"] is None, (
        f"the hours a refusal leaves are the post-hoc review's business, not a second question: {item}"
    )

    again = client.post(
        f"/api/v1/admin/overtime/crossings/{WORKER}/accept",
        json={"authorised_hours": 12.0, "note": NOTE},
        headers=bearer(HEAD_ADMIN),
    )
    assert again.status_code == 200, again.text
    assert again.json()["already_decided"] is True, (
        "a refusal was overwritten mid-shift: the operator's answer was replaced without them "
        "being asked again, and the queue had told them the shift was settled"
    )
    assert len(_decisions()) == 1, _decisions()


def test_an_operator_can_withdraw_a_ceiling_the_shift_has_outgrown(client, app_module):
    """The other direction: a ceiling can be taken back, and the record keeps both answers.

    The refusal names the regular paid day as its ceiling, which is what settles the clock-out -
    so withdrawing an authorisation is a real decision about the money rather than a comment on
    one, and it ends the mid-shift questions for this shift (``authorisation_covers``).
    """
    _plant_open_shift()
    paid = round(float(_assessment()["paid_hours"]), 4)
    first = _plant_standing_decision(ceiling=round(paid - 0.5, 4), recorded=round(paid - 0.6, 4))

    withdrawn = client.post(
        f"/api/v1/admin/overtime/crossings/{WORKER}/decline",
        json={"note": "I authorised too much for this shift - keep it at the standard day."},
        headers=bearer(ADMIN),
    )
    assert withdrawn.status_code == 200, withdrawn.text
    body = withdrawn.json()
    assert body["decision"] == overtime.DECISION_DECLINED, body
    assert body["superseded"]["id"] == first, body

    rows = _decisions()
    assert [row["decision"] for row in rows] == [
        overtime.DECISION_AUTHORISED,
        overtime.DECISION_DECLINED,
    ], rows
    assert rows[0]["superseded_by"] == rows[1]["id"], rows
    assert _audit("overtime_decline"), "the withdrawal is not on the record"

    item = client.get("/api/v1/admin/overtime/crossings", headers=bearer(ADMIN)).json()[0]
    assert item["needs_answer"] is False, (
        f"the withdrawal left the queue asking about a shift that has just been refused: {item}"
    )


def test_a_shift_settles_at_the_ceiling_that_superseded_the_first(client, app_module):
    """The money follows the *live* answer: the chain is history, not arithmetic."""
    _plant_open_shift()
    rules = _rules()
    assessment = _assessment()
    paid = round(float(assessment["paid_hours"]), 4)
    clock_in = _clock_in()
    ceiling = round(paid - 0.5, 4)
    first = _plant_standing_decision(ceiling=ceiling, recorded=round(ceiling - 0.1, 4))

    second = client.post(
        f"/api/v1/admin/overtime/crossings/{WORKER}/accept",
        json={"authorised_hours": paid + 2.0},
        headers=bearer(HEAD_ADMIN),
    )
    assert second.status_code == 200, second.text

    with db(write=True) as conn:
        settled = overtime.apply_authorisation(conn, WORKER, clock_in, assessment, rules)
    assert settled["authorised_hours"] == round(paid + 2.0, 4), settled
    assert settled["overtime_hours"] == 0.0, settled
    assert settled["needs_approval"] is False, settled
    assert settled["authorisation"]["id"] != first, settled

    # The control: with the extension struck out and the earlier ceiling live again, the same shift
    # holds the excess it did not authorise. Without this half, a resolver that ignored every
    # decision would pass the assertions above.
    with db(write=True) as conn:
        conn.execute("DELETE FROM overtime_authorisations WHERE id != ?", (first,))
        conn.execute("UPDATE overtime_authorisations SET superseded_by = NULL WHERE id = ?", (first,))
        held = overtime.apply_authorisation(conn, WORKER, clock_in, assessment, rules)
    assert held["authorised_hours"] == ceiling, held
    assert held["overtime_hours"] == pytest.approx(0.5, abs=0.02), held
    assert held["needs_approval"] is True, held


def test_only_the_newest_answer_is_live_after_two_extensions(app_module):
    """The chain keeps growing: an extension supersedes the last one, not the original.

    Built by the writer itself, at three instants, because the ids it reserves have to be the ones
    it links: the earlier row is stamped with the id the *next* row is about to take (the partial
    unique index admits one live decision per shift, so the outgoing answer stands down before the
    incoming one exists), and an off-by-one there would show as a first row pointing at an answer
    that never superseded it.
    """
    clock_in = _plant_open_shift()
    started = datetime.strptime(clock_in, TS)

    # Each answer authorises the hours worked at its own instant - the blank default - so the next
    # instant is past it and the chain grows. Timing is injected; nothing here sleeps.
    for hours, actor in ((8.8, HEAD_ADMIN), (9.0, ADMIN), (9.2, HEAD_ADMIN)):
        with db(write=True) as conn:
            overtime.decide_crossing(
                conn,
                worker_id=WORKER,
                accept=True,
                actor_id=actor,
                now=started + timedelta(hours=hours),
            )

    rows = _decisions()
    assert len(rows) == 3, f"a second extension did not chain onto the first: {rows}"
    assert rows[0]["superseded_by"] == rows[1]["id"], (
        f"the original answer was re-pointed at the newest one, so the chain lost a link: {rows}"
    )
    assert rows[1]["superseded_by"] == rows[2]["id"], rows
    assert rows[2]["superseded_by"] is None, rows
    assert [row["decided_by"] for row in rows] == [HEAD_ADMIN, ADMIN, HEAD_ADMIN], rows
    with db() as conn:
        live = overtime.live_decision(conn, WORKER, _clock_in())
    assert int(live["id"]) == rows[2]["id"], rows
    with db(write=True) as conn:
        settled = overtime.apply_authorisation(
            conn, WORKER, _clock_in(), _assessment(), _rules()
        )
    assert settled["authorisation"]["id"] == rows[2]["id"], settled


def test_the_table_still_admits_only_one_live_answer_per_shift(client, app_module):
    """Why the writer stamps before it inserts: the schema refuses two live answers.

    Asserted against a raw connection rather than the application's, because it is a property of
    the table: a code change that inserted first and stamped afterwards must raise here rather
    than leave two live answers to one question for the clock-out to choose between.
    """
    _plant_open_shift()
    paid = round(float(_assessment()["paid_hours"]), 4)
    _plant_standing_decision(ceiling=round(paid - 0.5, 4), recorded=round(paid - 0.6, 4))

    conn = sqlite3.connect(str(current_db_path()))
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO overtime_authorisations (worker_id, clock_in_time, decision, "
                "authorised_hours, recorded_hours_at_decision, decided_by, decided_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    WORKER,
                    _clock_in(),
                    overtime.DECISION_AUTHORISED,
                    paid + 1.0,
                    paid,
                    HEAD_ADMIN,
                    datetime.now().strftime(TS),
                ),
            )
            conn.commit()
    finally:
        conn.close()


def test_a_ceiling_is_outgrown_by_the_passage_of_time_and_answered_again(app_module):
    """The real sequence, with the clock supplied rather than waited on.

    A blank acceptance authorises the hours worked *at that moment*, so a shift that keeps working
    outgrows it by construction - the first half asserts exactly that, and it is the property that
    makes a stated ceiling the only way to cover the rest of a night. Driving the writer twice with
    an injected ``now`` keeps the test about the rule rather than about a test's patience.
    """
    clock_in = _plant_open_shift()
    started = datetime.strptime(clock_in, TS)

    with db(write=True) as conn:
        first = overtime.decide_crossing(
            conn,
            worker_id=WORKER,
            accept=True,
            actor_id=HEAD_ADMIN,
            now=started + timedelta(hours=8.75),
        )
    assert first["already_decided"] is False and first["superseded"] is None, first
    assert first["authorised_hours"] == pytest.approx(first["recorded_hours_at_decision"], abs=0.01), first
    first_id = _decisions()[0]["id"]

    # A second tap a minute later is the same question, not a new one: the tolerance is what stops
    # a ceiling measured at an instant from reopening itself.
    with db(write=True) as conn:
        repeated = overtime.decide_crossing(
            conn,
            worker_id=WORKER,
            accept=True,
            actor_id=HEAD_ADMIN,
            now=started + timedelta(hours=8.76),
        )
    assert repeated["already_decided"] is True, repeated

    with db(write=True) as conn:
        later = overtime.decide_crossing(
            conn,
            worker_id=WORKER,
            accept=True,
            authorised_hours=12.0,
            actor_id=HEAD_ADMIN,
            now=started + timedelta(hours=9.5),
        )
    assert later["already_decided"] is False, later
    assert later["superseded"]["id"] == first_id, (
        f"the extension replaced nothing, so the ceiling it extended is not on its record: {later}"
    )
    rows = _decisions()
    assert [row["superseded_by"] for row in rows] == [rows[1]["id"], None], rows


# ---------------------------------------------------------------------------
# 3c. the worker is told what was decided, not only that a line was crossed
# ---------------------------------------------------------------------------
def _worker_notices(kind: str | None = None) -> list[dict]:
    sql = "SELECT * FROM worker_notifications"
    params: tuple = ()
    if kind:
        sql += " WHERE kind = ?"
        params = (kind,)
    return _rows(sql + " ORDER BY id", params)


def test_authorising_tells_the_worker_what_was_authorised_and_by_whom(client, app_module):
    """The decision is a notice of its own, and it states the decision.

    The crossing notice the worker already had says a line was crossed and that the time past it
    needs approval - a statement about the *shift*, and the last word they had until their
    clock-out. What the answer adds is the figure their remaining hours are paid against.
    """
    _plant_open_shift()
    paid = round(float(_assessment()["paid_hours"]), 4)
    accepted = client.post(
        f"/api/v1/admin/overtime/crossings/{WORKER}/accept",
        json={"authorised_hours": 12.0, "note": NOTE},
        headers=bearer(HEAD_ADMIN),
    )
    assert accepted.status_code == 200, accepted.text

    notices = _worker_notices()
    assert len(notices) == 1, f"the worker was told {len(notices)} times about one decision: {notices}"
    notice = notices[0]
    assert notice["kind"] == notifications.KIND_WORKER_OVERTIME_AUTHORISED, notice
    assert "12" in notice["title"], notice["title"]
    assert "12" in notice["body"], notice["body"]
    name = _rows("SELECT name FROM users WHERE id = ?", (HEAD_ADMIN,))[0]["name"]
    assert name in notice["body"], (
        f"the notice does not say who authorised it, so the worker has to ask: {notice['body']}"
    )
    payload = json.loads(notice["payload"])
    assert payload["authorised_hours"] == 12.0, payload
    assert payload["decided_by_name"] == name, payload
    assert payload["recorded_hours_at_decision"] == pytest.approx(paid, abs=0.01), payload
    assert payload["authorisation_id"] == _decisions()[0]["id"], payload
    # The operator's note is written for the record: the console promises a worker never sees a
    # reviewer's note, and a channel built later must not be the way that promise is broken.
    assert NOTE not in notice["body"] and NOTE not in notice["title"], notice
    # The reason is still on the record - it just is not addressed to the worker.
    assert _decisions()[0]["note"] == NOTE, _decisions()


def test_a_refused_shift_is_told_in_its_own_words(client, app_module):
    """Declined is a different kind, not the same one with different words.

    "Authorised up to 10.5 h" and "no extra time is authorised" are opposite statements, and a
    kind carrying both could be neither counted nor translated as either - and the worker would
    have to parse the sentence to find out which one happened to them.
    """
    _plant_open_shift()
    rules = _rules()
    declined = client.post(
        f"/api/v1/admin/overtime/crossings/{WORKER}/decline",
        json={"note": "No overtime was agreed for this site today."},
        headers=bearer(ADMIN),
    )
    assert declined.status_code == 200, declined.text

    notices = _worker_notices()
    assert len(notices) == 1, notices
    assert notices[0]["kind"] == notifications.KIND_WORKER_OVERTIME_DECLINED, notices[0]
    assert not _worker_notices(notifications.KIND_WORKER_OVERTIME_AUTHORISED), (
        "a refusal was filed under the authorising kind"
    )
    payload = json.loads(notices[0]["payload"])
    assert payload["decision"] == overtime.DECISION_DECLINED, payload
    assert payload["authorised_hours"] == float(rules["regular_hours"]), payload
    assert f"{float(rules['regular_hours']):g}" in notices[0]["body"], notices[0]["body"]


def test_the_worker_is_told_again_when_a_ceiling_is_extended(client, app_module):
    """One notice per decision, keyed on the decision - the extension is the answer that counts.

    A ``dedupe_key`` naming the *shift* would swallow this: the worker would be holding a notice
    saying 9.5 h while their clock-out settles at 12 h, which is worse than no notice at all.
    """
    clock_in = _plant_open_shift()
    started = datetime.strptime(clock_in, TS)
    # The first answer through the writer, with its clock injected: notifying is part of writing
    # the decision now, so the state a real extension starts from has a notice of its own.
    with db(write=True) as conn:
        first = overtime.decide_crossing(
            conn, worker_id=WORKER, accept=True, actor_id=HEAD_ADMIN,
            now=started + timedelta(hours=8.8),
        )
    assert first["worker_notified"] is True, first

    extended = client.post(
        f"/api/v1/admin/overtime/crossings/{WORKER}/accept",
        json={"authorised_hours": 12.0},
        headers=bearer(HEAD_ADMIN),
    )
    assert extended.status_code == 200, extended.text

    notices = _worker_notices(notifications.KIND_WORKER_OVERTIME_AUTHORISED)
    assert len(notices) == 2, f"the extension did not reach the worker: {notices}"
    assert "12" in notices[-1]["body"], notices[-1]["body"]
    payload = json.loads(notices[-1]["payload"])
    assert payload["superseded_authorisation_id"] == first["authorisation_id"], payload
    assert notices[0]["dedupe_key"] != notices[1]["dedupe_key"], notices


def test_a_repeated_answer_tells_the_worker_nothing_new(client, app_module):
    """A second tap on an answered crossing is not a second event, so it is not a second notice."""
    _plant_open_shift()
    paid = round(float(_assessment()["paid_hours"]), 4)
    path = f"/api/v1/admin/overtime/crossings/{WORKER}/accept"
    first = client.post(path, json={"authorised_hours": paid + 1.0}, headers=bearer(HEAD_ADMIN))
    assert first.status_code == 200, first.text
    again = client.post(path, json={"authorised_hours": paid + 2.0}, headers=bearer(ADMIN))
    assert again.json()["already_decided"] is True, again.text

    assert len(_worker_notices()) == 1, (
        f"an answer the record already held was sent to the worker again: {_worker_notices()}"
    )


def test_a_refused_answer_writes_no_notice_at_all(client, app_module):
    """Nothing partial: the notice is written in the transaction that writes the decision.

    A decision that is refused (a ceiling past the 24 h bound) must leave neither a row nor a
    notice, or the worker is told about an authorisation that does not exist. A ceiling below
    the hours worked is no longer in this company: it is a legal answer now - the operator
    names what they are authorising and the excess keeps counting - so it writes a row and
    delivers its notice like any other acceptance.
    """
    _plant_open_shift()
    refused = client.post(
        f"/api/v1/admin/overtime/crossings/{WORKER}/accept",
        json={"authorised_hours": overtime.MAX_AUTHORISED_HOURS + 1},
        headers=bearer(ADMIN),
    )
    assert refused.status_code == 400, refused.text
    assert not _decisions(), _decisions()
    assert not _worker_notices(), _worker_notices()


def test_the_notice_reaches_the_push_after_the_commit_and_only_once(client, app_module, monkeypatch):
    """Delivery happens outside the transaction, and a repeat delivers nothing.

    ``push`` selects notices that are not yet delivered, so inside the transaction that wrote one
    it cannot see it - the same reason every other worker notice in this application is
    dispatched by its caller afterwards (see ``overtime.deliver_worker_notices``). The half that
    a call-count alone would miss is the repeat: the second tap must not push, or the worker's
    phone buzzes about a decision that was already on their screen.
    """
    dispatched: list[bool] = []
    monkeypatch.setattr(overtime.push, "dispatch_async", lambda: (dispatched.append(True) or True))

    _plant_open_shift()
    paid = round(float(_assessment()["paid_hours"]), 4)
    path = f"/api/v1/admin/overtime/crossings/{WORKER}/accept"
    first = client.post(path, json={"authorised_hours": paid + 1.0}, headers=bearer(HEAD_ADMIN))
    assert first.status_code == 200, first.text
    assert dispatched == [True], "the decision was written and never dispatched"

    again = client.post(path, json={"authorised_hours": paid + 2.0}, headers=bearer(ADMIN))
    assert again.json()["already_decided"] is True, again.text
    assert dispatched == [True], (
        "a repeated answer pushed a notice the worker had already been given"
    )


# ---------------------------------------------------------------------------
# 4. the consequence: the clock-out settles at the ceiling
# ---------------------------------------------------------------------------
def test_an_answered_crossing_settles_the_clock_out_at_the_ceiling(client, app_module):
    _plant_open_shift()
    rules = _rules()
    assessment = _assessment()
    paid = round(float(assessment["paid_hours"]), 4)
    regular = float(rules["regular_hours"])

    with db(write=True) as conn:
        # Nothing answered: the resolver must return today's arithmetic untouched, which is what
        # lets every existing clock-out path keep the numbers its tests pin.
        plain = overtime.apply_authorisation(conn, WORKER, _rows(
            "SELECT clock_in_time FROM active_sessions WHERE worker_id = ?", (WORKER,)
        )[0]["clock_in_time"], assessment, rules)
        assert plain["overtime_hours"] == assessment["overtime_hours"], plain
        assert plain["needs_approval"] is True, plain
        assert plain["authorised_hours"] is None, plain
        assert plain is not assessment, "the assessment was mutated in place"

        # Authorised above what the shift has worked: nothing is held, and the authorisation is
        # reported beside the row - so the clock-out does not ask again for hours already decided.
        conn.execute(
            "INSERT INTO overtime_authorisations (worker_id, clock_in_time, decision, "
            "authorised_hours, recorded_hours_at_decision, decided_by, decided_at, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (WORKER, _clock_in(), overtime.DECISION_AUTHORISED, paid + 1.0, paid, ADMIN,
             datetime.now().strftime(TS), NOTE),
        )
        settled = overtime.apply_authorisation(conn, WORKER, _clock_in(), assessment, rules)
        assert settled["overtime_hours"] == 0.0, settled
        assert settled["needs_approval"] is False, (
            "the clock-out is still holding a shift somebody authorised, so the queue asks the "
            f"question the operator already answered: {settled}"
        )
        assert settled["authorisation"]["decided_by"] == ADMIN, settled
        assert settled["authorised_hours"] == round(paid + 1.0, 4), settled

    # And the other half, in its own shift: an authorisation for less than the shift goes on to
    # work leaves exactly the excess for a *second* question - the hours past the ceiling, not
    # the whole overtime figure over again.
    with db(write=True) as conn:
        conn.execute("DELETE FROM overtime_authorisations")
        conn.execute(
            "INSERT INTO overtime_authorisations (worker_id, clock_in_time, decision, "
            "authorised_hours, recorded_hours_at_decision, decided_by, decided_at, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (WORKER, _clock_in(), overtime.DECISION_AUTHORISED, regular + 0.25, regular, ADMIN,
             datetime.now().strftime(TS), None),
        )
        excess = overtime.apply_authorisation(conn, WORKER, _clock_in(), assessment, rules)
        assert excess["overtime_hours"] == round(paid - (regular + 0.25), 4), excess
        assert excess["needs_approval"] is True, excess

    # A decision taken for a *previous* shift is not authorisation for this one: the clock-in
    # time is the pairing, and yesterday's answer does not match today's shift.
    with db(write=True) as conn:
        conn.execute("DELETE FROM overtime_authorisations")
        conn.execute(
            "INSERT INTO overtime_authorisations (worker_id, clock_in_time, decision, "
            "authorised_hours, recorded_hours_at_decision, decided_by, decided_at, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (WORKER, "2020-01-01 06:00:00", overtime.DECISION_AUTHORISED, 12.0, 8.0, ADMIN,
             datetime.now().strftime(TS), None),
        )
        stale = overtime.apply_authorisation(conn, WORKER, _clock_in(), assessment, rules)
        assert stale["authorisation"] is None, stale
        assert stale["overtime_hours"] == assessment["overtime_hours"], stale


def _clock_in() -> str:
    return _rows("SELECT clock_in_time FROM active_sessions WHERE worker_id = ?", (WORKER,))[0][
        "clock_in_time"
    ]


def test_the_punch_path_consults_the_decision(app_module):
    """Wiring, asserted at the source, and said so rather than dressed up as an end-to-end proof.

    Driving a real clock-out from here would need a camera, a face and a liveness model, and the
    interesting part - that the settled numbers change - is the resolver above, already pinned.
    What cannot be proved by the resolver is that the clock-out *calls* it; a helper nothing
    invokes passes every unit test in this file while the queue keeps asking.
    """
    source = Path(main.__file__).read_text(encoding="utf-8")
    assert "overtime.apply_authorisation(" in source, (
        "the clock-out no longer consults the mid-shift decision, so an answer recorded in the "
        "Approvals queue changes nothing about what the shift is paid for"
    )


def test_a_decision_whose_account_is_gone_still_names_somebody(client, app_module):
    """A decision outlives the account that made it, so the fallback is the id, not a blank.

    Deleting an administrator does not undeclare the overtime they authorised, and the operator
    looking at the queue still has to be able to tell that *somebody* answered it.
    """
    clock_in = _plant_open_shift()
    with db(write=True) as conn:
        conn.execute(
            "INSERT INTO overtime_authorisations (worker_id, clock_in_time, decision, "
            "authorised_hours, recorded_hours_at_decision, decided_by, decided_at, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (WORKER, clock_in, overtime.DECISION_AUTHORISED, 10.0, 9.0, "404040404",
             datetime.now().strftime(TS), NOTE),
        )
    item = client.get("/api/v1/admin/overtime/crossings", headers=bearer(ADMIN)).json()[0]
    assert item["decision"]["decided_by"] == "404040404", item
    assert item["decision"]["decided_by_name"] == "404040404", item


def test_the_schema_that_holds_the_answer_is_registered(app_module):
    assert ("overtime_authorisations" in [name for _, name, _ in migrations.MIGRATIONS]), (
        "the decision table is not in the migration registry"
    )
    assert migrations.SCHEMA_VERSION == max(version for version, _, _ in migrations.MIGRATIONS)
    columns = _rows("SELECT name FROM pragma_table_info('overtime_authorisations')")
    assert {row["name"] for row in columns} >= {
        "worker_id",
        "clock_in_time",
        "decision",
        "authorised_hours",
        "recorded_hours_at_decision",
        "decided_by",
        "decided_at",
        "note",
    }, columns
