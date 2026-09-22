"""Data retention and erasure: what is deleted, when, and what is provably left behind.

WHY THIS SUITE EXISTS
---------------------
Retention is the one part of this application whose *correct* behaviour is invisible. Nothing
breaks when it does not run: no punch is refused, no screen is empty, no worker complains. The
sweep either keeps erasing a departed worker's face and a year of IP addresses, or it silently
stops, and in both cases the application looks exactly the same. So the claims have to be
pinned by tests rather than by watching it:

1. **The policy is arithmetic, and arithmetic is testable.** ``0`` means keep forever, in every
   knob; N means "strictly older than N days ago", with the boundary on the side that does not
   delete something written a moment ago.
2. **A dry run is incapable of writing.** Not "we pass ``dry_run`` down the call stack" — the
   connection it opens is read-only, so an accidental write raises rather than quietly doing
   half a sweep. This is the property an operator is trusting before the first ``--apply``.
3. **Erasing a face means erasing the file and the reference to it.** Both halves, in the order
   that cannot leave an unattributed face on disk, and with the bytes actually overwritten
   before the name is unlinked — proven here through a hard link, which is the only way to
   observe what was written to an inode the filesystem has just unlinked.
4. **``audit_log`` is still append-only afterwards.** The retention delete is the single
   sanctioned exception and it takes the database's own guard off to do it; the guard has to be
   back, byte for byte, when the sweep returns — including when the delete fails.
5. **A sweep is evidence.** Counts, cutoffs and a digest of what went, written in the same
   transaction that did the deleting, so the event cannot describe a sweep that did not happen.
6. **The one thing that is never deleted is the hours record.** ``attendance_logs`` is pay.
7. **The sweep cannot take the API down with it.** One target failing does not cancel the
   others, and nothing is deleted when the caller asked for a report.

Everything here runs against the throwaway database and the throwaway biometric directories the
harness installs (see ``harness``): the sweep deletes files, and a test that ran against the
live directories would be destroying a real worker's face.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import harness
import punch_frames
import quick_links
import retention
from harness import (
    ADMIN,
    HEAD_ADMIN,
    MOALLEM,
    SEED_USERS,
    WORKER,
    bearer,
    db_rows,
    db_scalar,
)

#: The reference every seeded account carries, captured at import. The database is restored
#: from a pristine snapshot before every test but the *files* are not, and two tests below
#: deactivate an account - which is supposed to wipe that account's face. Without this the
#: damage would surface two suites away as "Reference embedding not found". Same guard, same
#: reason as ``test_quick_links``.
SEEDED_REFERENCES = {user_id: harness.reference_path(user_id).read_text() for user_id in SEED_USERS}


@pytest.fixture(autouse=True)
def _restore_seeded_files():
    yield
    for user_id, text in SEEDED_REFERENCES.items():
        harness.seed_reference(user_id, text)


@pytest.fixture(autouse=True)
def _quick_photo_dir(tmp_path_factory):
    """Keep punch selfies out of the repository.

    The module reads ``PHOTOS_DIR`` at call time, so repointing it is enough - the trick
    ``test_quick_links`` uses for the same reason. Without it the sweep under test would walk
    the live ``quick_link_photos/`` and delete real punch selfies.
    """
    original = quick_links.PHOTOS_DIR
    directory = tmp_path_factory.mktemp("retention_punch_photos")
    quick_links.PHOTOS_DIR = str(directory)
    yield directory
    quick_links.PHOTOS_DIR = original


@pytest.fixture(autouse=True)
def _punch_frame_dir(tmp_path_factory):
    """Keep punch frames out of the repository, for the same reason as the selfies above.

    ``punch_frames.FRAMES_DIR`` is read at call time (``frames_dir()``), so repointing the
    module attribute is enough - and the sweep reads the same attribute, so the test and the
    code under test are always looking at the same throwaway tree.
    """
    original = punch_frames.FRAMES_DIR
    directory = tmp_path_factory.mktemp("retention_punch_frames")
    punch_frames.FRAMES_DIR = str(directory)
    yield directory
    punch_frames.FRAMES_DIR = original


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _old(days: float = 0, *, hours: float = 0) -> str:
    """A timestamp ``days``/``hours`` in the past, in the application's own format."""
    moment = datetime.now() - timedelta(days=days, hours=hours)
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def _exec(sql: str, params: tuple = ()) -> None:
    conn = sqlite3.connect(harness.DB_PATH)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _inside_temp(path: Path) -> Path:
    """Refuse to write outside the throwaway tree.

    Copied in spirit from ``harness.seed_reference``: the one mistake this suite must not make
    is putting a file where the sweep under test would delete a real person's face, and the
    directories it is pointed at are exactly that.
    """
    resolved = Path(path).resolve()
    if Path(harness.PROJECT_ROOT).resolve() in resolved.parents:
        raise RuntimeError(f"refusing to touch a file inside the checkout: {resolved}")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def _file(directory: Path, name: str, *, age_days: float = 0.0, content: bytes = b"biometric") -> Path:
    """One file in a biometric directory, with an mtime aged as asked."""
    path = _inside_temp(Path(directory) / name)
    path.write_bytes(content)
    if age_days:
        when = time.time() - age_days * 86400
        os.utime(path, (when, when))
    return path


def _punch_photo(name: str, *, age_days: float = 0.0) -> Path:
    return _file(Path(quick_links.PHOTOS_DIR), name, age_days=age_days, content=b"\xff\xd8\xffselfie")


