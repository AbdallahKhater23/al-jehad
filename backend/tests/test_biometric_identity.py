"""Where a person's face is stored, and under which name.

WHY THIS EXISTS
---------------
A person's biometric record is two files - the template every punch is scored against, and
the reference selfie beside it - and they used to be named after the account:
``local_references/1.json``. Two things follow from a filename that *is* an account id, and
neither is about tidiness:

1. **A face is reachable from a number anybody can iterate.** ``1.json``, ``2.json``,
   ``3.json`` name people. A directory listing, a stray backup, a misconfigured static
   mount or a path traversal then maps a face to a worker id - and the id is the login half
   of the credential, so one number joins a face to an account.
2. **A template can be inherited by the next worker issued that id.** Account ids are
   recycled. A delete that could not remove a file says so (``face_removal_failed``) and
   leaves the face on disk, and the worker who takes the id next would have their punch
   matched against the *previous* holder's face.

So a template is filed under an immutable random id minted per account (``biometrics``).
These tests pin the properties that matter, not the implementation:

* a new enrollment writes the id-named files and nothing named after the account id;
* the id never changes - re-enrollment replaces the files, not the identity;
* an account whose files are still under the old name is still *recognised* (the fallback),
  so the roster does not call it unenrolled - and is never *scored*, because the name is the
  one fact that cannot say whether the file is the worker's current face or the copy a later
  enrolment replaced; the adoption rename files it properly and is idempotent, and the
  enrolment that supersedes it retires it;
* a new account can never resolve to the face of the id it took;
* deleting an account removes the face under *both* names;
* enrollment leaves no temporary copy of anybody's face anywhere;
* a punch selfie is not named after the punch it belongs to;
* and the test harness itself cannot write a face outside its throwaway directory.

The database is restored from a snapshot before every test but the directories of files are
not, so each test uses ids no other test enrolls and cleans up the files it plants.
"""

from __future__ import annotations

import io
import json
import os
import re
import sqlite3
import zipfile
from pathlib import Path

import pytest

import biometrics
import harness
import quick_links
from harness import ADMIN, HEAD_ADMIN, INSIDE_DOWNTOWN, MOALLEM, WORKER, bearer, db_rows, db_scalar, jpeg_bytes

STRONG_PASSWORD = "site-attendance-2026"

#: Ids in blocks of their own, never reused between the tests below: the reference
#: directory outlives the database reset, so "this account has no template" only means
#: something for an id no other test enrols.
NEW_WORKER = "411"
LEGACY_WORKER = "412"
REUSED_WORKER = "413"
DELETE_WORKER = "414"
READY_WORKER = "415"
#: The leftover that could become the template again, and the accounts that pin what
#: happens to one (see the retirement tests).
RETIRE_WORKER = "418"
CLEAN_WORKER = "419"
COLLIDE_WORKER = "420"
LOCKED_WORKER = "421"
ADOPT_WORKER = "422"
KEPT_WORKER = "423"
KEEP_WORKER = "424"
MISFILED_WORKER = "425"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def create_account(client, user_id: str, *, image=None, role: str = "worker", as_role: str = ADMIN):
    """``/admin/users/create`` with or without a photo (which enrolls the account)."""
    files = None
    if image is not None:
        files = {"photo": ("photo.jpg", image, "image/jpeg")}
    return client.post(
        "/api/v1/admin/users/create",
        headers=bearer(as_role),
        data={
            "user_id": user_id,
            "name": f"Worker {user_id}",
            "role": role,
            "password": STRONG_PASSWORD,
        },
        files=files,
    )


def biometric_id(user_id: str) -> str:
    return str(db_scalar("SELECT biometric_id FROM users WHERE id = ?", (user_id,)))


def sql(statement: str, params: tuple = ()) -> None:
    connection = sqlite3.connect(harness.DB_PATH)
    try:
        connection.execute(statement, params)
        connection.commit()
    finally:
        connection.close()


def names(directory) -> set[str]:
    return set(os.listdir(directory))


def quarantine_named(directory, user_id: str) -> list[str]:
    """The files the old name for ``user_id`` was retired into (see ``biometrics``)."""
    return sorted(
        name
        for name in names(directory)
        if name.startswith(f"{user_id}.") and biometrics.QUARANTINE_SUFFIX in name
    )


def discard(user_id: str) -> None:
    """Remove every file this suite could have written for ``user_id``, retired ones included.

    The reference directories outlive the database reset, so anything a test plants or
    enrolls is cleaned up: otherwise a later test sees a face it never created, and the
    readiness check that counts leftovers would be measuring this suite instead of the
    application.
    """
    prefixes = (f"{user_id}.", f"{biometric_id(user_id)}.")
    for folder in (harness.REFS_DIR, harness.PHOTOS_DIR):
        for name in names(folder):
            if name.startswith(prefixes):
                (folder / name).unlink(missing_ok=True)


def roster_entry(client, user_id: str) -> dict:
    rows = client.get("/api/v1/admin/users", headers=bearer(ADMIN)).json()
    return next(row for row in rows if row["id"] == user_id)


# ---------------------------------------------------------------------------
# the name
# ---------------------------------------------------------------------------
def test_a_template_is_filed_under_an_immutable_id_not_the_account_id(client):
    """The whole point: the filename stops naming the person."""
    created = create_account(client, NEW_WORKER, image=jpeg_bytes())
    assert created.status_code == 200, created.text[:300]

    resolved = biometric_id(NEW_WORKER)
    assert resolved and resolved != NEW_WORKER, "the account id is not the id of its files"
    assert len(resolved) == 32 and all(character in "0123456789abcdef" for character in resolved), (
        f"an id has to be unguessable, got {resolved!r}"
    )

    template = harness.reference_path(NEW_WORKER)
    assert template.exists(), f"no template at {template.name}"
    assert template.name == f"{resolved}.json"
    assert json.loads(template.read_text())

    photo = harness.stored_photo_path(NEW_WORKER)
    assert photo is not None, "the console keeps the reference selfie beside the row"
    assert photo.name == f"{resolved}.jpg"

    for folder, suffix in ((harness.REFS_DIR, ".json"), (harness.PHOTOS_DIR, ".jpg")):
        assert not (folder / f"{NEW_WORKER}{suffix}").exists(), (
            "nothing biometric may be named after the account id"
        )

    assert roster_entry(client, NEW_WORKER)["face_enrolled"] is True
    punch = harness.clock_in(client, NEW_WORKER, headers=bearer(NEW_WORKER))
    assert punch.status_code == 200, punch.text[:300]


