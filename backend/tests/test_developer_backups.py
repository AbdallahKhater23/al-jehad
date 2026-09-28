"""The console's backups: a snapshot taken, listed, and verified by hand.

WHY THIS EXISTS
---------------
``tools/backup.py`` has existed as a command-line tool, and the console's one-call snapshot hook
(``POST /developer/db/backup``) writes a single file. Neither is what an operator needs in front
of them at three in the morning: a button, a list of what is in the backup directory, and a way
to ask whether any of it is still *intact*. This suite is about the three answers that button and
that list have to give, and each one is a way a backup surface is confidently wrong:

* **a snapshot is a whole project, and it verifies itself.** Not "a file was written": the
  database inside it is opened through SQLite and read, the manifest is there, and the verdict
  the route reports is the verdict the tool's own verifier reached over the bytes on disk. A
  snapshot of a live WAL database taken with a file copy can capture a state that never existed,
  and a size check would call that a success.
* **the list has to tell the two kinds apart.** A snapshot (source, database, assets, manifest,
  verdict) and a single-file database copy both live in ``settings.backup_dir``, and a row that
  did not distinguish them would have an operator restoring from the wrong one. A directory with
  no verdict at all - an interrupted snapshot - is *shown* rather than skipped, because that is
  the row somebody has to see before trusting the one beside it.
* **verification re-reads the bytes.** A stored ``VERIFY.json`` says what was true when the
  snapshot was written; the question a month later is whether the bytes are still those bytes.
  The tampered-snapshot test is the whole point of the route: the stored verdict still says
  ``PASS``, and the endpoint has to say ``FAIL`` and name the file.

Between them these also pin the shape of the *act*: one snapshot at a time, a name that is a name
and never a path, and an audit row for every write.

    pytest backend/tests/test_developer_backups.py -q
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
from database import db
from harness import ADMIN, WORKER, bearer

import developer
import security
from config import settings
from tools import backup as project_backup

TS = "%Y-%m-%d %H:%M:%S"
DEV_ID = developer.DEVELOPER_ID_DEFAULT
DEV_PASSWORD = "root-credential-for-the-backups-suite-0001"

SNAPSHOT = "/api/v1/developer/db/snapshot"
BACKUPS = "/api/v1/developer/db/backups"


# ---------------------------------------------------------------------------
# helpers and fixtures
# ---------------------------------------------------------------------------
def _as_developer() -> dict[str, str]:
    """Seed the root account into this test's database and return a header for it."""
    developer.seed_developer_account(password=DEV_PASSWORD, actor="test:developer_backups")
    return bearer(DEV_ID, role=security.DEVELOPER_ROLE)


def _rows(sql: str, params: tuple = ()) -> list[dict]:
    with db() as conn:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _audit_rows(action: str) -> list[dict]:
    return _rows("SELECT * FROM audit_log WHERE action = ? ORDER BY id", (action,))


@pytest.fixture
def backup_directory(tmp_path, monkeypatch) -> Path:
    """Point the backup destination into this test's own directory, and return it.

    ``settings.backup_dir`` is the *deployment's* - ``<project root>/backups`` unless the
    environment says otherwise, and this checkout has real snapshots in it. A test that wrote
    there would be writing into a directory somebody is relying on. Both routes read the setting
    when they are called, so setting it here is enough.
    """
    directory = tmp_path / "dev-backups"
    monkeypatch.setattr(settings, "backup_dir", directory)
    return directory


@pytest.fixture
def fake_assets(tmp_path, monkeypatch) -> dict[str, Path]:
    """Three breadcrumb directories standing in for the biometric assets.

    The real ``local_references`` is the only copy of every enrolled face template in the
    deployment, and ``worker_photos``/``certs`` are not small either. What is under test is
    whether the route passes ``include_assets`` through to the tool, and ``create_snapshot``
    reads these three as module attributes - so pointing them at three directories in ``tmp_path``
    drives the identical code path without copying the deployment's faces into a test directory
    on every run.
    """
    made: dict[str, Path] = {}
    for name, attribute in (
        ("local_references", "LIVE_REFS"),
        ("worker_photos", "WORKER_PHOTOS"),
        ("certs", "CERTS_DIR"),
    ):
        fake = tmp_path / name
        fake.mkdir()
        (fake / "one.bin").write_bytes(b"stand-in for a face template")
        monkeypatch.setattr(project_backup, attribute, fake)
        made[attribute] = fake
    return made


