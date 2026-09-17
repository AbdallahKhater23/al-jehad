"""Shared harness for the security-baseline suite.

WHY THIS EXISTS
---------------
These tests run against ``backend/main.py`` exactly as it exists *today* and
encode the security properties the remediation plan must deliver. Tests marked
``security_gap`` are expected to FAIL right now -- a red test is the evidence
that the gap is real and exploitable, and the very same test turns green once
the fix lands. Tests marked ``regression`` must stay green before *and* after.

    pytest -m security_gap -q      # expected to fail today (the evidence)
    pytest -m regression -q        # guards that must never break

SAFETY RULES THIS HARNESS ENFORCES
----------------------------------
1. ``main.py`` resolves SQLite as ``sqlite3.connect("times.db")`` -- relative to
   the *current working directory*. This module therefore chdir()s into a
   throwaway temp directory holding a COPY of the live ``times.db`` *before* the
   app is imported. That matters: importing the app runs ``init_db()`` and
   ``enforce_11h_cutoff()``, and today ``init_db()`` rewrites an existing user's
   password hash, so importing against the real working directory would corrupt
   real data. The live database is never written by this suite.
2. ``WORKER_PHOTOS_DIR`` / ``LOCAL_REFS_DIR`` are redirected into the temp
   directory so enrollment tests cannot overwrite real biometric files.
3. ``deepface`` is replaced by an in-process stub before import: the suite must
   not load TensorFlow, download model weights, or depend on ML timing.
4. ``requests.post``/``get`` are replaced for the whole session, because today
   ``send_whatsapp_alert()`` falls back to *hardcoded* Twilio credentials and
   would otherwise place a real API call (and message a personal number) from a
   test run.
5. The temp database is restored from a pristine snapshot before every test, so
   the tests that mutate data stay order-independent.
6. Every table that records *what people did* is then emptied in that copy (see
   ``ACTIVITY_TABLES``). The snapshot is a database in daily use, so without this a
   real punch, quick link or password reset from the same day sits inside a test's
   fixture - and assertions like "exactly one late arrival" fail on somebody else's
   traffic, which reads as a regression in the feature under test. The clone gives
   the suite its schema and its configuration; the tests give it their data.
"""

from __future__ import annotations

import atexit
import base64
import hashlib
import io
from contextlib import contextmanager
import json
import math
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import types
import uuid
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Final

BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_DIR.parent
LIVE_DB = PROJECT_ROOT / "times.db"
LIVE_REFS = PROJECT_ROOT / "local_references"
MAIN_PY = BACKEND_DIR / "main.py"

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

if not LIVE_DB.exists():
    raise RuntimeError(f"the live database {LIVE_DB} is missing; this suite clones it")


def _deterministic_vector(size: int, *, seed: int = 0) -> list[float]:
    """Reproducible pseudo-embedding, used only if the repo has no enrollment file."""
    import numpy as np

    return list(np.random.default_rng(seed).normal(size=size))

# ---------------------------------------------------------------------------
# 1. Database isolation -- must happen before the app is imported
# ---------------------------------------------------------------------------
TMP_ROOT = Path(tempfile.mkdtemp(prefix="attendance_security_tests_"))
PRISTINE_DB = LIVE_DB.read_bytes()
DB_PATH = TMP_ROOT / "times.db"
DB_PATH.write_bytes(PRISTINE_DB)


REFS_DIR = TMP_ROOT / "refs"
PHOTOS_DIR = TMP_ROOT / "photos"
REFS_DIR.mkdir()
PHOTOS_DIR.mkdir()

#: What ``DeepFace.represent(model_name="VGG-Face")`` actually returns. It was 2622
#: here, which no VGG-Face embedding is - so the search below never matched a real
#: template, every seeded vector was the synthetic fallback, and the mismatch was
#: invisible. A wrong dimension is exactly how a wrong *write* would go unnoticed too.
_VGG_EMBEDDING_DIM = 4096