def test_the_id_never_changes_when_a_person_is_re_enrolled(client):
    """Re-enrolling replaces the files the id names; it does not move the account."""
    assert create_account(client, NEW_WORKER, image=jpeg_bytes()).status_code == 200
    first = biometric_id(NEW_WORKER)

    again = harness.enroll(client, NEW_WORKER, headers=bearer(HEAD_ADMIN))
    assert again.status_code == 200, again.text[:300]

    assert biometric_id(NEW_WORKER) == first, "an identity that moves is not immutable"
    assert harness.reference_path(NEW_WORKER).name == f"{first}.json"
    assert db_scalar("SELECT template_version FROM users WHERE id = ?", (NEW_WORKER,)) >= 2


# ---------------------------------------------------------------------------
# the leftover that could become the template again
# ---------------------------------------------------------------------------
# While an id-named file exists, whatever else is on disk does not matter: ``resolve_reference``
# prefers it. The state worth designing against is the day it does not exist - a references
# directory restored from an older backup, a hand cleanup, a delete that could not finish - and
# then a legacy-named file left lying there becomes the template again, silently, and the worker
# is scored against a face they replaced months ago. Shadowing is not safety; the leftover has to
# be retired by the enrollment that supersedes it.
def test_re_enrolling_retires_the_old_name_so_the_old_face_cannot_come_back(client):
    assert create_account(client, RETIRE_WORKER, image=jpeg_bytes()).status_code == 200
    legacy_template = harness.REFS_DIR / f"{RETIRE_WORKER}.json"
    legacy_template.write_text(harness.reference_text())
    legacy_photo = harness.PHOTOS_DIR / f"{RETIRE_WORKER}.jpg"
    legacy_photo.write_bytes(jpeg_bytes())
    template = harness.reference_path(RETIRE_WORKER)

    try:
        again = harness.enroll(client, RETIRE_WORKER, headers=bearer(HEAD_ADMIN))
        assert again.status_code == 200, again.text[:300]

        assert not legacy_template.exists() and not legacy_photo.exists(), (
            "a face this account has replaced must not still answer to the old name"
        )
        assert quarantine_named(harness.REFS_DIR, RETIRE_WORKER), "retired, not deleted"
        assert quarantine_named(harness.PHOTOS_DIR, RETIRE_WORKER)
        assert template.exists() and json.loads(template.read_text())["embedding"]
        punch = harness.clock_in(client, RETIRE_WORKER, headers=bearer(RETIRE_WORKER))
        assert punch.status_code == 200, punch.text[:300]

        # The state the retirement exists for. Before it, this punch was scored against the
        # template the worker was re-enrolled away from, with nothing to indicate it.
        template.unlink()
        assert harness.template_exists(RETIRE_WORKER) is False, (
            "the retired file is still reachable by the fallback"
        )
        gone = harness.clock_in(client, RETIRE_WORKER, headers=bearer(RETIRE_WORKER))
        assert gone.status_code == 404, "there is no face left to fall back to"
    finally:
        discard(RETIRE_WORKER)


def test_a_re_enrollment_with_nothing_left_over_retires_nothing(client):
    """The other half of the statement: an account enrolled under the id scheme as usual
    must not have files moved out from under it, or every re-enrollment would look like a
    cleanup."""
    assert create_account(client, CLEAN_WORKER, image=jpeg_bytes()).status_code == 200
    before = (names(harness.REFS_DIR), names(harness.PHOTOS_DIR))

    try:
        assert harness.enroll(client, CLEAN_WORKER, headers=bearer(HEAD_ADMIN)).status_code == 200
        assert names(harness.REFS_DIR) == before[0], "nothing was added or removed"
        assert names(harness.PHOTOS_DIR) == before[1]
        assert quarantine_named(harness.REFS_DIR, CLEAN_WORKER) == []
        assert quarantine_named(harness.PHOTOS_DIR, CLEAN_WORKER) == []
    finally:
        discard(CLEAN_WORKER)


def test_a_biometric_id_that_is_not_an_id_is_replaced_rather_than_trusted(client):
    """``<account id>.json`` is *both* names when the id column holds the account id.

    Nothing this build writes looks like that - an id is 32 hex characters, and migration 11
    mints them with the same function - but a hand-set value can be exactly it, and it has to be
    repaired rather than accommodated: the *name* of the account's own file is derived from the
    column, so a value that is not an id files the next enrollment outside the only naming
    scheme the gate scores, and the refusal would have no way out.
    """
    assert create_account(client, COLLIDE_WORKER).status_code == 200
    sql("UPDATE users SET biometric_id = ? WHERE id = ?", (COLLIDE_WORKER, COLLIDE_WORKER))
    old_template = harness.REFS_DIR / f"{COLLIDE_WORKER}.json"
    old_template.write_text(harness.reference_text())

    try:
        # Repairing the row is what this is for: the value is not an id, and the *name* of the
        # account's own file is derived from the value - so trusting it files the next
        # enrollment in the same refused place, and the refusal would have no way out.
        repaired = biometrics.id_for(COLLIDE_WORKER)
        assert repaired != COLLIDE_WORKER, "a number is not an id, however it got into the column"
        assert biometrics.is_id_named(repaired) is True
        assert db_scalar("SELECT biometric_id FROM users WHERE id = ?", (COLLIDE_WORKER,)) == repaired

        # The file the old value named is a leftover now, and a leftover is never scored...
        assert harness.clock_in(client, COLLIDE_WORKER, headers=bearer(COLLIDE_WORKER)).status_code == 400

        # ...and the enrollment is the way out, which retires it and files the face properly.
        assert harness.enroll(client, COLLIDE_WORKER, headers=bearer(HEAD_ADMIN)).status_code == 200
        assert not old_template.exists()
        assert quarantine_named(harness.REFS_DIR, COLLIDE_WORKER), "retired, not deleted"
        punch = harness.clock_in(client, COLLIDE_WORKER, headers=bearer(COLLIDE_WORKER))
        assert punch.status_code == 200, punch.text[:300]
    finally:
        discard(COLLIDE_WORKER)
        old_template.unlink(missing_ok=True)
        for name in quarantine_named(harness.REFS_DIR, COLLIDE_WORKER):
            (harness.REFS_DIR / name).unlink(missing_ok=True)


