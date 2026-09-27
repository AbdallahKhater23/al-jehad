"""The root tier's four operational tools: the snapshot, the probe, the re-index, the session.

``test_developer_diagnostics`` covers the panels - what the deployment *is*. This file covers the
four routes that *act* on it, and each one is written around the mistake it exists to prevent:

* **a snapshot has to be a database.** ``Connection.backup`` rather than a file copy, because the
  journal is live: the tests open what was written, through SQLite, and read a row committed just
  before it. A size check would pass on a copy that captured half a transaction - and this is the
  copy an operator reaches for *after* the change that went wrong.
* **a probe has to answer the question that was asked.** A worker told "outside geofence" while
  standing on the site is one of three things, and the distance alone separates only one of them.
  The reversed pair and the impossible fix are driven here, and the number is compared against
  ``main.get_distance_meters`` rather than against arithmetic copied into this file.
* **a re-index has to be able to refuse.** Only two of the five staleness reasons are about how a
  face is *filed*; the other three are about the crop or the model, where a distance between the
  old vector and the new one means nothing at all. Both halves are driven, and a re-derivation
  that does not reproduce the stored vector is held to its refusal even when the run is not a dry
  one - because the verdict is the answer, and a template that disagrees with the face it came
  from is worse than a stale one, since it looks current.
* **a session has to be openable and useless.** A 15-minute token carrying the target's own role
  would be a *working* session for that account, and in this application a worker's session can
  clock a punch in - a payable row attributed to the worker and to nobody else. The tests read
  through the token, are refused on the first write at the one dependency every authenticated
  route goes through, and check that the same write succeeds for an ordinary session.

    pytest backend/tests/test_developer_tools.py -q
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
from database import db
from harness import ADMIN, HEAD_ADMIN, MOALLEM, WORKER, bearer

import biometrics
import developer
import face_detector
import face_engine
import harness
import main
import security
from config import settings

TS = "%Y-%m-%d %H:%M:%S"
DEV_ID = developer.DEVELOPER_ID_DEFAULT
DEV_PASSWORD = "root-credential-for-the-tools-suite-0001"

TOWER = "Downtown Tower A"
TOWER_LAT, TOWER_LON, TOWER_RADIUS = harness.SITES[TOWER]

#: The exposure window, spelled out here so that widening it in ``developer`` is a failing test
#: rather than a quietly accepted longer session.
FIFTEEN_MINUTES = 900


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _as_developer() -> dict[str, str]:
    """Seed the root account into this test's database and return a header for it."""
    developer.seed_developer_account(password=DEV_PASSWORD, actor="test:developer_tools")
    return bearer(DEV_ID, role=security.DEVELOPER_ROLE)


def _rows(sql: str, params: tuple = ()) -> list[dict]:
    with db() as conn:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _audit_rows(action: str) -> list[dict]:
    return _rows("SELECT * FROM audit_log WHERE action = ? ORDER BY id", (action,))


def _alert_rows(kind: str) -> list[dict]:
    return _rows("SELECT * FROM developer_alerts WHERE kind = ? ORDER BY id", (kind,))


def _user(user_id: str) -> dict:
    return _rows("SELECT * FROM users WHERE id = ?", (user_id,))[0]


@pytest.fixture(autouse=True)
def _backup_directory(tmp_path, monkeypatch) -> Path:
    """Point the snapshot destination into this test's own directory, and return it.

    ``settings.backup_dir`` is the *deployment's* - ``<project root>/backups`` unless the
    environment says otherwise - and a test that snapshotted into it would leave a copy of the
    fixture database in the checkout, one more per run. The endpoint reads the setting when it is
    called, so setting it here is enough. Returned rather than hidden so a test can say where the
    file landed.
    """
    directory = tmp_path / "dev-backups"
    monkeypatch.setattr(settings, "backup_dir", directory)
    return directory


def _reference_vector() -> list[float]:
    """The vector the fixture's own templates carry, read from the fixture rather than rebuilt."""
    return json.loads(harness.reference_text())["embedding"]


