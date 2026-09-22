"""Reading an alert and *answering* one are different acts.

The server has written administrator alerts for a while, and the only way to close one was
``POST /admin/notifications/{id}/read`` - which records that somebody looked. For most alerts
that is the right answer: a retention sweep happened, a worker wrote a note, and there is
nothing to decide. For the ones that record a *decision* it is the wrong answer entirely. A
forced start past a failing self-test means a deployment is serving code the self-test refused;
"somebody read it" cannot be reviewed afterwards, cannot be shown to have been the right call,
and gives the next person to open the row the same question. ``STARTUP_OVERRIDE_REASON`` already
demands a reason to *open* that hatch - an unattributed escape hatch is indistinguishable from a
permanent bypass - and this is the other half: somebody has to accept it, in their own words,
and both are on the record.

So this file is about the acknowledgement as an event rather than a flag:

* **the note is required**, and vetted as free text exactly like a note or a rejection reason
  (markup refused, apostrophes kept) - an empty acknowledgement is the thing being fixed;
* **the actor is on the record**: ``audit_log`` gains ``notification_acknowledge`` with the
  administrator's id and role, the note, the alert's kind and title, and the request's own
  address and user agent, so "who decided this, and why" is answerable without asking around;
* **the first answer is the one kept**: a second acknowledgement is refused with a ``409``
  naming who answered first, because silently rewriting it would leave the trail describing a
  decision nobody made;
* **answering reads it too**, and reading it does not answer it - the two acts stay distinct,
  which is what keeps an informational alert clearable without looking like an acceptance;
* **readiness closes the loop**: the advisory check fails while the newest forced start has no
  acknowledgement and passes the moment one exists, keyed per *reason* so an acceptance given
  for one override cannot answer a different one.

    pytest backend/tests/test_notification_acknowledgement.py -q
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from database import db
from harness import ADMIN, HEAD_ADMIN, MOALLEM, WORKER, bearer, current_db_path

import notifications
import readiness

TS = "%Y-%m-%d %H:%M:%S"
NOTE = "Checked the ledger by hand: the migration is recorded, the column is there."


def _plant_alert(
    *,
    kind: str = notifications.KIND_STARTUP_OVERRIDE,
    severity: str = notifications.SEVERITY_CRITICAL,
    title: str = "Startup gate overridden",
    body: str = "The server started with failing checks ['schema_current']. Reason given: an incident.",
    payload: dict | None = None,
    dedupe_key: str | None = None,
    read_by: str | None = None,
) -> int:
    """Write an alert row directly, the way the startup gate does.

    Not through the endpoint: the acknowledgement is what is under test, and the alert has to
    exist before it can be answered - including the ones written by a *previous* build, which is
    most of them on a deployment that has been running for a while.
    """
    stamp = datetime.now().strftime(TS)
    with db(write=True) as conn:
        cursor = conn.execute(
            "INSERT INTO admin_notifications "
            "(kind, severity, title, body, payload, dedupe_key, created_at, read_at, read_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                kind,
                severity,
                title,
                body,
                json.dumps(payload or {"reason": "an incident"}) if payload is not None or True else None,
                dedupe_key or f"{kind}:{title}",
                stamp,
                stamp if read_by else None,
                read_by,
            ),
        )
        return int(cursor.lastrowid)


def _alert(alert_id: int) -> dict:
    """The row as stored, read with the file the test process is pointed at."""
    conn = sqlite3.connect(str(current_db_path()))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM admin_notifications WHERE id = ?", (alert_id,)).fetchone()
    finally:
        conn.close()
    assert row is not None, f"alert {alert_id} is gone"
    return dict(row)


def _acknowledgements() -> list[dict]:
    """Every acknowledgement event in the append-only trail."""
    conn = sqlite3.connect(str(current_db_path()))
    conn.row_factory = sqlite3.Row
    try:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM audit_log WHERE action = 'notification_acknowledge' ORDER BY id"
            ).fetchall()
        ]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The answer, and who gave it
# ---------------------------------------------------------------------------
def test_an_alert_is_acknowledged_with_a_reason_and_the_actor_is_recorded(client, app_module):
    alert_id = _plant_alert()

    response = client.post(
        f"/api/v1/admin/notifications/{alert_id}/acknowledge",
        json={"note": NOTE},
        headers=bearer(HEAD_ADMIN),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["notification"]["acknowledged_by"] == HEAD_ADMIN
    assert body["notification"]["acknowledgement_note"] == NOTE
    assert body["notification"]["acknowledged_at"]

    stored = _alert(alert_id)
    assert stored["acknowledged_by"] == HEAD_ADMIN
    assert stored["acknowledgement_note"] == NOTE

    events = _acknowledgements()
    assert len(events) == 1, events
    event = events[0]
    assert event["actor_id"] == HEAD_ADMIN, "the decision has no name on it"
    assert event["actor_role"] == "head_admin"
    assert event["entity"] == "admin_notifications"
    assert event["entity_id"] == str(alert_id)
    # The reason is what the trail is *for*: an event that only recorded the id would answer
    # "who clicked" and nothing else.
    after = json.loads(event["after_json"])
    assert after["note"] == NOTE
    assert after["kind"] == notifications.KIND_STARTUP_OVERRIDE
    assert after["title"] == "Startup gate overridden"
    assert after["severity"] == notifications.SEVERITY_CRITICAL
    assert json.loads(event["before_json"]) == {"acknowledged": False}
    # Provenance: the trail is read during an investigation, and "from where, with what" is
    # part of the answer when a token has been shared or a script has been pointed at the API.
    assert event["user_agent"], "the request's user agent was not recorded"
    assert "/api/v1" not in json.dumps(after), "the trail should not carry the request body wholesale"


def test_the_reason_is_required_and_vetted_like_any_other_free_text(client, app_module):
    """An empty acknowledgement is the thing this flow exists to prevent.

    ``textguard.prose`` is the same profile every note in this application goes through, so a
    reason that reaches the database cannot *be* markup - the CSV export, an operator's shell
    and a view nobody has written yet are all readers that might forget to escape.
    """
    alert_id = _plant_alert()
    path = f"/api/v1/admin/notifications/{alert_id}/acknowledge"

    # Both halves of "a note is required" answer 422, and that is the point of pinning them
    # together: a *missing* note and an *empty* one are refused by the same validator, so a
    # client that sends `{"note": ""}` cannot slip past a check that only looked for the key.
    # (422 rather than 400 because the refusal is the request model's - the same shape
    # ``ReviewRejectionRequest`` takes for a refusal with no reason.)
    for payload in ({}, {"note": ""}, {"note": "   "}, {"note": "\n\t "}, {"note": None}):
        refused = client.post(path, json=payload, headers=bearer(ADMIN))
        assert refused.status_code == 422, f"{payload!r} was accepted as a reason: {refused.text}"

    markup = client.post(path, json={"note": "<script>alert(1)</script>"}, headers=bearer(ADMIN))
    assert markup.status_code == 422, "markup was stored as a reason"
    assert "plain text" in markup.text, markup.text

    # Nothing above wrote anything - not the acknowledgement, not an audit row.
    assert _alert(alert_id)["acknowledged_at"] is None
    assert _acknowledgements() == []


def test_a_second_answer_is_refused_and_names_the_first(client, app_module):
    """The first acceptance is the one that was actually made, so it is the one kept."""
    alert_id = _plant_alert()
    path = f"/api/v1/admin/notifications/{alert_id}/acknowledge"

    first = client.post(path, json={"note": NOTE}, headers=bearer(ADMIN))
    assert first.status_code == 200

    second = client.post(
        path, json={"note": "a different administrator, a different opinion"}, headers=bearer(HEAD_ADMIN)
    )
    assert second.status_code == 409, second.text
    detail = second.json()["detail"]
    assert ADMIN in detail and HEAD_ADMIN not in detail.split("by")[1][:20], (
        f"the refusal should name the administrator who answered first: {detail}"
    )

    stored = _alert(alert_id)
    assert stored["acknowledgement_note"] == NOTE, "the second answer overwrote the first"
    assert stored["acknowledged_by"] == ADMIN
    assert len(_acknowledgements()) == 1, "the refused answer was written to the trail anyway"


def test_an_alert_that_does_not_exist_cannot_be_acknowledged(client, app_module):
    response = client.post(
        "/api/v1/admin/notifications/999999/acknowledge",
        json={"note": NOTE},
        headers=bearer(ADMIN),
    )
    assert response.status_code == 404
    assert _acknowledgements() == []


def test_a_worker_cannot_answer_a_system_alert(client, app_module):
    """The alert is about the deployment; the people who run it are administrators."""
    alert_id = _plant_alert()
    path = f"/api/v1/admin/notifications/{alert_id}/acknowledge"
    for user_id in (WORKER, MOALLEM):
        response = client.post(path, json={"note": NOTE}, headers=bearer(user_id))
        assert response.status_code == 403, f"{user_id} could acknowledge an operator alert"
    assert client.post(path, json={"note": NOTE}).status_code in (401, 403), (
        "an anonymous caller reached the acknowledgement"
    )
    assert _alert(alert_id)["acknowledged_at"] is None
    assert _acknowledgements() == []


def test_answering_an_alert_reads_it_without_rewriting_an_earlier_reader(client, app_module):
    """An accepted alert has by definition been seen - but ``read_by`` is not the answerer."""
    alert_id = _plant_alert()
    response = client.post(
        f"/api/v1/admin/notifications/{alert_id}/acknowledge",
        json={"note": NOTE},
        headers=bearer(ADMIN),
    )
    assert response.status_code == 200
    stored = _alert(alert_id)
    assert stored["read_at"], "an acknowledged alert stayed in the unread count"
    assert stored["read_by"] == ADMIN

    # An alert somebody else had already opened keeps that reader: acknowledging answers the
    # question, it does not claim the person was the first to look.
    other = _plant_alert(title="Second alert", dedupe_key="startup_override:second", read_by=WORKER)
    client.post(
        f"/api/v1/admin/notifications/{other}/acknowledge", json={"note": NOTE}, headers=bearer(ADMIN)
    )
    assert _alert(other)["read_by"] == WORKER
    assert _alert(other)["acknowledged_by"] == ADMIN


def test_reading_an_alert_is_not_answering_it(client, app_module):
    """The two acts stay distinct, which is what keeps the check honest."""
    alert_id = _plant_alert()
    response = client.post(f"/api/v1/admin/notifications/{alert_id}/read", headers=bearer(ADMIN))
    assert response.status_code == 200

    stored = _alert(alert_id)
    assert stored["read_at"], "the read endpoint stopped recording that somebody looked"
    assert stored["acknowledged_at"] is None, "reading an alert accepted it"
    assert _acknowledgements() == []
    assert readiness._check_startup_override_acknowledged({}).ok is False, (
        "a read alert stopped being reported as unanswered"
    )


def test_the_listing_counts_what_is_waiting_for_an_answer(client, app_module):
    answered = _plant_alert(dedupe_key="startup_override:answered")
    waiting = _plant_alert(title="Still waiting", dedupe_key="startup_override:waiting")
    client.post(
        f"/api/v1/admin/notifications/{answered}/acknowledge", json={"note": NOTE}, headers=bearer(ADMIN)
    )

    body = client.get("/api/v1/admin/notifications", headers=bearer(ADMIN)).json()
    assert body["unacknowledged"] == 1, body["unacknowledged"]
    rows = {row["id"]: row for row in body["notifications"]}
    assert rows[answered]["acknowledgement_note"] == NOTE
    assert rows[answered]["acknowledged_by"] == ADMIN
    assert rows[waiting]["acknowledged_at"] is None
    assert rows[waiting]["acknowledgement_note"] is None, (
        "an unanswered alert carries an acknowledgement field that is not empty"
    )


# ---------------------------------------------------------------------------
# The check that closes the loop
# ---------------------------------------------------------------------------
def test_readiness_reports_a_forced_start_nobody_has_answered(app_module):
    alert_id = _plant_alert(payload={"reason": "the electrician is here tomorrow", "override_until": None})

    check = readiness._check_startup_override_acknowledged({})
    assert check.name == "startup_override_acknowledged"
    assert check.tier == readiness.TIER_ADVISORY
    assert check.ok is False
    assert check.value["alert_id"] == alert_id
    assert check.value["reason"] == "the electrician is here tomorrow"
    assert check.value["acknowledged"] is False
    # The detail tells the operator what to *do*, not only what is wrong - the endpoint is the
    # only way to close it, and the alert id is what the route needs.
    assert f"/api/v1/admin/notifications/{alert_id}/acknowledge" in check.detail
    assert "the electrician is here tomorrow" in check.detail


def test_readiness_says_nothing_to_answer_when_nothing_was_forced(app_module):
    check = readiness._check_startup_override_acknowledged({})
    assert check.ok is True
    assert check.value["forced_start"] is False
    assert "no forced start" in check.detail


def test_the_check_reads_the_newest_overrides_and_the_public_probe_carries_it(client, app_module):
    """An acceptance given for one reason must not answer a different one."""
    _plant_alert(title="First forced start", payload={"reason": "first"}, dedupe_key="startup_override:first")
    newest = _plant_alert(
        title="Second forced start", payload={"reason": "second"}, dedupe_key="startup_override:second"
    )

    check = readiness._check_startup_override_acknowledged({})
    assert check.value["alert_id"] == newest, "an older alert answered for the newer one"
    assert check.value["reason"] == "second"

    client.post(
        f"/api/v1/admin/notifications/{newest}/acknowledge",
        json={"note": "the ledger was checked by hand"},
        headers=bearer(ADMIN),
    )
    answered = readiness._check_startup_override_acknowledged({})
    assert answered.ok is True, answered.detail
    assert answered.value["acknowledged_by"] == ADMIN
    assert answered.value["note"] == "the ledger was checked by hand"

    # The public probe is what a monitor watches, so the verdict has to be there - as a
    # degraded check, never as a 503: the application is serving, and what is degraded is an
    # override nobody has accepted.
    body = client.get("/api/v1/readiness").json()
    assert "startup_override_acknowledged" not in body["degraded_checks"], (
        "an answered forced start is still being reported as unanswered"
    )


def test_the_public_probe_names_it_while_it_is_unanswered(client, app_module):
    _plant_alert(payload={"reason": "an incident"})
    response = client.get("/api/v1/readiness")
    body = response.json()
    assert "startup_override_acknowledged" in body["degraded_checks"], body["degraded_checks"]
    assert response.status_code == 200, (
        "an unaccepted override is advisory: the deployment is serving, and a 503 would take a "
        "working site out of service over a missing reason"
    )
    assert body["checks"]["startup_override_acknowledged"] == {"ok": False, "tier": "advisory"}