def test_the_retirement_never_moves_a_file_it_was_told_to_keep(client):
    """``quarantine_legacy`` takes what it must not touch, and honours it.

    Reached directly because the collision it guards is one the application no longer creates:
    a ``biometric_id`` that is not an id is replaced (see ``ensure_id``), so ``<id>.json`` and
    ``<account id>.json`` cannot be the same file any more. The guard stays because it protects
    a *write* from being undone by its own cleanup, which is not a thing to be clever about.
    """
    assert create_account(client, KEEP_WORKER).status_code == 200
    legacy = harness.REFS_DIR / f"{KEEP_WORKER}.json"
    legacy.write_text(harness.reference_text())

    try:
        assert biometrics.quarantine_legacy(KEEP_WORKER, keep=(str(legacy),)) == []
        assert legacy.exists(), "the path it was handed is the one it must not move"
        assert quarantine_named(harness.REFS_DIR, KEEP_WORKER) == []

        moved = biometrics.quarantine_legacy(KEEP_WORKER)
        assert len(moved) == 1 and not legacy.exists(), "without the guard it is retired as usual"
    finally:
        discard(KEEP_WORKER)
        for name in quarantine_named(harness.REFS_DIR, KEEP_WORKER):
            (harness.REFS_DIR / name).unlink(missing_ok=True)


def test_a_leftover_that_cannot_be_moved_does_not_fail_the_enrollment(client, monkeypatch, caplog):
    """A file a running backup holds open on Windows cannot be renamed, and that must not
    cost a worker their enrollment - or hide behind a silent failure."""
    assert create_account(client, LOCKED_WORKER, image=jpeg_bytes()).status_code == 200
    legacy_template = harness.REFS_DIR / f"{LOCKED_WORKER}.json"
    legacy_template.write_text(harness.reference_text())
    real_replace = os.replace

    def refuse_the_retirement(source, target):
        # Only the retirement: the two writes that make the template go through ``os.replace``
        # as well, and a test that broke those would be testing nothing at all.
        if biometrics.QUARANTINE_SUFFIX in str(target):
            raise OSError(13, "the file is open in another process")
        return real_replace(source, target)

    monkeypatch.setattr(os, "replace", refuse_the_retirement)
    try:
        enrolled = harness.enroll(client, LOCKED_WORKER, headers=bearer(HEAD_ADMIN))
        assert enrolled.status_code == 200, enrolled.text[:300]
        assert legacy_template.exists(), "the leftover is still there, honestly"
        assert "could not be moved aside" in caplog.text, "and the failure was said out loud"
        assert json.loads(harness.reference_path(LOCKED_WORKER).read_text())["embedding"]
        punch = harness.clock_in(client, LOCKED_WORKER, headers=bearer(LOCKED_WORKER))
        assert punch.status_code == 200, punch.text[:300]
        # Readiness is what tells an operator: the file it could not retire is counted.
        assert biometrics.legacy_files_remaining()["references"] >= 1
    finally:
        monkeypatch.undo()
        discard(LOCKED_WORKER)


def test_two_accounts_never_share_an_id_or_a_template(client):
    assert create_account(client, NEW_WORKER, image=jpeg_bytes()).status_code == 200
    assert create_account(client, LEGACY_WORKER, image=jpeg_bytes()).status_code == 200

    ids = {biometric_id(NEW_WORKER), biometric_id(LEGACY_WORKER)}
    assert len(ids) == 2, "two faces must never be reachable from one name"
    assert harness.reference_path(NEW_WORKER) != harness.reference_path(LEGACY_WORKER)


# ---------------------------------------------------------------------------
# the upgrade: what is already on disk
# ---------------------------------------------------------------------------
def test_a_template_left_under_the_old_name_is_recognised_but_never_scored(client):
    """The fallback finds it. Nothing scores it.

    Two halves, and both matter. *Recognised*: the account reads as enrolled, so the roster,
    the readiness check and the re-enrollment worklist all name it - a face that is on disk
    must not be reported as no face at all. *Never scored*: the name is the one fact that
    cannot say whether the file is this worker's current face or a copy a later enrollment
    replaced, and a punch decided against the wrong one is invisible afterwards.
    """
    assert create_account(client, LEGACY_WORKER).status_code == 200
    legacy = harness.REFS_DIR / f"{LEGACY_WORKER}.json"
    legacy.write_text(harness.reference_text())
    resolved = biometric_id(LEGACY_WORKER)

    try:
        assert harness.template_exists(LEGACY_WORKER), "the face is there, and says so"
        assert roster_entry(client, LEGACY_WORKER)["face_enrolled"] is True
        assert biometrics.is_id_named(str(legacy)) is False
        assert biometrics.is_id_named(harness.REFS_DIR / f"{resolved}.json") is True
        assert biometrics.reference_health(LEGACY_WORKER) == (False, biometrics.STALE_LEGACY_NAME)

        punch = harness.clock_in(client, LEGACY_WORKER, headers=bearer(LEGACY_WORKER))
        assert punch.status_code == 400, punch.text[:300]
        detail = punch.json()["detail"]
        assert detail["error_code"] == "reference_stale", detail
        assert detail["status"] == biometrics.STATUS_NEEDS_REENROLLMENT, detail
        assert detail["stale_reason"] == biometrics.STALE_LEGACY_NAME, detail
        assert "enroll" in detail["message"].lower() and "administrator" in detail["message"].lower()
        assert db_scalar(
            "SELECT COUNT(*) FROM attendance_logs WHERE worker_id = ?", (LEGACY_WORKER,)
        ) == 0, "a refused punch writes nothing"

        # The control: the *same bytes* under the name this build files faces under are scored,
        # so what was refused is the filing and not the vector - a check that refused the
        # template itself would look identical from the punch above.
        legacy.replace(harness.REFS_DIR / f"{resolved}.json")
        punch = harness.clock_in(client, LEGACY_WORKER, headers=bearer(LEGACY_WORKER))
        assert punch.status_code == 200, punch.text[:300]
    finally:
        discard(LEGACY_WORKER)


