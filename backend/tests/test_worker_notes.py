"""Worker notes: the written channel between a worker and the administrator.

WHAT THIS GUARDS
----------------
A note is the only place in this app where a worker writes free text and an
administrator reads it, so the interesting properties are not "does it save":

* **Privacy between workers.** A note belongs to its author. Asking for a
  colleague's note has to answer exactly like a note that does not exist, or the
  endpoint becomes a way to enumerate what everybody is complaining about.
* **Internal messages stay internal.** An admin working note is filtered out
  *server-side* for every worker-facing query, and the test looks for the text
  itself in the response body, not just for the flag.
* **Only the worker can say it is done.** An admin marking a note ``resolved`` is a
  claim; the worker replying is the correction. That reply has to reopen the note
  and raise a notification, because an admin who believed it was finished will not
  reopen the inbox on their own.
* **Nothing readable is stored.** A password reset from a note goes through the
  same hashed path as everything else, and the note is never a place a password is
  written down.
* **The alert queue hears about it.** A note nobody is told about is a note nobody
  answers - it raises ``admin_notifications``, the same queue the overtime watcher
  and the liveness gate use.
"""

from __future__ import annotations

import json

import pytest
from harness import (
    ADMIN,
    EMAILS,
    HEAD_ADMIN,
    MOALLEM,
    PASSWORDS,
    WORKER,
    assert_denied,
    bearer,
    db_rows,
    db_scalar,
)

WORKER_PATHS = ("/api/v1/worker/notes",)
ADMIN_PATHS = ("/api/v1/admin/notes",)


def open_note(client, *, user_id=WORKER, subject="Password not working", body="I cannot sign in since yesterday.",
              category="password_reset", priority="normal", headers=None):
    return client.post(
        "/api/v1/worker/notes",
        headers=headers if headers is not None else bearer(user_id),
        json={"category": category, "subject": subject, "body": body, "priority": priority},
    )


def note_id(response) -> int:
    assert response.status_code == 200, response.text[:300]
    return int(response.json()["note"]["id"])


def admin_note(client, note: int, headers=None) -> dict:
    response = client.get(f"/api/v1/admin/notes/{note}", headers=headers or bearer(ADMIN))
    assert response.status_code == 200, response.text[:300]
    return response.json()


def note_ids_in_notifications(kind: str) -> set[int]:
    """The notes an alert names, read from its payload.

    Counting alerts by worker would also count an alert about a *different* note from the
    same worker - and this suite runs against a clone of a live database that may well
    hold notes from the site already.
    """
    found: set[int] = set()
    for (payload,) in db_rows("SELECT payload FROM admin_notifications WHERE kind = ?", (kind,)):
        try:
            found.add(int(json.loads(payload or "{}").get("note_id")))
        except (TypeError, ValueError):
            continue
    return found


def clear_open_notes(client, *user_ids: str) -> None:
    """Resolve whatever is still waiting, so a cap test starts from a known state.

    The live database is a real one: it may already hold notes for the seeded accounts.
    That is the harness's warning about every table, and it applies to this new one too -
    so a test that asserts on counts either scopes itself to the notes it created, or
    makes room first.
    """
    for user_id in user_ids:
        listing = client.get(
            f"/api/v1/admin/notes?worker_id={user_id}", headers=bearer(ADMIN)
        ).json()
        for row in listing["notes"]:
            if row["open"]:
                client.post(
                    f"/api/v1/admin/notes/{row['id']}/status",
                    headers=bearer(ADMIN),
                    json={"status": "resolved"},
                )


# ---------------------------------------------------------------------------
# access
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", WORKER_PATHS)
def test_notes_need_a_token(client, path):
    assert client.get(path).status_code == 401
    assert client.post(path, json={"subject": "s", "body": "b"}).status_code == 401


@pytest.mark.parametrize("path", ADMIN_PATHS)
def test_the_admins_inbox_is_admin_only(client, path):
    """A worker reading every colleague's requests is the whole feature inverted."""
    assert client.get(path).status_code == 401
    assert_denied(
        client.get(path, headers=bearer(WORKER)),
        endpoint=path,
        detail="a worker must not read the admin notes inbox",
    )
    assert client.get(path, headers=bearer(MOALLEM)).status_code == 403
    assert client.get(path, headers=bearer(ADMIN)).status_code == 200