def _stale_template(user_id: str, *, pipeline: str | None = None) -> Path:
    """Write a template the way a *previous* build wrote one, and return its path.

    ``pipeline`` records a crop that is not the live one, which is a different refusal from the
    bare vector: the record is complete, and the face in it was embedded through another chain.
    """
    vector = _reference_vector()
    if pipeline is None:
        return harness.seed_reference(user_id, json.dumps(vector))
    return harness.seed_reference(
        user_id,
        json.dumps(
            {
                "model": face_engine.FACE_MODEL,
                "pipeline": pipeline,
                "dimensions": len(vector),
                "embedding": vector,
            }
        ),
    )


def _plant_photo(user_id: str, image: bytes | None = None) -> Path:
    """Put a reference selfie where ``biometrics.resolve_photo`` will find it.

    The fixture seeds the *template* for every account and no selfie beside it, which is a real
    state: the photo is written by an enrolment, and the template can outlive it. A test that
    needs the decode half of a re-index has to provide one.
    """
    path = Path(biometrics.photo_path(_user(user_id)["biometric_id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(image if image is not None else harness.jpeg_bytes())
    return path


def _mark_enrolled(
    user_id: str, *, on: str | None = "2026-01-02 09:00:00", status: str = "active"
) -> None:
    """Set the two columns a re-index's worklist is filtered by.

    The fixture seeds the template *file* and leaves ``enrolled_at`` null - stamping it is what an
    enrolment through the application does - so a test that wants an account to be in a re-index
    has to say so. It is the same column the roster reports freshness from, which is why the
    endpoint reads it rather than the file.
    """
    with db(write=True) as conn:
        conn.execute(
            "UPDATE users SET enrolled_at = ?, status = ? WHERE id = ?", (on, status, user_id)
        )


def _one_stale_account_with_a_selfie() -> Path:
    """One account a re-index can act on: no-provenance template, and a selfie beside it."""
    _mark_enrolled(WORKER)
    template = _stale_template(WORKER)
    _plant_photo(WORKER)
    return template


def _reindex(client, **params) -> dict:
    """One re-index run. Everything is a query parameter, including the dry run.

    The runs below pass ``limit=1`` and the fixture's lowest account id is the seeded worker, so
    the worklist is exactly the account under test - the ordering is ``CAST(id AS INTEGER) ASC``,
    and the assertions on what the run *did* are what would fail if that stopped being true.
    """
    response = client.post(
        "/api/v1/developer/biometrics/reindex", params=params, headers=_as_developer()
    )
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# the snapshot
# ---------------------------------------------------------------------------
def test_a_snapshot_is_a_whole_database_written_into_the_backup_directory(
    client, app_module, _backup_directory
):
    _as_developer()
    with db(write=True) as conn:
        conn.execute(
            "INSERT INTO worker_devices (device_id, worker_id, key_salt, key_epoch, created_at) "
            "VALUES ('dev-snapshot-marker', ?, 'salt', 1, ?)",
            (WORKER, datetime.now().strftime(TS)),
        )
    live_users = _rows("SELECT COUNT(*) AS n FROM users")[0]["n"]
    assert not _backup_directory.exists(), "the destination is supposed to be created by the call"

    response = client.post("/api/v1/developer/db/backup", headers=_as_developer())
    assert response.status_code == 200, response.text
    body = response.json()
    snapshot = Path(body["file"])

    assert body["status"] == "success" and body["verified"] == "ok", body
    assert _backup_directory.is_dir()
    assert snapshot.parent == _backup_directory, "the snapshot landed outside the backup directory"
    assert snapshot.name.startswith(f"{developer.BACKUP_PREFIX}_") and snapshot.suffix == ".db"
    assert body["size_bytes"] == snapshot.stat().st_size > 0
    assert body["database"] and body["took_ms"] >= 0

    # Read back through SQLite, not by size. A file copy of the ``.db`` alone can capture a state
    # that never existed once writers are active, and it takes no lock while doing it: the row
    # committed one line before this call is the cheapest proof that the copy is a database.
    copied = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
    try:
        assert copied.execute("PRAGMA quick_check").fetchall() == [("ok",)]
        assert copied.execute("SELECT COUNT(*) FROM users").fetchone()[0] == live_users
        marker = copied.execute(
            "SELECT device_id FROM worker_devices WHERE device_id = 'dev-snapshot-marker'"
        ).fetchone()
    finally:
        copied.close()
    assert marker == ("dev-snapshot-marker",), "the snapshot is missing the commit that preceded it"

    # And the live database is untouched by having been copied under it: it still takes a write.
    with db(write=True) as conn:
        conn.execute(
            "INSERT INTO worker_devices (device_id, worker_id, key_salt, key_epoch, created_at) "
            "VALUES ('dev-after-snapshot', ?, 'salt', 1, ?)",
            (WORKER, datetime.now().strftime(TS)),
        )
    assert _rows("SELECT COUNT(*) AS n FROM worker_devices")[0]["n"] >= 2

    rows = _audit_rows("dev_backup_created")
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["actor_id"] == DEV_ID and row["actor_role"] == security.DEVELOPER_ROLE
    assert row["entity"] == "database" and row["entity_id"] == str(snapshot)
    assert row["ip"], "the trail does not say where the snapshot was taken from"
    assert json.loads(row["after_json"])["size_bytes"] == body["size_bytes"]
    assert json.loads(row["before_json"])["database"] == body["database"]


class _FrozenDatetime(datetime):
    """The clock, held still, so two clicks can be made to land in the same second."""

    @classmethod
    def now(cls, tz=None):  # noqa: D102 - the signature is ``datetime``'s own
        return cls(2026, 9, 27, 12, 0, 0)


def test_a_second_snapshot_in_the_same_second_is_a_second_file(
    client, app_module, monkeypatch, _backup_directory
):
    """Clicking twice has asked for two snapshots.

    ``sqlite3.connect`` opens whatever path it is given, so a destination that already exists
    would be backed up *into*: the second click would replace a copy somebody may already be
    relying on, and the reply would call it a success either way.
    """
    _as_developer()
    monkeypatch.setattr(developer, "datetime", _FrozenDatetime)

    first = client.post("/api/v1/developer/db/backup", headers=_as_developer()).json()
    second = client.post("/api/v1/developer/db/backup", headers=_as_developer()).json()

    assert first["file"] != second["file"], "the second snapshot overwrote the first"
    assert Path(first["file"]).exists() and Path(second["file"]).exists()
    assert Path(second["file"]).name == f"{developer.BACKUP_PREFIX}_20260927_120000_2.db"
    assert first["verified"] == second["verified"] == "ok"
    assert len(list(_backup_directory.glob(f"{developer.BACKUP_PREFIX}_*.db"))) == 2
    assert len(_audit_rows("dev_backup_created")) == 2


# ---------------------------------------------------------------------------
# the geofence probe
# ---------------------------------------------------------------------------
def test_the_geofence_probe_reports_the_distance_the_application_itself_computes(
    client, app_module
):
    _as_developer()
    # ~55 m north of the tower's centre: inside a 65 m fence, with metres to spare.
    lat, lon = TOWER_LAT + 0.0005, TOWER_LON
    expected = main.get_distance_meters(TOWER_LAT, TOWER_LON, lat, lon)
    before = _rows("SELECT COUNT(*) AS n FROM attendance_logs")[0]["n"]

    response = client.post(
        "/api/v1/developer/geo/test-point",
        json={"lat": lat, "lon": lon, "site_name": TOWER},
        headers=_as_developer(),
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["site_name"] == TOWER
    assert body["site_center"] == {"lat": TOWER_LAT, "lon": TOWER_LON}
    assert body["site_radius_meters"] == TOWER_RADIUS
    assert body["target_coordinates"] == {"lat": lat, "lon": lon}
    assert body["calculated_distance_meters"] == pytest.approx(expected, abs=0.01)
    assert body["inside_geofence"] is (expected <= TOWER_RADIUS)
    assert body["margin_meters"] == pytest.approx(TOWER_RADIUS - expected, abs=0.01)
    assert body["coordinates_plausible"] is True
    assert body["note"], "the answer does not say what it is not a substitute for"
    # A probe, with no side effects: no attendance row, no queue entry, nothing to undo.
    assert _rows("SELECT COUNT(*) AS n FROM attendance_logs")[0]["n"] == before


def test_the_probe_costs_the_reversed_pair_and_the_impossible_fix(client, app_module):
    """The three ways a worker standing on the site is told they are not at it."""
    _as_developer()

    # 1. The pair sent the other way round: thousands of kilometres as sent, metres as swapped.
    reversed_pair = client.post(
        "/api/v1/developer/geo/test-point",
        json={"lat": TOWER_LON, "lon": TOWER_LAT, "site_name": TOWER},
        headers=_as_developer(),
    ).json()
    assert reversed_pair["inside_geofence"] is False
    assert reversed_pair["inside_geofence_if_swapped"] is True
    assert reversed_pair["swapped_distance_meters"] == pytest.approx(0.0, abs=0.5)
    assert reversed_pair["calculated_distance_meters"] > reversed_pair["swapped_distance_meters"]
    assert reversed_pair["coordinates_plausible"] is True, "a swapped pair is a real fix"

    # 2. A fix that cannot exist - reported rather than raised, because an operator's probe is
    #    exactly where an implausible pair should stay inspectable.
    impossible = client.post(
        "/api/v1/developer/geo/test-point",
        json={"lat": 200.0, "lon": TOWER_LON, "site_name": TOWER},
        headers=_as_developer(),
    )
    assert impossible.status_code == 200, impossible.text
    body = impossible.json()
    assert body["coordinates_plausible"] is False
    assert "range" in body["coordinates_complaint"].lower()
    assert body["inside_geofence"] is False

    # 3. A real fix that is simply outside the fence: the number is the diagnosis, and the sign
    #    of the margin is what says which way.
    outside = client.post(
        "/api/v1/developer/geo/test-point",
        json={"lat": TOWER_LAT + 0.01, "lon": TOWER_LON, "site_name": TOWER},
        headers=_as_developer(),
    ).json()
    assert outside["inside_geofence"] is False
    assert outside["margin_meters"] < 0
    assert outside["coordinates_plausible"] is True


def test_the_probe_refuses_a_site_that_does_not_exist(client, app_module):
    _as_developer()
    missing = client.post(
        "/api/v1/developer/geo/test-point",
        json={"lat": TOWER_LAT, "lon": TOWER_LON, "site_name": "Nowhere Yard"},
        headers=_as_developer(),
    )
    assert missing.status_code == 404, missing.text
    assert missing.json()["detail"] == "Site not found"


# ---------------------------------------------------------------------------
# the biometric re-index
# ---------------------------------------------------------------------------
def test_a_dry_run_reports_what_it_would_rewrite_and_writes_nothing(client, app_module):
    _as_developer()
    template = _one_stale_account_with_a_selfie()
    before = template.read_bytes()

    body = _reindex(client, limit=1)

    assert body["status"] == "success"
    assert body["dry_run"] is True and body["only_stale"] is True
    assert body["total_scanned"] == 1
    assert body["would_reindex"] == 1 and body["reindexed"] == 0
    assert body["pipeline"] == biometrics.current_pipeline()
    assert body["stored_selfie_max_edge"] == biometrics.PHOTO_MAX_EDGE
    assert body["frame_ceiling"] == settings.face_frame_max_px
    # The stubbed engine reproduces the stored vector exactly, so the measurement is zero - and it
    # is *reported*, which is what makes the dry run worth reading: an operator looks at the
    # distribution before anything moves.
    assert body["distances"]["measured"] == 1
    assert body["distances"]["max"] == pytest.approx(0.0, abs=1e-6)
    assert body["distances"]["min"] == pytest.approx(0.0, abs=1e-6)
    assert body["errors"] == [] and body["refusals"] == []

    # The buckets are disjoint and every scanned account lands in exactly one of them.
    buckets = (
        body["reindexed"]
        + body["would_reindex"]
        + body["already_current"]
        + body["missing_photo"]
        + body["needs_photo"]
        + body["failed_detection"]
        + body["busy"]
        + body["error_count"]
    )
    assert buckets == body["total_scanned"], body

    # The write is irreversible in the way that matters - the old vector is gone once it is
    # replaced - so the default run records what it would do and does not do it.
    assert template.read_bytes() == before, "the dry run wrote to the template"
    assert _user(WORKER)["template_version"] == 0

    rows = _audit_rows("dev_biometric_reindex")
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["actor_id"] == DEV_ID and row["actor_role"] == security.DEVELOPER_ROLE
    assert row["entity"] == "biometrics" and row["entity_id"] == biometrics.current_pipeline()
    assert json.loads(row["before_json"]) == {"dry_run": True, "only_stale": True, "limit": 1}
    assert json.loads(row["after_json"])["would_reindex"] == 1


def test_a_real_run_rewrites_the_record_and_cannot_repeat_it(client, app_module):
    _as_developer()
    template = _one_stale_account_with_a_selfie()
    photo = Path(biometrics.photo_path(_user(WORKER)["biometric_id"]))

    body = _reindex(client, limit=1, dry_run="false")

    assert body["dry_run"] is False
    assert body["reindexed"] == 1 and body["would_reindex"] == 0
    assert body["needs_photo"] == 0 and body["error_count"] == 0 and body["errors"] == []

    # What is on disk is now a record this build can score, and the account says the record moved.
    assert biometrics.template_problem(str(template))[0] is None
    assert _user(WORKER)["template_version"] == 1
    # ``enrolled_at`` is deliberately left alone: it answers "when was this face captured", and a
    # stamp from today on a photograph taken last year would be a lie in the column the freshness
    # reports are built on.
    assert _user(WORKER)["enrolled_at"] == "2026-01-02 09:00:00"
    # Both files of the record: the selfie is re-encoded beside the template, not left behind it.
    assert photo.read_bytes()[:2] == b"\xff\xd8"

    # The whole point of the write: this account could not be scored a moment ago and can be now.
    assert biometrics.reference_health(WORKER) == (True, None)

    # Nothing left to do, and the version does not move twice for one rebuilt record.
    again = _reindex(client, limit=1)
    assert again["already_current"] == 1 and again["would_reindex"] == 0
    assert _user(WORKER)["template_version"] == 1

    alerts = _alert_rows("biometric_reindex")
    assert len(alerts) == 1, alerts
    assert alerts[0]["severity"] == "warning"
    assert alerts[0]["summary"].startswith("1 face template")
    assert json.loads(alerts[0]["detail_json"])["reindexed"] == 1


def test_the_gate_is_the_live_band_and_not_a_line_of_this_endpoint_s_own(
    client, app_module, face, monkeypatch
):
    """One distance, two bands, two verdicts.

    ``FACE_MODE = "review"`` produces a fixed 0.45, which the fixture's calibration refuses and a
    widened band accepts. A re-index that compared against a line of its own - a hardcoded 0.5, or
    the stricter "the distance must be nearly zero" a tempting reading of the spec suggests - would
    give the same answer to both, and would send accounts back for a photograph the deployment's
    own gate would have approved.
    """
    _as_developer()
    _one_stale_account_with_a_selfie()
    monkeypatch.setattr(face, "FACE_MODE", "review")
    pipeline = face_detector.active_pipeline()

    refused = _reindex(client, limit=1, dry_run="false")
    assert refused["reindexed"] == 0 and refused["needs_photo"] == 1
    assert refused["distances"]["max"] == pytest.approx(harness.REVIEW_DISTANCE, abs=0.01)
    assert refused["refusals"][0]["approve_line"] == pytest.approx(
        face_detector.active_band().approve
    )
    assert refused["distances"]["max"] > face_detector.active_band().approve, (
        "this half is about a distance the live band refuses"
    )

    monkeypatch.setitem(
        face_detector.BANDS,
        pipeline,
        face_detector.MatchBand(
            model=face_engine.FACE_MODEL,
            approve=0.9,
            review=0.9,
            genuine_ceiling=0.2,
            impostor_floor=0.9,
            evidence="a band widened by this test, and nothing anybody measured",
        ),
    )
    accepted = _reindex(client, limit=1, dry_run="false")
    assert accepted["reindexed"] == 1, accepted
    assert accepted["distances"]["max"] == pytest.approx(harness.REVIEW_DISTANCE, abs=0.01)
    assert _user(WORKER)["template_version"] == 1


def test_a_face_the_selfie_cannot_reproduce_is_sent_back_for_a_photograph(
    client, app_module, face, monkeypatch
):
    _as_developer()
    template = _one_stale_account_with_a_selfie()
    before = template.read_bytes()
    monkeypatch.setattr(face, "FACE_MODE", "mismatch")

    body = _reindex(client, limit=1, dry_run="false")

    assert body["needs_photo"] == 1 and body["reindexed"] == 0
    refusal = body["refusals"][0]
    assert refusal["id"] == WORKER
    assert refusal["reason"] == biometrics.STALE_NO_PROVENANCE
    assert refusal["distance"] > refusal["approve_line"]
    assert "did not reproduce" in refusal["why"]

    # Nothing was written, even though the run was not a dry one: the re-derived face is not the
    # face the template was made from, and replacing it anyway would leave a record that looks
    # current and matches nobody.
    assert template.read_bytes() == before
    assert _user(WORKER)["template_version"] == 0
    assert _alert_rows("biometric_reindex") == []


def test_a_pipeline_mismatch_is_refused_before_the_selfie_is_ever_looked_for(client, app_module):
    """The reason is knowable from the template alone, so nothing else is read.

    The account has **no** selfie on disk. A run that answered ``missing_photo`` would have been
    replying "there is no photograph" to a question whose answer is "a photograph from this
    account would not help" - and it would have paid for a decode to say it.
    """
    _as_developer()
    _mark_enrolled(WORKER)
    _stale_template(WORKER, pipeline="mtcnn-legacy")
    assert biometrics.resolve_photo(WORKER) is None, "this test is about the account with no selfie"

    body = _reindex(client, limit=1, dry_run="false")

    assert body["needs_photo"] == 1
    assert body["missing_photo"] == 0
    refusal = body["refusals"][0]
    assert refusal["reason"] == biometrics.STALE_OTHER_PIPELINE
    assert refusal["why"] == developer.REINDEX_NEEDS_PHOTO[biometrics.STALE_OTHER_PIPELINE]
    # No distance was measured, because there is nothing to measure it against: the decision was
    # made from the record alone.
    assert refusal["distance"] is None and refusal["approve_line"] is None


def test_a_template_filed_under_the_old_name_is_read_and_re_filed(client, app_module):
    """The reason a re-index exists for: a face that is fine and filed wrong.

    The name rule refuses a legacy-named file *without reading it*, because a name cannot tell you
    which of two files it is. A re-index is the one caller that can answer that with evidence
    instead: the vector it is about to write is compared with this one, and only a match is
    written anywhere.
    """
    _as_developer()
    _mark_enrolled(WORKER)
    harness.reference_path(WORKER).unlink()
    legacy = Path(biometrics.legacy_reference_path(WORKER))
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(json.dumps(_reference_vector()))
    _plant_photo(WORKER)
    assert biometrics.resolve_reference(WORKER) == str(legacy), "the fallback is the file in play"

    body = _reindex(client, limit=1, dry_run="false")

    assert body["reindexed"] == 1, body
    assert body["refusals"] == []
    # Re-filed under the account's immutable id, with the provenance the old build never wrote,
    # and the superseded file retired out of the fallback's reach.
    assert harness.template_exists(WORKER)
    assert biometrics.template_problem(biometrics.resolve_reference(WORKER))[0] is None
    assert not legacy.exists(), "the superseded file is still reachable"


def test_the_worklist_is_the_enrolled_active_population(client, app_module):
    """``enrolled_at`` and ``status`` - and the files are deliberately not the question.

    The template and the selfie are left exactly where they are between the halves below: what
    changes is the column the roster reports an enrolment from. A run that scanned by
    ``os.path.exists`` would clock work for an account whose enrolment was never recorded, and
    would keep scanning one an administrator has deliberately switched off.
    """
    _as_developer()
    _one_stale_account_with_a_selfie()
    # Narrow the population to the account under test. The fixture clones a live database, and a
    # count that included whatever enrolled accounts it happens to carry would be a statement
    # about the checkout rather than about the filter.
    with db(write=True) as conn:
        conn.execute("UPDATE users SET enrolled_at = NULL WHERE id <> ?", (WORKER,))

    assert _reindex(client, limit=5)["total_scanned"] == 1

    _mark_enrolled(WORKER, on=None)
    assert biometrics.is_enrolled(WORKER) is True, (
        "the files are still there - the column is the question"
    )
    body = _reindex(client, limit=5)
    assert body["total_scanned"] == 0 and body["would_reindex"] == 0

    _mark_enrolled(WORKER, status="inactive")
    assert _reindex(client, limit=5)["total_scanned"] == 0

    # A blank status is an account nobody has switched off, which is how the rest of the
    # application reads that column too.
    _mark_enrolled(WORKER, status="")
    assert _reindex(client, limit=5)["total_scanned"] == 1


def test_every_reason_a_template_can_be_refused_for_has_an_outcome_here():
    """The two tables are about the five reasons ``biometrics`` can report, and nothing else.

    They are literals because ``developer`` cannot import ``biometrics`` at module scope (that is
    the direction the import graph runs), so this is what keeps them honest: a reason renamed or
    added in ``biometrics`` without a decision here fails, and so does a *routing* code creeping
    into the reason table - a template is never refused for ``stale_template_version``, which is
    what a client switches on rather than what a template is.
    """
    reasons = {
        biometrics.STALE_UNREADABLE,
        biometrics.STALE_NO_PROVENANCE,
        biometrics.STALE_OTHER_PIPELINE,
        biometrics.STALE_OTHER_MODEL,
        biometrics.STALE_LEGACY_NAME,
    }
    assert set(developer.REINDEX_VERIFIABLE) | set(developer.REINDEX_NEEDS_PHOTO) == reasons
    assert not set(developer.REINDEX_VERIFIABLE) & set(developer.REINDEX_NEEDS_PHOTO)
    assert set(biometrics.REENROLLMENT_STATUSES) == reasons, (
        "the re-enrollment contract and this table no longer describe the same set of reasons"
    )
    assert not set(developer.REINDEX_NEEDS_PHOTO) & {
        biometrics.REASON_STALE_TEMPLATE_VERSION,
        biometrics.REASON_STALE_TEMPLATE_PIPELINE,
    }


# ---------------------------------------------------------------------------
# the read-only impersonation session
# ---------------------------------------------------------------------------
def test_the_impersonation_session_is_read_only_and_expires_in_fifteen_minutes(client, app_module):
    _as_developer()
    minted = client.post(f"/api/v1/developer/auth/impersonate/{WORKER}", headers=_as_developer())
    assert minted.status_code == 200, minted.text
    body = minted.json()

    assert body["impersonating_worker_id"] == WORKER
    assert body["role"] == "worker"
    assert body["token_type"] == "bearer"
    assert body["expires_in_seconds"] == FIFTEEN_MINUTES
    assert body["read_only"] is True
    assert "15" in body["warning"] and "read-only" in body["warning"].lower()

    claims = security.decode_access_token(body["access_token"])
    assert claims["sub"] == WORKER and claims["role"] == "worker"
    assert claims["scope"] == security.READONLY_SCOPE
    assert claims["ver"] == _user(WORKER)["token_version"], (
        "a session minted without the live token version would outlive a revocation"
    )
    assert claims["exp"] - claims["iat"] == FIFTEEN_MINUTES, (
        "the exposure window is not the one the response promises"
    )

    session = {"Authorization": f"Bearer {body['access_token']}"}
    # It reads exactly what the account reads, which is the whole reason to mint one: a mobile
    # rendering bug and a notification inbox are only visible from the account's own seat.
    me = client.get("/api/v1/auth/me", headers=session)
    assert me.status_code == 200, me.text
    assert me.json() == {"id": WORKER, "name": _user(WORKER)["name"], "role": "worker"}

    # ...and it cannot do the account's work. The refusal is at the one dependency every
    # authenticated route goes through, so it holds for endpoints that do not exist yet: a
    # worker's session can clock a punch in, and that punch would be a payable row attributed to
    # the worker rather than to whoever was holding the root credential.
    columns_before = _user(WORKER)["report_columns"]
    refused = client.post(
        "/api/v1/worker/me/report/columns", json={"columns": ["date", "hours"]}, headers=session
    )
    assert refused.status_code == 403, refused.text
    assert "read-only" in refused.json()["detail"].lower()
    assert _user(WORKER)["report_columns"] == columns_before, "the session wrote a preference"

    # The control: the same write, by the account's own session, succeeds. Without it a 403 here
    # would be evidence about the route rather than about the scope.
    allowed = client.post(
        "/api/v1/worker/me/report/columns",
        json={"columns": ["date", "hours"]},
        headers=bearer(WORKER),
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["columns"] == ["date", "hours"]


def test_a_session_cannot_be_minted_for_the_tiers_above_the_developer(client, app_module):
    _as_developer()
    root = _as_developer()

    for target in (DEV_ID, HEAD_ADMIN):
        refused = client.post(f"/api/v1/developer/auth/impersonate/{target}", headers=root)
        assert refused.status_code == 403, f"{target}: {refused.text}"
        assert "Cannot impersonate root or head_admin tiers" in refused.json()["detail"]

    unknown = client.post("/api/v1/developer/auth/impersonate/999999", headers=root)
    assert unknown.status_code == 404, unknown.text

    # A switched-off account is not a debugging subject: ``get_current_user`` does not read
    # ``users.status`` on every request, so a token minted here would be a *working* session for
    # an account an administrator has deliberately stopped.
    with db(write=True) as conn:
        conn.execute("UPDATE users SET status = 'inactive' WHERE id = ?", (MOALLEM,))
    stopped = client.post(f"/api/v1/developer/auth/impersonate/{MOALLEM}", headers=root)
    assert stopped.status_code == 403, stopped.text
    assert "not active" in stopped.json()["detail"]

    assert _audit_rows("dev_impersonation_token_minted") == [], (
        "a refusal left a mint on the trail"
    )
    assert _alert_rows("impersonation_token_minted") == []


def test_the_mint_is_on_the_trail_and_the_token_is_not(client, app_module):
    _as_developer()
    minted = client.post(f"/api/v1/developer/auth/impersonate/{ADMIN}", headers=_as_developer())
    assert minted.status_code == 200, minted.text
    token = minted.json()["access_token"]

    rows = _audit_rows("dev_impersonation_token_minted")
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["actor_id"] == DEV_ID and row["actor_role"] == security.DEVELOPER_ROLE
    assert row["entity"] == "users" and row["entity_id"] == ADMIN
    assert row["ip"], "the trail does not say where the credential was minted from"
    assert json.loads(row["before_json"])["role"] == "admin"
    after = json.loads(row["after_json"])
    assert after["scope"] == security.READONLY_SCOPE
    assert after["ttl_seconds"] == FIFTEEN_MINUTES

    alerts = _alert_rows("impersonation_token_minted")
    assert len(alerts) == 1, alerts
    assert alerts[0]["severity"] == "warning"
    assert ADMIN in alerts[0]["summary"] and "read-only" in alerts[0]["summary"]

    # The trail is read by more people than the person who minted the credential, and the hub is
    # a screen: neither carries the token.
    assert token not in json.dumps(rows)
    assert token not in json.dumps(alerts)

    # The session is the administrator's, not the root tier's - it cannot walk into the surface
    # that minted it.
    session = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/v1/auth/me", headers=session).json()["role"] == "admin"
    assert client.get("/api/v1/developer/runtime", headers=session).status_code == 403