def test_adoption_files_what_the_old_scheme_left_and_is_idempotent(client):
    assert create_account(client, LEGACY_WORKER).status_code == 200
    resolved = biometric_id(LEGACY_WORKER)

    legacy_template = harness.REFS_DIR / f"{LEGACY_WORKER}.json"
    legacy_template.write_text(harness.reference_text())
    legacy_photo = harness.PHOTOS_DIR / f"{LEGACY_WORKER}.jpg"
    legacy_photo.write_bytes(jpeg_bytes())

    summary = biometrics.adopt_legacy_files()
    assert f"{resolved}.json" in summary["renamed"]
    assert f"{resolved}.jpg" in summary["renamed"]
    assert not legacy_template.exists() and not legacy_photo.exists()
    assert harness.template_exists(LEGACY_WORKER)
    assert harness.stored_photo_path(LEGACY_WORKER) is not None

    again = biometrics.adopt_legacy_files()
    assert again["renamed"] == [], "a second boot has nothing left to rename"
    assert again["failed"] == []


def test_adoption_retires_a_leftover_it_finds_beside_a_usable_template(client):
    """A site that upgraded before the enrollment-time retirement existed, and has a
    superseded file on disk, is repaired by its next boot rather than by a human."""
    assert create_account(client, ADOPT_WORKER, image=jpeg_bytes()).status_code == 200
    legacy_template = harness.REFS_DIR / f"{ADOPT_WORKER}.json"
    legacy_template.write_text(harness.reference_text())
    legacy_photo = harness.PHOTOS_DIR / f"{ADOPT_WORKER}.jpg"
    legacy_photo.write_bytes(jpeg_bytes())

    try:
        summary = biometrics.adopt_legacy_files()
        retired = quarantine_named(harness.REFS_DIR, ADOPT_WORKER)
        assert retired and retired[0] in summary["superseded"], (
            "a leftover beside its id-named twin is retired, and the summary names it"
        )
        assert not legacy_template.exists() and not legacy_photo.exists()
        assert summary["kept"] == []
        assert f"{ADOPT_WORKER}.json" not in summary["renamed"], "there was nothing to rename"

        punch = harness.clock_in(client, ADOPT_WORKER, headers=bearer(ADOPT_WORKER))
        assert punch.status_code == 200, punch.text[:300]
        assert biometrics.legacy_files_remaining()["references"] == 0, (
            "an account with a leftover no longer counts against the naming check"
        )

        again = biometrics.adopt_legacy_files()
        assert again["superseded"] == [] and again["kept"] == [], "and a second boot is quiet"
    finally:
        discard(ADOPT_WORKER)


def test_adoption_keeps_a_leftover_beside_a_template_it_cannot_read(client):
    """The one state where the leftover is the only readable template there is.

    The surviving file is only worth leaving the account on if it can be read, so a
    truncated or empty id-named template means the legacy copy stays exactly where it is
    and is reported, rather than being retired into a re-enrollment the worker cannot
    perform by themselves.
    """
    assert create_account(client, KEPT_WORKER, image=jpeg_bytes()).status_code == 200
    legacy_template = harness.REFS_DIR / f"{KEPT_WORKER}.json"
    legacy_template.write_text(harness.reference_text())
    template = harness.reference_path(KEPT_WORKER)

    try:
        for unreadable in (b"", b"not a template at all"):
            template.write_bytes(unreadable)
            summary = biometrics.adopt_legacy_files()
            assert legacy_template.exists(), (
                "the only readable template this account has must not be moved aside"
            )
            assert f"{KEPT_WORKER}.json" in summary["kept"]
            assert quarantine_named(harness.REFS_DIR, KEPT_WORKER) == []
    finally:
        discard(KEPT_WORKER)


def test_a_new_account_cannot_inherit_the_face_of_the_id_it_took(client):
    """The inheritance bug, end to end - the reason the ids exist."""
    assert create_account(client, REUSED_WORKER, image=jpeg_bytes()).status_code == 200
    stranded = harness.REFS_DIR / f"{REUSED_WORKER}.json"
    # A delete that failed to remove the file: reported by the endpoint, not hidden, and
    # exactly the state a reused account id used to be handed.
    stranded.write_text(harness.reference_text())
    previous_id = biometric_id(REUSED_WORKER)

    sql("DELETE FROM users WHERE id = ?", (REUSED_WORKER,))
    assert stranded.exists(), "the file the failed removal left behind"

    # The next worker is issued the id that just came free, without a photo.
    created = create_account(client, REUSED_WORKER)
    assert created.status_code == 200, created.text[:300]

    assert biometric_id(REUSED_WORKER) != previous_id, "a new person, a new id"
    assert not stranded.exists(), "the stranded file is moved out of the way"
    assert any(
        name.startswith(f"{REUSED_WORKER}.json{biometrics.QUARANTINE_SUFFIX}")
        for name in names(harness.REFS_DIR)
    ), "it is kept, not deleted: it is still somebody's face"
    assert harness.template_exists(REUSED_WORKER) is False, (
        "the new worker would otherwise punch as the previous holder of this id"
    )
    assert roster_entry(client, REUSED_WORKER)["face_enrolled"] is False

    punch = harness.clock_in(client, REUSED_WORKER, headers=bearer(REUSED_WORKER))
    assert punch.status_code == 404, "no reference of their own, no clock-in"


def test_deleting_an_account_removes_the_face_under_both_names(client):
    assert create_account(client, DELETE_WORKER, image=jpeg_bytes()).status_code == 200
    legacy_template = harness.REFS_DIR / f"{DELETE_WORKER}.json"
    legacy_template.write_text(harness.reference_text())
    legacy_photo = harness.PHOTOS_DIR / f"{DELETE_WORKER}.jpg"
    legacy_photo.write_bytes(jpeg_bytes())

    response = client.post(
        "/api/v1/admin/users/delete", headers=bearer(ADMIN), json={"user_id": DELETE_WORKER}
    )
    assert response.status_code == 200, response.text[:300]
    assert response.json()["face_removal_failed"] == [], "nothing is left behind to report"

    assert not legacy_template.exists() and not legacy_photo.exists()
    assert harness.stored_photo_path(DELETE_WORKER) is None
    assert harness.template_exists(DELETE_WORKER) is False


