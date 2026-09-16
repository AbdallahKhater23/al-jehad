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
* an account whose files are still under the old name keeps working, because an upgrade
  must not cost a worker their clock-in mid-shift (the fallback), and the adoption rename
  then files them properly and is idempotent;
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


def test_two_accounts_never_share_an_id_or_a_template(client):
    assert create_account(client, NEW_WORKER, image=jpeg_bytes()).status_code == 200
    assert create_account(client, LEGACY_WORKER, image=jpeg_bytes()).status_code == 200

    ids = {biometric_id(NEW_WORKER), biometric_id(LEGACY_WORKER)}
    assert len(ids) == 2, "two faces must never be reachable from one name"
    assert harness.reference_path(NEW_WORKER) != harness.reference_path(LEGACY_WORKER)


# ---------------------------------------------------------------------------
# the upgrade: what is already on disk
# ---------------------------------------------------------------------------
def test_a_template_left_under_the_old_name_is_still_read(client):
    """The fallback that makes this change safe on a running site."""
    assert create_account(client, LEGACY_WORKER).status_code == 200
    legacy = harness.REFS_DIR / f"{LEGACY_WORKER}.json"
    legacy.write_text(harness.reference_text())

    assert harness.template_exists(LEGACY_WORKER), "an id-named file does not exist yet"
    assert roster_entry(client, LEGACY_WORKER)["face_enrolled"] is True

    punch = harness.clock_in(client, LEGACY_WORKER, headers=bearer(LEGACY_WORKER))
    assert punch.status_code == 200, (
        "a worker enrolled before the change must keep clocking in: " + punch.text[:200]
    )


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
