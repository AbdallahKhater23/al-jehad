"""What this application will and will not store, at the boundary and in the database.

WHY THIS EXISTS
---------------
Every string a client sent used to be stored as it arrived, and those strings are rendered
back out - in the roster, in the audit log, in a notification, in a CSV an operator opens in
Excel. A worker named ``<img src=x onerror=...>`` is therefore not a worker with a funny
name; it is code, waiting for the next administrator to open the screen that shows names.

``backend/textguard.py`` is the rule and the request models call it. This suite pins the two
halves that make it worth having:

* **what passes**, because a validator that refuses a legitimate Arabic name, an apostrophe
  in a note, or a dialling code is a validator that gets switched off; and
* **what is refused**, at the API, with the row count checked afterwards - a 422 that still
  wrote the row is not a defence.

It also covers the part input validation cannot fix: rows written before the rule existed.
``readiness`` reports them (``stored_text``) and the frontend escapes them (see
``test_frontend_xss.py``); the last tests here prove the report actually finds them.
"""

from __future__ import annotations

import sqlite3

import harness
import pytest
import textguard
from harness import ADMIN, HEAD_ADMIN, WORKER, bearer, db_rows, db_scalar

#: Payloads, grouped by the shape they attack with. Each is a real construction: an element,
#: an attribute handler, a scheme, an entity-encoded tag, and a payload that only works if
#: the value is interpolated into a quoted attribute rather than into text.
MARKUP = [
    "<script>alert(1)</script>",
    "<img src=x onerror=alert(1)>",
    "<svg/onload=alert(1)>",
    '"><script>alert(1)</script>',
    "<SCRIPT>alert(1)</SCRIPT>",
    "<%2Fscript>",
]
SCHEMES = ["javascript:alert(1)", "JavaScript:alert(1)", "data:text/html;base64,PHNjcmlwdD4="]
ENTITIES = ["&lt;script&gt;alert(1)&lt;/script&gt;", "&#x3C;script&#x3E;", "&LT;img src=x&GT;"]
HANDLERS = ["onclick=alert(1)", 'onerror = "alert(1)"', "onmouseover=alert(1)"]


# ---------------------------------------------------------------------------
# the rules themselves
# ---------------------------------------------------------------------------
def test_an_arabic_name_passes_intact():
    """Arabic is a first-class case, not a tolerated one: this is the workforce."""
    for name in (
        "محمد كمال",
        "عبد الرحمن",
        "مُحَمَّد",  # with the harakat
        "أحمد الفقي",
        "٠١٢٣٤٥",  # Arabic-Indic digits
        "اسم مركب (فرع ٢)",
    ):
        assert textguard.identifier(name, field="Name") == name


def test_latin_names_and_site_names_pass_intact():
    for value in (
        "Mohamed Kamal",
        "Al-Jehad Tower B (Phase 2)",
        "Site_3 / Block 4, Zone 9",
        "Nour El-Din",
    ):
        assert textguard.identifier(value, field="Name", max_length=textguard.MAX_SITE_NAME) == value


@pytest.mark.parametrize("value", MARKUP + SCHEMES + ENTITIES + HANDLERS)
def test_markup_is_refused_in_an_identifier(value):
    with pytest.raises(ValueError) as refusal:
        textguard.identifier(value, field="Name")
    # The message has to name the character, or an administrator staring at a 422 learns
    # nothing about why the name they can read on the ID card was refused.
    assert "U+" in str(refusal.value) or "may contain" in str(refusal.value)


def test_an_apostrophe_is_refused_in_an_identifier_and_kept_in_prose():
    """The one rule a person will notice, pinned here with its reasoning.

    An apostrophe both breaks an unquoted SQL literal and escapes an HTML attribute, so the
    allowlist refuses it for *identifiers* - and prose keeps it, because a note is a
    sentence and every renderer escapes it.
    """
    with pytest.raises(ValueError):
        textguard.identifier("O'Brien", field="Name")
    assert textguard.prose("it's the second time & nobody came; again", field="Message") == (
        "it's the second time & nobody came; again"
    )


def test_ordinary_prose_keeps_its_punctuation():
    sentence = "The lift's 2nd stop isn't working (again!) — 2 days lost, 3 people waiting."
    assert textguard.prose(sentence, field="Message") == sentence


def test_prose_survives_a_comparison_that_only_looks_like_a_tag():
    """"clocks in < 5 min late" is a sentence; ``<script`` is not."""
    assert textguard.prose("clocks in < 5 min late", field="Message") == "clocks in < 5 min late"
    assert textguard.prose("the data: nothing arrived yet", field="Message") == (
        "the data: nothing arrived yet"
    )
    with pytest.raises(ValueError):
        textguard.prose("clocks in <script>alert(1)</script>", field="Message")


