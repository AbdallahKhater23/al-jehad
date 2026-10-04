"""Who a worker answers to, and what their name is in each language.

WHY THIS EXISTS
---------------
The registration link now asks two things it did not ask before: which moallem the applicant
is joining, and their name in the language they are reading. Both are *people* data - a name
that must render for a reader in one of four scripts, and an assignment that must resolve to a
live account - so both have failure modes that are silent on screen and expensive in the
roster. Every assertion below is one of those failures:

1. **A worker's assignment is stored, and a moallem's is not.** The role supervises; assigning
   a moallem to a moallem is a loop waiting to be written, and the form ignores the field
   rather than storing one.
2. **An assignment that cannot be resolved is refused, not stored.** A deleted supervisor, a
   switched-off one, one still waiting for approval, or an account that is not a moallem at all
   - each is answered by name on both surfaces (the public form and the console), because a
   roster row showing a worker reporting to nobody is worse than one showing nobody assigned.
3. **The applicant cannot pick what the write would refuse.** The list the dropdown is built
   from is the same predicate the write validates with, so the control never offers a trap -
   and it is behind the link, so the staff list is not reachable from any URL.
4. **A name is stored per language and read in all of them.** The slot the applicant typed in
   is what is stored; the language they are reading is what is displayed, with the canonical
   name as the fallback - never an empty cell, and never a name copied into a slot it was not
   said in.
5. **The console can move an assignment and edit a name, and the trail says so.** ``user_edit``
   carries the assignment and the name map in its ``before``/``after`` pair - the roster only
   knows today's answer, and a payroll dispute is about last month's.
6. **Deleting a moallem releases their crew in the same transaction.** SQLite does not enforce
   the column's ``ON DELETE SET NULL`` here (the pragma is off), so the write that keeps the
   roster resolvable is this code's, and the delete row names who was released.
7. **The two views an administrator reads carry the assignment**: the timesheet row and the
   live board, both of which already join ``users``.
"""

from __future__ import annotations

import io
import json
import random
import re
import sqlite3
from pathlib import Path

import pytest
from fastapi import HTTPException
from PIL import Image