def _seed_use(photo_name: str | None, *, age_days: float) -> int:
    """One ``quick_link_uses`` row, aged. Returns its id."""
    conn = sqlite3.connect(harness.DB_PATH)
    try:
        cursor = conn.execute(
            "INSERT INTO quick_link_uses (link_id, worker_id, action, site_name, photo_path, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (1, WORKER, "Clock In", harness.DOWNTOWN, photo_name, _old(age_days)),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def _seed_audit(action: str, *, age_days: float) -> int:
    conn = sqlite3.connect(harness.DB_PATH)
    try:
        cursor = conn.execute(
            "INSERT INTO audit_log (actor_id, actor_role, action, entity, entity_id, ip, user_agent, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (ADMIN, "admin", action, "users", WORKER, "10.0.0.9", "pytest", _old(age_days)),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def _seed_notification(*, age_days: float, read: bool) -> int:
    conn = sqlite3.connect(harness.DB_PATH)
    try:
        cursor = conn.execute(
            "INSERT INTO admin_notifications (kind, severity, title, body, created_at, read_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "late_arrival",
                "warning",
                f"aged-{age_days}",
                "body",
                _old(age_days),
                _old(age_days) if read else None,
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def _seed_queued_punch(*, age_days: float, status: str = "accepted", materialized: int | None = 4242) -> int:
    conn = sqlite3.connect(harness.DB_PATH)
    try:
        cursor = conn.execute(
            "INSERT INTO punch_queue (client_punch_id, device_id, worker_id, action, client_timestamp, "
            "lat, lon, signature, received_at, status, materialized_log_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"punch-{age_days}-{status}-{uuid.uuid4().hex}",
                "device-x",
                WORKER,
                "Clock In",
                _old(age_days),
                30.05,
                31.23,
                "sig",
                _old(age_days),
                status,
                materialized,
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def _seed_anchor(*, age_days: float) -> int:
    conn = sqlite3.connect(harness.DB_PATH)
    try:
        cursor = conn.execute(
            "INSERT INTO device_anchors (anchor_id, device_id, worker_id, server_time, issued_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (f"anchor-{uuid.uuid4().hex}", "device-x", WORKER, _old(age_days), _old(age_days)),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def _deactivate(user_id: str) -> None:
    _exec("UPDATE users SET status = 'inactive' WHERE id = ?", (user_id,))


def _sweep(**kwargs) -> dict:
    """A sweep with the periods this suite reasons about, unless a test overrides them."""
    return retention.sweep(**kwargs)


def _target(report: dict, name: str) -> dict:
    return report["targets"][name]


# ---------------------------------------------------------------------------
# the policy is arithmetic
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "attribute",
    [
        "punch_photo_days",
        "biometric_days",
        "audit_days",
        "notification_days",
        "punch_queue_days",
        "anchor_days",
    ],
)
def test_zero_means_keep_forever_in_every_knob(attribute):
    """One value spells "off" in all six periods.

    An operator who wants the sweeper to leave one category alone should never have to look up
    whether that category uses ``0``, ``-1`` or ``None``. A ``None`` cutoff is how every target
    reads it, and every target honours it as "skip".
    """
    fresh = retention.Policy(**{attribute: 0})
    assert fresh.cutoff(getattr(fresh, attribute)) is None
    assert fresh.cutoff_epoch(getattr(fresh, attribute)) is None


@pytest.mark.parametrize("days", [1, 30, 365])
def test_a_cutoff_is_exactly_the_configured_number_of_days_ago(days):
    """The boundary is strict: something written at the cutoff stays.

    The direction matters more than the arithmetic. A cutoff computed a minute too late deletes
    a row that is still inside its window, and a row here can be a punch's only record.
    """
    policy = retention.Policy()
    now = datetime(2026, 9, 16, 12, 0, 0)
    cutoff = policy.cutoff(days, now=now)
    assert cutoff == (now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    assert policy.cutoff_epoch(days, now=now) == now.timestamp() - days * 86400.0


def test_the_policy_mirrors_the_settings(monkeypatch):
    """``python -m retention --policy`` must print what the application will actually do."""
    from config import settings

    monkeypatch.setattr(settings, "retention_punch_photo_days", 3, raising=False)
    monkeypatch.setattr(settings, "retention_punch_frame_days", 3, raising=False)
    monkeypatch.setattr(settings, "retention_audit_days", 7, raising=False)
    current = retention.policy()
    assert current.punch_photo_days == 3
    assert current.punch_frame_days == 3
    assert current.audit_days == 7
    assert set(current.as_dict()) == {
        "punch_photo_days",
        "punch_frame_days",
        "biometric_days",
        "audit_days",
        "notification_days",
        "punch_queue_days",
        "anchor_days",
        "max_items_per_sweep",
    }


def test_the_batch_cap_is_how_a_backlog_drains_without_blocking_a_punch(client, app_module, monkeypatch):
    """A first run over a year of accumulation must not hold the write lock through all of it.

    The sweep takes SQLite's single write lock for as long as it works, and the thing waiting on
    that lock is a worker clocking in. So the work is bounded per run and the remainder is
    *reported*, which is what makes "draining" a fact rather than a hope.
    """
    from config import settings

    # A window long enough that only the five rows this test seeds are eligible: the database
    # is a clone of a live one and already carries recent audit rows of its own.
    monkeypatch.setattr(settings, "retention_audit_days", 30, raising=False)
    monkeypatch.setattr(settings, "retention_max_items_per_sweep", 2, raising=False)
    for _ in range(5):
        _seed_audit("user_edit", age_days=400)

    first = _sweep(dry_run=False)
    assert _target(first, "audit_log")["deleted"] == 2, "more than the cap was removed in one go"
    assert _target(first, "audit_log")["deferred"] == 3, "the remainder is not reported"

    second = _sweep(dry_run=False)
    assert _target(second, "audit_log")["deleted"] == 2
    assert _target(second, "audit_log")["deferred"] == 1

    third = _sweep(dry_run=False)
    assert _target(third, "audit_log")["deleted"] == 1
    assert _target(third, "audit_log")["deferred"] == 0, "the backlog did not drain"
    assert db_scalar("SELECT COUNT(*) FROM audit_log WHERE created_at < ?", (_old(400),)) == 0


def test_a_cap_of_zero_means_no_cap_at_all(client, app_module, monkeypatch):
    """The one knob whose zero is not "keep forever" - and it says so in the policy print."""
    from config import settings

    monkeypatch.setattr(settings, "retention_audit_days", 30, raising=False)
    monkeypatch.setattr(settings, "retention_max_items_per_sweep", 0, raising=False)
    for _ in range(5):
        _seed_audit("user_edit", age_days=400)

    report = _sweep(dry_run=False)

    assert retention.policy().cap is None
    assert _target(report, "audit_log")["deferred"] == 0
    assert _target(report, "audit_log")["deleted"] >= 5


def test_the_digest_is_order_independent_and_set_sensitive():
    """The compliance digest proves *which* rows went, so it must not depend on read order."""
    assert retention.digest(["b", "a", "c"]) == retention.digest(["c", "a", "b"])
    assert retention.digest(["a", "b"]) != retention.digest(["a", "c"])
    assert retention.digest([]) == retention.digest([])


# ---------------------------------------------------------------------------
# a dry run cannot write
# ---------------------------------------------------------------------------
def test_a_dry_run_writes_nothing_at_all(client, app_module):
    """The promise an operator relies on before the first ``--apply``.

    Nothing means nothing: not the rows a sweep would delete, not a run row, not a notification,
    and not the compliance event that would otherwise record the deletion.
    """
    photo = _punch_photo("dry-run.jpg", age_days=400)
    use_id = _seed_use("dry-run.jpg", age_days=400)
    audit_id = _seed_audit("user_edit", age_days=4000)
    notification_id = _seed_notification(age_days=4000, read=True)
    # Counted before and after rather than compared against zero: this database is a clone of a
    # live one, and a live deployment that is doing its job already has sweeps recorded in it.
    before_audit = db_scalar("SELECT COUNT(*) FROM audit_log")
    before_runs = db_scalar("SELECT COUNT(*) FROM retention_runs")

    report = _sweep(dry_run=True)

    assert report["dry_run"] is True
    assert photo.exists(), "a dry run deleted a photo"
    assert db_scalar("SELECT photo_path FROM quick_link_uses WHERE id = ?", (use_id,)) == "dry-run.jpg"
    assert db_scalar("SELECT COUNT(*) FROM audit_log WHERE id = ?", (audit_id,)) == 1
    assert db_scalar("SELECT COUNT(*) FROM admin_notifications WHERE id = ?", (notification_id,)) == 1
    assert db_scalar("SELECT COUNT(*) FROM audit_log") == before_audit, "a dry run wrote an audit row"
    assert db_scalar("SELECT COUNT(*) FROM retention_runs") == before_runs, "a dry run recorded a run"
    assert report["targets"]["punch_photos"]["matched"] == 1
    assert report["targets"]["audit_log"]["matched"] >= 1
    assert "compliance_event_id" not in report, "a dry run does not claim to have written an event"


def test_a_dry_run_reports_what_it_would_delete_without_deleting_it(client, app_module):
    _punch_photo("would-go.jpg", age_days=400)
    _seed_use("would-go.jpg", age_days=400)
    report = _sweep(dry_run=True)
    target = _target(report, "punch_photos")
    assert target["matched"] == 1
    assert target["references"] == 1, "the report says a reference would be cleared"
    assert target["bytes"] > 0, "the report says bytes would be overwritten"
    assert target["deleted"] == 0, "'deleted' counts what was actually removed"


def test_the_dry_run_connection_is_read_only(client, app_module, monkeypatch):
    """The guarantee is structural: a dry run *cannot* write, it is not merely asked not to.

    Asserted by intercepting the connection the sweep opens, so this fails if a later change
    opens a writable one and passes ``dry_run`` around instead.
    """
    opened: dict = {}
    real_connect = retention.database.connect

    def spy(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        opened.update(kwargs)
        return connection

    monkeypatch.setattr(retention.database, "connect", spy)
    _sweep(dry_run=True)
    assert opened.get("read_only") is True


def test_an_applied_sweep_records_the_run_and_the_compliance_event(client, app_module):
    """Two records, because they answer different questions.

    ``retention_runs`` answers "is it actually running?" after a restart; the audit event
    answers "what exactly did it erase?" for an auditor. A sweep that did neither would be
    indistinguishable from a sweep that never happened.
    """
    _seed_audit("user_edit", age_days=4000)
    report = _sweep(dry_run=False, actor="pytest:retention")

    assert report["deleted_total"] >= 1
    run = retention.last_run()
    assert run is not None
    assert run["actor"] == "pytest:retention"
    assert run["deleted_total"] == report["deleted_total"]
    assert run["digest"] == report["compliance_digest"]

    rows = db_rows("SELECT after_json FROM audit_log WHERE action = 'retention_sweep' ORDER BY id DESC LIMIT 1")
    assert rows, "the sweep left no compliance event"
    after = json.loads(rows[0][0])
    assert after["deleted_total"] == report["deleted_total"]
    assert after["policy"]["audit_days"] == retention.policy().audit_days
    assert after["failures_total"] == 0
    assert after["compliance_digest"]


def test_a_sweep_with_nothing_to_do_writes_no_notification(client, app_module):
    """A timer that notifies six times a day about nothing is a timer nobody reads."""
    before = db_scalar("SELECT COUNT(*) FROM admin_notifications WHERE kind = 'retention_sweep'")
    report = _sweep(dry_run=False)
    after_count = db_scalar("SELECT COUNT(*) FROM admin_notifications WHERE kind = 'retention_sweep'")
    if report["deleted_total"] == 0:
        assert after_count == before


# ---------------------------------------------------------------------------
# erasure: overwrite, then unlink
# ---------------------------------------------------------------------------
def test_wiping_a_file_overwrites_its_bytes_before_unlinking(tmp_path):
    """Proven through a hard link: the only way to see what was written to a removed inode.

    The unlink is the part an operator can check. The overwrite is the part that makes the
    deletion worth anything on a medium that reuses blocks, and it is invisible afterwards -
    unless a second name still refers to the same inode, which is what this uses.
    """
    secret = b"FACE-BYTES" * 512
    original = _inside_temp(tmp_path / "face.jpg")
    original.write_bytes(secret)
    mirror = _inside_temp(tmp_path / "mirror.jpg")
    try:
        os.link(original, mirror)
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("hard links are not available on this filesystem")

    size = retention.wipe_file(str(original), directory=str(tmp_path))

    assert size == len(secret)
    assert not original.exists()
    assert mirror.exists(), "the second name shares the inode the wipe wrote to"
    assert secret not in mirror.read_bytes(), "the original bytes survived on disk"


def test_a_wiped_file_is_unlinked_so_the_path_stops_existing(tmp_path):
    target = _inside_temp(tmp_path / "gone.jpg")
    target.write_bytes(b"x" * 32)
    assert retention.secure_remove("gone.jpg", directory=str(tmp_path), dry_run=False) == 32
    assert not target.exists()


def test_a_dry_run_wipe_reports_the_size_and_removes_nothing(tmp_path):
    target = _inside_temp(tmp_path / "kept.jpg")
    target.write_bytes(b"y" * 64)
    assert retention.secure_remove("kept.jpg", directory=str(tmp_path), dry_run=True) == 64
    assert target.exists()


def test_a_name_that_traverses_out_of_the_directory_is_refused(tmp_path):
    """The path comes out of a database column, so it is attacker-influenced input.

    ``secure_remove`` takes the basename, so ``../../.env`` cannot name a file outside the
    directory being swept - pinned here because the safety is one ``os.path.basename`` away
    from being removed by somebody tidying up.
    """
    outside = _inside_temp(tmp_path / "sibling" / "keep.me")
    outside.write_bytes(b"keep")
    directory = tmp_path / "swept"
    directory.mkdir()

    assert retention.secure_remove("../sibling/keep.me", directory=str(directory), dry_run=False) == 0
    assert outside.exists()


def test_a_symlink_is_refused_rather_than_followed(tmp_path):
    """Following one would overwrite whatever it points at while reporting its own directory."""
    real = _inside_temp(tmp_path / "real.jpg")
    real.write_bytes(b"do not touch")
    link = _inside_temp(tmp_path / "swept" / "link.jpg")
    try:
        os.symlink(real, link)
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("symlinks are not available on this filesystem")

    with pytest.raises(ValueError):
        retention.wipe_file(str(link), directory=str(link.parent))
    assert real.read_bytes() == b"do not touch"


# ---------------------------------------------------------------------------
# punch selfies
# ---------------------------------------------------------------------------
def test_a_punch_selfie_past_the_window_is_wiped_and_its_reference_cleared(client, app_module):
    """Both halves. A cleared row with the file still there is a face nobody can explain."""
    photo = _punch_photo("past.jpg", age_days=retention.policy().punch_photo_days + 1)
    use_id = _seed_use("past.jpg", age_days=retention.policy().punch_photo_days + 1)

    report = _sweep(dry_run=False)

    assert not photo.exists()
    assert db_scalar("SELECT photo_path FROM quick_link_uses WHERE id = ?", (use_id,)) is None
    assert _target(report, "punch_photos")["bytes"] > 0


def test_a_punch_selfie_inside_the_window_is_left_alone(client, app_module):
    photo = _punch_photo("recent.jpg", age_days=1)
    use_id = _seed_use("recent.jpg", age_days=1)
    _sweep(dry_run=False)
    assert photo.exists()
    assert db_scalar("SELECT photo_path FROM quick_link_uses WHERE id = ?", (use_id,)) == "recent.jpg"


def test_the_boundary_is_inclusive_of_the_window_and_exclusive_of_the_past(client, app_module):
    """A photo written exactly at the cutoff is one day old, not one day past."""
    days = retention.policy().punch_photo_days
    inside = _punch_photo("boundary-in.jpg", age_days=days - 0.01)
    outside = _punch_photo("boundary-out.jpg", age_days=days + 0.01)
    _seed_use("boundary-in.jpg", age_days=days - 0.01)
    _seed_use("boundary-out.jpg", age_days=days + 0.01)

    _sweep(dry_run=False)

    assert inside.exists(), "a selfie still inside its window was deleted"
    assert not outside.exists()


def test_a_row_pointing_at_a_photo_that_is_already_gone_is_still_cleared(client, app_module):
    """A broken reference is not a record. Leaving it makes every later sweep re-report it."""
    use_id = _seed_use("never-written.jpg", age_days=400)
    report = _sweep(dry_run=False)
    assert db_scalar("SELECT photo_path FROM quick_link_uses WHERE id = ?", (use_id,)) is None
    assert _target(report, "punch_photos")["failures"] == []


def test_an_orphaned_punch_selfie_past_the_window_is_wiped(client, app_module):
    """The row may already be gone - a crash between wiping and clearing, or an old version.

    This is the residue pass, and it is the only thing that finds a face the database can no
    longer name: a query cannot see it, and ``os.listdir`` can.
    """
    orphan = _punch_photo("orphan-old.jpg", age_days=400)
    _sweep(dry_run=False)
    assert not orphan.exists()


def test_an_orphaned_punch_selfie_inside_the_window_is_left_alone(client, app_module):
    """A selfie written a moment ago may be the other half of a punch in flight."""
    orphan = _punch_photo("orphan-new.jpg", age_days=0)
    _sweep(dry_run=False)
    assert orphan.exists()


def test_punch_photos_are_kept_forever_when_the_period_is_zero(client, app_module, monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "retention_punch_photo_days", 0, raising=False)
    photo = _punch_photo("ancient.jpg", age_days=4000)
    use_id = _seed_use("ancient.jpg", age_days=4000)
    report = _sweep(dry_run=False)
    assert photo.exists()
    assert db_scalar("SELECT photo_path FROM quick_link_uses WHERE id = ?", (use_id,)) == "ancient.jpg"
    assert "0" in str(_target(report, "punch_photos")["skipped"])


# ---------------------------------------------------------------------------
# punch frames (the review-card evidence)
# ---------------------------------------------------------------------------
def _punch_frame(name: str, *, age_days: float = 0.0) -> Path:
    return _file(Path(punch_frames.FRAMES_DIR), name, age_days=age_days, content=b"\xff\xd8\xffframe")


def _seed_log_frame(frame_name: str | None, *, age_days: float) -> int:
    """One ``attendance_logs`` row claiming a frame, aged. Returns its id.

    The row itself is never deleted - that is the target above this one - so only its frame
    column is at stake here.
    """
    conn = sqlite3.connect(harness.DB_PATH)
    try:
        cursor = conn.execute(
            "INSERT INTO attendance_logs (worker_id, site_name, action, timestamp, hours, score, status, punch_frame) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (WORKER, harness.DOWNTOWN, "Clock In", _old(age_days), 8.0, 0.1, "pending_review", frame_name),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def test_a_punch_frame_past_the_window_is_wiped_and_its_row_cleared(client, app_module):
    """Evidence expires with the window - and the row stops claiming what is no longer there.

    Clearing the column is the point: a review card whose frame answers 404 must say "no frame
    was stored", not leave the row pointing at a file an earlier sweep removed.
    """
    log_id = _seed_log_frame("aabb" * 8 + ".jpg", age_days=400)
    frame = _punch_frame("aabb" * 8 + ".jpg", age_days=400)

    report = _sweep(dry_run=False)

    assert not frame.exists()
    assert db_scalar("SELECT punch_frame FROM attendance_logs WHERE id = ?", (log_id,)) is None
    target = _target(report, "punch_frames")
    assert target["deleted"] >= 1
    assert target["references"] >= 1


def test_a_punch_frame_inside_the_window_survives_its_review(client, app_module):
    """The review is still sitting in the queue, and its evidence stays with it."""
    log_id = _seed_log_frame("ccdd" * 8 + ".jpg", age_days=0)
    frame = _punch_frame("ccdd" * 8 + ".jpg", age_days=0)

    _sweep(dry_run=False)

    assert frame.exists()
    assert db_scalar("SELECT punch_frame FROM attendance_logs WHERE id = ?", (log_id,)) == "ccdd" * 8 + ".jpg"


def test_an_orphaned_punch_frame_past_the_window_is_wiped(client, app_module):
    """A rolled-back punch leaves a file no row claims; the residue pass collects it."""
    frame = _punch_frame("eeff" * 8 + ".jpg", age_days=400)
    _sweep(dry_run=False)
    assert not frame.exists()


def test_an_orphaned_punch_frame_inside_the_window_is_left_alone(client, app_module):
    frame = _punch_frame("1122" * 8 + ".jpg", age_days=0)
    _sweep(dry_run=False)
    assert frame.exists()


def test_a_claimed_frame_is_never_swept_as_residue(client, app_module):
    """The residue pass must not eat a frame some *other* row still points at.

    The old row's frame goes (past the window, row cleared first), the fresh row's frame is
    claimed by a live reference and survives even though it sits in the same directory.
    """
    old_id = _seed_log_frame("3344" * 8 + ".jpg", age_days=400)
    old_frame = _punch_frame("3344" * 8 + ".jpg", age_days=400)
    fresh_id = _seed_log_frame("5566" * 8 + ".jpg", age_days=0)
    fresh_frame = _punch_frame("5566" * 8 + ".jpg", age_days=0)

    _sweep(dry_run=False)

    assert not old_frame.exists()
    assert fresh_frame.exists()
    assert db_scalar("SELECT punch_frame FROM attendance_logs WHERE id = ?", (fresh_id,)) == "5566" * 8 + ".jpg"


def test_punch_frames_are_kept_forever_when_the_period_is_zero(client, app_module, monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "retention_punch_frame_days", 0, raising=False)
    frame = _punch_frame("7788" * 8 + ".jpg", age_days=4000)
    log_id = _seed_log_frame("7788" * 8 + ".jpg", age_days=4000)
    report = _sweep(dry_run=False)
    assert frame.exists()
    assert db_scalar("SELECT punch_frame FROM attendance_logs WHERE id = ?", (log_id,)) == "7788" * 8 + ".jpg"
    assert "0" in str(_target(report, "punch_frames")["skipped"])


# ---------------------------------------------------------------------------
# biometrics
# ---------------------------------------------------------------------------
def test_a_deactivated_accounts_face_is_wiped_immediately(client, app_module):
    """Not after a week: deactivation is when the relationship ends.

    ``main.deactivate_user`` already deletes the face at that moment; this is the retry for the
    case where it could not finish (a file held open by a backup is the usual one). Waiting the
    orphan window would mean a departed worker's face sitting on disk for a week for no reason.
    """
    seeded = harness.reference_path(WORKER)
    assert seeded.exists(), "the fixture expects a seeded template for this account"
    _deactivate(WORKER)

    report = _sweep(dry_run=False)

    assert not seeded.exists()
    assert _target(report, "biometric_files")["deleted"] >= 1


def test_an_active_accounts_face_is_never_touched(client, app_module):
    """Including under the legacy account-id name the readers still fall back to."""
    seeded = harness.reference_path(WORKER)
    legacy = _file(Path(harness.REFS_DIR), f"{WORKER}.json", content=b"legacy-template")
    legacy_photo = _file(Path(harness.PHOTOS_DIR), f"{WORKER}.jpg")

    _sweep(dry_run=False)

    assert seeded.exists()
    assert legacy.exists(), "a live account's legacy-named template was deleted"
    assert legacy_photo.exists()


def test_an_orphaned_template_past_the_window_is_wiped(client, app_module):
    orphan = _file(Path(harness.REFS_DIR), "deadbeef" * 4 + ".json", age_days=30)
    _sweep(dry_run=False)
    assert not orphan.exists()


def test_an_orphaned_template_inside_the_window_is_left_alone(client, app_module):
    """The grace window is what stops an enrollment in flight being swept out from under itself."""
    orphan = _file(Path(harness.REFS_DIR), "feedface" * 4 + ".json", age_days=0)
    _sweep(dry_run=False)
    assert orphan.exists()


def test_a_quarantined_legacy_file_past_the_window_is_wiped(client, app_module):
    """``.unclaimed-<hex>`` names are unreadable by design - and still somebody's face.

    They are quarantined rather than deleted when an account id is reused, because deleting a
    face is a human's decision. Past the window that decision has been made for them, or the
    file would sit there forever.
    """
    old = _file(Path(harness.REFS_DIR), f"{WORKER}.json.unclaimed-abcd1234", age_days=30)
    fresh = _file(Path(harness.REFS_DIR), f"{MOALLEM}.json.unclaimed-beef5678", age_days=0)

    _sweep(dry_run=False)

    assert not old.exists()
    assert fresh.exists(), "a quarantine inside the window is left for a human"


def test_a_staging_file_from_an_interrupted_write_is_wiped(client, app_module):
    """``write_reference`` renames into place; a crash leaves half a template in a ``.tmp``.

    Half an embedding is still biometric data, and nothing will ever read or clean it up.
    """
    staging = _file(Path(harness.REFS_DIR), f"{WORKER}.json.deadbeef.tmp", age_days=30)
    fresh = _file(Path(harness.PHOTOS_DIR), f"{WORKER}.jpg.cafebabe.tmp", age_days=0)

    _sweep(dry_run=False)

    assert not staging.exists()
    assert fresh.exists()


def test_files_that_are_not_biometric_are_ignored(client, app_module):
    """A ``.txt``, a subdirectory, and a name with the right stem but the wrong suffix."""
    notes = _file(Path(harness.REFS_DIR), "readme.txt", age_days=4000)
    stray = _file(Path(harness.PHOTOS_DIR), "settings.json.bak", age_days=4000)
    _sweep(dry_run=False)
    assert notes.exists()
    assert stray.exists()


def test_biometric_files_are_kept_forever_when_the_period_is_zero(client, app_module, monkeypatch):
    """Zero turns off the residue net, not the deletion that happens at deactivation.

    The two are separable on purpose: ``main.deactivate_user`` removes the face the moment the
    account is closed, whatever this is set to, so an operator who sets zero is saying "do not
    go looking for leftovers", not "keep the faces of people who have left".
    """
    from config import settings

    monkeypatch.setattr(settings, "retention_biometric_days", 0, raising=False)
    _deactivate(WORKER)
    seeded = harness.reference_path(WORKER)
    orphan = _file(Path(harness.REFS_DIR), "cafef00d" * 4 + ".json", age_days=4000)

    report = _sweep(dry_run=False)

    assert orphan.exists()
    assert seeded.exists(), "with residue sweeping off, nothing on disk is touched"
    assert _target(report, "biometric_files")["skipped"]


# ---------------------------------------------------------------------------
# the audit log, and its guard
# ---------------------------------------------------------------------------
def test_expired_audit_rows_are_deleted_and_recent_ones_are_not(client, app_module):
    old = _seed_audit("user_edit", age_days=retention.policy().audit_days + 5)
    recent = _seed_audit("user_edit", age_days=1)

    report = _sweep(dry_run=False)

    assert db_scalar("SELECT COUNT(*) FROM audit_log WHERE id = ?", (old,)) == 0
    assert db_scalar("SELECT COUNT(*) FROM audit_log WHERE id = ?", (recent,)) == 1
    assert _target(report, "audit_log")["matched"] >= 1


def test_the_append_only_guard_is_restored_by_the_sweep(client, app_module):
    """Restored from ``sqlite_master``, so what comes back is provably what was taken off.

    This is the whole reason a retention delete is allowed to exist here. If the guard did not
    come back, every later bug, injection attempt or careless endpoint could rewrite history -
    and nothing would say so.
    """
    guard_before = db_scalar("SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = 'audit_log_no_delete'")
    assert guard_before, "the fixture expects the append-only guard to exist"

    _sweep(dry_run=False)

    guard_after = db_scalar("SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = 'audit_log_no_delete'")
    assert guard_after == guard_before
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        _exec("DELETE FROM audit_log")


def test_a_sweep_does_not_touch_the_update_guard(client, app_module):
    guard = db_scalar("SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = 'audit_log_no_update'")
    _sweep(dry_run=False)
    assert db_scalar("SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = 'audit_log_no_update'") == guard
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        _exec("UPDATE audit_log SET ip = '1.2.3.4'")


def test_a_target_that_fails_after_dropping_the_guard_leaves_it_in_place(client, app_module):
    """The transaction is what makes the drop safe, not care in the code that follows it.

    A crash, a disk error or an exception between the drop and the restore must leave the guard
    exactly where it was. Verified with a target that drops the guard and then raises: the
    SAVEPOINT rollback has to undo the DDL as well as the delete.
    """

    # A row for the hostile target to delete, and a count to compare against afterwards. This
    # used to assert ``COUNT(*) > 0``, which passed for as long as the *live* database happened
    # to contain audit rows - i.e. it was asserting that the deployment had been used, not that
    # the rollback worked. The fixture no longer inherits that history, so the row is planted.
    _seed_audit("user_edit", age_days=1)
    before = db_scalar("SELECT COUNT(*) FROM audit_log")
    assert before >= 1, "the planted audit row did not land"

    def hostile(conn, *, dry_run):
        conn.execute("DROP TRIGGER IF EXISTS audit_log_no_delete")
        conn.execute("DELETE FROM audit_log")
        raise RuntimeError("a target that dies mid-flight")

    connection = retention.database.connect(isolation_level=None)
    try:
        connection.execute("BEGIN IMMEDIATE")
        outcome = retention._target(connection, "hostile", hostile, dry_run=False)
        connection.execute("COMMIT")
    finally:
        connection.close()

    assert "RuntimeError" in outcome["error"]
    assert db_scalar("SELECT COUNT(*) FROM audit_log") == before, ("the target's deletes survived its failure")
    assert db_scalar("SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = 'audit_log_no_delete'"), (
        "the append-only guard was left off"
    )


def test_audit_rows_are_kept_forever_when_the_period_is_zero(client, app_module, monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "retention_audit_days", 0, raising=False)
    ancient = _seed_audit("user_edit", age_days=4000)
    report = _sweep(dry_run=False)
    assert db_scalar("SELECT COUNT(*) FROM audit_log WHERE id = ?", (ancient,)) == 1
    assert _target(report, "audit_log")["skipped"]


def test_the_compliance_event_carries_the_cutoff_the_counts_and_a_digest(client, app_module):
    _seed_audit("user_edit", age_days=retention.policy().audit_days + 5)
    _seed_audit("user_edit", age_days=retention.policy().audit_days + 6)
    report = _sweep(dry_run=False, actor="auditor:test")

    rows = db_rows("SELECT after_json FROM audit_log WHERE action = 'retention_sweep' ORDER BY id DESC LIMIT 1")
    after = json.loads(rows[0][0])
    assert after["cutoffs"]["audit_log"] == report["cutoffs"]["audit_log"]
    assert after["deleted_total"] == report["deleted_total"]
    assert after["targets"]["audit_log"]["matched"] == report["targets"]["audit_log"]["matched"]
    assert len(after["targets"]["audit_log"]["digest"]) == 64
    assert after["compliance_digest"] == report["compliance_digest"]


def test_the_digest_changes_with_what_is_deleted(client, app_module):
    """A digest that did not move would prove nothing about the rows that went."""
    _seed_audit("user_edit", age_days=4000)
    first = _sweep(dry_run=True)["targets"]["audit_log"]["digest"]
    _seed_audit("user_edit", age_days=4001)
    second = _sweep(dry_run=True)["targets"]["audit_log"]["digest"]
    assert first != second


# ---------------------------------------------------------------------------
# notifications
# ---------------------------------------------------------------------------
def test_a_read_notification_past_the_window_is_deleted(client, app_module):
    old = _seed_notification(age_days=retention.policy().notification_days + 5, read=True)
    _sweep(dry_run=False)
    assert db_scalar("SELECT COUNT(*) FROM admin_notifications WHERE id = ?", (old,)) == 0


def test_an_unread_notification_is_never_deleted_and_is_reported(client, app_module):
    """Age does not make it done. It is a task somebody has not picked up."""
    stale = _seed_notification(age_days=retention.policy().notification_days + 5, read=False)
    report = _sweep(dry_run=False)
    assert db_scalar("SELECT COUNT(*) FROM admin_notifications WHERE id = ?", (stale,)) == 1
    assert _target(report, "notifications")["stale_unread"] >= 1


def test_a_read_notification_inside_the_window_is_kept(client, app_module):
    recent = _seed_notification(age_days=1, read=True)
    _sweep(dry_run=False)
    assert db_scalar("SELECT COUNT(*) FROM admin_notifications WHERE id = ?", (recent,)) == 1


# ---------------------------------------------------------------------------
# queued punches and replay anchors
# ---------------------------------------------------------------------------
def test_a_materialised_queued_punch_past_the_window_is_deleted(client, app_module):
    """Its record is in ``attendance_logs``; what is left here is location history."""
    queued = _seed_queued_punch(age_days=retention.policy().punch_queue_days + 5, materialized=4242)
    _sweep(dry_run=False)
    assert db_scalar("SELECT COUNT(*) FROM punch_queue WHERE id = ?", (queued,)) == 0


def test_a_rejected_queued_punch_past_the_window_is_deleted(client, app_module):
    rejected = _seed_queued_punch(
        age_days=retention.policy().punch_queue_days + 5, status="rejected", materialized=None
    )
    _sweep(dry_run=False)
    assert db_scalar("SELECT COUNT(*) FROM punch_queue WHERE id = ?", (rejected,)) == 0


def test_an_unaccounted_queued_punch_is_kept_and_reported(client, app_module):
    """Neither materialised nor refused: this is a punch with no record anywhere else.

    Deleting it would erase an arrival rather than a duplicate, so it is counted and left for a
    human - the one place the sweep deliberately does less than it could.
    """
    orphan = _seed_queued_punch(age_days=4000, status="flagged", materialized=None)
    report = _sweep(dry_run=False)
    assert db_scalar("SELECT COUNT(*) FROM punch_queue WHERE id = ?", (orphan,)) == 1
    assert _target(report, "punch_queue")["retained_unaccounted"] >= 1


def test_a_queued_punch_inside_the_window_is_kept(client, app_module):
    queued = _seed_queued_punch(age_days=1)
    _sweep(dry_run=False)
    assert db_scalar("SELECT COUNT(*) FROM punch_queue WHERE id = ?", (queued,)) == 1


def test_stale_replay_anchors_are_deleted_and_fresh_ones_are_kept(client, app_module):
    stale = _seed_anchor(age_days=retention.policy().anchor_days + 1)
    fresh = _seed_anchor(age_days=0)
    _sweep(dry_run=False)
    assert db_scalar("SELECT COUNT(*) FROM device_anchors WHERE id = ?", (stale,)) == 0
    assert db_scalar("SELECT COUNT(*) FROM device_anchors WHERE id = ?", (fresh,)) == 1


# ---------------------------------------------------------------------------
# the hours record is never touched
# ---------------------------------------------------------------------------
def test_attendance_logs_are_never_deleted_and_are_reported(client, app_module):
    """The one target that only counts. Hours worked are pay, and pay is not retention's."""
    _exec(
        "INSERT INTO attendance_logs (worker_id, site_name, action, timestamp, hours, score, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (WORKER, harness.DOWNTOWN, "Clock In", _old(4000), 8.0, 0.1, "approved"),
    )
    before = db_scalar("SELECT COUNT(*) FROM attendance_logs")

    report = _sweep(dry_run=False)

    assert db_scalar("SELECT COUNT(*) FROM attendance_logs") == before
    target = _target(report, "attendance_logs")
    assert target["deleted"] == 0
    assert target["rows_total"] == before
    assert target["past_audit_horizon"] >= 1
    assert "never deleted" in target["skipped"]


def test_there_is_no_setting_that_deletes_attendance_rows(app_module):
    """Pinned as a fact about the configuration surface, not just about today's code path."""
    from config import Settings

    names = set(Settings.model_fields)
    assert not [name for name in names if "retention" in name and "attendance" in name]
    assert not [name for name in retention.policy().as_dict() if "attendance" in name]


# ---------------------------------------------------------------------------
# failure isolation
# ---------------------------------------------------------------------------
def test_one_failing_target_does_not_cancel_the_others(client, app_module, monkeypatch):
    """A locked audit table must not stop the punch selfies being erased, six hours at a time."""
    photo = _punch_photo("erased-anyway.jpg", age_days=400)
    _seed_use("erased-anyway.jpg", age_days=400)
    _seed_audit("user_edit", age_days=4000)

    def broken(conn, *, dry_run):
        raise RuntimeError("this target cannot run")

    targets = tuple(
        (name, broken if name == "audit_log" else function) for name, function in retention.TARGETS
    )
    monkeypatch.setattr(retention, "TARGETS", targets)

    report = _sweep(dry_run=False)

    assert not photo.exists()
    assert "RuntimeError" in _target(report, "audit_log")["error"]
    assert report["targets_failed"] == ["audit_log"]
    assert report["failures_total"] >= 1, "a target that could not run is a failure of the sweep"
    run = retention.last_run()
    assert run is not None and run["failures_total"] >= 1, "the failures are recorded, not just logged"


def test_a_failed_target_does_not_leave_the_transaction_unusable(client, app_module, monkeypatch):
    """The savepoint is what makes the isolation real rather than nominal."""

    def broken(conn, *, dry_run):
        conn.execute("INSERT INTO admin_notifications (kind, severity, title, body, created_at) VALUES ('x','info','t','b','2026-01-01 00:00:00')")
        raise RuntimeError("half a write, then a failure")

    monkeypatch.setattr(retention, "_sweep_notifications", broken)
    monkeypatch.setattr(
        retention,
        "TARGETS",
        tuple((name, broken if name == "notifications" else function) for name, function in retention.TARGETS),
    )
    report = _sweep(dry_run=False)
    assert db_scalar("SELECT COUNT(*) FROM admin_notifications WHERE kind = 'x'") == 0
    assert _target(report, "notifications")["error"]


def test_an_unexpected_error_is_reported_rather_than_raised(client, app_module, monkeypatch):
    """A timer that raises dies, and nobody hears from retention again.

    The sweep reports the failure, records it, and the next run tries again - so the operator
    sees ``failure(s)`` in the report and a failing readiness check instead of silence.
    """

    def explode(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(retention.database, "connect", explode)
    report = _sweep(dry_run=False)
    assert "OperationalError" in report["error"]
    assert report["failures_total"] == 1


# ---------------------------------------------------------------------------
# the HTTP surface
# ---------------------------------------------------------------------------
def test_the_retention_endpoint_is_admin_only(client):
    """Who may see the policy, the residue and the last sweep: administrators, nobody else."""
    for user_id in (WORKER, MOALLEM):
        response = client.get("/api/v1/admin/retention", headers=bearer(user_id))
        harness.assert_denied(response, endpoint="GET /admin/retention", detail=f"{user_id} read the policy")
    for user_id in (ADMIN, HEAD_ADMIN):
        assert client.get("/api/v1/admin/retention", headers=bearer(user_id)).status_code == 200


def test_the_retention_endpoint_reports_the_policy_the_scheduler_and_the_last_run(client, app_module):
    _sweep(dry_run=False, actor="pytest:http")
    body = client.get("/api/v1/admin/retention", headers=bearer(ADMIN)).json()

    assert body["policy"] == retention.policy().as_dict()
    assert body["scheduler"]["interval_seconds"] > 0
    assert body["last_run"]["actor"] == "pytest:http"
    assert "residue" in body
    assert "never deleted" in body["attendance_logs"]


def test_the_retention_endpoint_reports_residue_that_the_database_cannot_see(client, app_module):
    """A face with no owner is invisible to a query and obvious to the filesystem."""
    _file(Path(harness.REFS_DIR), "0badc0de" * 4 + ".json", age_days=30)
    body = client.get("/api/v1/admin/retention", headers=bearer(ADMIN)).json()
    assert body["residue"]["orphaned_biometric_files"] >= 1
    assert body["residue"]["listed"]


def test_the_dry_run_endpoint_writes_nothing(client, app_module):
    photo = _punch_photo("api-dry.jpg", age_days=400)
    use_id = _seed_use("api-dry.jpg", age_days=400)
    before_audit = db_scalar("SELECT COUNT(*) FROM audit_log")
    before_runs = db_scalar("SELECT COUNT(*) FROM retention_runs")

    body = client.post("/api/v1/admin/retention/dry-run", headers=bearer(ADMIN)).json()

    assert body["dry_run"] is True
    assert photo.exists()
    assert db_scalar("SELECT photo_path FROM quick_link_uses WHERE id = ?", (use_id,)) == "api-dry.jpg"
    assert db_scalar("SELECT COUNT(*) FROM audit_log") == before_audit
    assert db_scalar("SELECT COUNT(*) FROM retention_runs") == before_runs


def test_the_dry_run_endpoint_is_admin_only(client):
    for user_id in (WORKER, MOALLEM):
        response = client.post("/api/v1/admin/retention/dry-run", headers=bearer(user_id))
        harness.assert_denied(response, endpoint="POST /admin/retention/dry-run", detail=f"{user_id} swept the database")


# ---------------------------------------------------------------------------
# the timer
# ---------------------------------------------------------------------------
def test_the_watcher_does_not_start_when_it_is_disabled(app_module):
    assert retention.start_watcher(enabled=False) is False
    assert retention.watcher_running() is False


def test_starting_the_watcher_twice_does_not_start_two_sweepers(app_module):
    try:
        assert retention.start_watcher(interval=3600, initial_delay=3600, enabled=True) is True
        assert retention.start_watcher(interval=3600, initial_delay=3600, enabled=True) is True
        assert sum(1 for thread in _threads() if thread.name == "retention-sweeper") == 1
    finally:
        retention.stop_watcher(timeout=10)


def _threads():
    import threading

    return list(threading.enumerate())


def test_the_watcher_sweeps_and_then_stops(client, app_module):
    """The timer actually calls the sweep, and stops when told to.

    Deliberately short intervals: what is being pinned is that the thread runs the real sweep
    (and records it) rather than that the schedule is six hours.
    """
    retention.LAST_REPORT = None
    try:
        assert retention.start_watcher(interval=1, initial_delay=0, enabled=True) is True
        deadline = time.time() + 20
        while retention.LAST_REPORT is None and time.time() < deadline:
            time.sleep(0.1)
    finally:
        retention.stop_watcher(timeout=10)

    assert retention.LAST_REPORT is not None, "the sweeper never ran"
    assert retention.LAST_REPORT["dry_run"] is False
    assert not retention.watcher_running()
    assert retention.last_run() is not None


def test_stopping_a_watcher_that_never_started_is_harmless(app_module):
    retention.stop_watcher(timeout=0)
    assert retention.watcher_running() is False


# ---------------------------------------------------------------------------
# readiness
# ---------------------------------------------------------------------------
def _check(name: str):
    import readiness

    return {check.name: check for check in readiness.run_checks(None)[0]}[name]


def test_readiness_reports_retention_as_healthy_once_a_sweep_has_run(client, app_module):
    _sweep(dry_run=False)
    check = _check("retention_sweep")
    assert check.ok is True
    assert check.tier == "advisory"
    assert check.value["last_run"]["deleted_total"] == retention.last_run()["deleted_total"]


def test_readiness_reports_an_overdue_sweeper(client, app_module):
    """A policy nobody is running is the finding, and silence is how it hides."""
    _exec(
        "INSERT INTO retention_runs (started_at, finished_at, dry_run, actor, deleted_total, bytes_wiped, "
        "failures_total) VALUES (?, ?, 0, 'system:test', 0, 0, 0)",
        (_old(days=30), _old(days=30)),
    )
    check = _check("retention_sweep")
    assert check.ok is False
    assert "past the" in check.detail


def test_readiness_reports_a_sweep_that_could_not_finish(client, app_module):
    _exec(
        "INSERT INTO retention_runs (started_at, finished_at, dry_run, actor, deleted_total, bytes_wiped, "
        "failures_total) VALUES (?, ?, 0, 'system:test', 3, 1024, 2)",
        (_old(hours=1), _old(hours=1)),
    )
    check = _check("retention_sweep")
    assert check.ok is False
    assert "2 failure(s)" in check.detail


def test_readiness_is_quiet_about_residue_when_there_is_none(client, app_module):
    _sweep(dry_run=False)
    check = _check("retention_residue")
    assert check.ok is True, check.detail


def test_readiness_names_residue_that_is_still_on_disk(client, app_module):
    _file(Path(harness.REFS_DIR), "deadc0de" * 4 + ".json", age_days=30)
    check = _check("retention_residue")
    assert check.ok is False
    assert "past their retention window" in check.detail


def test_readiness_does_not_delete_anything(client, app_module):
    """A check that reports residue must never be the thing that removes it."""
    orphan = _file(Path(harness.REFS_DIR), "feedbeef" * 4 + ".json", age_days=30)
    _check("retention_residue")
    assert orphan.exists()


# ---------------------------------------------------------------------------
# migration
# ---------------------------------------------------------------------------
def test_migration_13_creates_the_retention_runs_table(client, app_module):
    columns = {row[1] for row in db_rows("PRAGMA table_info(retention_runs)")}
    assert {
        "id",
        "started_at",
        "finished_at",
        "dry_run",
        "actor",
        "policy_json",
        "summary_json",
        "deleted_total",
        "bytes_wiped",
        "failures_total",
        "digest",
    } <= columns


def test_the_schema_version_names_the_newest_migration(app_module):
    import migrations

    assert migrations.SCHEMA_VERSION == max(version for version, _, _ in migrations.MIGRATIONS)
    assert migrations.SCHEMA_VERSION >= 13


def test_the_migration_is_replayable(app_module):
    """Every migration in this codebase is additive and idempotent; this one is no exception."""
    import migrations

    connection = sqlite3.connect(harness.DB_PATH)
    try:
        migrations.migration_13_retention_runs(connection)
        migrations.migration_13_retention_runs(connection)
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# the operator entry point
# ---------------------------------------------------------------------------
def test_the_cli_defaults_to_a_dry_run(client, app_module, capsys):
    """The default an operator meets first must be the one that cannot destroy anything."""
    photo = _punch_photo("cli.jpg", age_days=400)
    _seed_use("cli.jpg", age_days=400)

    assert retention.main(["--json"]) == 0

    body = json.loads(capsys.readouterr().out)
    assert body["dry_run"] is True
    assert photo.exists()


def test_the_cli_apply_deletes_and_records(client, app_module, capsys):
    photo = _punch_photo("cli-apply.jpg", age_days=400)
    _seed_use("cli-apply.jpg", age_days=400)

    assert retention.main(["--apply", "--json"]) == 0

    body = json.loads(capsys.readouterr().out)
    assert body["dry_run"] is False
    assert not photo.exists()
    assert retention.last_run() is not None


def test_the_cli_policy_prints_every_period(client, app_module, capsys):
    assert retention.main(["--policy"]) == 0
    printed = capsys.readouterr().out
    for name in retention.policy().as_dict():
        assert name in printed


def test_the_cli_refuses_contradictory_flags(client, app_module, capsys):
    assert retention.main(["--dry-run", "--apply"]) == 2
    assert "contradictory" in capsys.readouterr().err


def test_the_cli_refuses_vacuum_without_apply(client, app_module, capsys):
    assert retention.main(["--vacuum"]) == 2
    assert "only makes sense with --apply" in capsys.readouterr().err


def test_the_default_actor_names_the_system(app_module):
    assert retention._default_actor().startswith("system:")