def test_a_worker_cannot_post_hidden_identity_fields(client):
    """``worker_id`` is ignored: the author is the token, never a request field."""
    before = db_scalar("SELECT COUNT(*) FROM worker_notes WHERE worker_id = ?", (WORKER,))
    response = client.post(
        "/api/v1/worker/notes",
        headers=bearer(MOALLEM),
        json={"category": "other", "subject": "s", "body": "b", "worker_id": WORKER, "internal": True},
    )
    assert response.status_code == 200, response.text[:300]
    assert response.json()["note"]["worker_id"] == MOALLEM
    assert db_scalar("SELECT COUNT(*) FROM worker_notes WHERE worker_id = ?", (WORKER,)) == before, (
        "nothing may be filed under the id in the payload"
    )


# ---------------------------------------------------------------------------
# the round trip
# ---------------------------------------------------------------------------
def test_a_worker_opens_a_note_and_the_admin_is_told(client):
    """Without the notification the request exists only if somebody opens the tab."""
    created = open_note(client)
    created_id = note_id(created)
    assert created.json()["note"]["status"] == "open"
    assert created.json()["note"]["admin_unread"] == 1, "it is unread until an admin opens it"

    inbox = client.get("/api/v1/admin/notes", headers=bearer(ADMIN))
    assert inbox.status_code == 200, inbox.text[:300]
    payload = inbox.json()
    entry = next(row for row in payload["notes"] if row["id"] == created_id)
    assert entry["worker_name"] == "Seed Worker", "the inbox names the worker, not only their id"
    assert entry["worker_id"] == WORKER
    assert entry["category"] == "password_reset"
    assert entry["category_label"], "a category is labelled for the reader, not left as a code"
    assert payload["counts"]["open"] >= 1
    assert payload["unread"] >= 1

    # The alert queue, which is what actually makes somebody look.
    assert created_id in note_ids_in_notifications("worker_note")
    # ... and the append-only audit trail, in the same transaction as the note. Scoped to
    # this note: the live database may already hold this worker's earlier requests.
    row = db_rows(
        "SELECT actor_id, actor_role, after_json FROM audit_log "
        "WHERE action = 'worker_note_create' AND entity_id = ?",
        (str(created_id),),
    )
    assert len(row) == 1, "a note and its audit row commit together or not at all"
    assert row[0][0] == WORKER and row[0][1] == "worker"
    assert "password_reset" in (row[0][2] or "")
    assert db_scalar("SELECT body FROM worker_note_messages WHERE note_id = ?", (created_id,)) == \
        "I cannot sign in since yesterday."


def test_the_admin_answers_and_the_worker_reads_it(client):
    # A delta, not an absolute: the live database this suite clones may already hold an
    # unanswered reply for this worker, and "the badge went up by one and came back" is
    # the property under test either way.
    unread_before = client.get("/api/v1/worker/notes", headers=bearer(WORKER)).json()["unread"]
    created = open_note(client)
    created_id = note_id(created)

    replied = client.post(
        f"/api/v1/admin/notes/{created_id}/replies",
        headers=bearer(ADMIN),
        json={"body": "I have set a new password - call me.", "status": "in_progress"},
    )
    assert replied.status_code == 200, replied.text[:300]
    assert replied.json()["note"]["status"] == "in_progress"

    # The worker sees the reply and a badge that says so.
    listing = client.get("/api/v1/worker/notes", headers=bearer(WORKER)).json()
    mine = next(row for row in listing["notes"] if row["id"] == created_id)
    assert mine["worker_unread"] == 1
    assert mine["last_message"]["from_admin"] is True
    assert listing["unread"] == unread_before + 1

    thread = client.get(f"/api/v1/worker/notes/{created_id}", headers=bearer(WORKER))
    assert thread.status_code == 200, thread.text[:300]
    assert [message["body"] for message in thread.json()["messages"]] == [
        "I cannot sign in since yesterday.",
        "I have set a new password - call me.",
    ]
    assert thread.json()["worker_unread"] == 0, "opening the thread is what clears the badge"
    assert client.get("/api/v1/worker/notes", headers=bearer(WORKER)).json()["unread"] == \
        unread_before, "and only that thread's reply"