@pytest.mark.parametrize("value", MARKUP + ENTITIES)
def test_prose_refuses_markup_and_encoded_markup(value):
    with pytest.raises(ValueError):
        textguard.prose(value, field="Message")


@pytest.mark.parametrize("value", SCHEMES + HANDLERS)
def test_prose_refuses_script_schemes_and_inline_handlers(value):
    with pytest.raises(ValueError):
        textguard.prose(value, field="Message")


def test_the_refusal_says_what_it_found():
    assert textguard.active_content_reason("<b>x</b>") == "an HTML tag"
    assert textguard.active_content_reason("&lt;b&gt;") == "an encoded HTML entity"
    assert textguard.active_content_reason("javascript:alert(1)") == "a javascript: or data: URL"
    assert textguard.active_content_reason('onclick="alert(1)"') == "an inline event handler"
    assert textguard.active_content_reason("nothing to see, 2 < 3 and 5 > 4") is None


def test_bidirectional_overrides_and_control_characters_are_stripped_not_refused():
    """A paste from a word processor is not an attack; it is a name nobody can read.

    ``U+202E`` reverses everything after it - a stored name renders as a *different* name -
    and a NUL makes SQLite and Python disagree about the same string's length. Both are
    removed rather than refused, and the Arabic orthographic joiners are explicitly kept.
    """
    assert textguard.identifier("Ahmed\u202e moc.elpmaxe", field="Name") == "Ahmed moc.elpmaxe"
    assert textguard.identifier("Ahmed\x00Admin", field="Name") == "AhmedAdmin"
    assert textguard.identifier("Ahmed\nAdmin", field="Name") == "Ahmed Admin"
    assert textguard.identifier("  Ａhmed   Kamal  ", field="Name") == "Ahmed Kamal"
    joined = "محمد\u200dكمال"
    assert textguard.prose(joined, field="Message") == joined, "ZWJ is Arabic orthography"


def test_limits_are_enforced_with_the_length_in_the_message():
    with pytest.raises(ValueError) as refusal:
        textguard.identifier("x" * (textguard.MAX_NAME + 1), field="Name")
    assert str(textguard.MAX_NAME) in str(refusal.value)
    with pytest.raises(ValueError):
        textguard.prose("", field="Message")


def test_a_contact_keeps_dialling_punctuation_and_refuses_markup():
    assert textguard.contact("+20 (100) 123-4567", field="Phone") == "+20 (100) 123-4567"
    assert textguard.contact("ana.torres+site@example.test", field="Email") == (
        "ana.torres+site@example.test"
    )
    with pytest.raises(ValueError):
        textguard.contact("ana@example.test<script>", field="Email")
    with pytest.raises(ValueError):
        textguard.contact("ana@example.test; DROP TABLE users", field="Email")


def test_the_policy_document_matches_the_rules():
    policy = textguard.policy()
    assert policy["identifier_punctuation"] == textguard.IDENTIFIER_PUNCTUATION
    assert "Arabic" in policy["identifier_scripts"]
    assert len(policy["prose_refused"]) == 4
    assert policy["max_lengths"]["name"] == textguard.MAX_NAME


# ---------------------------------------------------------------------------
# the boundary: JSON models
# ---------------------------------------------------------------------------
def add_user(client, **overrides):
    payload = {
        "user_id": "321",
        "name": "Nguyen Van A",
        "email": "nguyen@example.test",
        "phone": "+200000000003",
        "password": "Fresh-Pass-123",
        "role": "worker",
    }
    payload.update(overrides)
    return client.post(
        "/api/v1/admin/users/add", headers=bearer(HEAD_ADMIN), json=payload
    )


def test_a_hostile_name_is_refused_and_no_row_is_written(client):
    response = add_user(client, name="<img src=x onerror=alert(1)>Mallory")
    assert response.status_code == 422, response.text[:300]
    assert db_scalar("SELECT COUNT(*) FROM users WHERE id = ?", ("321",)) == 0


def test_an_arabic_name_is_created_and_stored_exactly(client):
    """The end-to-end version of the unit test above: no mojibake, no stripping."""
    response = add_user(client, name="محمد كمال", email="", phone="")
    assert response.status_code == 200, response.text[:300]
    assert db_scalar("SELECT name FROM users WHERE id = ?", ("321",)) == "محمد كمال"