def _reference_embedding() -> list[float]:
    """A real VGG-Face embedding, copied from the live repo when there is one.

    Any live template will do. DeepFace is stubbed, so what the tests need from this
    vector is that it is a genuine 2622-float embedding, not that it belongs to user 1 -
    and the live files may since have been renamed to their account's biometric id (see
    ``biometrics``), so the search is by shape rather than by name. Read-only either way.
    """
    for candidate in [LIVE_REFS / "1.json", *sorted(LIVE_REFS.glob("*.json"))]:
        try:
            values = json.loads(candidate.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(values, list) and len(values) == _VGG_EMBEDDING_DIM:
            return [float(value) for value in values]
    return [float(value) for value in _deterministic_vector(_VGG_EMBEDDING_DIM, seed=7)]


# The fixed application reads its configuration from the environment; give the
# tests a signing secret and guarantee the Twilio variables stay unset so the
# "no baked-in credentials" test is meaningful.
os.environ["DATABASE_PATH"] = str(DB_PATH)
os.environ.setdefault("SECRET_KEY", "test-only-secret-key-never-used-in-production-0123456789abcdef")
for _twilio_var in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_WHATSAPP_FROM", "ADMIN_WHATSAPP_TO"):
    os.environ.pop(_twilio_var, None)

# The overtime watcher is a daemon thread that writes to the database on a timer. In a
# test run that means notifications appearing mid-test for sessions a test just planted,
# which turns deterministic assertions into races - so it stays off and the tests call
# ``overtime.scan_overtime()`` directly instead.
os.environ["OVERTIME_WATCHER_ENABLED"] = "0"
# The retention sweeper is the same shape of hazard, with a worse failure mode: it deletes
# files. It starts from the app's lifespan, so leaving it on would have a test run sweeping the
# throwaway biometric directories on a timer while other tests are asserting on them. The
# retention suite starts it deliberately, with ``enabled=True``, for the two tests that are
# about the timer itself.
os.environ["RETENTION_ENABLED"] = "0"
# Liveness is exercised with an injected fake ONNX session; leaving the mode at the
# shipped default (advisory) means a test that does not opt in is never blocked by an
# absent model, exactly like a real deployment that has not installed one yet.
os.environ.pop("LIVENESS_MODE", None)
os.environ.pop("ENROLLMENT_LIVENESS_MODE", None)

os.chdir(TMP_ROOT)


def _cleanup() -> None:
    os.chdir(BACKEND_DIR)
    shutil.rmtree(TMP_ROOT, ignore_errors=True)


atexit.register(_cleanup)


# ---------------------------------------------------------------------------
# 2. Deterministic DeepFace stub (no TensorFlow, no model weights)
# ---------------------------------------------------------------------------
def _cosine_similar_vector(reference, target_cosine: float) -> list[float]:
    import numpy as np

    ref = np.asarray(reference, dtype=float)
    ref_unit = ref / np.linalg.norm(ref)
    probe = np.random.default_rng(20260912).normal(size=ref.shape)
    probe -= probe.dot(ref_unit) * ref_unit
    probe /= np.linalg.norm(probe)
    return list(target_cosine * ref_unit + math.sqrt(max(0.0, 1.0 - target_cosine**2)) * probe)


class FakeDeepFace:
    """Stand-in for ``deepface.DeepFace`` with a controllable outcome.

    ``FACE_MODE``:

    * ``"match"``    returns the enrolled embedding verbatim -> distance ~0.00 -> approved
    * ``"review"``   returns a vector at cosine similarity 0.5 -> distance ~0.50 -> pending_review
    * ``"mismatch"`` returns the negated embedding -> distance ~2.00 -> face rejected
    * ``"none"``     raises ValueError, exactly like a frame with no detectable face

    ``FACE_COUNT`` is how many "faces" the detector reports; >1 triggers the
    multi-face guards in ``compare_faces_sync`` and ``/admin/enroll``.
    """

    FACE_MODE = "match"
    FACE_COUNT = 1

    def build_model(self, *args, **kwargs):
        return None

    def extract_faces(self, img_path=None, **kwargs):
        """Detector only: no embedding, no reference comparison.

        This is the half of the API a quick clock link uses, where a face has to be
        *present* but is deliberately not matched against anybody's template - so the stub
        answers "how many faces" and nothing else. ``"none"`` returns an empty list, which is
        what a real detector in ``enforce_detection=False`` mode does, and the key is
        ``confidence`` - the key ``extract_faces`` really returns (``represent`` calls it
        ``face_confidence``).
        """
        if self.FACE_MODE == "none":
            return []
        return [
            {"face": None, "facial_area": {"x": 0, "y": 0, "w": 10, "h": 10}, "confidence": 0.99}
            for _ in range(self.FACE_COUNT)
        ]

    def represent(self, img_path=None, **kwargs):
        if self.FACE_MODE == "none":
            raise ValueError("No face detected (stubbed)")
        # Wherever the template really is: the id-named file once the application has
        # booted and adopted it, the legacy name before that. Read through the same
        # resolver the application reads through, never by constructing the name here.
        #
        # With a fallback to the seeded vector, because deleting an account's template is
        # itself something the suite tests: a stub that could only answer by reading a file
        # under test would fail the test it is there to serve.
        try:
            reference = json.loads(reference_path(WORKER).read_text())
        except OSError:
            reference = _reference_embedding()
        if self.FACE_MODE == "mismatch":
            vector = [-value for value in reference]
        elif self.FACE_MODE == "review":
            vector = _cosine_similar_vector(reference, 0.5)
        else:
            vector = list(reference)
        return [{"embedding": vector, "face_confidence": 0.99} for _ in range(self.FACE_COUNT)]


FAKE_FACE = FakeDeepFace()
_fake_deepface = types.ModuleType("deepface")
_fake_deepface.DeepFace = FAKE_FACE
sys.modules["deepface"] = _fake_deepface


# ---------------------------------------------------------------------------
# 3. Outbound network guard -- no real Twilio call may ever leave a test run
# ---------------------------------------------------------------------------
class _FakeHttpResponse:
    status_code = 201
    text = '{"sid": "SM_test_stub"}'

    def json(self):
        return {"sid": "SM_test_stub"}


class OutboundRecorder:
    """Records (and refuses to perform) outbound HTTP from the application."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, url, *args, **kwargs):
        self.calls.append({"url": str(url), "args": args, "kwargs": kwargs})
        return _FakeHttpResponse()

    def urls(self) -> list[str]:
        return [call["url"] for call in self.calls]


OUTBOUND = OutboundRecorder()


def install_outbound_guard() -> None:
    import requests

    requests.post = OUTBOUND  # type: ignore[assignment]
    requests.get = OUTBOUND  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# 4. Known seed state (temp database only)
# ---------------------------------------------------------------------------
WORKER = "1"
MOALLEM = "600"
ADMIN = "1000"
HEAD_ADMIN = "5000"

#: id -> (name, role, password, login email)
SEED_USERS: dict[str, tuple[str, str, str, str]] = {
    WORKER: ("Seed Worker", "worker", "worker-pass-123", "seed1@example.test"),
    MOALLEM: ("Seed Lead Worker", "moallem", "moallem-pass-123", "seed600@example.test"),
    ADMIN: ("Seed Admin", "admin", "admin-pass-123", "seed1000@example.test"),
    HEAD_ADMIN: ("Seed Head Admin", "head_admin", "head-pass-123", "seed5000@example.test"),
}
#: Tables that hold **records of what people did**, as opposed to the configuration this suite
#: seeds itself. Every one is emptied in the throwaway copy before each test.
#:
#: This is not tidiness. The copy is a photograph of a database that is **in use**, so this
#: morning's real punch, a real quick link and a real late-arrival notification are all in it -
#: and a test that asserts "exactly one late arrival", "this account has never had its password
#: reset" or "the audit log names these two actions" then fails on somebody else's traffic. The
#: failure looks like a regression in the feature under test, which is the expensive part: it
#: sends you looking at code that is fine, and it trains people to ignore a red suite. Clearing
#: the activity is what makes the copy a fixture.
#:
#: ``test_fixture_state.py`` fails if the schema grows a table that is in none of these lists,
#: so this cannot silently fall behind the application.
ACTIVITY_TABLES: Final = (
    "attendance_logs",          # punches, approved hours: a real shift is a real row
    "active_sessions",          # who is on site right now
    "admin_notifications",      # the late arrivals, the reviews, the retention reports
    "audit_log",                # every admin action, *and* the source of the roster's
                                # ``password_changed_at`` and of the "was this audited" tests
    "quick_links",              # a live punch link is a working credential
    "quick_link_uses",
    "worker_notes",             # notes are people talking to each other
    "worker_note_messages",
    "worker_devices",           # enrolled phones and their HMAC keys
    "device_anchors",
    "punch_queue",              # offline punches waiting to be replayed
    "retention_runs",
    "enrollment_invites",       # a live invite is a credential too
    "enrollment_jobs",
    "enrollment_job_items",
)

#: Tables left exactly as the live database has them: the schema ledger, and nothing else. The
#: ledger is *kept* on purpose - it is what tells ``init_db`` which migrations this generation
#: has already applied, and re-running them all is not the same thing as resuming.
CONFIGURATION_TABLES: Final = ("schema_migrations",)

#: Tables ``seed_database`` rewrites from scratch on every test, so their live contents never
#: reach an assertion: the roster, the sites, and the shift rules a punch is judged by.
SEEDED_TABLES: Final = ("users", "construction_sites", "shift_rules")

#: The biometric id every seeded account is given, minted once for the session.
#:
#: The database is restored from a snapshot and reseeded before every test, and the reseed
#: is an ``INSERT OR REPLACE`` - which deletes the row and inserts a new one, so every
#: column the insert does not name falls back to its default. An id taken from the snapshot
#: would therefore vanish on the first reseed and orphan the template files the harness had
#: already written under it (which is precisely how this was found: seeded faces resolved
#: to ``<id>.json`` files that no longer existed, and every enrollment answered 500). One
#: value, used by both the row and the filename, and independent of how old the snapshot
#: is - the harness can run against a live database that has never seen this migration.
SEED_BIOMETRIC_IDS: dict[str, str] = {user_id: uuid.uuid4().hex for user_id in SEED_USERS}

PASSWORDS = {user_id: spec[2] for user_id, spec in SEED_USERS.items()}
EMAILS = {user_id: spec[3] for user_id, spec in SEED_USERS.items()}
ROLES = {user_id: spec[1] for user_id, spec in SEED_USERS.items()}


def reference_path(user_id: str) -> Path:
    """The template file ``/attendance/verify`` will score this account against.

    The name is the account's immutable biometric id, not the account id, so a test
    cannot build it by hand: ask ``biometrics`` - the module the application itself asks
    - and it answers with whichever name is really on disk (the id-named file, or a
    legacy-named one it still reads while an upgrade catches up).

    Seeded accounts are the exception, and deliberately so: their id is
    ``SEED_BIOMETRIC_IDS``, which is also what ``seed_database`` writes into the row, so
    the name stays the same before the first reseed as after it.

    Seeded names are built from ``REFS_DIR`` rather than from the application. At *import*
    time the application still points at the live directories (``conftest`` redirects them
    later, per test), so asking ``biometrics`` before then would put a template in a real
    worker's directory - which is exactly what a first attempt at this did.
    """
    import biometrics

    seeded = SEED_BIOMETRIC_IDS.get(str(user_id))
    if seeded:
        return REFS_DIR / f"{seeded}.json"
    return Path(biometrics.resolve_reference(str(user_id)))


def reference_text() -> str:
    """The template vector every seeded user carries (one vector: DeepFace is stubbed)."""
    return json.dumps(_reference_embedding())


def seed_reference(user_id: str, text: str | None = None) -> Path:
    """Write a template for ``user_id`` where the application will read it."""
    target = reference_path(user_id)
    if REFS_DIR.resolve() not in target.resolve().parents:
        # The one thing this harness must never do is put a face in the live directory -
        # safety rule 2 in the module docstring. A name resolved through the application
        # before ``conftest`` has redirected it would do exactly that, silently.
        raise RuntimeError(
            f"refusing to seed a biometric template outside the throwaway directory: {target}"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text if text is not None else reference_text())
    return target


def template_exists(user_id: str) -> bool:
    """Whether a template can be read for this account, under either name.

    Preferred over a path assertion: "no template" has to mean *neither* name, or a
    legacy file left on disk would read as an enrolled worker.
    """
    import biometrics

    return biometrics.is_enrolled(user_id)


def stored_photo_path(user_id: str) -> Path | None:
    """The reference selfie kept beside this account's row, or ``None``."""
    import biometrics

    resolved = biometrics.resolve_photo(user_id)
    return Path(resolved) if resolved else None


def write_enrollment_templates() -> None:
    """Give every seeded user an enrolled template.

    ``/attendance/verify`` refuses to run without a stored reference (404
    "Facial reference not registered"), so without this the workflow tests would
    fail for a reason that has nothing to do with security. All templates share
    one vector because DeepFace is stubbed.

    Written under whatever name the account really uses: seeding the account-id name for
    an account that already has an id would leave the id-named path - the one every punch
    resolves through - untested.
    """
    for user_id in SEED_USERS:
        seed_reference(user_id)


write_enrollment_templates()

#: A flagged record with a deterministic id, so approval tests never guess.
SEEDED_PENDING_LOG_ID = 900001

SITES = {
    "Downtown Tower A": (30.05, 31.23, 65.0),
    "New Capital Zone B": (29.98, 31.75, 100.0),
}
#: A short, fixed attendance history: two completed shifts for the seeded worker, on two
#: dates in March 2026 - inside the reports' widest period ("2026-01-01" to "2026-12-31") and
#: outside "this month", so a report that defaults to the current month cannot pick them up by
#: accident and a report that asks for the year always finds them.
#:
#: It exists because the report suite is *about* history: the timesheet, the CSV export and the
#: attendance rate are assertions on rows nobody had ever seeded. Until this, the only rows they
#: saw were the live deployment's, so "the report must not be empty" was really asserting that
#: somebody had been clocking in at work this month - which is true some weeks and false others.
#:
#: Only :data:`WORKER` has history, and that is deliberate: ``test_auth_token_contract`` asserts
#: the lead worker starts with *no* attendance rows, so adding any here would break a different
#: invariant to fix this one.
SEEDED_HISTORY_DAYS: Final = ("2026-03-02", "2026-03-09")
SEEDED_HISTORY_HOURS: Final = 8.0

#: Per-site clock-in windows, seeded only when a test asks for them (``SEED_SITE_WINDOWS``).
#: Both seeded sites inherit the global rule by default, which is what keeps the shift-window
#: tests that predate per-site windows measuring the global rule they were written against.
DOWNTOWN = "Downtown Tower A"
ZONE_B = "New Capital Zone B"

#: ``site_name -> (clock_in_window_start, clock_in_window_end, site_timezone)``, any of which
#: may be ``None`` to leave that field inheriting. Applied by ``seed_database``, so a test can
#: put a night shift on a site for one scenario and have the next test start clean.
SEED_SITE_WINDOWS: dict[str, tuple[str | None, str | None, str | None]] = {}
INSIDE_DOWNTOWN = "30.05,31.23"      # inside exactly one seeded site
INSIDE_ZONE_B = "29.98,31.75"        # inside the *other* seeded site, for per-site rules
OUTSIDE_ALL_SITES = "51.5074,-0.1278"  # London: inside no geofence
IMPLAUSIBLE_COORDINATES = "0,0"      # the classic fixed mock-GPS reading


# ---------------------------------------------------------------------------
# 5. Database helpers
# ---------------------------------------------------------------------------
def seed_database(app_module) -> None:
    """Put the throwaway database into a deterministic state.

    Only the temp copy is touched. User rows are rewritten so tests never depend
    on whichever credentials the live server happens to have, one in-progress
    session is created so clock-out tests are stable, and one ``pending_review``
    record is seeded under a fixed id so approval tests can address it exactly.
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    user_rows = []
    for user_id, (name, role, password, email) in SEED_USERS.items():
        password_hash = app_module.pwd_context.hash(password)
        # The biometric id is named explicitly because REPLACE drops every column this
        # insert does not mention - including that one, which names the files holding the
        # account's face (see SEED_BIOMETRIC_IDS).
        user_rows.append(
            (user_id, name, email, "", password_hash, role, SEED_BIOMETRIC_IDS[user_id])
        )
        # Today's clock-in path authenticates from a module-level dict rather
        # than the database; keep it coherent with what we just wrote.
        cache = getattr(app_module, "USER_CACHE", None)
        if isinstance(cache, dict):
            cache[user_id] = password_hash

    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO users (id, name, email, phone, password_hash, role, "
            "biometric_id) VALUES (?,?,?,?,?,?,?)",
            user_rows,
        )
        conn.execute("DELETE FROM construction_sites")
        conn.executemany(
            "INSERT INTO construction_sites "
            "(site_name, lat, lon, radius, clock_in_window_start, clock_in_window_end, site_timezone) "
            "VALUES (?,?,?,?,?,?,?)",
            [
                (name, lat, lon, radius, *SEED_SITE_WINDOWS.get(name, (None, None, None)))
                for name, (lat, lon, radius) in SITES.items()
            ],
        )
        conn.execute("DELETE FROM active_sessions")
        conn.execute(
            "INSERT INTO active_sessions (worker_id, site_name, clock_in_time) VALUES (?,?,?)",
            (WORKER, "Downtown Tower A", now),
        )
        # Normalise flagged records: the live database may already contain its own
        # pending_review rows, and the tests assert on exact counts.
        conn.execute(
            "DELETE FROM attendance_logs WHERE status = 'pending_review' AND worker_id IN (%s)"
            % ",".join("?" * len(SEED_USERS)),
            tuple(SEED_USERS),
        )
        conn.execute(
            "INSERT INTO attendance_logs (id, worker_id, site_name, action, timestamp, hours, score, status, status_code) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (SEEDED_PENDING_LOG_ID, WORKER, "Downtown Tower A", "Clock In", now, 0.0, 0.5, "pending_review", "pending_review"),
        )
        # The completed shifts the reports read (see ``SEEDED_HISTORY_DAYS``): a clock-in and a
        # clock-out per day, approved, with the paid hours recorded on the clock-out the way the
        # punch path records them. The timesheet selects ``action = 'Clock Out'``, so a day with
        # only a clock-in would be a shift the report never shows.
        history = []
        for day in SEEDED_HISTORY_DAYS:
            history.append(
                (WORKER, "Downtown Tower A", "Clock In", f"{day} 05:00:00", 0.0, 0.9, "Approved", "approved")
            )
            history.append(
                (WORKER, "Downtown Tower A", "Clock Out", f"{day} 13:00:00", SEEDED_HISTORY_HOURS, 0.9,
                 "Approved", "approved")
            )
        conn.executemany(
            "INSERT INTO attendance_logs (worker_id, site_name, action, timestamp, hours, score, "
            "status, status_code, source, approved_hours) VALUES (?,?,?,?,?,?,?,?,'online',?)",
            [
                (*row, SEEDED_HISTORY_HOURS if row[2] == "Clock Out" else None)
                for row in history
            ],
        )
        # The company rules are normalised as well, and for the same reason as the rows above:
        # they are configuration a real administrator can now change from the console (Admin ->
        # Shift rules, including the company clock-in window), so a test that asserts "an
        # inheriting site is late at noon" has to be asserting the *shipped* window rather than
        # whatever shift the company moved to this week. Only the documented keys are touched -
        # a column this build does not know about is left where it is.
        defaults = getattr(app_module, "DEFAULT_SHIFT_RULES", None) or {}
        if defaults:
            if conn.execute("SELECT id FROM shift_rules WHERE id = 1").fetchone() is None:
                conn.execute(
                    "INSERT INTO shift_rules (id, %s, updated_at) VALUES (1, %s, ?)"
                    % (", ".join(defaults), ", ".join("?" * len(defaults))),
                    (*defaults.values(), now),
                )
            else:
                conn.execute(
                    "UPDATE shift_rules SET %s, updated_at = ? WHERE id = 1"
                    % ", ".join(f"{key} = ?" for key in defaults),
                    (*defaults.values(), now),
                )
        conn.commit()
    finally:
        conn.close()


def clear_activity() -> None:
    """Empty every activity table in the throwaway copy (see ``ACTIVITY_TABLES``).

    Called on every reset, *after* ``init_db`` (so a table this build knows about exists even
    when the snapshot predates it) and *before* ``seed_database`` (which writes the one seeded
    session and the one ``pending_review`` record the tests address by id).

    A table that is missing from the snapshot is skipped rather than fatal: the copy is whatever
    generation the live database happened to be, and the suite's job is to run against the
    application as it is, not to require the live file to be current.

    ``audit_log`` cannot simply be deleted from: it is append-only and the *database* enforces
    it (migration 1 installs a ``BEFORE DELETE`` trigger), so every delete aborts. That guard is
    there for the right reason and this is not a reason to weaken it - retention's own rule
    applies here too: read the trigger out of ``sqlite_master``, drop it, delete, and put the
    same text back, all in one transaction, so a crash in the middle leaves the guard on.
    """
    import retention  # imported here so this module stays importable without the backend path

    connection = sqlite3.connect(str(DB_PATH), timeout=30.0, isolation_level=None)
    try:
        connection.execute("BEGIN IMMEDIATE")
        guard = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?",
            (retention.AUDIT_DELETE_GUARD,),
        ).fetchone()
        guard_sql = str(guard[0]) if guard is not None and guard[0] else None
        if guard_sql:
            connection.execute(f"DROP TRIGGER IF EXISTS {retention.AUDIT_DELETE_GUARD}")
        try:
            for table in ACTIVITY_TABLES:
                try:
                    connection.execute(f"DELETE FROM {table}")
                except sqlite3.OperationalError:
                    continue
        finally:
            if guard_sql:
                connection.execute(guard_sql)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _restore_pristine() -> None:
    """Replace the working database with the pristine snapshot, deterministically.

    Three things have to happen in this order, and the order is the whole point:

    1. fold any write-ahead log into the main file and leave WAL mode, so that
       ``journal_mode`` is ``delete`` and no ``-wal``/``-shm`` sidecar exists;
    2. overwrite the file with the pristine bytes;
    3. prove the reset actually happened.

    Skipping step 1 is a subtle, non-deterministic disaster: a leftover ``-wal``
    from the previous generation can still be replayed onto the restored file if
    the two happen to share a file change counter. The database then reports
    ``schema_migrations`` as fully applied while the pages that carry the migrated
    columns are the pristine ones, so the schema looks current and is not.
    """
    connection = sqlite3.connect(str(DB_PATH), timeout=30.0, isolation_level=None)
    try:
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        mode = connection.execute("PRAGMA journal_mode = DELETE").fetchone()[0]
    finally:
        connection.close()

    if str(mode).lower() != "delete":
        raise RuntimeError(
            f"the test database is still in {mode!r} journal mode, so it cannot be restored "
            "safely; a connection to it is probably still open"
        )

    for sidecar in ("-wal", "-shm"):
        stale = Path(str(DB_PATH) + sidecar)
        if stale.exists():
            stale.unlink()
    DB_PATH.write_bytes(PRISTINE_DB)

    # Verify by content, not by a proxy. This used to assert that 'schema_migrations' was
    # absent, which silently assumed the live database was always the *pre*-migration
    # prototype. The moment the live database itself was migrated, that assumption made
    # the check fail on a perfectly good restore and every test errored in setup. Comparing
    # the restored bytes against the snapshot is stronger and independent of which
    # generation the snapshot is from.
    expected_digest = hashlib.sha256(PRISTINE_DB).hexdigest()
    restored_digest = hashlib.sha256(DB_PATH.read_bytes()).hexdigest()
    if restored_digest != expected_digest:
        raise RuntimeError(
            "the test database was not restored cleanly: the file on disk does not match the "
            f"pristine snapshot ({restored_digest[:12]} != {expected_digest[:12]}), so every "
            "following test would run against a half-restored database"
        )


def assert_database_isolation() -> None:
    """Fail loudly if the application under test is pointed at the live database.

    Every safety property of this suite rests on one thing: the app talks to a throwaway
    copy. If ``DATABASE_PATH`` is lost or overridden - by a stray ``monkeypatch.delenv``,
    a future refactor, or a runner that does not inherit the environment - the tests would
    start mutating real payroll data and the first symptom would be silent. So the
    resolution is asserted before any test runs instead of assumed.
    """
    import config

    resolved = Path(config.settings.database_path).resolve()
    if resolved != DB_PATH.resolve():
        raise RuntimeError(
            f"the application under test resolved its database to {resolved} instead of the "
            f"throwaway copy {DB_PATH}. Refusing to run: this suite must never touch live data."
        )


@contextmanager
def with_site_windows(app_module, windows: dict[str, tuple[str | None, str | None, str | None]]):
    """Seed per-site clock-in windows for the duration of one test, then restore the default.

    A context manager rather than a fixture argument because the reseed has to happen
    *after* the mapping is set: ``deterministic_state`` has already re-seeded the database by
    the time a test body runs, so a test that assigned ``SEED_SITE_WINDOWS`` would otherwise
    be measuring the previous contents of that dict. Restoration is in a ``finally`` so one
    failing test cannot leave a night shift on a site for every test after it - the shared
    database is exactly the thing that makes that failure mode contagious.
    """
    previous = dict(SEED_SITE_WINDOWS)
    SEED_SITE_WINDOWS.clear()
    SEED_SITE_WINDOWS.update(windows)
    try:
        seed_database(app_module)
        yield SEED_SITE_WINDOWS
    finally:
        SEED_SITE_WINDOWS.clear()
        SEED_SITE_WINDOWS.update(previous)


def reset_database(app_module) -> None:
    """Restore the pristine snapshot, migrate it, and re-seed. Called before every test.

    The migrations matter: the snapshot is the *pre-remediation* live database, so
    it has none of the columns the additive migrations introduce. Re-running
    ``init_db()`` here is what keeps every test running against the same schema the
    application ships, exactly as a real deployment would.

    Between the two, every activity table is emptied (``clear_activity``): the snapshot is a
    live database, and the person using this application writes rows into it all day. See
    ``ACTIVITY_TABLES`` - the short version is that the clone gives the fixture its **shape and
    its configuration**, and the tests give it their data.
    """
    _restore_pristine()
    app_module.init_db()
    clear_activity()
    seed_database(app_module)
    reset_rate_limits(app_module)
    FAKE_FACE.FACE_MODE = "match"
    FAKE_FACE.FACE_COUNT = 1


def reset_rate_limits(app_module) -> None:
    """Clear slowapi buckets so one test's burst cannot 429 the next one."""
    limiter = getattr(app_module, "limiter", None)
    for attribute in ("reset", "clear"):
        method = getattr(limiter, attribute, None)
        if callable(method):
            try:
                method()
            except Exception:  # pragma: no cover - best effort only
                pass


def db_scalar(sql: str, params: tuple = ()):
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute(sql, params).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def db_rows(sql: str, params: tuple = ()):
    conn = sqlite3.connect(DB_PATH)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 6. Credential helpers
# ---------------------------------------------------------------------------
def token_version(user_id: str) -> int:
    """The user's current token version, read from the database.

    Mirrors the claim the application mints into a real token: a rotation bumps
    this value, which is what revokes the tokens issued before it.
    """
    try:
        value = db_scalar("SELECT COALESCE(token_version, 0) FROM users WHERE id = ?", (user_id,))
    except sqlite3.Error:  # column not present yet in a pre-migration snapshot
        return 0
    return int(value or 0)


def bearer(user_id: str, *, expired: bool = False, secret: str | None = None, role: str | None = None) -> dict[str, str]:
    """An ``Authorization`` header for a caller.

    The server ignores this header today -- which is exactly what the red tests
    demonstrate (a caller needs *no* credential at all). Once the app issues and
    validates real JWTs, this mints a genuine HS256 token *including the ``ver``
    claim the app itself emits*, so a token minted before a password rotation is
    distinguishable from one minted after it.
    """
    import jwt as pyjwt

    now = int(time.time())
    payload = {
        "sub": user_id,
        "role": role or ROLES.get(user_id, "worker"),
        "ver": token_version(user_id),
        "iat": now - (7200 if expired else 0),
        "exp": now - 60 if expired else now + 12 * 3600,
        "jti": uuid.uuid4().hex,
    }
    token = pyjwt.encode(payload, secret or os.environ["SECRET_KEY"], algorithm="HS256")
    return {"Authorization": f"Bearer {token}"}


def token_signed_with_a_different_key(user_id: str = WORKER, role: str = "head_admin") -> str:
    import jwt as pyjwt

    payload = {"sub": user_id, "role": role, "iat": int(time.time()), "exp": int(time.time()) + 3600}
    return pyjwt.encode(payload, "wrong-key-the-attacker-chose-instead-0123456789", algorithm="HS256")


def unsigned_token(user_id: str, role: str = "head_admin") -> str:
    """A well-formed ``alg: none`` token -- the classic JWT forgery attempt."""

    def segment(data: dict) -> str:
        raw = json.dumps(data, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    header = segment({"alg": "none", "typ": "JWT"})
    payload = segment({"sub": user_id, "role": role, "iat": int(time.time()), "exp": int(time.time()) + 3600})
    return f"{header}.{payload}."


def assert_denied(response, *, endpoint: str, detail: str) -> None:
    """An unauthorised caller must not be able to act.

    Only 401 (no valid credential) and 403 (authenticated but not permitted) are
    acceptable answers. Note what anything else means:

    * ``200`` -- the endpoint performed the privileged action for an
      unauthorised caller: the actual vulnerability.
    * ``422`` -- authorization is being expressed as *payload validation* (the
      caller forgot to claim ``admin_id``), not as a credential check. That is
      not a control: whoever sends the field passes.
    * ``400``/``404``/``500`` -- incidental state, not an authorization decision.
    """
    assert response.status_code in (401, 403), (
        f"SECURITY GAP at {endpoint}: {detail}. "
        f"Expected 401/403 but got {response.status_code}: {response.text[:200]!r}"
    )


# ---------------------------------------------------------------------------
# 7. Request helpers
# ---------------------------------------------------------------------------
@lru_cache(maxsize=1)
def jpeg_bytes() -> bytes:
    """A synthetic image; real face content is irrelevant because DeepFace is stubbed."""
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (240, 240), (128, 132, 136)).save(buffer, format="JPEG")
    return buffer.getvalue()


def clock_in(
    client,
    user_id: str,
    *,
    action: str = "Clock In",
    coordinates: str = INSIDE_DOWNTOWN,
    headers: dict | None = None,
    image: bytes | None = None,
):
    """One punch. No password: the bearer token is the credential.

    The form used to carry the worker's password as well, and this helper used to be
    where every test typed it. ``/attendance/verify`` no longer takes one, so a helper
    that still sent it would be describing a field the server ignores - and a suite that
    passes whatever it likes in an ignored field stops being evidence of anything.
    ``user_id`` stays because the endpoint still echoes it and still refuses a subject
    that is not the token's owner.
    """
    return client.post(
        "/api/v1/attendance/verify",
        data={
            "worker_id": user_id,
            "action": action,
            "location_input": coordinates,
        },
        files={"selfie": ("selfie.jpg", image if image is not None else jpeg_bytes(), "image/jpeg")},
        headers=headers or {},
    )


def enroll(client, user_id: str = WORKER, *, headers: dict | None = None, image: bytes | None = None):
    return client.post(
        "/api/v1/admin/enroll",
        data={"worker_id": user_id},
        files={"photo": ("photo.jpg", image if image is not None else jpeg_bytes(), "image/jpeg")},
        headers=headers or {},
    )


def send(client, method: str, path: str, *, headers: dict | None = None, json_body=None, data=None):
    return client.request(method, path, headers=headers or {}, json=json_body, data=data)


def load_second_app_instance():
    """Import ``main.py`` a second time, simulating a second ASGI worker process.

    Two uvicorn workers share one SQLite file but each keeps its own module-level
    state (today: the ``USER_CACHE`` password dictionary). Importing runs no database
    work - ``init_db()`` moved into the app's ``lifespan``, and the old import-time
    ``enforce_11h_cutoff()`` is gone - so the second instance only has to be pointed at
    the same reference/photo directories the first one uses.
    """
    import importlib.util

    name = f"main_second_worker_{uuid.uuid4().hex[:8]}"
    spec = importlib.util.spec_from_file_location(name, MAIN_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.LOCAL_REFS_DIR = str(REFS_DIR)
    module.WORKER_PHOTOS_DIR = str(PHOTOS_DIR)
    return module


APP_IMPORT_PROBE = (
    "import sys, types;"
    "m = types.ModuleType('deepface');"
    "D = type('D', (), {'build_model': staticmethod(lambda *a, **k: None),"
    " 'represent': staticmethod(lambda *a, **k: [])});"
    "m.DeepFace = D();"
    "sys.modules['deepface'] = m;"
    "import main;"
    "print('APP_IMPORTED_OK')"
)


def import_app_in_subprocess(*, secret_key: str | None) -> subprocess.CompletedProcess:
    """Import the app in a clean interpreter to observe fail-closed behaviour."""
    env = {key: value for key, value in os.environ.items() if key != "SECRET_KEY"}
    if secret_key is not None:
        env["SECRET_KEY"] = secret_key
    env["PYTHONPATH"] = str(BACKEND_DIR)
    env["PYTHONIOENCODING"] = "utf-8"
    # Point the probe at a .env that does not exist. Without this the test measures the
    # developer's machine rather than the application: a perfectly correct `backend/../.env`
    # supplies a real key, the probe starts, and the "no signing secret" assertion fails on
    # a working installation. The property under test is "no configuration source at all",
    # so every source has to be removed, not just the environment variable.
    env["ENV_FILE"] = str(TMP_ROOT / "absent-for-this-probe.env")
    return subprocess.run(
        [sys.executable, "-c", APP_IMPORT_PROBE],
        cwd=str(TMP_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