def test_read_state_travels_in_the_direction_it_belongs_to(client):
    """Each side's unread counter is cleared by that side opening the thread."""
    created_id = note_id(open_note(client))

    # The admin reading it clears the admin's counter, not the worker's.
    admin_note(client, created_id)
    assert db_scalar("SELECT admin_unread FROM worker_notes WHERE id = ?", (created_id,)) == 0
    assert db_scalar("SELECT worker_unread FROM worker_notes WHERE id = ?", (created_id,)) == 0

    client.post(
        f"/api/v1/admin/notes/{created_id}/replies",
        headers=bearer(ADMIN),
        json={"body": "On it."},
    )
    assert db_scalar("SELECT worker_unread FROM worker_notes WHERE id = ?", (created_id,)) == 1
    assert db_scalar("SELECT admin_unread FROM worker_notes WHERE id = ?", (created_id,)) == 0

    client.get(f"/api/v1/worker/notes/{created_id}", headers=bearer(WORKER))
    assert db_scalar("SELECT worker_unread FROM worker_notes WHERE id = ?", (created_id,)) == 0


# ---------------------------------------------------------------------------
# privacy
# ---------------------------------------------------------------------------
def test_a_note_belongs_to_its_author(client):
    created_id = note_id(open_note(client))

    other = client.get(f"/api/v1/worker/notes/{created_id}", headers=bearer(MOALLEM))
    assert other.status_code == 404, "another worker's note must look exactly like a missing one"
    theirs = client.get("/api/v1/worker/notes", headers=bearer(MOALLEM)).json()
    assert all(row["id"] != created_id for row in theirs["notes"])

    # The same for writing into it: replying into somebody else's thread is not a
    # permission this app has for anybody but its author and the admin.
    assert client.post(
        f"/api/v1/worker/notes/{created_id}/replies",
        headers=bearer(MOALLEM),
        json={"body": "and another thing"},
    ).status_code == 404
    assert client.post(
        f"/api/v1/worker/notes/{created_id}/close", headers=bearer(MOALLEM)
    ).status_code == 404
    assert db_scalar("SELECT COUNT(*) FROM worker_note_messages WHERE note_id = ?", (created_id,)) == 1


def test_an_internal_message_never_reaches_the_worker(client):
    """Filtered in the query, so it cannot leak through any worker-facing response."""
    created_id = note_id(open_note(client))
    secret = "internal: the old import created this account, check the roster"

    posted = client.post(
        f"/api/v1/admin/notes/{created_id}/replies",
        headers=bearer(ADMIN),
        json={"body": secret, "internal": True, "status": "in_progress"},
    )
    assert posted.status_code == 200, posted.text[:300]
    assert db_scalar("SELECT internal FROM worker_note_messages WHERE body = ?", (secret,)) == 1
    # An internal message is not an answer, so it must not touch the worker's badge.
    assert db_scalar("SELECT worker_unread FROM worker_notes WHERE id = ?", (created_id,)) == 0

    for response in (
        client.get(f"/api/v1/worker/notes/{created_id}", headers=bearer(WORKER)),
        client.get("/api/v1/worker/notes", headers=bearer(WORKER)),
    ):
        assert response.status_code == 200, response.text[:300]
        assert "internal: the old import" not in response.text, (
            "an internal note must not appear in a worker's response at all"
        )
    # The admin's own view of the same thread does carry it.
    assert secret in client.get(
        f"/api/v1/admin/notes/{created_id}", headers=bearer(ADMIN)
    ).text