def test_a_hostile_edit_is_refused_and_the_old_name_survives(client):
    before = db_scalar("SELECT name FROM users WHERE id = ?", (WORKER,))
    response = client.post(
        "/api/v1/admin/users/edit",
        headers=bearer(HEAD_ADMIN),
        json={
            "user_id": WORKER,
            "name": "<script>alert(1)</script>",
            "email": "",
            "phone": "",
        },
    )
    assert response.status_code == 422, response.text[:300]
    assert db_scalar("SELECT name FROM users WHERE id = ?", (WORKER,)) == before


def test_an_empty_name_is_still_a_400_with_the_consoles_own_sentence(client):
    """The shape check is Pydantic's (422); emptiness stays the endpoint's (400).

    Both are refusals, and the console already shows this exact sentence for a blank name -
    changing *that* to a validation error would have been a regression dressed as a fix.
    """
    response = client.post(
        "/api/v1/admin/users/edit",
        headers=bearer(HEAD_ADMIN),
        json={"user_id": WORKER, "name": "   ", "email": "", "phone": ""},
    )
    assert response.status_code == 400, response.text[:300]
    assert "Name must not be empty" in response.json()["detail"]


def test_the_multipart_account_console_refuses_markup_too(client):
    """The Credentials tab posts a form, not JSON: the same door, so the same rule."""
    response = client.post(
        "/api/v1/admin/users/create",
        headers=bearer(HEAD_ADMIN),
        data={
            "user_id": "322",
            "name": "<svg/onload=alert(1)>",
            "role": "worker",
            "password": "Fresh-Pass-123",
        },
    )
    assert response.status_code == 400, response.text[:300]
    assert "Name" in response.json()["detail"]
    assert db_scalar("SELECT COUNT(*) FROM users WHERE id = ?", ("322",)) == 0


def test_a_hostile_site_name_is_refused_and_no_site_is_created(client):
    response = client.post(
        "/api/v1/admin/sites/add",
        headers=bearer(HEAD_ADMIN),
        json={"site_name": "<script>alert(1)</script> Depot", "location_input": "30.04,31.23", "radius": 150},
    )
    assert response.status_code == 422, response.text[:300]
    assert db_scalar("SELECT COUNT(*) FROM construction_sites WHERE site_name LIKE '%Depot%'") == 0


def test_an_ordinary_site_name_is_created_and_stored_exactly(client):
    response = client.post(
        "/api/v1/admin/sites/add",
        headers=bearer(HEAD_ADMIN),
        json={"site_name": "Al-Jehad Tower B (Phase 2)", "location_input": "30.04,31.23", "radius": 150},
    )
    assert response.status_code == 200, response.text[:300]
    assert db_scalar(
        "SELECT COUNT(*) FROM construction_sites WHERE site_name = ?",
        ("Al-Jehad Tower B (Phase 2)",),
    ) == 1


def open_note(client, **overrides):
    payload = {
        "category": "other",
        "subject": "Broken lift",
        "body": "The lift's 2nd stop isn't working.",
        "priority": "normal",
    }
    payload.update(overrides)
    return client.post("/api/v1/worker/notes", headers=bearer(WORKER), json=payload)


def test_a_note_carrying_markup_is_refused_and_nothing_is_written(client):
    before = db_scalar("SELECT COUNT(*) FROM worker_notes WHERE worker_id = ?", (WORKER,))
    response = open_note(client, body="<img src=x onerror=alert(1)>")
    assert response.status_code == 400, response.text[:300]
    assert db_scalar("SELECT COUNT(*) FROM worker_notes WHERE worker_id = ?", (WORKER,)) == before
    # The same gate covers the subject, which is the field that ends up in a notification.
    refused = open_note(client, subject="<script>alert(1)</script>")
    assert refused.status_code == 400, refused.text[:300]


def test_a_note_keeps_its_apostrophes_and_reaches_the_administrator_verbatim(client):
    """A note is prose: refusing it for writing "isn't" would be the validator's own bug."""
    response = open_note(client, body="the lift isn't working - 2nd time this week & nobody came")
    assert response.status_code == 200, response.text[:300]
    stored = db_scalar(
        "SELECT body FROM worker_notes WHERE id = ?", (int(response.json()["note"]["id"]),)
    )
    assert stored == "the lift isn't working - 2nd time this week & nobody came"


