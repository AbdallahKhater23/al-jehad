"""The root tier: what it reaches, what is concealed from it, and what cannot mint it.

The role itself is four lines in ``security`` - a constant, a band, a wildcard in
``require_role`` and a guard. Everything in this file is about the parts that are *not* one
line, because those are the parts that fail silently:

* **the wildcard is one-directional.** A developer session satisfies every administrator guard,
  which is the whole point of a superset. The reverse must not hold: an administrator refused by
  ``require_developer`` is the separation, and a suite that only proved the first direction
  would pass on the day somebody added ``DEVELOPER_ROLE`` to ``admin_only``.
* **every concealment is tested twice - hidden from the administrator, visible to the root.**
  A filter that hides the account from everybody is a bug that looks exactly like a working
  filter, and it locks the operator out of the one account they must be able to edit.
* **concealment is in the query, not after it.** A list narrowed in Python still returns the
  *count* of what it removed, so the tests here compare the whole roster rather than looking
  for an id.
* **the escalation doors are closed by name.** The console (both content types) and a
  registration link are the three paths that can write a ``users`` row with a caller-chosen
  role, and a root id is in-band by construction - so the only thing standing between an
  administrator and root is an explicit refusal, which is what these assert.

    pytest backend/tests/test_developer_role.py -q
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest
from database import db
from fastapi import HTTPException
from harness import ADMIN, HEAD_ADMIN, MOALLEM, WORKER, bearer, current_db_path

import developer
import security

TS = "%Y-%m-%d %H:%M:%S"
DEV_ID = developer.DEVELOPER_ID_DEFAULT  # 309010401073 - the 64-bit id the role was built for
DEV_PASSWORD = "root-credential-for-the-test-0001"

#: Every route the root tier owns. Used as one list so a route added to the module without a
#: test in this file still gets covered by the refusal assertions - which is the direction that
#: matters: a new developer route that a head administrator can reach is the failure mode.
DEVELOPER_ROUTES = (
    "/api/v1/developer/runtime",
    "/api/v1/developer/alerts",
    "/api/v1/developer/diagnostics/pool",
    "/api/v1/developer/diagnostics/slow-queries",
    "/api/v1/developer/audit",
)


def _bearer_as_developer() -> dict[str, str]:
    """An Authorization header for the root account, minted the way a real session is.

    ``harness.bearer`` reads the row's ``token_version``, so a token produced here stops
    verifying the moment the credential is rotated - the same property the running app has.
    """
    return bearer(DEV_ID, role=security.DEVELOPER_ROLE)


def _seed() -> dict:
    return developer.seed_developer_account(password=DEV_PASSWORD, actor="test:seed_developer.py")


def _rows(sql: str, params: tuple = ()) -> list[dict]:
    conn = sqlite3.connect(str(current_db_path()))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _user(user_id: str) -> dict | None:
    rows = _rows("SELECT * FROM users WHERE id = ?", (user_id,))
    return rows[0] if rows else None


def _plant_audit_row(*, actor_id: str, entity_id: str | None, action: str = "user_edit") -> int:
    """An audit row written directly, because a *previous* deployment's rows are the interesting
    ones: they exist before the role did, and the concealment has to work on those too.
    """
    with db(write=True) as conn:
        cursor = conn.execute(
            "INSERT INTO audit_log (action, actor_id, actor_role, entity, entity_id, "
            "before_json, after_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                action,
                actor_id,
                "head_admin",
                "users",
                entity_id,
                "{}",
                "{}",
                datetime.now().strftime(TS),
            ),
        )
        return int(cursor.lastrowid)


# ---------------------------------------------------------------------------
# the wildcard, in both directions
# ---------------------------------------------------------------------------
def test_a_developer_session_reaches_the_administrator_surfaces(client, app_module):
    _seed()
    response = client.get("/api/v1/admin/users", headers=_bearer_as_developer())
    assert response.status_code == 200, response.text
    # ...and the wildcard is a *superset of the audience*, not a replacement for it: the same
    # route still answers an ordinary head administrator. A wildcard that broke the ordinary
    # path would be an outage wearing a security fix.
    assert client.get("/api/v1/admin/users", headers=bearer(HEAD_ADMIN)).status_code == 200


def test_no_business_role_can_reach_the_developer_surface(client, app_module):
    _seed()
    for user_id in (ADMIN, HEAD_ADMIN, MOALLEM, WORKER):
        for path in DEVELOPER_ROUTES:
            refused = client.get(path, headers=bearer(user_id))
            assert refused.status_code == 403, f"{user_id} reached {path}: {refused.status_code}"
    # Unauthenticated is 401 rather than 403 - "who are you" and "not you" are different
    # answers, and a probe that conflates them cannot tell a misconfiguration from a stranger.
    for path in DEVELOPER_ROUTES:
        assert client.get(path).status_code == 401, path


def test_the_developer_surface_is_closed_to_every_door_the_console_uses(client, app_module):
    """The refusals above are on the GETs; the mutations are where a real escalation would land."""
    _seed()
    head = bearer(HEAD_ADMIN)
    for path in (
        "/api/v1/developer/diagnostics/caches/flush",
        "/api/v1/developer/alerts/1/read",
        "/api/v1/developer/diagnostics/query-plan/attendance_by_site",
    ):
        assert client.post(path, headers=head).status_code in (403, 405), path
    patch = client.patch(
        "/api/v1/developer/runtime/maintenance_mode", json={"value": True}, headers=head
    )
    assert patch.status_code == 403, patch.text


# ---------------------------------------------------------------------------
# concealment: hidden from the administrators, visible to the root
# ---------------------------------------------------------------------------
def test_the_roster_hides_the_root_account_from_every_administrator(client, app_module):
    _seed()
    for actor in (ADMIN, HEAD_ADMIN):
        listed = client.get("/api/v1/admin/users", headers=bearer(actor))
        assert listed.status_code == 200, listed.text
        ids = [str(row["id"]) for row in listed.json()]
        assert DEV_ID not in ids, f"{actor} can see the root account on the roster"
        # Not a coincidence of ordering or paging: the account is absent from a roster that is
        # otherwise complete, because the filter runs in SQL rather than over the finished list.
        assert len(ids) == len(_rows("SELECT id FROM users WHERE role <> ?", (security.DEVELOPER_ROLE,)))

    # ...and the root tier sees itself, which is the only way the one account that cannot be
    # created through the API can be edited at all.
    mine = client.get("/api/v1/admin/users", headers=_bearer_as_developer())
    assert DEV_ID in [str(row["id"]) for row in mine.json()]


def test_reading_the_root_account_by_id_answers_404_not_403(client, app_module):
    _seed()
    for actor in (ADMIN, HEAD_ADMIN):
        response = client.get(f"/api/v1/admin/users/{DEV_ID}", headers=bearer(actor))
        # 404, in the same words as an id that was never issued. A 403 would confirm the
        # account exists, which is exactly what concealment is for - and it would also make
        # the id *testable*, turning a hidden account into an enumerable one.
        assert response.status_code == 404, response.text
        assert "not found" in response.json()["detail"].lower()
    assert client.get(f"/api/v1/admin/users/{DEV_ID}", headers=_bearer_as_developer()).status_code == 200


def test_the_audit_trail_hides_the_root_account_as_actor_and_as_subject(client, app_module):
    _seed()
    as_actor = _plant_audit_row(actor_id=DEV_ID, entity_id="1")
    as_subject = _plant_audit_row(actor_id=HEAD_ADMIN, entity_id=DEV_ID)
    # The trap this row exists for: ``entity_id NOT IN (...)`` is NULL-tainted, so a concealment
    # written without ``COALESCE`` silently drops every row that has no subject - which is most
    # of the table - and the trail looks intact while being mostly gone.
    no_subject = _plant_audit_row(actor_id=HEAD_ADMIN, entity_id=None)
    ordinary = _plant_audit_row(actor_id=ADMIN, entity_id="7")

    seen = {row["id"] for row in client.get("/api/v1/admin/audit_log", headers=bearer(HEAD_ADMIN)).json()}
    assert as_actor not in seen, "an administrator can read what the root account did"
    assert as_subject not in seen, "an administrator can read a row about the root account"
    assert no_subject in seen, "the concealment filtered out rows with no subject"
    assert ordinary in seen

    root_sees = {row["id"] for row in client.get("/api/v1/admin/audit_log", headers=_bearer_as_developer()).json()}
    assert {as_actor, as_subject, no_subject, ordinary} <= root_sees


def test_the_private_hub_is_a_table_no_administrator_facing_query_names(client, app_module):
    """Concealment by construction: the alert hub is its own table, not a filtered view."""
    _seed()  # raises a ``developer_account_seeded`` alert on the way through
    hub = client.get("/api/v1/developer/alerts", headers=_bearer_as_developer())
    assert hub.status_code == 200, hub.text
    kinds = [row["kind"] for row in hub.json()["alerts"]]
    assert "developer_account_seeded" in kinds, kinds
    # The root tier may read the administrator's inbox - it is a superset - but the two stores
    # must not bleed into each other, or an infrastructure alert becomes visible to the audience
    # it was hidden from.
    assert not _rows("SELECT id FROM developer_alerts WHERE kind = 'startup_override'")
    admin_inbox = client.get("/api/v1/admin/notifications", headers=bearer(HEAD_ADMIN)).json()
    titles = str(admin_inbox)
    assert DEV_ID not in titles, "the administrator's inbox carries the root account's id"


# ---------------------------------------------------------------------------
# no API may mint the root account
# ---------------------------------------------------------------------------
def test_every_creation_path_refuses_the_root_role_by_name(client, app_module):
    _seed()
    head = bearer(HEAD_ADMIN)
    root_id = developer.DEVELOPER_ID_DEFAULT
    for actor in (ADMIN, HEAD_ADMIN):
        json_body = {
            "user_id": root_id,
            "name": "Not The Developer",
            "role": "developer",
            "password": "an-administrator-chose-this-1",
        }
        refused = client.post("/api/v1/admin/users/add", json=json_body, headers=bearer(actor))
        # 403 and not 400: "you may not" is a different answer from "that id is malformed", and
        # only one of them is true here - the id is in-band for the role by construction.
        assert refused.status_code == 403, f"{actor}: {refused.status_code} {refused.text}"
        assert "seed tool" in refused.json()["detail"]

        multipart = client.post(
            "/api/v1/admin/users/create",
            data={
                "user_id": root_id,
                "name": "Not The Developer",
                "role": "developer",
                "password": "an-administrator-chose-this-1",
            },
            headers=head,
        )
        assert multipart.status_code == 403, multipart.text

    # Nothing was written by either attempt, and the seeded row is the only one in the band.
    assert len(_rows("SELECT id FROM users WHERE CAST(id AS INTEGER) >= ?", (security.DEVELOPER_ID_FLOOR,))) == 1


def test_a_registration_link_cannot_carry_the_root_role(client, app_module):
    _seed()
    refused = client.post(
        "/api/v1/admin/enrollment/invites",
        json={"user_id": developer.DEVELOPER_ID_DEFAULT, "role": "developer", "name": "x"},
        headers=bearer(HEAD_ADMIN),
    )
    assert refused.status_code in (403, 409, 422), refused.text
    assert not _rows("SELECT id FROM enrollment_invites WHERE pending_role = 'developer'")


# ---------------------------------------------------------------------------
# the seed
# ---------------------------------------------------------------------------
def test_the_seed_is_idempotent_and_never_restores_an_operators_password():
    first = _seed()
    assert first["created"] is True and first["rotated"] is True
    stored = _user(DEV_ID)
    assert stored["role"] == security.DEVELOPER_ROLE
    assert security.verify_password(DEV_PASSWORD, stored["password_hash"])

    # Re-running with a different credential must not install it: a seeder that rewrote a live
    # password on every deploy is a permanent backdoor on a schedule, and the value it writes
    # lives in a pipeline's environment forever.
    again = developer.seed_developer_account(password="a-different-password-entirely-2")
    assert again["created"] is False and again["rotated"] is False
    after = _user(DEV_ID)
    assert after["password_hash"] == stored["password_hash"]
    assert not security.verify_password("a-different-password-entirely-2", after["password_hash"])
    assert int(after["token_version"]) == int(stored["token_version"])


def test_rotating_the_credential_revokes_the_sessions_that_were_issued_before_it(client, app_module):
    _seed()
    before = int(_user(DEV_ID)["token_version"])
    # Minted *before* the rotation and held across it: a token minted afterwards would carry
    # the new version and prove nothing but that the helper reads the database.
    stale = _bearer_as_developer()
    assert client.get("/api/v1/developer/runtime", headers=stale).status_code == 200

    rotated = developer.seed_developer_account(
        password="rotated-root-credential-0003", rotate_password=True
    )
    assert rotated["created"] is False and rotated["rotated"] is True
    now = _user(DEV_ID)
    assert security.verify_password("rotated-root-credential-0003", now["password_hash"])
    assert int(now["token_version"]) == before + 1
    # The token issued under the old credential is dead, which is the point of bumping the
    # version rather than replacing the hash quietly: a rotation that left live sessions
    # running would not be a rotation.
    assert client.get("/api/v1/developer/runtime", headers=stale).status_code in (401, 403)
    assert client.get("/api/v1/developer/runtime", headers=_bearer_as_developer()).status_code == 200


def test_the_seed_refuses_what_it_must_refuse():
    # No default credential, ever: this call and the empty-string case are the two ways a
    # seeder is talked into a known password.
    with pytest.raises(ValueError):
        developer.seed_developer_account(password="")
    # ``security`` owns the band, so an id outside the root band is refused with the same
    # answer every other account-creation path gives - not a bespoke seeder error a caller
    # would have to know about.
    with pytest.raises(HTTPException) as band:
        developer.seed_developer_account(password=DEV_PASSWORD, user_id=ADMIN)
    assert band.value.status_code == 400
    with pytest.raises(HTTPException) as wrong_role:
        security.validate_user_id_for_role(DEV_ID, "admin")
    assert wrong_role.value.status_code == 400

    # An id in the root band that already belongs to an ordinary account is *not* promoted:
    # silently resolving that would turn a seeded id collision into an escalation.
    with db(write=True) as conn:
        conn.execute(
            "INSERT INTO users (id, name, email, phone, password_hash, role) "
            "VALUES (?, 'Somebody Else', '', '', ?, 'admin')",
            (str(int(developer.DEVELOPER_ID_DEFAULT) + 1), security.hash_password("some-other-credential-4")),
        )
    with pytest.raises(ValueError):
        developer.seed_developer_account(
            password=DEV_PASSWORD, user_id=str(int(developer.DEVELOPER_ID_DEFAULT) + 1)
        )
    assert _user(str(int(developer.DEVELOPER_ID_DEFAULT) + 1))["role"] == "admin"


def test_the_stored_row_holds_a_hash_and_no_plaintext_anywhere():
    _seed()
    stored = _user(DEV_ID)
    assert stored["password_hash"].startswith(("$2a$", "$2b$", "$2y$")), stored["password_hash"][:8]
    # Every column, not just the one: a seeder that "helpfully" records the generated value
    # somewhere else in the row is the same leak with a different column name.
    assert DEV_PASSWORD not in " ".join(str(value) for value in stored.values())


# ---------------------------------------------------------------------------
# runtime configuration, from the surface that owns it
# ---------------------------------------------------------------------------
def test_runtime_configuration_is_set_from_the_root_tier_and_refuses_unknown_keys(client, app_module):
    _seed()
    root = _bearer_as_developer()
    before = client.get("/api/v1/developer/runtime", headers=root).json()

    changed = client.patch(
        "/api/v1/developer/runtime/maintenance_mode",
        json={"value": True, "note": "database reindex"},
        headers=root,
    )
    assert changed.status_code == 200, changed.text

    after = client.get("/api/v1/developer/runtime", headers=root).json()
    assert after["values"]["maintenance_mode"] is True
    # The version is what makes the setting *distributed*: every worker compares it before
    # trusting its own cached read, so this is the assertion that the change is visible to
    # processes that are not this one.
    assert after["version"] > before["version"]

    unknown = client.patch(
        "/api/v1/developer/runtime/not_a_real_key", json={"value": 1}, headers=root
    )
    assert unknown.status_code == 400, unknown.text
    # A flag nothing reads is refused rather than stored, so an operator cannot believe they
    # changed something they did not.
    assert "not_a_real_key" not in client.get("/api/v1/developer/runtime", headers=root).json()["values"]


def test_every_response_carries_the_trace_id_an_alert_row_is_joined_by(client, app_module):
    _seed()
    response = client.get("/api/v1/developer/diagnostics/pool", headers=_bearer_as_developer())
    assert response.status_code == 200, response.text
    assert response.headers.get("X-Trace-Id"), "a failure cannot be joined to its log line"

    # The same id is what a slow statement and an alert are stamped with, so a diagnostics read
    # and the infrastructure event it explains are one investigation rather than two.
    alert = _rows("SELECT trace_id, detail_json FROM developer_alerts WHERE kind = 'developer_account_seeded'")
    assert alert and alert[0]["detail_json"], alert
    assert DEV_PASSWORD not in alert[0]["detail_json"]