import database
import harness
import main
import names
import registrations
import textguard
from harness import (
    ADMIN,
    MOALLEM,
    OFF_OFFICE,
    WORKER,
    bearer,
    clock_in,
    current_db_path,
    db_rows,
    db_scalar,
    seed_reference,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FRONTEND = PROJECT_ROOT / "frontend"

PUBLIC = f"/api/v1/register/{registrations.link_token(0)}"
STRONG_PASSWORD = "site-attendance-2026"
#: A second moallem, created by the tests that need to *move* an assignment rather than set one.
SECOND_MOALLEM = "601"


@pytest.fixture
def intake(monkeypatch):
    """Intake open for one test: it ships closed, so this is the operator's switch."""
    from config import settings

    monkeypatch.setattr(settings, "registration_enabled", True)
    return settings


def photo(seed: int = 0, size: tuple[int, int] = (240, 240)) -> bytes:
    """A valid JPEG whose bytes differ per seed, as the registration suite's own helper does."""
    rng = random.Random(seed)
    image = Image.new("RGB", size)
    image.putdata(
        [(rng.randrange(256), rng.randrange(256), rng.randrange(256)) for _ in range(size[0] * size[1])]
    )
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG")
    return buffer.getvalue()


def submit(client, *, role="worker", image=None, **fields):
    data = {"password": STRONG_PASSWORD, "role": role, "consent": "true"}
    data.update({key: value for key, value in fields.items() if value is not None})
    return client.post(
        PUBLIC,
        data=data,
        files={"photo": ("photo.jpg", image if image is not None else photo(), "image/jpeg")},
    )


def free_worker_id() -> str:
    """A worker id nothing in this database is using.

    The pristine snapshot is a database in daily use, so an id a test can borrow is not one it
    may write down: the developer's own roster holds numbers this suite knows nothing about, and
    a fixed id failed the day a real worker was given it. The lowest free one, so a failure is
    reproducible rather than "the id that happened to be free".
    """
    taken = {str(row[0]) for row in db_rows("SELECT id FROM users")}
    for candidate in range(2, 500):
        if str(candidate) not in taken:
            return str(candidate)
    raise AssertionError("the worker band has no free id for this test")


def plant_user(user_id: str, name: str, role: str, *, status: str = "active") -> None:
    """One account, planted directly: the states this suite needs are not all reachable by API."""
    conn = sqlite3.connect(current_db_path())
    try:
        conn.execute(
            "INSERT INTO users (id, name, email, phone, password_hash, role, status) "
            "VALUES (?, ?, '', '', 'x', ?, ?)",
            (user_id, name, role, status),
        )
        conn.commit()
    finally:
        conn.close()


def created_id(response) -> str:
    assert response.status_code == 200, response.text[:400]
    return str(response.json()["user_id"])


def edit(client, user_id: str, **fields):
    body = {"user_id": user_id, "name": "Edited Name", "email": "", "phone": ""}
    body.update(fields)
    return client.post("/api/v1/admin/users/edit", headers=bearer(ADMIN), json=body)


def moallem_list(client) -> list[dict]:
    response = client.get(f"{PUBLIC}/moallems")
    assert response.status_code == 200, response.text[:300]
    return response.json()["moallems"]


def roster_row(client, user_id: str) -> dict:
    response = client.get("/api/v1/admin/users", headers=bearer(ADMIN))
    assert response.status_code == 200, response.text[:300]
    rows = [row for row in response.json() if row["id"] == user_id]
    assert rows, f"{user_id} is not in the roster"
    return rows[0]


# ---------------------------------------------------------------------------
# the name map, on its own
# ---------------------------------------------------------------------------
def test_a_name_read_in_a_language_it_was_not_given_in_falls_back_to_the_one_it_was():
    stored = names.parse({"ar": "أحمد علي"})
    assert names.display(stored, "ar") == "أحمد علي"
    # Not translated yet, and the reader is on the English screen: the name they were given,
    # not an empty cell and not a different person's.
    assert names.display(stored, "en") == "أحمد علي"
    assert names.display(stored, "en", "Ahmed Ali") == "أحمد علي"
    assert names.display({}, "en", "Ahmed Ali") == "Ahmed Ali"


def test_only_the_languages_this_build_reads_survive_a_stored_map():
    stored = names.parse('{"en": "Ahmed", "fr": "Ahmed", "ar": "  أحمد   علي  "}')
    assert stored == {"en": "Ahmed", "ar": "أحمد علي"}
    assert names.serialise({}) is None
    assert json.loads(names.serialise({"en": "Ahmed"})) == {"en": "Ahmed"}


def test_the_translation_pass_fills_the_missing_slots_and_never_invents_one():
    asked: list[tuple[str, str, str]] = []

    def provider(text, source, target):
        asked.append((text, source, target))
        return {"ar": "أحمد", "hi": None, "ur": "احمد"}[target]

    filled = names.translate({"en": "Ahmed"}, translate_one=provider)
    assert filled == {"en": "Ahmed", "ar": "أحمد", "ur": "احمد"}
    # The language the applicant typed in is not asked for again: a round trip through a
    # translator can come back different from the name it started with.
    assert [call[2] for call in asked] == ["ar", "hi", "ur"]

    def refusing(text, source, target):
        raise RuntimeError("no provider today")

    assert names.translate({"en": "Ahmed"}, translate_one=refusing) == {"en": "Ahmed"}
    assert names.translate({"en": "Ahmed"}) == {"en": "Ahmed"}


def test_a_hindi_name_is_a_name():
    """The allowlist carries Devanagari because the form is read in Hindi."""
    assert textguard.identifier("अहमद अली", field="Name") == "अहमद अली"
    assert names.primary({"hi": "अहमद अली"}) == "अहमद अली"
    # The danda is punctuation, and punctuation is not in the identifier class - including
    # this one, which is why it is refused like the apostrophe rather than quietly dropped.
    with pytest.raises(ValueError):
        textguard.identifier("अहमद।", field="Name")


# ---------------------------------------------------------------------------
# the form
# ---------------------------------------------------------------------------
def test_a_worker_picks_their_moallem_when_they_register(client, intake):
    account = created_id(
        submit(client, full_name="Ahmed Ali", moallem_id=MOALLEM, name_ar="أحمد علي")
    )
    assert db_scalar("SELECT moallem_id FROM users WHERE id = ?", (account,)) == MOALLEM
    # The name map is what was *said*, per language; the canonical name is the first language
    # the applicant filled - the Arabic slot here, because a labelled answer beats the
    # unlabelled field every older client sends.
    assert json.loads(
        db_scalar("SELECT name_i18n FROM users WHERE id = ?", (account,))
    ) == {"ar": "أحمد علي"}
    assert db_scalar("SELECT name FROM users WHERE id = ?", (account,)) == "أحمد علي"

    event = json.loads(
        db_scalar(
            "SELECT after_json FROM audit_log WHERE action = 'user_self_registered' "
            "AND entity_id = ?",
            (account,),
        )
    )
    assert event["moallem_id"] == MOALLEM, event
    assert event["names"] == {"ar": "أحمد علي"}, event


def test_a_moallem_is_never_assigned_to_a_moallem(client, intake):
    """The field is ignored rather than refused: the role supervises, and a client that sends
    it anyway gets the account it asked for instead of an error about a control it cannot see."""
    account = created_id(submit(client, full_name="Lead Two", role="moallem", moallem_id=MOALLEM))
    assert db_scalar("SELECT role FROM users WHERE id = ?", (account,)) == "moallem"
    assert db_scalar("SELECT moallem_id FROM users WHERE id = ?", (account,)) is None


@pytest.mark.parametrize(
    "target, planted",
    [
        (WORKER, None),
        (OFF_OFFICE, None),
        ("4444", ("Dormant Moallem", "moallem", "inactive")),
        ("4445", ("Waiting Moallem", "moallem", "pending_approval")),
    ],
)
def test_an_assignment_that_cannot_be_resolved_is_refused(client, intake, target, planted):
    if planted:
        plant_user(target, planted[0], planted[1], status=planted[2])
    before = db_scalar("SELECT COUNT(*) FROM users")
    response = submit(client, full_name="Ahmed Ali", moallem_id=target)
    assert response.status_code == 400, response.text[:300]
    assert response.json()["detail"]["error_code"] == "moallem_not_available"
    # Nothing was created either: the refusal happens inside the write's transaction, before
    # the account exists - so a refused assignment cannot leave an account nobody asked for.
    assert db_scalar("SELECT COUNT(*) FROM users") == before


def test_a_submission_with_no_name_at_all_is_refused(client, intake):
    response = client.post(
        PUBLIC,
        data={"password": STRONG_PASSWORD, "role": "worker", "consent": "true"},
        files={"photo": ("photo.jpg", photo(), "image/jpeg")},
    )
    assert response.status_code == 400, response.text[:300]
    assert response.json()["detail"]["error_code"] == "name_required"


def test_the_only_unlabelled_name_field_still_works(client, intake):
    """Every client that predates the map sends ``full_name``, and it is still a name."""
    account = created_id(submit(client, full_name="Single Name"))
    assert db_scalar("SELECT name FROM users WHERE id = ?", (account,)) == "Single Name"
    assert db_scalar("SELECT name_i18n FROM users WHERE id = ?", (account,)) is None


# ---------------------------------------------------------------------------
# the list the dropdown is built from
# ---------------------------------------------------------------------------
def test_the_form_is_never_told_a_moallem_the_write_would_refuse(client, intake):
    plant_user("4446", "Dormant Moallem", "moallem", status="inactive")
    plant_user("4447", "Waiting Moallem", "moallem", status="pending_approval")
    plant_user(SECOND_MOALLEM, "Seed Second Lead", "moallem")
    listed = {row["id"] for row in moallem_list(client)}
    assert MOALLEM in listed and SECOND_MOALLEM in listed
    assert not listed & {"4446", "4447", WORKER, OFF_OFFICE}
    row = [entry for entry in moallem_list(client) if entry["id"] == MOALLEM][0]
    # The map travels, not a rendered string: the page re-renders the dropdown in whichever
    # language the reader switched to, without asking again.
    assert row["name"] and isinstance(row["names"], dict)


def test_the_staff_list_is_behind_the_link(client, app_module):
    assert client.get("/api/v1/register/not-a-token/moallems").status_code == 404
    generation = registrations.link_generation()
    stale = registrations.link_token(generation + 1)
    assert client.get(f"/api/v1/register/{stale}/moallems").status_code == 404


# ---------------------------------------------------------------------------
# the console
# ---------------------------------------------------------------------------
def test_the_roster_shows_the_assignment_and_both_names(client, intake):
    account = created_id(
        submit(client, full_name="Ahmed Ali", moallem_id=MOALLEM, name_ar="أحمد علي")
    )
    row = roster_row(client, account)
    assert row["moallem_id"] == MOALLEM
    assert row["moallem_name"] == "Seed Lead Worker"
    assert row["name_i18n"] == {"ar": "أحمد علي"}
    assert row["moallem_names"] == {}


def test_an_administrator_assigns_moves_and_unassigns_a_moallem(client, intake):
    account = created_id(submit(client, full_name="Ahmed Ali"))
    assert roster_row(client, account)["moallem_id"] is None

    assert edit(client, account, moallem_id=MOALLEM).status_code == 200
    assert roster_row(client, account)["moallem_name"] == "Seed Lead Worker"

    plant_user(SECOND_MOALLEM, "Seed Second Lead", "moallem")
    moved = edit(client, account, moallem_id=SECOND_MOALLEM)
    assert moved.status_code == 200, moved.text[:300]
    assert roster_row(client, account)["moallem_name"] == "Seed Second Lead"

    cleared = edit(client, account, moallem_id="")
    assert cleared.status_code == 200, cleared.text[:300]
    assert roster_row(client, account)["moallem_id"] is None
    # Both moves are in the trail, with the pair a reader compares.
    events = [
        json.loads(row[0])
        for row in db_rows(
            "SELECT after_json FROM audit_log WHERE action = 'user_edit' AND entity_id = ? "
            "ORDER BY id",
            (account,),
        )
    ]
    assert [event["moallem_id"] for event in events] == [MOALLEM, SECOND_MOALLEM, None], events


@pytest.mark.parametrize("target", [WORKER, OFF_OFFICE, "4444", "4445"])
def test_the_console_refuses_an_assignment_it_cannot_resolve(client, intake, target):
    if target.startswith("4444") or target.startswith("4445"):
        plant_user(target, "Faded Lead", "moallem", status="inactive")
    account = created_id(submit(client, full_name="Ahmed Ali"))
    response = edit(client, account, moallem_id=target)
    assert response.status_code == 400, response.text[:300]
    assert db_scalar("SELECT moallem_id FROM users WHERE id = ?", (account,)) is None


def test_a_moallem_account_is_not_given_a_moallem(client, app_module):
    response = edit(client, MOALLEM, moallem_id=SECOND_MOALLEM)
    assert response.status_code == 400, response.text[:300]
    assert "only a worker" in response.json()["detail"].lower()


def test_an_account_cannot_be_its_own_moallem(app_module):
    """The one loop the role rule does not close, held directly: the API cannot reach it."""
    with database.db() as conn:
        with pytest.raises(HTTPException) as refusal:
            main._moallem_assignment(conn, user_id=MOALLEM, role="worker", requested=MOALLEM)
    assert refusal.value.status_code == 400
    assert "its own moallem" in str(refusal.value.detail)


def test_the_console_edits_a_name_language_by_language(client, intake):
    account = created_id(submit(client, full_name="Ahmed Ali"))
    named = edit(client, account, name="Ahmed Ali", name_en="Ahmed Ali", name_ar="أحمد علي")
    assert named.status_code == 200, named.text[:300]
    assert json.loads(
        db_scalar("SELECT name_i18n FROM users WHERE id = ?", (account,))
    ) == {"en": "Ahmed Ali", "ar": "أحمد علي"}

    # Clearing the English slot keeps the Arabic name and moves the canonical value to it:
    # one display name, and it is one the account still has.
    cleared = edit(client, account, name="أحمد علي", name_en="")
    assert cleared.status_code == 200, cleared.text[:300]
    assert json.loads(
        db_scalar("SELECT name_i18n FROM users WHERE id = ?", (account,))
    ) == {"ar": "أحمد علي"}
    assert db_scalar("SELECT name FROM users WHERE id = ?", (account,)) == "أحمد علي"

    # Clearing the *last* language with no canonical name beside it is refused: a nameless
    # account is a number on a timesheet with nobody behind it.
    last = edit(client, account, name="", name_ar="")
    assert last.status_code == 400, last.text[:300]
    assert json.loads(
        db_scalar("SELECT name_i18n FROM users WHERE id = ?", (account,))
    ) == {"ar": "أحمد علي"}

    # ...while clearing it *with* a name that stands on its own is the account going back to
    # the unlabelled state every older client writes: a name, and no map.
    unlabelled = edit(client, account, name="Someone Else", name_ar="")
    assert unlabelled.status_code == 200, unlabelled.text[:300]
    assert db_scalar("SELECT name_i18n FROM users WHERE id = ?", (account,)) is None
    assert db_scalar("SELECT name FROM users WHERE id = ?", (account,)) == "Someone Else"


def test_deleting_a_moallem_releases_their_crew_and_names_them(client, app_module):
    """The column declares ``ON DELETE SET NULL``; SQLite does not enforce it here, so this is
    the write that has to be right or a whole crew reports to an id that no longer exists."""
    created = client.post(
        "/api/v1/admin/users/add",
        headers=bearer(ADMIN),
        json={
            "user_id": SECOND_MOALLEM,
            "name": "Seed Second Lead",
            "password": STRONG_PASSWORD,
            "role": "moallem",
        },
    )
    assert created.status_code == 200, created.text[:300]
    assert edit(client, WORKER, name="Seed Worker", moallem_id=SECOND_MOALLEM).status_code == 200
    assert db_scalar("SELECT moallem_id FROM users WHERE id = ?", (WORKER,)) == SECOND_MOALLEM

    deleted = client.post(
        "/api/v1/admin/users/delete",
        headers=bearer(ADMIN),
        json={"user_id": SECOND_MOALLEM},
    )
    assert deleted.status_code == 200, deleted.text[:300]
    assert db_scalar("SELECT moallem_id FROM users WHERE id = ?", (WORKER,)) is None
    event = json.loads(
        db_scalar(
            "SELECT after_json FROM audit_log WHERE action = 'user_delete' AND entity_id = ?",
            (SECOND_MOALLEM,),
        )
    )
    assert event["workers_released"] == [WORKER], event


# ---------------------------------------------------------------------------
# the two views
# ---------------------------------------------------------------------------
def test_the_live_board_shows_which_moallem_the_shift_belongs_to(client, app_module):
    """The fixture leaves the seeded worker on shift, so the board is read as it ships."""
    assert edit(client, WORKER, name="Seed Worker", moallem_id=MOALLEM).status_code == 200
    board = client.get("/api/v1/admin/active_sessions", headers=bearer(ADMIN))
    assert board.status_code == 200, board.text[:300]
    row = [entry for entry in board.json() if entry["worker_id"] == WORKER]
    assert row, "the seeded worker's open shift is not on the board"
    assert row[0]["moallem_id"] == MOALLEM
    assert row[0]["moallem_name"] == "Seed Lead Worker"
    assert row[0]["name_i18n"] == {}
    # ...and an account with no moallem is a row with no moallem: null, not a missing row.
    others = [entry for entry in board.json() if entry["worker_id"] != WORKER]
    assert all("moallem_id" in entry for entry in others)


# ---------------------------------------------------------------------------
# the three places the four languages are written down
# ---------------------------------------------------------------------------
def test_the_page_the_console_and_the_server_name_the_same_languages():
    """One list of four, read off three files: a name is stored under one of these codes.

    The applicant's page posts ``name_<code>``, the console edits four boxes keyed by the same
    codes, and the server stores the map - so a language added to one of them and not the others
    is a name that arrives nowhere, or a box an administrator fills in and saves into a key
    ``names.parse`` drops. Written down once, in the module both sides are held to.
    """
    capture = (FRONTEND / "capture.js").read_text(encoding="utf-8")
    page = re.findall(r'code: "([a-z]{2})"', capture)
    console = (FRONTEND / "admin_modules.js").read_text(encoding="utf-8")
    block = re.search(r"NAME_LANGUAGES: \[([^\]]*)\]", console)
    assert block, "the console's four name slots have been renamed or moved out of reach"
    boxes = re.findall(r"'([a-z]{2})'", block.group(1))
    assert page == list(names.LANGUAGES), (
        f"the registration page offers {page} and the server stores {list(names.LANGUAGES)}"
    )
    assert boxes == list(names.LANGUAGES), (
        f"the console edits {boxes} and the server stores {list(names.LANGUAGES)}"
    )


def test_the_timesheet_carries_the_moallem_of_the_shift(client, app_module, intake):
    worker = free_worker_id()
    plant_user(worker, "Shift Worker", "worker")
    seed_reference(worker)
    assert edit(client, worker, name="Shift Worker", moallem_id=MOALLEM).status_code == 200
    assert clock_in(client, worker, headers=bearer(worker)).status_code == 200
    closed = clock_in(client, worker, action="Clock Out", headers=bearer(worker), confirmed=True)
    assert closed.status_code == 200, closed.text[:300]

    report = client.get("/api/v1/admin/reports/shifts", headers=bearer(ADMIN))
    assert report.status_code == 200, report.text[:300]
    body = report.json()
    assert "moallem_name" in body["fields"] and "worker_names" in body["fields"]
    rows = [row for row in body["rows"] if row["worker_id"] == worker]
    assert rows, "the shift that was just worked is not on the timesheet"
    assert rows[0]["moallem_id"] == MOALLEM
    assert rows[0]["moallem_name"] == "Seed Lead Worker"
    assert rows[0]["worker_name"] == "Shift Worker"
    # A worker nobody supervises is a row with no moallem - an answer, not a dropped row.
    unassigned = [row for row in body["rows"] if row["worker_id"] == WORKER]
    if unassigned:
        assert unassigned[0]["moallem_id"] is None