def test_the_notification_body_carries_no_markup(client):
    """The alert is built by string concatenation from the worker's name and subject.

    That is the exact shape a stored payload needs to reach an administrator, so this asserts
    the alert is text - and the note that produced it was accepted, which is the point: the
    guarantee comes from the values, not from the alert.
    """
    response = open_note(client, subject="Password reset", body="I cannot sign in since yesterday.")
    assert response.status_code == 200, response.text[:300]
    bodies = [
        row[0]
        for row in db_rows("SELECT body FROM admin_notifications WHERE kind = ?", ("worker_note",))
    ]
    assert bodies, "a worker's note must notify an administrator"
    assert all("<" not in body for body in bodies), bodies
    assert any("Password reset" in body for body in bodies), bodies


def test_an_enrollment_invite_cannot_name_a_worker_with_markup(client):
    """``kind=register`` writes this name onto a new account; nothing else constrains it."""
    response = client.post(
        "/api/v1/admin/enrollment/invites",
        headers=bearer(ADMIN),
        json={
            "worker_id": "400",
            "kind": "register",
            "name": "<script>alert(1)</script>",
            "role": "worker",
        },
    )
    assert response.status_code == 422, response.text[:300]
    # ``name`` on the request becomes ``pending_name`` on the row: the name is not written
    # anywhere until the link is opened and the account is created.
    assert db_scalar(
        "SELECT COUNT(*) FROM enrollment_invites WHERE pending_name LIKE '%script%'"
    ) == 0


def test_a_quick_links_note_is_prose_and_not_markup(client):
    response = client.post(
        "/api/v1/admin/quick_links",
        headers=bearer(ADMIN),
        json={"worker_id": WORKER, "note": "<script>alert(1)</script>"},
    )
    assert response.status_code == 422, response.text[:300]
    stored = client.post(
        "/api/v1/admin/quick_links",
        headers=bearer(ADMIN),
        json={"worker_id": WORKER, "note": "the man on the third tower (isn't on site yet)"},
    )
    assert stored.status_code == 200, stored.text[:300]
    assert db_scalar("SELECT COUNT(*) FROM quick_links WHERE note LIKE '%third tower%'") == 1


def test_a_review_note_is_refused_at_the_model(client):
    """``/admin/approve_review`` takes an administrator's note; the model is the gate.

    The note reaches the audit log and the worker's own record, so it is prose - and prose is
    where an injected payload would otherwise be stored by the one account type that is
    trusted to write freely.
    """
    response = client.post(
        "/api/v1/admin/approve_review",
        headers=bearer(ADMIN),
        json={"log_id": 1, "approved_hours": 8, "note": "<script>alert(1)</script>"},
    )
    assert response.status_code == 422, response.text[:300]
    assert "plain text" in response.text or "HTML tag" in response.text


# ---------------------------------------------------------------------------
# the part validation cannot fix: text written before the rule existed
# ---------------------------------------------------------------------------
def _write_legacy_row() -> str:
    """Write the row the front door now refuses, the way an old version would have.

    Straight to SQLite on purpose: this is the *only* way to produce the situation the check
    exists for, and a test that went through the API could never reach it.
    """
    conn = sqlite3.connect(harness.DB_PATH)
    try:
        conn.execute(
            "UPDATE users SET name = ? WHERE id = ?", ("<img src=x onerror=alert(1)>", WORKER)
        )
        conn.commit()
    finally:
        conn.close()
    return "<img src=x onerror=alert(1)>"


def test_stored_text_the_rules_would_refuse_is_reported(app_module):
    payload = _write_legacy_row()
    import readiness

    checks, _ = readiness.run_checks(app_module)
    check = next(c for c in checks if c.name == "stored_text")
    assert check.ok is False, "a name that would be refused today must be reported"
    assert check.tier == "advisory", "it is a look-at-this, not a refusal to start"
    assert "users.name" in check.detail, check.detail
    assert check.value["offender_count"] >= 1
    assert payload in db_scalar("SELECT name FROM users WHERE id = ?", (WORKER,))


def test_a_clean_database_reports_nothing_to_look_at(app_module):
    import readiness

    checks, _ = readiness.run_checks(app_module)
    check = next(c for c in checks if c.name == "stored_text")
    assert check.ok is True, check.detail
    assert check.value["offender_count"] == 0


def test_the_read_path_still_serves_legacy_text(client, app_module):
    """Reporting is not hiding: an operator can still see the row and decide.

    The frontend escapes what it renders (``test_frontend_xss.py`` renders exactly this
    payload through the real files), so serving it is safe - and refusing to serve it would
    take a worker off a roster over a name somebody else typed an hour earlier.
    """
    payload = _write_legacy_row()
    response = client.get("/api/v1/admin/users", headers=bearer(HEAD_ADMIN))
    assert response.status_code == 200, response.text[:300]
    assert payload in response.text