def _snapshot(client, **body) -> dict:
    """Take a snapshot as the developer, and insist the request succeeded."""
    response = client.post(SNAPSHOT, json=body, headers=_as_developer())
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# taking one
# ---------------------------------------------------------------------------
def test_a_snapshot_holds_the_database_its_manifest_and_its_own_verdict(
    client, app_module, backup_directory
):
    """The whole chain: the tool writes it, the tool verifies it, and both are readable after."""
    _as_developer()
    with db(write=True) as conn:
        conn.execute(
            "INSERT INTO worker_devices (device_id, worker_id, key_salt, key_epoch, created_at) "
            "VALUES ('dev-snapshot-marker', ?, 'salt', 1, ?)",
            (WORKER, datetime.now().strftime(TS)),
        )
    live_users = _rows("SELECT COUNT(*) AS n FROM users")[0]["n"]
    assert not backup_directory.exists(), "the destination is supposed to be created by the call"

    body = _snapshot(client, include_assets=False)
    folder = Path(body["directory"])

    assert body["status"] == "success" and body["verified"] == "PASS", body
    assert body["errors"] == [] and body["integrity_check"] == "ok", body
    assert folder.parent == backup_directory and folder.name.startswith(
        f"{developer.SNAPSHOT_PREFIX}_"
    ), folder
    assert body["files"] > 0 and body["files_checked"] == body["files"], body
    assert body["size_bytes"] > 0 and body["database"] and body["took_ms"] >= 0
    assert body["include_assets"] is False and body["prefix"] == developer.SNAPSHOT_PREFIX

    # The layout the tool promises, present in the directory it said it wrote.
    for relative in ("source/main.py", "data/times.db", "MANIFEST.sha256", "VERIFY.json"):
        assert (folder / relative).exists(), f"the snapshot has no {relative}"
    assert (folder / "assets").is_dir() and not list(
        (folder / "assets").iterdir()
    ), "assets were copied although the request said not to"

    # The verdict on disk is the verdict the route reported - not a second opinion this module
    # formed on its own, which is the thing that could drift from the tool.
    stored = json.loads((folder / "VERIFY.json").read_text(encoding="utf-8"))
    assert stored["status"] == body["verified"] == "PASS", stored["status"]
    assert stored["prefix"] == developer.SNAPSHOT_PREFIX
    assert stored["fingerprint"]["count:users"] == live_users
    manifest = {
        line.split("  ", 1)[1]: line.split("  ", 1)[0]
        for line in (folder / "MANIFEST.sha256").read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    assert manifest["data/times.db"] == stored["db_sha256"], (
        "the manifest and the verdict disagree about the snapshot's own database"
    )
    assert "source/main.py" in manifest, "the source tree is not in the manifest"
    assert len(manifest) == body["files"], body
    # The manifest describes the payload, not itself: hashing the two files that carry the
    # verdicts would make the manifest unable to check anything after the first write.
    assert "VERIFY.json" not in manifest and "MANIFEST.sha256" not in manifest

    # Read back through SQLite, not by size: a copy of a live WAL database that was taken with
    # ``shutil`` can capture a state that never existed, and the row committed one line before
    # this call is the cheapest proof that what is in here is a database.
    copied = sqlite3.connect(f"file:{folder / 'data' / 'times.db'}?mode=ro", uri=True)
    try:
        assert copied.execute("PRAGMA quick_check").fetchall() == [("ok",)]
        assert copied.execute("SELECT COUNT(*) FROM users").fetchone()[0] == live_users
        assert copied.execute(
            "SELECT device_id FROM worker_devices WHERE device_id = 'dev-snapshot-marker'"
        ).fetchone() == ("dev-snapshot-marker",)
    finally:
        copied.close()

    rows = _audit_rows("dev_snapshot_created")
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["actor_id"] == DEV_ID and row["actor_role"] == security.DEVELOPER_ROLE
    assert row["entity"] == "backup" and row["entity_id"] == str(folder)
    assert row["ip"], "the trail does not say where the snapshot was taken from"
    after = json.loads(row["after_json"])
    assert after["name"] == folder.name and after["verified"] == "PASS"
    assert after["files"] == body["files"] and after["size_bytes"] == body["size_bytes"]
    assert json.loads(row["before_json"])["database"] == body["database"]


def test_the_button_copies_the_faces_by_default_and_only_when_asked_otherwise(
    client, app_module, backup_directory, fake_assets
):
    """The default is the whole deployment, because a snapshot without the templates cannot
    restore an enrolled worker - and the request that says ``false`` really means it."""
    _as_developer()
    with_faces = _snapshot(client)  # no body at all: what the console's button sends
    assert with_faces["include_assets"] is True
    assert (
        Path(with_faces["directory"]) / "assets" / "local_references" / "one.bin"
    ).exists(), "the default did not copy the face templates"

    without = _snapshot(client, include_assets=False)
    assert without["include_assets"] is False
    assert not (Path(without["directory"]) / "assets" / "local_references").exists()
    # Both are real snapshots of the same database; only one of them can restore a face.
    assert Path(with_faces["directory"]) != Path(without["directory"])
    assert with_faces["verified"] == without["verified"] == "PASS"


class _FrozenClock:
    """The tool's clock, held still, so two snapshots can be made to land in one second.

    A stand-in *module* rather than a patched ``datetime``: ``tools/backup.py`` reads the clock
    through ``_dt.datetime`` and nowhere else, so replacing that one attribute freezes the
    snapshot's own timestamps without touching the clock the test process or SQLite is using.
    """

    class datetime(datetime):  # noqa: N801 - it is ``datetime.datetime``'s own name
        @classmethod
        def now(cls, tz=None):  # noqa: D102 - the signature is ``datetime``'s
            return cls(2026, 9, 27, 12, 0, 0)

    fromisoformat = staticmethod(datetime.fromisoformat)


def test_two_snapshots_in_the_same_second_are_two_directories(
    client, app_module, backup_directory, monkeypatch
):
    """Clicking twice has asked for two snapshots.

    The tool names its directory ``<prefix>_<stamp>`` and the stamp is whole seconds, so without
    a suffix the second click would be an exception after the first one had already cost the disk
    and the CPU - and the operator would be told the thing they asked for twice had failed.
    """
    _as_developer()
    monkeypatch.setattr(project_backup, "_dt", _FrozenClock)

    first = _snapshot(client, include_assets=False)
    second = _snapshot(client, include_assets=False)

    # The counter goes into the *prefix* - ``create_snapshot`` joins the prefix onto the stamp
    # itself, so the second attempt is ``manual_dev_2_20260927_120000``. What matters is that the
    # two are different directories, both verified, and that the second was not a refusal.
    assert Path(first["directory"]).name == "manual_dev_20260927_120000", first["directory"]
    assert Path(second["directory"]).name == "manual_dev_2_20260927_120000", second["directory"]
    assert second["prefix"] == "manual_dev_2"
    assert Path(first["directory"]).is_dir() and Path(second["directory"]).is_dir()
    assert first["verified"] == second["verified"] == "PASS"
    assert len(list(backup_directory.iterdir())) == 2
    assert len(_audit_rows("dev_snapshot_created")) == 2


def test_a_second_snapshot_while_one_is_writing_is_an_answer_not_a_second_copy(
    client, app_module, backup_directory
):
    """A snapshot is the heaviest thing this deployment does, and the second click is a mistake."""
    _as_developer()
    assert developer._SNAPSHOT_LOCK.acquire(blocking=False)
    try:
        busy = client.post(SNAPSHOT, json={"include_assets": False}, headers=_as_developer())
    finally:
        developer._SNAPSHOT_LOCK.release()

    assert busy.status_code == 409, busy.text
    assert busy.json()["detail"]["error_code"] == "snapshot_in_progress"
    assert not list(backup_directory.glob("*")), "a refused snapshot left something behind"
    assert _audit_rows("dev_snapshot_created") == []

    # ...and the lever is released, not lost: the next attempt is a snapshot.
    assert Path(_snapshot(client, include_assets=False)["directory"]).is_dir()


def test_a_name_that_is_not_a_name_is_refused_and_nothing_lands_outside(
    client, app_module, backup_directory, tmp_path
):
    """The prefix reaches a path join in another module, and a name out of a URL reaches it too.

    ``create_snapshot`` builds ``backup_dir / f"{prefix}_{stamp}"``, so a prefix of ``../escaped``
    would have written a snapshot outside the backup directory - and the verify route takes a path
    segment straight from the URL. Neither is a place a *name* is allowed to be.
    """
    _as_developer()
    escaped = client.post(
        SNAPSHOT, json={"prefix": "../escaped", "include_assets": False}, headers=_as_developer()
    )
    assert escaped.status_code == 400, escaped.text
    assert escaped.json()["detail"]["error_code"] == "bad_snapshot_name"
    assert not backup_directory.exists(), "a refused request created the backup directory"
    assert not list(tmp_path.glob("escaped*")), "the runaway prefix wrote something"

    # 405 belongs in this set: a name carrying a separator does not reach the route at all, so
    # there is no handler to refuse it - which is the right answer for "a path is not a name".
    for name in ("%2e%2e", "a%2Fb", "...", "has%20space"):
        refused = client.post(f"{BACKUPS}/{name}/verify", headers=_as_developer())
        assert refused.status_code in (400, 404, 405), f"{name}: {refused.status_code}"

    # A name that is a name, pointing at nothing, is a 404 rather than a 400: the request was
    # well formed, the snapshot simply is not there.
    missing = client.post(f"{BACKUPS}/manual_dev_20200101_000000/verify", headers=_as_developer())
    assert missing.status_code == 404, missing.text
    assert _audit_rows("dev_snapshot_created") == [], "a refused snapshot was audited as a write"


# ---------------------------------------------------------------------------
# listing what is there
# ---------------------------------------------------------------------------
def test_the_directory_that_does_not_exist_yet_is_an_empty_list_not_an_error(
    client, app_module, backup_directory
):
    """A deployment that has never taken a backup is the normal state, not a failure."""
    _as_developer()
    listed = client.get(BACKUPS, headers=_as_developer())

    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body["exists"] is False and body["items"] == [] and body["newest"] is None
    assert body["directory"] == str(backup_directory)
    assert body["snapshots"] == body["databases"] == body["unverified"] == 0


def test_the_list_tells_a_snapshot_from_a_database_copy_and_from_a_stray(
    client, app_module, backup_directory
):
    """Three kinds of thing can be in that directory, and only one of them has a manifest."""
    _as_developer()
    taken = _snapshot(client, include_assets=False)
    plain = client.post("/api/v1/developer/db/backup", headers=_as_developer())
    assert plain.status_code == 200, plain.text
    # An interrupted snapshot: the directory and its half-written contents, no verdict.
    half = backup_directory / "manual_dev_20260901_010101"
    half.mkdir(parents=True)
    (half / "data").mkdir()
    (half / "data" / "times.db").write_bytes(b"not a database")

    body = client.get(f"{BACKUPS}?limit=10", headers=_as_developer()).json()
    kinds = {item["name"]: item["kind"] for item in body["items"]}

    assert kinds[Path(taken["directory"]).name] == "snapshot"
    assert kinds[Path(plain.json()["file"]).name] == "database"
    assert kinds["manual_dev_20260901_010101"] == "unverified"
    assert body["snapshots"] == 1 and body["databases"] == 1 and body["unverified"] == 1
    assert body["total_bytes"] == sum(item["size_bytes"] for item in body["items"]) > 0
    assert body["exists"] is True and body["prefix"] == developer.SNAPSHOT_PREFIX
    assert body["newest"]["name"] == body["items"][0]["name"]

    row = next(item for item in body["items"] if item["kind"] == "snapshot")
    assert row["status"] == "PASS" and row["integrity_check"] == "ok"
    assert row["files"] == taken["files"] and row["verifiable"] is True
    assert row["age_hours"] is not None and 0 <= row["age_hours"] < 1
    assert row["db_sha256"] and row["created_at"]

    copy = next(item for item in body["items"] if item["kind"] == "database")
    assert copy["status"] is None and copy["verifiable"] is False
    assert copy["notes"], "a database copy must say that it has no manifest behind it"

    stray = next(item for item in body["items"] if item["kind"] == "unverified")
    assert stray["verifiable"] is False and stray["status"] is None
    assert stray["notes"] and "VERIFY" in stray["notes"][0]

    # The newest few survive the limit, which is the only truncation worth having: the row an
    # operator wants is the one at the top.
    newest_first = client.get(f"{BACKUPS}?limit=1", headers=_as_developer()).json()
    assert newest_first["count"] == 1 and newest_first["total"] == 3
    assert newest_first["items"][0]["name"] == body["items"][0]["name"]


# ---------------------------------------------------------------------------
# verifying one
# ---------------------------------------------------------------------------
def test_verification_re_hashes_the_bytes_and_not_the_verdict_already_on_disk(
    client, app_module, backup_directory
):
    """The stored verdict says PASS; the file underneath it has been changed.

    This is the reason the route exists rather than a field on the list: ``VERIFY.json`` records
    what was true when the snapshot was written, and a month later the question is whether the
    bytes are still those bytes. A revert, a hand edit, a disk that lost a page - this is where
    any of them is caught, before the restore that needs them.
    """
    _as_developer()
    taken = _snapshot(client, include_assets=False)
    folder = Path(taken["directory"])

    clean = client.post(f"{BACKUPS}/{folder.name}/verify", headers=_as_developer())
    assert clean.status_code == 200, clean.text
    assert clean.json()["status"] == "PASS" and clean.json()["errors"] == []
    assert clean.json()["files_checked"] == clean.json()["files_listed"] == taken["files"]
    assert clean.json()["name"] == folder.name and clean.json()["took_ms"] >= 0

    stored_before = (folder / "VERIFY.json").read_text(encoding="utf-8")
    assert json.loads(stored_before)["status"] == "PASS", "the fixture is not the case under test"
    readme = folder / "source" / "README.md"
    assert readme.exists(), "the snapshot did not copy the source tree it claims to"
    readme.write_text(readme.read_text(encoding="utf-8") + "\n# tampered\n", encoding="utf-8")

    marked = client.post(f"{BACKUPS}/{folder.name}/verify", headers=_as_developer())
    assert marked.status_code == 200, marked.text
    report = marked.json()
    assert report["status"] == "FAIL", report
    assert any("README.md" in error for error in report["errors"]), report["errors"]
    assert report["files_checked"] < report["files_listed"]
    assert report["integrity_check"] == "ok", (
        "the database is untouched - the *file* moved, and the report has to be able to say that"
    )
    # A verification is a read: it does not rewrite the verdict it just disproved.
    assert (folder / "VERIFY.json").read_text(encoding="utf-8") == stored_before
    assert _audit_rows("dev_snapshot_created") and not _audit_rows("dev_snapshot_failed")


# ---------------------------------------------------------------------------
# who may do any of this
# ---------------------------------------------------------------------------
def test_an_administrator_cannot_take_list_or_verify_anything(client, app_module, backup_directory):
    """The refusals the route list in ``test_developer_role`` also covers, driven with a body.

    That list checks the dependency on every developer route; this checks the two that carry a
    payload, where a 405 would hide a missing guard.
    """
    _as_developer()
    taken = _snapshot(client, include_assets=False)
    name = Path(taken["directory"]).name
    admin = bearer(ADMIN)

    for response in (
        client.get(BACKUPS, headers=admin),
        client.post(SNAPSHOT, json={"include_assets": False}, headers=admin),
        client.post(f"{BACKUPS}/{name}/verify", headers=admin),
    ):
        assert response.status_code == 403, response.text
    assert len(list(backup_directory.iterdir())) == 1, "an administrator's request wrote something"