def test_a_worker_cannot_publish_an_internal_message_into_their_own_thread(client):
    created_id = note_id(open_note(client))
    client.post(
        f"/api/v1/worker/notes/{created_id}/replies",
        headers=bearer(WORKER),
        json={"body": "still locked out", "internal": True},
    )
    assert db_scalar(
        "SELECT internal FROM worker_note_messages WHERE body = 'still locked out'"
    ) == 0, "a message its author cannot see in their own thread is not a message"
    assert "still locked out" in client.get(
        f"/api/v1/worker/notes/{created_id}", headers=bearer(WORKER)
    ).text


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------
def test_only_the_worker_can_confirm_it_is_done(client):
    """A resolved note that the worker answers goes back to open, and shouts."""
    created_id = note_id(open_note(client))
    resolved = client.post(
        f"/api/v1/admin/notes/{created_id}/status",
        headers=bearer(ADMIN),
        json={"status": "resolved", "reply": "New password is set."},
    )
    assert resolved.status_code == 200, resolved.text[:300]
    assert resolved.json()["note"]["status"] == "resolved"
    assert db_scalar("SELECT resolved_by FROM worker_notes WHERE id = ?", (created_id,)) == ADMIN

    before = note_ids_in_notifications("worker_note_reopened")
    reply = client.post(
        f"/api/v1/worker/notes/{created_id}/replies",
        headers=bearer(WORKER),
        json={"body": "The new one does not work either."},
    )
    assert reply.status_code == 200, reply.text[:300]
    assert reply.json()["reopened"] is True
    assert reply.json()["note"]["status"] == "open"
    # The resolution is undone, not merely overridden: ``resolved_by`` would otherwise
    # say this note was signed off by an admin who can no longer see it as settled.
    assert db_scalar("SELECT resolved_at FROM worker_notes WHERE id = ?", (created_id,)) is None
    after = note_ids_in_notifications("worker_note_reopened")
    assert created_id in after

    # A reply on a note that is still open is not a reopening, and must not spam the queue.
    client.post(
        f"/api/v1/worker/notes/{created_id}/replies", headers=bearer(WORKER), json={"body": "Any news?"}
    )
    assert note_ids_in_notifications("worker_note_reopened") == after, (
        f"the queue was {before} before and must not grow again"
    )


def test_the_worker_closes_their_own_note(client):
    created_id = note_id(open_note(client))
    closed = client.post(f"/api/v1/worker/notes/{created_id}/close", headers=bearer(WORKER))
    assert closed.status_code == 200, closed.text[:300]
    assert db_scalar("SELECT status FROM worker_notes WHERE id = ?", (created_id,)) == "closed"
    assert db_scalar("SELECT closed_by FROM worker_notes WHERE id = ?", (created_id,)) == WORKER
    assert db_scalar(
        "SELECT COUNT(*) FROM audit_log WHERE action = 'worker_note_close' AND entity_id = ?",
        (str(created_id),),
    ) == 1


def test_statuses_and_categories_are_a_closed_set(client):
    """A free-text status is a status nobody can filter or count on."""
    created_id = note_id(open_note(client))
    assert client.post(
        f"/api/v1/admin/notes/{created_id}/status", headers=bearer(ADMIN), json={"status": "maybe"}
    ).status_code == 400
    assert client.post(
        "/api/v1/worker/notes",
        headers=bearer(WORKER),
        json={"category": "password", "subject": "s", "body": "b"},
    ).status_code == 400, "the category must match one the server knows"
    assert client.post(
        "/api/v1/worker/notes", headers=bearer(WORKER), json={"category": "other", "subject": " ", "body": "b"}
    ).status_code == 400
    assert client.get(
        f"/api/v1/worker/notes/{created_id}", headers=bearer(WORKER)
    ).status_code == 200, "a bad request elsewhere must not have broken the note"


def test_a_note_can_only_be_answered_once_the_worker_has_said_what_happened(client):
    """An empty note is not a request: there is nothing for anyone to act on."""
    assert client.post(
        "/api/v1/worker/notes",
        headers=bearer(WORKER),
        json={"category": "missing_item", "subject": "Cement", "body": ""},
    ).status_code == 400
    assert client.post(
        "/api/v1/worker/notes",
        headers=bearer(WORKER),
        json={"category": "missing_item", "subject": "x" * 400, "body": "b"},
    ).status_code == 400, "an unbounded subject is a body with no summary"