# ---------------------------------------------------------------------------
# no temporary copy of anybody's face
# ---------------------------------------------------------------------------
def test_an_enrollment_leaves_no_temporary_file_behind(client):
    """The embedding used to be computed from a JPEG written to disk first.

    Three places did it: ``local_references/.enroll_tmp_<random>.jpg`` for the console and
    the phone, ``tempfile.mkstemp`` for a bulk batch, and ``backend/temp_enroll_<id>.jpg``
    in the source tree for the enrollment tab - a full-size photo of a worker's face, named
    by their account id, left behind whenever the process died mid-request.
    """
    # Remove the seeded template first, so this enrollment has to write one and the
    # "exactly what it added" assertion below means something.
    for existing in (harness.reference_path(WORKER), harness.stored_photo_path(WORKER)):
        if existing is not None and existing.exists():
            existing.unlink()

    backend_before = names(harness.BACKEND_DIR)
    refs_before, photos_before = names(harness.REFS_DIR), names(harness.PHOTOS_DIR)

    assert harness.enroll(client, WORKER, headers=bearer(HEAD_ADMIN)).status_code == 200

    resolved = biometric_id(WORKER)
    assert names(harness.REFS_DIR) - refs_before == {f"{resolved}.json"}
    assert names(harness.PHOTOS_DIR) - photos_before == {f"{resolved}.jpg"}
    assert not [name for name in names(harness.BACKEND_DIR) - backend_before if name.startswith("temp_enroll")]
    assert not [name for name in names(harness.REFS_DIR) if name.endswith(".tmp")]
    assert not [name for name in names(harness.PHOTOS_DIR) if name.endswith(".tmp")]


def test_self_service_and_bulk_enrollment_leave_no_temporary_file_behind(client, jpeg):
    """The same property on the other two doors into enrollment."""
    for existing in (
        harness.reference_path(MOALLEM),
        harness.reference_path(ADMIN),
    ):
        if existing.exists():
            existing.unlink()

    backend_before = names(harness.BACKEND_DIR)
    refs_before = names(harness.REFS_DIR)

    invite = client.post(
        "/api/v1/admin/enrollment/invites",
        headers=bearer(ADMIN),
        json={"worker_id": MOALLEM, "kind": "enroll"},
    )
    assert invite.status_code == 200, invite.text[:300]
    submitted = client.post(
        f"/api/v1/enroll/{invite.json()['token']}",
        files={"photo": ("photo.jpg", jpeg, "image/jpeg")},
        data={"phone": "", "email": ""},
    )
    assert submitted.status_code == 200, submitted.text[:300]

    # The ZIP member is named by the roster's ``photo`` column, which is a filename inside
    # the archive - not an account id and not a biometric id (see ``_process_item``).
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("admin.jpg", jpeg)
    batch = client.post(
        "/api/v1/admin/enrollment/bulk",
        headers=bearer(ADMIN),
        files={
            "roster": (
                "roster.csv",
                f"user_id,name,role,photo\n{ADMIN},Seed Admin,admin,admin.jpg\n".encode(),
                "text/csv",
            ),
            "photos": ("photos.zip", buffer.getvalue(), "application/zip"),
        },
    )
    assert batch.status_code == 200, batch.text[:300]
    import enrollment

    enrollment.process_job(batch.json()["job_id"])

    added = names(harness.REFS_DIR) - refs_before
    assert added == {f"{biometric_id(MOALLEM)}.json", f"{biometric_id(ADMIN)}.json"}, (
        f"unexpected files in the reference directory: {sorted(added)}"
    )
    assert not [name for name in names(harness.BACKEND_DIR) - backend_before if name.startswith("temp_enroll")]
    assert not [name for name in names(harness.REFS_DIR) if name.endswith(".tmp")]


# ---------------------------------------------------------------------------
# punch selfies
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _quick_photo_dir(tmp_path_factory):
    """Keep punch selfies out of the repository, as the quick-link suite does."""
    original = quick_links.PHOTOS_DIR
    quick_links.PHOTOS_DIR = str(tmp_path_factory.mktemp("quick_link_photos"))
    yield
    quick_links.PHOTOS_DIR = original


def _punch(client) -> dict:
    """One quick-link punch for the lead worker, whose account carries no flags."""
    issued = client.post(
        "/api/v1/admin/quick_links", headers=bearer(HEAD_ADMIN), json={"worker_id": MOALLEM}
    )
    assert issued.status_code == 200, issued.text[:300]
    lat, lon = INSIDE_DOWNTOWN.split(",")
    punched = client.post(
        f"/api/v1/q/{issued.json()['token']}",
        data={"lat": lat, "lon": lon},
        files={"selfie": ("selfie.jpg", jpeg_bytes(), "image/jpeg")},
    )
    assert punched.status_code == 200, punched.text[:300]
    row = db_rows("SELECT id, link_id, photo_path FROM quick_link_uses ORDER BY id DESC LIMIT 1")[0]
    return {"id": row[0], "link_id": row[1], "photo_path": row[2]}


def test_a_punch_selfie_is_not_named_after_the_punch(client):
    """These are faces too, and they were named ``{link row id}_{timestamp}_{random}``."""
    use = _punch(client)
    name = str(use["photo_path"])

    # The old shape was ``{link row id}_{YYYYmmdd_HHMMSS}_{8 hex}.jpg``: the name says
    # which punch it belongs to. The new one is a random token and nothing else, so the
    # assertion is the shape rather than a substring - "1" appears inside most hex strings
    # by chance, and a test that fails on coincidence is a test that gets deleted.
    assert re.fullmatch(r"[0-9a-f]{32}\.jpg", name), f"unexpected punch selfie name: {name}"
    assert not name.startswith(f"{use['id']}_") and not name.startswith(f"{use['link_id']}_")
    assert os.path.exists(os.path.join(quick_links.PHOTOS_DIR, name))


def test_punch_selfies_from_the_old_scheme_are_renamed_and_still_served(client):
    use = _punch(client)
    use_id = int(use["id"])
    stored = quick_links.PHOTOS_DIR + os.sep + str(use["photo_path"])

    # Put it back the way the old code named it, and point the row at that.
    legacy_name = f"{use_id}_20260101_010101_deadbeef.jpg"
    os.replace(stored, os.path.join(quick_links.PHOTOS_DIR, legacy_name))
    sql("UPDATE quick_link_uses SET photo_path = ? WHERE id = ?", (legacy_name, use_id))

    summary = quick_links.adopt_legacy_photo_names()
    assert summary["renamed"] and summary["failed"] == []
    assert not os.path.exists(os.path.join(quick_links.PHOTOS_DIR, legacy_name))

    renamed = str(db_scalar("SELECT photo_path FROM quick_link_uses WHERE id = ?", (use_id,)))
    assert renamed != legacy_name and os.path.exists(os.path.join(quick_links.PHOTOS_DIR, renamed))

    served = client.get(f"/api/v1/admin/quick_link_photo/{use_id}", headers=bearer(ADMIN))
    assert served.status_code == 200, "the console fetches by use id, so nothing else broke"

    assert quick_links.adopt_legacy_photo_names()["renamed"] == [], "idempotent"


# ---------------------------------------------------------------------------
# the guards behind the guards
# ---------------------------------------------------------------------------
def test_a_null_id_is_not_the_string_none():
    """Found by this suite: a NULL column read as the string ``"None"``.

    That string is a filename - so every account whose column was NULL would have resolved
    to one template called ``None.json``, which is the collision the ids exist to prevent.
    """
    assert biometrics.id_from({"biometric_id": None}) is None
    assert biometrics.id_from({"biometric_id": "   "}) is None
    assert biometrics.id_from({}) is None
    assert biometrics.id_from({"biometric_id": "abc123"}) == "abc123"


def test_the_harness_cannot_seed_a_template_outside_its_throwaway_directory(monkeypatch):
    """Safety rule 2 of the harness, enforced in code rather than by convention.

    It is worth a test because getting it wrong is not a failing test: an earlier version of
    the harness resolved the application's *live* directories at import time and wrote a
    synthetic template over a real worker's.
    """
    assert harness.REFS_DIR.resolve() in harness.reference_path(READY_WORKER).resolve().parents

    monkeypatch.setattr(harness, "reference_path", lambda user_id: Path(os.getcwd()) / "oops.json")
    with pytest.raises(RuntimeError, match="outside the throwaway directory"):
        harness.seed_reference(READY_WORKER)


def test_readiness_reports_whether_any_face_is_still_named_after_an_account(client):
    import readiness

    assert create_account(client, READY_WORKER).status_code == 200
    assert readiness._check_biometric_file_naming({}).ok is True

    planted = harness.REFS_DIR / f"{READY_WORKER}.json"
    planted.write_text(harness.reference_text())
    try:
        check = readiness._check_biometric_file_naming({})
        assert check.name == "biometric_file_naming"
        assert check.ok is False, "a file under the old name means the change is not finished here"
        assert check.value["references"] >= 1
    finally:
        planted.unlink()


# ---------------------------------------------------------------------------
# the detector changed, so the crop changed
# ---------------------------------------------------------------------------
# The detector was replaced (MTCNN -> YuNet, see ``face_detector``), and a template is an
# embedding of the *crop* that detector chose - measured IoU 0.814 between the two boxes, so
# about 19% different. Scoring an old template against a new punch is therefore not a weak
# match, it is not a comparison at all: the number means nothing, and it can land in the
# *mismatch* band, which accuses a worker of being somebody else.
#
# So a template records what made it and a mismatch is refused. The one thing that must not
# happen is a refusal with no way out, which is what the re-enrollment path is for.
STALE_WORKER = "416"
CURRENT_WORKER = "417"