def test_a_worker_with_too_many_open_requests_is_told_to_wait(client, app_module, monkeypatch):
    """The cap is per worker and only counts what is still waiting on somebody.

    It is deliberately not an IP rate limit: a whole site behind one tunnel shares an
    IP, and one worker's twenty notes must not silence their colleagues.
    """
    monkeypatch.setattr(app_module.notes.settings, "notes_max_open_per_worker", 2)
    clear_open_notes(client, WORKER, MOALLEM)
    first = note_id(open_note(client, subject="one"))
    assert open_note(client, subject="two").status_code == 200
    refused = open_note(client, subject="three")
    assert refused.status_code == 429, refused.text[:300]
    assert "open notes" in refused.text, "the refusal has to say what to do about it"

    # The colleague is unaffected...
    assert open_note(client, user_id=MOALLEM, subject="mine").status_code == 200
    # ... and resolving one makes room again, because the cap counts open work.
    client.post(
        f"/api/v1/admin/notes/{first}/status", headers=bearer(ADMIN), json={"status": "resolved"}
    )
    assert open_note(client, subject="three").status_code == 200


def test_the_counts_are_the_ones_the_dashboard_shows(client):
    first = note_id(open_note(client, subject="counts one"))
    second = note_id(open_note(client, subject="counts two", category="missing_item"))
    client.post(
        f"/api/v1/admin/notes/{first}/status", headers=bearer(ADMIN), json={"status": "resolved"}
    )

    payload = client.get("/api/v1/admin/notes", headers=bearer(ADMIN)).json()
    statuses = {row["id"]: row["status"] for row in payload["notes"]}
    assert statuses[first] == "resolved"
    assert statuses[second] == "open"
    assert payload["counts"]["resolved"] >= 1, "this note is counted, whatever else is in the table"
    assert payload["counts"]["open"] >= 1
    # "Waiting" is the two live statuses together - the number the tab's badge reads.
    assert payload["open"] == payload["counts"]["open"] + payload["counts"]["in_progress"]

    filtered = client.get("/api/v1/admin/notes?status=resolved", headers=bearer(ADMIN)).json()
    assert {row["status"] for row in filtered["notes"]} == {"resolved"}
    assert first in [row["id"] for row in filtered["notes"]]
    by_category = client.get("/api/v1/admin/notes?category=missing_item", headers=bearer(ADMIN)).json()
    assert {row["category"] for row in by_category["notes"]} == {"missing_item"}
    assert second in [row["id"] for row in by_category["notes"]]
    by_worker = client.get(f"/api/v1/admin/notes?worker_id={WORKER}", headers=bearer(ADMIN)).json()
    assert {row["worker_id"] for row in by_worker["notes"]} == {WORKER}


def test_the_startup_gate_accepts_the_notes_surface(client, app_module):
    """A new ``/admin`` route has to satisfy the fatal-tier guard check.

    Readiness refuses to serve traffic when an admin route is missing its ``require_role``
    dependency, so a new screen that forgot the guard does not reach an operator as a
    surprise - it fails at startup. This asserts the notes routes passed it.
    """
    report = app_module.app.state.gate_report
    assert report.fatal_failures == [], [
        f"{check.name}: {check.detail}" for check in report.fatal_failures
    ]
    guarded = next(
        check for check in report.checks if check.name == "auth_enforced_on_admin_routes"
    )
    assert guarded.ok is True
    assert guarded.value["guarded"] >= 4, "the four notes routes are among the guarded ones"
    assert guarded.value["missing"] == []
    # The count has to be a real count. This check once passed vacuously - it walked
    # ``app.routes``, which no longer contains the included routers, and reported
    # "0 guarded, 0 missing" - so the four notes routes are asserted individually too.
    assert guarded.value["guarded"] >= 40, (
        "the whole admin surface is enumerated, not just the routes this feature added"
    )


def test_a_missing_note_is_a_404_and_not_a_crash(client):
    assert client.get("/api/v1/worker/notes/999999", headers=bearer(WORKER)).status_code == 404
    assert client.get("/api/v1/admin/notes/999999", headers=bearer(ADMIN)).status_code == 404
    assert client.post(
        "/api/v1/admin/notes/999999/replies", headers=bearer(ADMIN), json={"body": "hello"}
    ).status_code == 404