def _plant_stale_template(user_id: str, *, pipeline=None, dimensions=None) -> Path:
    """Write a template in the shape an upgraded site already has on disk.

    ``pipeline=None`` writes the bare JSON list every existing template is: a vector and no
    record of what produced it, which is exactly the case that needs re-enrolling. Passing a
    pipeline writes the newer record instead, which is how the dimension guard is reached
    without the provenance check answering first.
    """
    vector = harness._reference_embedding()
    if dimensions is not None:
        # Any requested width, so a test can plant a template from another model's space: repeat
        # the live vector and cut it down. Truncation alone stopped working when the live width
        # fell from 4096 to 128, because ``[:512]`` of a 128-float vector is still 128 - and the
        # "wrong size" case silently became a right-sized one with the wrong model name.
        vector = (vector * (dimensions // len(vector) + 1))[:dimensions]
    if pipeline is None:
        text = json.dumps(vector)
    else:
        text = json.dumps(
            {
                "model": "VGG-Face",
                "pipeline": pipeline,
                "dimensions": len(vector),
                "embedding": vector,
            }
        )
    target = harness.reference_path(user_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def test_a_template_from_the_old_pipeline_is_refused_not_scored(client):
    """The refusal names the fix, and it is *not* "Internal processing error".

    A meaningless distance is the worst available answer here: it can read as a mismatch, and
    then the record says a worker's face did not match when the truth is that the two were
    never comparable.
    """
    assert create_account(client, STALE_WORKER).status_code == 200
    planted = _plant_stale_template(STALE_WORKER)
    try:
        response = harness.clock_in(client, STALE_WORKER, headers=bearer(STALE_WORKER))

        assert response.status_code == 400, response.text[:300]
        detail = response.json()["detail"]
        assert detail["error_code"] == "reference_stale", detail
        # Machine-readable, beside the sentence: a client has to route this worker to a
        # different screen than "retake your photo", and the bare template records no
        # provenance at all, so the diagnosis is the *crop* kind.
        assert detail["status"] == biometrics.STATUS_NEEDS_REENROLLMENT, detail
        assert detail["reason"] == biometrics.REASON_STALE_TEMPLATE_PIPELINE, detail
        assert detail["stale_reason"] == biometrics.STALE_NO_PROVENANCE, detail
        assert "administrator" in detail["message"].lower(), detail
        assert "enroll" in detail["message"].lower(), detail
        assert "internal" not in detail["message"].lower(), (
            "a template change is not a server fault and must not be reported as one"
        )
        assert db_scalar("SELECT COUNT(*) FROM attendance_logs WHERE worker_id = ?", (STALE_WORKER,)) == 0
    finally:
        planted.unlink(missing_ok=True)


def test_a_template_of_the_wrong_size_is_refused_rather_than_500(client):
    """A shape mismatch inside ``cosine`` answers 500 and says nothing.

    The *pipeline* matches here on purpose, so this reaches the dimension guard rather than
    the provenance one - which is the case a model swap leaves behind, half-written: a 512-float
    ArcFace template beside a 128-float FaceNet one. The live embedding the stub reports is the
    seeded worker's full-width vector, so the two really are different sizes and the comparison
    really would raise.

    Same refusal as a stale template, because it is the same fix: a photograph. What must not
    happen is ``Internal processing error: shapes not aligned`` - a 500 that blames the server
    for a template change and tells the worker nothing.
    """
    import face_detector
    import face_onnx

    assert create_account(client, STALE_WORKER).status_code == 200
    planted = _plant_stale_template(STALE_WORKER, pipeline=face_detector.PIPELINE, dimensions=512)
    try:
        health = biometrics.reference_health(STALE_WORKER, expected_dimensions=face_onnx.DIMENSIONS)
        assert health == (False, biometrics.STALE_UNREADABLE), health

        response = harness.clock_in(client, STALE_WORKER, headers=bearer(STALE_WORKER))

        assert response.status_code == 400, response.text[:300]
        detail = response.json()["detail"]
        assert detail["error_code"] == "reference_stale"
        # The legacy-template case this whole section exists for: a vector of a different
        # width (a 4096-float VGG-Face template on a FaceNet deployment, here 512 so the test
        # does not carry a 16 KB fixture) is a *version* problem rather than a crop one, and
        # the refusal says which - without raising anything on the way to saying it.
        assert detail["status"] == biometrics.STATUS_NEEDS_REENROLLMENT, detail
        assert detail["reason"] == biometrics.REASON_STALE_TEMPLATE_VERSION, detail
        assert detail["stale_reason"] == biometrics.STALE_UNREADABLE, detail
    finally:
        planted.unlink(missing_ok=True)


def test_re_enrolling_clears_the_refusal(client):
    """The way out. A refusal with no path forward is a dead end for a whole workforce."""
    assert create_account(client, STALE_WORKER).status_code == 200
    planted = _plant_stale_template(STALE_WORKER)
    try:
        assert harness.clock_in(client, STALE_WORKER, headers=bearer(STALE_WORKER)).status_code == 400

        again = harness.enroll(client, STALE_WORKER, headers=bearer(ADMIN))
        assert again.status_code == 200, again.text[:300]

        punch = harness.clock_in(client, STALE_WORKER, headers=bearer(STALE_WORKER))
        assert punch.status_code == 200, (
            "re-enrollment is the repair, so it has to actually repair: " + punch.text[:200]
        )
        assert json.loads(harness.reference_path(STALE_WORKER).read_text(encoding="utf-8"))["pipeline"]
    finally:
        planted.unlink(missing_ok=True)


def test_a_misfiled_template_is_refused_by_the_scorer_that_has_no_account(client):
    """The offline scoring path shares ``compare_faces_sync``, and it is handed a *path*.

    Nothing there knows which account is involved, so the rule it applies is the name of the
    file - ``biometrics.is_id_named`` - which is the same rule the gate applies and the same
    reason the worklist reports. The refusal is checked here rather than through the offline
    endpoints because this is the seam both of them go through, and it is reachable without a
    frame: the name is judged before anything is read or run.
    """
    import main

    assert create_account(client, MISFILED_WORKER).status_code == 200
    legacy = harness.REFS_DIR / f"{MISFILED_WORKER}.json"
    legacy.write_text(harness.reference_text())
    try:
        refused = main.compare_faces_sync(str(legacy), None)

        assert refused["error"] == main.FACE_REFERENCE_MISFILED, refused
        assert refused["stale_reason"] == biometrics.STALE_LEGACY_NAME, refused
        # One *code* for the client - the re-enrollment screen, which is the fix - and a
        # separate *string*, because the stale sentence is a diagnosis and would be a false one
        # for a file that may have been written yesterday and filed wrongly.
        assert refused["error"] != main.FACE_REFERENCE_STALE
        error_code, message = main._frame_refusal(refused["error"])
        assert error_code == main.REFERENCE_STALE_CODE, (error_code, message)
        assert "enroll" in message.lower() and "administrator" in message.lower(), message
    finally:
        legacy.unlink(missing_ok=True)
        discard(MISFILED_WORKER)


def test_re_enrolling_clears_a_misfiled_refusal(client):
    """The way out, and it is the *same* way out: one photograph files the face properly."""
    assert create_account(client, MISFILED_WORKER).status_code == 200
    legacy = harness.REFS_DIR / f"{MISFILED_WORKER}.json"
    legacy.write_text(harness.reference_text())
    try:
        assert harness.clock_in(
            client, MISFILED_WORKER, headers=bearer(MISFILED_WORKER)
        ).status_code == 400

        assert harness.enroll(client, MISFILED_WORKER, headers=bearer(ADMIN)).status_code == 200
        assert not legacy.exists(), "the enrollment retires the name it replaced"

        punch = harness.clock_in(client, MISFILED_WORKER, headers=bearer(MISFILED_WORKER))
        assert punch.status_code == 200, (
            "a refusal without a way out is a dead end for a whole workforce: " + punch.text[:200]
        )
    finally:
        discard(MISFILED_WORKER)


def test_the_admin_worklist_names_a_template_filed_under_the_old_name(client):
    """The gate and the worklist have to agree, or an operator repairs the wrong list."""
    assert create_account(client, MISFILED_WORKER).status_code == 200
    legacy = harness.REFS_DIR / f"{MISFILED_WORKER}.json"
    legacy.write_text(harness.reference_text())
    try:
        response = client.get("/api/v1/admin/enroll/needs_reenrollment", headers=bearer(ADMIN))
        assert response.status_code == 200, response.text[:300]

        listed = {row["id"]: row for row in response.json()["stale"]}
        assert MISFILED_WORKER in listed, response.text[:300]
        assert listed[MISFILED_WORKER]["reason"] == biometrics.STALE_LEGACY_NAME
        assert listed[MISFILED_WORKER]["file"] == f"{MISFILED_WORKER}.json"
        # Not read, because it is not going to be scored: an operator needs to know which file
        # and that it has to be taken again, not what vector is inside it.
        assert listed[MISFILED_WORKER]["template_pipeline"] is None
    finally:
        legacy.unlink(missing_ok=True)
        discard(MISFILED_WORKER)


def test_a_template_the_application_wrote_is_current(client):
    """The other half: the check must not call everything stale.

    A guard that refuses every punch is indistinguishable from a broken deployment, and it
    would pass a test that only looked at the stale case.
    """
    assert create_account(client, CURRENT_WORKER, image=jpeg_bytes()).status_code == 200

    health = biometrics.reference_health(CURRENT_WORKER)
    assert health == (True, None), health
    punch = harness.clock_in(client, CURRENT_WORKER, headers=bearer(CURRENT_WORKER))
    assert punch.status_code == 200, punch.text[:300]


def test_the_admin_worklist_names_who_needs_a_new_photo(client):
    """The re-enrollment *path*: who to photograph, and why - without reading a filesystem."""
    assert create_account(client, STALE_WORKER).status_code == 200
    assert create_account(client, CURRENT_WORKER, image=jpeg_bytes()).status_code == 200
    planted = _plant_stale_template(STALE_WORKER)
    try:
        response = client.get("/api/v1/admin/enroll/needs_reenrollment", headers=bearer(ADMIN))
        assert response.status_code == 200, response.text[:300]
        body = response.json()

        listed = {row["id"]: row for row in body["stale"]}
        assert STALE_WORKER in listed, body
        assert listed[STALE_WORKER]["reason"] == biometrics.STALE_NO_PROVENANCE
        assert listed[STALE_WORKER]["name"], "an administrator acts on a name, not an id"
        assert CURRENT_WORKER not in listed, "a current template is not work"
        assert body["count"] == len(body["stale"])
        assert body["pipeline"] == biometrics.current_pipeline()
    finally:
        planted.unlink(missing_ok=True)


def test_the_worklist_is_an_admin_surface(client):
    """It names workers and their face files, so it is not a worker's to read."""
    response = client.get("/api/v1/admin/enroll/needs_reenrollment", headers=bearer(WORKER))
    harness.assert_denied(response, endpoint="needs_reenrollment", detail="the roster of faces")


def test_read_reference_accepts_both_shapes(tmp_path):
    """One parser for two on-disk formats, because an upgraded site holds both at once."""
    vector = [0.5, -0.25, 0.125]

    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps(vector), encoding="utf-8")
    parsed = biometrics.read_reference(str(legacy))
    assert parsed.embedding == vector
    assert parsed.pipeline is None, "a bare list is a template from before provenance"
    assert len(parsed) == 3

    current = tmp_path / "current.json"
    current.write_text(
        json.dumps({"pipeline": "x", "model": "VGG-Face", "embedding": vector}), encoding="utf-8"
    )
    parsed = biometrics.read_reference(str(current))
    assert parsed.embedding == vector and parsed.pipeline == "x" and parsed.model == "VGG-Face"

    for content in ("{\"not\": \"a template\"}", "\"a string\"", "42"):
        broken = tmp_path / "broken.json"
        broken.write_text(content, encoding="utf-8")
        with pytest.raises(ValueError):
            biometrics.read_reference(str(broken))


def test_the_stale_reason_separates_the_four_cases(tmp_path):
    """Four different causes, because an administrator has to know which one they are in.

    The vectors are live-width on purpose: a dimension is checked *before* provenance, so a
    short vector would be answered ``unreadable`` and the three provenance cases could never be
    reached - the test would pass while measuring the wrong branch.
    """
    import face_engine
    import face_onnx

    current = biometrics.current_pipeline()
    live_vector = [0.1, 0.2] + [0.0] * (face_onnx.DIMENSIONS - 2)
    live = biometrics.Reference(
        embedding=live_vector, pipeline=current, model=face_engine.FACE_MODEL
    )
    assert biometrics.stale_reason(live) is None

    assert biometrics.stale_reason(biometrics.Reference(embedding=live_vector, pipeline=None)) == (
        biometrics.STALE_NO_PROVENANCE
    )
    assert biometrics.stale_reason(
        biometrics.Reference(embedding=live_vector, pipeline="mtcnn", model=face_engine.FACE_MODEL)
    ) == biometrics.STALE_OTHER_PIPELINE
    assert biometrics.stale_reason(live, expected_dimensions=512) == biometrics.STALE_UNREADABLE
    assert biometrics.stale_reason(live, expected_dimensions=face_onnx.DIMENSIONS) is None

    # The migration case, and the reason the width is checked at all: a real VGG-Face template
    # left on disk is 4096 floats, which no FaceNet comparison can score. It is refused here
    # rather than reaching numpy as a shape error.
    assert biometrics.stale_reason(
        biometrics.Reference(embedding=[0.0] * 4096, pipeline=current, model="VGG-Face")
    ) == biometrics.STALE_UNREADABLE

    # The second half of the provenance. The crop can be the current one and the template still
    # unscoreable: the decision lines were measured in the vector space of a *different* model,
    # and ``face_detector.band_for`` is keyed by the pair precisely so this cannot slip past.
    assert biometrics.stale_reason(
        biometrics.Reference(embedding=live_vector, pipeline=current, model="ArcFace")
    ) == biometrics.STALE_OTHER_MODEL
    # A template that records no model at all is left alone: it is not a claim about a model
    # this build does not have, and the bare lists it joins are refused by provenance anyway.
    assert biometrics.stale_reason(biometrics.Reference(embedding=live_vector, pipeline=current)) is None

    # Every reason has something to say to the person who has to clear it - including the one
    # that is not about the vector at all: a template filed under the account id.
    assert set(biometrics.STALE_EXPLANATIONS) == {
        biometrics.STALE_UNREADABLE,
        biometrics.STALE_NO_PROVENANCE,
        biometrics.STALE_OTHER_PIPELINE,
        biometrics.STALE_OTHER_MODEL,
        biometrics.STALE_LEGACY_NAME,
    }


def test_readiness_reports_the_detector_and_the_re_enrollment_worklist(client):
    """An operator has to be able to see both halves without opening a directory."""
    import readiness

    assert create_account(client, STALE_WORKER).status_code == 200
    planted = _plant_stale_template(STALE_WORKER)
    try:
        check = readiness._check_face_detector({})
        assert check.name == "face_detector"
        assert check.tier == readiness.TIER_ADVISORY, "a missing model is not a reason to refuse to boot"
        assert "face detector" in check.detail
        assert "re-enrollment" in check.detail, check.detail
        assert check.value["count"] >= 1
    finally:
        planted.unlink(missing_ok=True)


def test_the_readiness_check_names_the_detector_that_is_live(client):
    """The fallback is reported, not hidden: it is thirty times slower per punch."""
    import face_detector
    import readiness

    check = readiness._check_face_detector({})
    assert check.value["detector"] == face_detector.DETECTOR_NAME
    assert check.ok is True, "the harness stubs the detector as present"
    assert face_detector.PIPELINE in check.detail