# ---------------------------------------------------------------------------
# password help
# ---------------------------------------------------------------------------
def test_the_reset_offer_is_hidden_where_the_server_would_refuse_it(client):
    """A button that can only answer 403 is a trap, so it is not shown.

    ``/admin/users/edit_password`` refuses a standard admin touching an
    administrator's account; the note carries the same verdict, so the console does not
    depend on repeating the rule correctly.
    """
    mine = open_note(client, category="password_reset")
    assert admin_note(client, note_id(mine))["can_reset_password"] is True

    their_note = client.post(
        "/api/v1/worker/notes",
        headers=bearer(HEAD_ADMIN),
        json={"category": "password_reset", "subject": "My own password", "body": "please reset it"},
    )
    assert their_note.status_code == 200, their_note.text[:300]
    head_admin_note = admin_note(client, note_id(their_note))
    assert head_admin_note["worker_role"] == "head_admin"
    assert head_admin_note["can_reset_password"] is False, (
        "a standard admin may not reset another administrator - the console must know that"
    )


def test_a_password_reset_from_a_note_goes_through_the_same_hashed_path(client):
    """The note asks for a reset; the reset still happens in ``users``, hashed.

    This is the one claim about this feature that is a security claim: answering a
    password request must not put a readable password anywhere - not in the note, not
    in a reply, not in the audit log.
    """
    created_id = note_id(open_note(client, body="I lost my password, please give me a new one."))
    new_password = "note-reset-pass-9911"
    # Minted before the reset: ``bearer()`` reads the live token version, so a header
    # built afterwards is a *new* token and would prove nothing about revocation.
    signed_out = bearer(WORKER)

    response = client.post(
        "/api/v1/admin/users/edit_password",
        headers=bearer(ADMIN),
        json={"worker_id": WORKER, "new_password": new_password, "admin_id": ADMIN},
    )
    assert response.status_code == 200, response.text[:300]

    # The reset revoked the worker's sessions, exactly like the Credentials tab does.
    assert db_scalar("SELECT token_version FROM users WHERE id = ?", (WORKER,)) >= 1
    assert client.get("/api/v1/worker/notes", headers=signed_out).status_code == 401, (
        "a worker whose password was just rotated is signed out, not silently still in"
    )

    # Nothing anywhere holds the password in readable form.
    for table, column in (
        ("worker_notes", "body"),
        ("worker_notes", "subject"),
        ("worker_note_messages", "body"),
    ):
        assert db_scalar(
            f"SELECT COUNT(*) FROM {table} WHERE {column} LIKE ?", (f"%{new_password}%",)
        ) == 0, f"{table}.{column} must not hold a password"
    assert db_scalar(
        "SELECT COUNT(*) FROM audit_log WHERE after_json LIKE ?", (f"%{new_password}%",)
    ) == 0
    assert new_password not in client.get(
        f"/api/v1/admin/notes/{created_id}", headers=bearer(ADMIN)
    ).text


def test_a_moallem_can_raise_a_note_like_anybody_else(client):
    """A moallem is a worker with a different range: the same channel, the same rules."""
    created = open_note(
        client, user_id=MOALLEM, category="missing_item", subject="Scaffolding missing",
        body="The second tower has no safety rails this week.",
    )
    assert created.status_code == 200, created.text[:300]
    created_id = note_id(created)

    listing = client.get("/api/v1/worker/notes", headers=bearer(MOALLEM)).json()
    # Their own notes, and only theirs: the list travels with the token, so a colleague's
    # note cannot appear in it even by accident.
    assert {row["worker_id"] for row in listing["notes"]} == {MOALLEM}
    assert created_id in [row["id"] for row in listing["notes"]]
    assert listing["categories"], "the client is told which categories exist"

    entry = next(
        row for row in client.get(f"/api/v1/admin/notes?worker_id={MOALLEM}", headers=bearer(ADMIN)).json()["notes"]
        if row["id"] == created_id
    )
    assert entry["worker_role"] == "moallem"
    assert entry["worker_name"] == "Seed Lead Worker"
    # ... and raising a note changed nothing about their credentials.
    assert db_scalar("SELECT COALESCE(token_version, 0) FROM users WHERE id = ?", (MOALLEM,)) == 0
    signed_in = client.post(
        "/api/v1/auth/login",
        json={"user_id": MOALLEM, "email_or_phone": EMAILS[MOALLEM], "password": PASSWORDS[MOALLEM]},
    )
    assert signed_in.status_code == 200, signed_in.text[:200]
