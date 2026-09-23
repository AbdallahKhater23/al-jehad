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
1. The suite runs with the process's working directory inside a throwaway temp
   directory that holds a COPY of the live ``times.db``. That has to be true *before*
   the app is imported: importing it runs ``init_db()`` and ``enforce_11h_cutoff()``,
   and today ``init_db()`` rewrites an existing user's password hash, so importing
   against the real working directory would corrupt real data. The live database is
   never written by this suite.

   The directory is entered by ``enter_temp_root()`` rather than at this module's
   import, and *that timing is load-bearing under ``pytest -n auto``*: pytest-xdist
   imports this module in every worker and then has that worker collect nodeids like
   ``tests/test_x.py::test_y``, resolved against the worker's working directory. A
   ``chdir`` here runs before that collection, so every nodeid pointed somewhere the
   temp directory does not have - each worker collected nothing and the whole suite
   reported "no tests ran" after three seconds, with no error to read. The suite calls
   ``enter_temp_root()`` from its session fixture (still before the first test body and
   still before ``main`` is imported) and the two tools below call it explicitly.
2. ``LOCAL_REFS_DIR``, ``WORKER_PHOTOS_DIR``, ``punch_frames.FRAMES_DIR``,
   ``quick_links.PHOTOS_DIR`` and ``corpus.ROOT_DIR`` are all redirected into the temp directory, so enrollment
   tests cannot overwrite real biometric files and a punch cannot file a worker's face under
   the checkout - see ``FILE_TREES`` for the table that keeps that list complete.
3. The embedding engine (``face_onnx``) and the detector (``face_detector``) are replaced by
   in-process stubs before import: the suite must not open an ONNX graph, run a real detector,
   or depend on ML timing. There is no ``deepface`` or ``tensorflow`` left to stub - the
   application imports neither, and a stub that still pretended to be one would hide the
   return of the dependency it was there to prevent.
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
import importlib
import io
import itertools
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

#: Every test gets its own database *file*, and the reset that installs it never has to
#: touch a file anything else has open. The previous design overwrote one fixed path in
#: place, which forced the reset to take the database's exclusive lock (the WAL fold and the
#: ``journal_mode = DELETE`` flip): a browser test's live uvicorn thread still serving the
#: previous test, a leaked connection after a failure, or another pytest process in this
#: checkout all held that lock - and ``reset_database`` died with "database is locked" in
#: fixture setup, failing tests that had not started yet. Two browser suites run together
#: reproduced it roughly every other run.
#:
#: So the database lives at ``CURRENT_DIR / "times.db"`` where ``CURRENT_DIR`` is a
#: directory junction (a symlink on POSIX) that each reset *repoints* at a fresh generation
#: directory built from the pristine snapshot. Building the new generation touches only
#: files nobody has ever opened, and repointing the link touches only the link - so no step
#: of a reset ever needs a lock on a database file. Connections still open on the previous
#: generation keep reading it harmlessly: that snapshot is never written again, so a
#: straggler's writes land in a directory that is about to be garbage and cannot corrupt
#: anything the next test sees. ``DB_PATH`` stays a stable path, so ``os.environ
#: ["DATABASE_PATH"]``, every ``harness.DB_PATH`` import, and ``config.settings.database_path``
#: keep working without per-module repointing - opening by this path always reaches the
#: current generation.
GENERATIONS_DIR = TMP_ROOT / "generations"
GENERATIONS_DIR.mkdir()
CURRENT_DIR = TMP_ROOT / "current"
DB_PATH = CURRENT_DIR / "times.db"
_GENERATION = itertools.count(1)

#: Every module-level directory the application writes files into, the subdirectory of a
#: generation that stands in for it, and the environment variable ``config`` reads it from:
#: ``(module, attribute, directory, env_var)``.
#:
#: One table, because the same names drive four things that must not be allowed to disagree -
#: what ``_write_generation`` creates, what ``redirect_file_directories`` points *this* process
#: at, what ``publish_file_directories`` puts in the environment for *child* processes, and what
#: a test can iterate to prove all three happened. A file tree added to the application is a
#: one-line change here, and ``test_fixture_state`` fails until it is made, in the same spirit
#: as the activity/config/seeded table lists in that suite.
#:
#: ``env_var`` is spelled out rather than derived from ``attribute`` because the two are read by
#: different audiences: the attribute is this repository's name for a variable, the environment
#: variable is what an operator - and a child process - has to type, so ``punch_frames.FRAMES_DIR``
#: is published as ``PUNCH_FRAMES_DIR``.
FILE_TREES: Final = (
    ("main", "LOCAL_REFS_DIR", "refs", "LOCAL_REFS_DIR"),
    ("main", "WORKER_PHOTOS_DIR", "photos", "WORKER_PHOTOS_DIR"),
    ("punch_frames", "FRAMES_DIR", "frames", "PUNCH_FRAMES_DIR"),
    ("quick_links", "PHOTOS_DIR", "quick_link_photos", "QUICK_LINK_PHOTOS_DIR"),
    # The calibration corpus (``corpus``): the same treatment as the other four, and for a sharper
    # reason - these are faces kept for *measurement*, and a suite that stored them in the checkout
    # would leave a labelled corpus of synthetic JPEGs sitting in the repository, one import away
    # from being measured as if it were real traffic.
    ("corpus", "ROOT_DIR", "corpus", "CALIBRATION_CORPUS_DIR"),
)

#: The generation subdirectories those names ask for.
GENERATION_DIRECTORIES: Final = tuple(directory for _, _, directory, _ in FILE_TREES)


def _write_generation() -> Path:
    """Materialise one test's whole world: the pristine database and empty file trees.

    The file trees start *empty* rather than carrying anything forward, and the seeded
    templates are written into the new generation by ``write_enrollment_templates`` on every
    reset. What a test needs is therefore always there; what a previous test *added* - an
    enrollment for an unseeded account, a reference selfie, a stored punch frame - cannot
    survive into it. That is the same promise the database has always had, and it was not true
    of the files: a directory shared by the whole process kept whatever the last test wrote.

    ``Never locked`` still holds for all of it: these are directories nothing has opened yet.
    """
    generation = GENERATIONS_DIR / f"gen-{next(_GENERATION):06d}"
    generation.mkdir()
    target = generation / "times.db"
    target.write_bytes(PRISTINE_DB)
    for name in GENERATION_DIRECTORIES:
        (generation / name).mkdir()
    return generation


def _repoint_current(generation: Path) -> None:
    """Point ``CURRENT_DIR`` at ``generation``, whatever the platform allows."""
    if CURRENT_DIR.is_symlink():
        CURRENT_DIR.unlink()
    elif CURRENT_DIR.exists():
        CURRENT_DIR.rmdir()
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(generation), str(CURRENT_DIR))
    else:
        CURRENT_DIR.symlink_to(generation, target_is_directory=True)


_repoint_current(_write_generation())


def current_generation() -> Path:
    """The generation directory ``CURRENT_DIR`` names right now, past the junction."""
    return Path(os.path.realpath(str(CURRENT_DIR)))


def redirect_file_directories() -> None:
    """Point every module that owns a file tree at this run's throwaway copies.

    Called once per process, from ``conftest``'s ``app_module``, and it is enough because each
    of these modules reads its global *at call time*: ``biometrics.directories()`` asks ``main``
    on every lookup, and ``punch_frames.frames_dir()`` / ``quick_links.photos_dir()`` read their
    own. Setting them to the *stable* path under ``CURRENT_DIR`` is what lets one repoint carry
    the rotation to all of them - the next write resolves the junction and lands in the new
    generation, with nothing re-imported or re-patched.

    Only the two ``main`` globals used to be redirected. A punch frame is written by the punch
    itself, so every test that stored one - and every load test - wrote it into
    ``<checkout>/punch_frames``, which had accumulated 1467 files by the time this was fixed.
    """
    for module_name, attribute, directory, _env_var in FILE_TREES:
        module = importlib.import_module(module_name)
        setattr(module, attribute, str(CURRENT_DIR / directory))
    # ...and the same locations go into the environment, so the two cannot drift apart.
    publish_file_directories()


def publish_file_directories() -> None:
    """Put every tree's location in the environment, the way ``DATABASE_PATH`` already is.

    A child process inherits the environment, not this process: ``os.environ`` crosses the
    ``subprocess`` boundary, a module attribute does not. The redirect above keeps *this*
    process's writers off the checkout; this keeps a *child's* writers off it too. The child
    imports the application fresh, ``config`` reads these variables, and
    ``main.LOCAL_REFS_DIR`` / ``main.WORKER_PHOTOS_DIR`` / ``punch_frames.FRAMES_DIR`` /
    ``quick_links.PHOTOS_DIR`` / ``corpus.ROOT_DIR`` come out pointing into the current
    generation.

    The values are the stable paths under ``CURRENT_DIR``, for the same reason ``DB_PATH`` is:
    the junction is resolved when a file is *opened*, so a child spawned before a reset and one
    spawned after it both write into the generation that is current when they write - no
    re-export, and nothing for the caller to remember.
    """
    for _module_name, _attribute, directory, env_var in FILE_TREES:
        os.environ[env_var] = str(CURRENT_DIR / directory)


def current_db_path() -> Path:
    """The concrete file ``DB_PATH`` resolves to right now, past the junction.

    Connections must be opened by this path rather than by ``DB_PATH`` itself. SQLite keys a
    database's locks - and, in WAL mode, its shared-memory index - by the path it was opened
    with, so two connections opened through the same junction *across* a repoint contend even
    though the junction now names a different file; on Windows the second one fails with
    ``database is locked``. Opening the generation itself is what makes the rotation hold. It
    is a no-op for a normal deployment, where the configured path is already its own file (see
    ``database.resolve_path``).
    """
    return current_generation() / "times.db"


#: The file trees, as *stable* paths: every one of them is a name inside the ``current``
#: junction, so a reset repoints all four along with the database, and a test that imported
#: ``REFS_DIR`` at module scope goes on naming the current generation for the whole run. They
#: are deliberately not the concrete generation paths - something holding one of those would go
#: on pointing at the world the test was rotated away from, which is the bug the database side
#: of this already documents in ``current_db_path``.
REFS_DIR = CURRENT_DIR / "refs"
PHOTOS_DIR = CURRENT_DIR / "photos"
FRAMES_DIR = CURRENT_DIR / "frames"
QUICK_LINK_PHOTOS_DIR = CURRENT_DIR / "quick_link_photos"

#: The embedding size this build's engine returns (``face_onnx.DIMENSIONS``), stated here so
#: the seeded template has the shape the application writes. It was 4096 under VGG-Face and
#: 2622 before that - a wrong number either way, which is why every seeded vector silently
#: became the synthetic fallback and the mismatch was invisible. ``test_fake_face_contract``
#: measures the dimension instead of trusting this constant.
_FACENET_EMBEDDING_DIM = 128


def _unit(values) -> list[float]:
    """A vector's unit-length form, the normalisation ``face_onnx`` applies to every embedding.

    A seeded template that were not unit-length would make the stub's ``"match"`` answer a
    non-zero distance to itself, so the shape the application writes is reproduced here.
    """
    import numpy as np

    array = np.asarray(values, dtype=np.float64)
    norm = float(np.linalg.norm(array))
    if norm == 0.0:
        return [float(value) for value in array]
    return [float(value) for value in (array / norm)]


def _reference_embedding() -> list[float]:
    """A unit-length FaceNet-128 embedding, copied from a live template when there is one.

    Any live template will do. The engine is stubbed, so what the tests need from this vector
    is that it has the shape and normalisation the application writes - and the live files may
    have been renamed to their account's biometric id (see ``biometrics``), so the search is by
    shape rather than by name. Read-only either way.
    """
    for candidate in [LIVE_REFS / "1.json", *sorted(LIVE_REFS.glob("*.json"))]:
        try:
            record = json.loads(candidate.read_text())
        except (OSError, ValueError):
            continue
        values = record.get("embedding") if isinstance(record, dict) else record
        if isinstance(values, list) and len(values) == _FACENET_EMBEDDING_DIM:
            return _unit([float(value) for value in values])
    return _unit(_deterministic_vector(_FACENET_EMBEDDING_DIM, seed=7))


# The fixed application reads its configuration from the environment; give the
# tests a signing secret and guarantee the Twilio variables stay unset so the
# "no baked-in credentials" test is meaningful.
os.environ["DATABASE_PATH"] = str(DB_PATH)
publish_file_directories()
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

def enter_temp_root() -> None:
    """Make the throwaway temp directory this process's working directory.

    Idempotent, and deliberately *not* run at import: see safety rule 1 above for why the
    timing decides whether ``pytest -n auto`` collects any tests at all. Called by the
    suite's session fixture, by ``conftest.app_module`` before it imports the app, and by
    ``tools/load_test.py`` / ``tools/profile_endpoints.py``.
    """
    if Path.cwd() != TMP_ROOT:
        os.chdir(TMP_ROOT)


def _cleanup() -> None:
    os.chdir(BACKEND_DIR)
    shutil.rmtree(TMP_ROOT, ignore_errors=True)


atexit.register(_cleanup)

#: Imported here rather than at the top because it lives in the backend directory, which is
#: only on ``sys.path`` after the bootstrap above. It is the seam this section replaces.
import face_onnx  # noqa: E402


# ---------------------------------------------------------------------------
# 2. Deterministic ONNX engine stub (no TensorFlow, no ONNX graph, no weights)
# ---------------------------------------------------------------------------
#: The distance ``FACE_MODE = "review"`` produces, named because the helper below takes a
#: *similarity* and the bands are written in distances: they are inverses (``cosine`` is
#: ``1 - similarity``), and getting that backwards is how a test quietly ends up driving the
#: refusal path while claiming to test the review band.
REVIEW_DISTANCE = 0.45


def _cosine_similar_vector(reference, target_cosine: float) -> list[float]:
    import numpy as np

    ref = np.asarray(reference, dtype=float)
    ref_unit = ref / np.linalg.norm(ref)
    probe = np.random.default_rng(20260912).normal(size=ref.shape)
    probe -= probe.dot(ref_unit) * ref_unit
    probe /= np.linalg.norm(probe)
    return list(target_cosine * ref_unit + math.sqrt(max(0.0, 1.0 - target_cosine**2)) * probe)


def _enrolled_embedding() -> list[float]:
    """The vector the application would score a capture against: ``WORKER``'s stored template.

    Read wherever the template really is - the id-named file once the application has booted
    and adopted it, the legacy name before that - through the same resolver the application
    reads through, never by constructing the name here. With a fallback to the seeded vector,
    because deleting an account's template is itself something the suite tests: a stub that
    could only answer by reading a file under test would fail the test it is there to serve.
    """
    import biometrics

    try:
        # Read through the application's own parser, in the shape the application writes (a
        # vector *and* what produced it). A stub that assumed a bare list would break the
        # moment the record grew, and the failure would look like a broken model.
        return list(biometrics.read_reference(str(reference_path(WORKER))).embedding)
    except (OSError, ValueError, TypeError):
        return _reference_embedding()


class FakeFaceNetEngine:
    """Stand-in for ``face_onnx``'s engine: the same answers, no graph and no ONNX Runtime.

    The suite has no exported model to run and no corpus to measure it against, so both halves
    of the embedding path are stubbed: ``detect_and_align`` supplies the faces, and this
    supplies the vectors. What it must reproduce exactly is the *contract* the application
    reads - ``embed_as_list`` returns a unit-length 128-float vector, and the cosine distance
    between it and an enrolled template is what the bands classify.

    ``FACE_MODE``:

    * ``"match"``    the enrolled embedding verbatim -> distance ~0.00 -> approved
    * ``"review"``   a vector at cosine distance ``REVIEW_DISTANCE`` -> pending review
    * ``"mismatch"`` the negated embedding -> distance ~2.00 -> refused
    * ``"none"``     raises ValueError, exactly like a crop the model cannot embed

    ``"review"`` sits at the ``REVIEW_DISTANCE`` distance rather than exactly on a band's
    boundary: the lines are derived per pipeline (``face_detector.MatchBand``), they are not
    round numbers, and a score placed on one would pin the comparison operator instead of the
    band.

    ``FACE_COUNT`` is how many *faces* the detector reports; >1 is what triggers the multi-face
    guards in ``compare_faces_sync`` and ``/admin/enroll``. It lives here rather than on the
    detector stub because a test sets one object, and the two stubs reading the same attribute
    is what keeps a count from being changed in one place and read in another.
    """

    FACE_MODE = "match"
    FACE_COUNT = 1

    def load(self) -> None:
        """There is no graph to open. Present so ``load_now()`` and readiness work."""

    @property
    def loaded(self) -> bool:
        return True

    def available(self) -> tuple[bool, str]:
        return True, ""

    def describe(self) -> dict:
        """The shape ``face_onnx.OnnxFaceNetEngine.describe`` returns, minus the real graph."""
        return {
            "model": face_onnx.MODEL_NAME,
            "dimensions": face_onnx.DIMENSIONS,
            "input_size": face_onnx.INPUT_SIZE,
            "path": "<stubbed>",
            "present": True,
            "fingerprint": "",
            "loaded": True,
            "load_seconds": 0.0,
            "error": None,
        }

    def embed(self, image):
        """A unit-length 128-float embedding, as ``(128,)`` float32."""
        import numpy as np

        return np.asarray(self.embed_as_list(image), dtype=np.float32)

    def embed_as_list(self, image) -> list[float]:
        """The embedding the application stores and scores, driven by ``FACE_MODE``.

        The enrolled vector is read back through the application's own resolver, so a test that
        has just enrolled somebody scores against *that* template rather than a constant - which
        is the whole reason ``"match"`` can mean distance zero.
        """
        if self.FACE_MODE == "none":
            raise ValueError("No face detected (stubbed)")
        reference = _enrolled_embedding()
        if self.FACE_MODE == "mismatch":
            vector = [-value for value in reference]
        elif self.FACE_MODE == "review":
            vector = _cosine_similar_vector(reference, 1.0 - REVIEW_DISTANCE)
        else:
            vector = list(reference)
        return _unit(vector)


FAKE_ENGINE = FakeFaceNetEngine()
#: The seam is ``face_onnx``'s shared engine, replaced through its own setter rather than by
#: monkeypatching ``get_engine`` - so every path that embeds (the punch, the four enrollment
#: flows, the quick link) reads the stub without knowing it is one. There is no ``deepface``
#: module to stub any more: the application imports neither TensorFlow nor DeepFace, and the
#: best proof of that is that this file no longer has to pretend the package exists.
face_onnx.set_engine(FAKE_ENGINE)


# ---------------------------------------------------------------------------
# 2b. Deterministic face detector stub (no ONNX, no OpenCV model)
# ---------------------------------------------------------------------------
# ``face_engine`` detects through ``face_detector`` (YuNet) whenever its model is on disk -
# and the model is committed beside the code, so without this stub every punch test would
# run a real ONNX detector over a synthetic JPEG, find nothing, and be answered with "no face
# detected". The seam is ``detect_and_align``: the single function the engine calls, and the
# one whose *answer* the application actually reads (how many faces are in the frame, at what
# confidence, and which crop is handed to the model). Stubbing it rather than the detector as
# a whole keeps the YuNet routing itself under test - the per-face embedding, the face-count
# guard, the crop - and fakes only the pixels.
import face_detector as _face_detector  # noqa: E402 - after the stubs it replaces


def _stub_available() -> tuple[bool, str]:
    """The model is present, so ``face_engine`` takes the detector path.

    Declared true rather than false on purpose: the fallback path is the old code, and a
    suite that never exercises the new one cannot notice it breaking. The detector's own
    loading, alignment and geometry are covered directly in ``test_face_detector.py``.
    """
    return True, ""


def _stub_detect_landmarks(image):
    """The detector's geometry, in the shape ``face_detector.detect_landmarks`` returns.

    Stubbed for the same reason ``detect_and_align`` is: the capture path (``corpus``) must not run
    ONNX over a synthetic JPEG, and the coordinates it stores have to be *some* geometry rather than
    a real face's. The five points sit inside the same 10x10 area the align stub reports, so a test
    can reason about the box and the landmarks together.
    """
    if FAKE_ENGINE.FACE_MODE == "none":
        return []
    # A real 112-template scaled into the stub's 10x10 detection - the same landmarks a YuNet row
    # would carry for a face that size, so `face_px` and the crop are consistent with each other.
    template = np.asarray(_face_detector._ALIGN_TEMPLATE, dtype=np.float32) * (10.0 / 112.0)
    return [
        {
            "landmarks": template.copy(),
            "facial_area": {"x": 0, "y": 0, "w": 10, "h": 10},
            "confidence": 0.99,
        }
        for _ in range(FAKE_ENGINE.FACE_COUNT)
    ]


def _stub_detect_and_align(image):
    """The detector's answer, in the shape ``extract_faces`` and ``represent`` read.

    One entry per ``FACE_COUNT``, each carrying ``confidence`` (the key ``extract_faces``
    uses) and the same frame as its "crop", which is what the engine stub embeds. The
    ``"none"`` mode answers an empty list, which is what a real detector does for a frame it
    cannot use.

    Read from the ``FAKE_ENGINE`` *instance*, not the class: a test sets ``face.FACE_COUNT = 2``
    on the fixture's object, which shadows the class attribute - and reading the class would
    quietly report one face to every caller, so the multi-face guard would never fire.
    """
    if FAKE_ENGINE.FACE_MODE == "none":
        return []
    return [
        {"face": image, "facial_area": {"x": 0, "y": 0, "w": 10, "h": 10}, "confidence": 0.99}
        for _ in range(FAKE_ENGINE.FACE_COUNT)
    ]


#: What was replaced, so a test that needs the real detector can put it back. Kept here
#: rather than reloaded from source: ``importlib.reload`` would un-stub the module for the
#: whole session and leave every later punch test running ONNX over a synthetic JPEG.
REAL_FACE_DETECTOR = {
    "available": _face_detector.available,
    "detect_and_align": _face_detector.detect_and_align,
    "detect_landmarks": _face_detector.detect_landmarks,
}

_face_detector.available = _stub_available
_face_detector.detect_and_align = _stub_detect_and_align
_face_detector.detect_landmarks = _stub_detect_landmarks


# ---------------------------------------------------------------------------
# 2c. A fixture calibration band, so the application can score at all
# ---------------------------------------------------------------------------
#: The boundaries ``(genuine_ceiling, impostor_floor)`` the fixture band is derived from, per
#: pipeline this build can run. The shipped table holds the one band somebody measured through a
#: real detector and a real corpus (see ``face_detector.BANDS``), and it is **not** this: a crop
#: nobody has measured must refuse to score, and the startup gate says so. A test deployment
#: differs in exactly one respect - it has no corpus to measure and every embedding it will ever
#: see comes from ``FAKE_ENGINE`` - so the fixture supplies the calibration instead, through the
#: same ``MatchBand.derived()`` rule the real tool uses. Different boundaries per pipeline,
#: because a test asserts that the live pipeline's band and the fallback's are not the same
#: object.
FIXTURE_BAND_BOUNDARIES: Final = {
    "yunet-2023mar": (0.20, 0.65),
    "mtcnn": (0.18, 0.60),
}

#: Written as the evidence string on every fixture band, because a number an operator finds in
#: a log has to say where it came from - and this one came from nowhere measured.
FIXTURE_BAND_EVIDENCE = (
    "FIXTURE CALIBRATION, NOT A MEASUREMENT: supplied by tests/harness.py so the suite can "
    "score stubbed embeddings. The shipped face_detector.BANDS holds lines measured through a "
    "real detector against a labelled corpus, and they are not these - anything quoting this "
    "string is reading the fixture."
)


def install_test_band() -> None:
    """Give every pipeline this build can run a band derived by the project's own rule.

    Installed rather than typed as literals so the lines are outputs of ``MatchBand.derived()``,
    exactly like a real band - which is what lets ``test_face_match_bands`` hold the fixture to
    the same derivability rule it holds a measured table to. Idempotent, and called both here
    and from ``reset_database``, so a test that clears the table to exercise the refusal path
    cannot leave the next test unable to start.
    """
    import face_detector
    import face_engine

    for pipeline, (ceiling, floor) in FIXTURE_BAND_BOUNDARIES.items():
        seed = face_detector.MatchBand(
            model=face_engine.FACE_MODEL,
            approve=0.0,  # replaced below by the rule's own output, never hand-typed
            review=0.0,
            genuine_ceiling=ceiling,
            impostor_floor=floor,
            evidence=FIXTURE_BAND_EVIDENCE,
        )
        approve, review = seed.derived()
        face_detector.BANDS[pipeline] = face_detector.MatchBand(
            model=face_engine.FACE_MODEL,
            approve=approve,
            review=review,
            genuine_ceiling=ceiling,
            impostor_floor=floor,
            evidence=FIXTURE_BAND_EVIDENCE,
        )


#: The table a *deployment* loads, copied before the fixture below replaces it.
#:
#: Without this copy the suite can only ever see its own calibration, and a shipped table that
#: went missing would keep every test in ``test_face_match_bands`` green right up to the startup
#: gate refusing to open the port - which is what happened when the VGG bands were retired and
#: this fixture was introduced to stand in for them. That file holds this copy to the coverage
#: the gate needs, through the same invariants it applies to the fixture, because the fixture is
#: what the tests score with and this is what a deployment scores with.
SHIPPED_BANDS: Final = dict(_face_detector.BANDS)

install_test_band()


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
    "worker_notifications",     # what a worker has been told, and whether they read it
    "worker_push_subscriptions",  # a live push subscription is a way to reach somebody's
                                # phone, and re-registering is a per-device fact
    "overtime_authorisations",  # the answer to a crossing while the shift is still running.
                                # Real usage leaves them live, and ``open_crossings`` hands one
                                # back inside every queue item - so a suite about an
                                # *unanswered* crossing would inherit somebody's approval
    "developer_alerts",        # the private alert hub: an infrastructure alert raised by real
                                # usage is exactly what a suite about alerting must not
                                # inherit, and the hub is read as a whole
    "developer_config",         # runtime flags and log levels, shared by every worker through
                                # the database. A test that flips one has to flip it back by
                                # being cleared, not by remembering to
    "refused_punches",          # the triage record of the punches the face check refused.
                                # Real usage files refusals all day; a suite asserting "the
                                # refused punch is the one this test just caused" would
                                # inherit somebody else's refusals and read them as its own
    "corpus_capture_consents",  # per-worker opt-in to calibration capture. Real usage grants
                                # and withdraws these; a suite testing the capture gate would
                                # inherit a live "granted" row and read it as its own setup
)

#: Tables left exactly as the live database have them: the two ledgers this application
#: *resumes* from, and nothing else. Both are written at runtime and read as a starting point
#: rather than as data - deleting either one would be a corrupted generation, not a clean one:
#:
#: * ``schema_migrations`` tells ``init_db`` which migrations this generation has already
#:   applied, and re-running them all is not the same thing as resuming;
#: * ``developer_config_version`` is the counter that invalidates every worker's cached runtime
#:   configuration. A missing row does not reset the cache, it stops the bump - an ``UPDATE
#:   ... WHERE id = 1`` that matches nothing - so clearing it would leave a suite testing a
#:   stale-config bug that the clear itself invented.
CONFIGURATION_TABLES: Final = ("schema_migrations", "developer_config_version")

#: Tables ``seed_database`` rewrites from scratch on every test, so their live contents never
#: reach an assertion: the roster, the sites, the shift rules a punch is judged by, and the
#: company's own name and mark.
SEEDED_TABLES: Final = ("users", "construction_sites", "shift_rules", "company_settings")

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
    """The template every seeded user carries (one vector: the engine is stubbed).

    Written in the shape ``biometrics.write_reference`` writes - the vector *and* the model
    and pipeline that produced it. Seeding a bare list, or the wrong model name, would seed a
    **stale** template, and then every punch test would be measuring the re-enrollment refusal
    instead of the flow it means to test. A test that wants the stale case should say so (see
    ``test_biometric_identity``), not get it by accident from the fixture.
    """
    import face_detector
    import face_engine

    return json.dumps(
        {
            "model": face_engine.FACE_MODEL,
            "pipeline": face_detector.PIPELINE,
            "dimensions": len(_reference_embedding()),
            "embedding": _reference_embedding(),
        }
    )


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
    one vector because the embedding engine is stubbed.

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
        # There used to be a second write here, into ``app_module.USER_CACHE`` - a per-process
        # password dictionary the login path authenticated from. That dictionary is gone
        # (identity is read from the database on every request, which is what makes a rotation
        # visible to every ASGI worker), so the ``getattr`` had been writing to nothing while the
        # comment above it still claimed a live coupling. Removed rather than left as a guard
        # against a name that no longer exists.

    conn = sqlite3.connect(current_db_path())
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
        # The company's name and mark are configuration too, and this is the one setting whose
        # *absence* is meaningful: ``NULL`` is "nobody configured this", which is what makes the
        # lockup this application ships with the default rather than something somebody chose.
        # A live company name reaching a test would make "a deployment that never opened the
        # panel" untestable - and would put a real logo in a suite's assertions - so the row is
        # put back to unconfigured on every reset. The version counter is left where it is: it
        # is a cache-buster, not a setting, and nothing asserts a value for it twice.
        conn.execute(
            "UPDATE company_settings SET company_name = NULL, company_legal = NULL, "
            "company_est = NULL, company_tagline = NULL, logo_bytes = NULL, logo_mime = NULL, "
            "logo_width = NULL, logo_height = NULL, updated_by = NULL WHERE id = 1"
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

    connection = sqlite3.connect(str(current_db_path()), timeout=30.0, isolation_level=None)
    try:
        connection.execute("BEGIN IMMEDIATE")
        # Both append-only guards are lifted the same way, and both put back in the same
        # transaction: ``audit_log`` (retention's guard) and ``corpus_capture_consents``
        # (migration 23's), whose consent history is evidence for exactly the same reason the
        # audit trail is. The guards exist for the right reason and this is not a reason to
        # weaken them - read the trigger out of ``sqlite_master``, drop it, delete, put the
        # same text back, so a crash in the middle leaves the guard on.
        guard_names = (retention.AUDIT_DELETE_GUARD, "corpus_capture_consents_no_update", "corpus_capture_consents_no_delete")
        guards: list[tuple[str, str]] = []
        for name in guard_names:
            row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?", (name,)
            ).fetchone()
            if row is not None and row[0]:
                guards.append((name, str(row[0])))
        try:
            for name, _sql in guards:
                connection.execute(f"DROP TRIGGER IF EXISTS {name}")
            for table in ACTIVITY_TABLES:
                try:
                    connection.execute(f"DELETE FROM {table}")
                except sqlite3.OperationalError:
                    continue
        finally:
            for _name, sql in guards:
                connection.execute(sql)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _restore_pristine() -> None:
    """Install a fresh database for the next test, deterministically.

    The reset is a *rotation*, not an overwrite: a new generation directory is written from
    the pristine snapshot - touching only files nothing has ever opened - and the
    ``current`` junction is repointed at it. The old generation is left for its stragglers
    (see ``_sweep_old_generations``) and removed a few generations later, when nothing can
    still be holding it.

    This replaced an in-place restore that had to fold the WAL and flip
    ``journal_mode`` back to ``delete`` on the *live* path - both of which need the
    database's exclusive lock. Any connection still open on it (a browser test's uvicorn
    thread finishing the previous test, a leaked connection after a failure, a second pytest
    process) turned fixture setup into ``sqlite3.OperationalError: database is locked`` for
    a test that had not started. There is no step left here that can block.

    The digest check survives from the old design: prove the file the next test will open is
    byte-for-byte the pristine snapshot, not half a restore.

    The same repoint carries the file trees (``refs``, ``photos``, ``frames``,
    ``quick_link_photos``): they are names inside the same junction, so one reset hands the next
    test a pristine database *and* empty file trees in a single step. Before this they were
    written once per process, so a template one test enrolled, a selfie it stored or a frame it
    wrote was still on disk for every test after it - and a retention test that deleted files
    was deleting the fixture the next test was going to read.
    """
    generation = _write_generation()
    _repoint_current(generation)

    target = generation / "times.db"
    expected_digest = hashlib.sha256(PRISTINE_DB).hexdigest()
    restored_digest = hashlib.sha256(target.read_bytes()).hexdigest()
    if restored_digest != expected_digest:
        raise RuntimeError(
            "the test database was not restored cleanly: the file on disk does not match the "
            f"pristine snapshot ({restored_digest[:12]} != {expected_digest[:12]}), so every "
            "following test would run against a half-restored database"
        )
    _sweep_old_generations(keep=3)


def _sweep_old_generations(*, keep: int = 3) -> None:
    """Delete generations older than the last ``keep``.

    A straggler connection may still hold an old generation open - deleting its file is fine
    on POSIX and fails on Windows, so the sweep is best-effort and simply leaves whatever it
    cannot remove for a later reset. ``keep`` is small but never 1: the previous generation
    is exactly the one a still-running browser server is most likely to be reading.
    """
    generations = sorted(GENERATIONS_DIR.glob("gen-*"))
    for stale in generations[:-keep] if len(generations) > keep else []:
        shutil.rmtree(stale, ignore_errors=True)


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

    The biometric templates are re-seeded here too, not only at import: a template carries the
    pipeline that produced it, and a test that patches ``face_detector.PIPELINE`` while it
    seeds one (``test_face_match_bands`` does, deliberately, to build the stale case) would
    otherwise leave that patched name **on disk** for every later test in the process - which
    turned a whole run's punches into ``reference_stale`` refusals from that point on. The
    database gets a pristine snapshot per test, and now the file trees get the same treatment:
    they are rotated with it, so a template written by a test that has finished is gone rather
    than merely overwritten.
    """
    _restore_pristine()
    app_module.init_db()
    clear_activity()
    seed_database(app_module)
    write_enrollment_templates()
    reset_rate_limits(app_module)
    install_test_band()
    FAKE_ENGINE.FACE_MODE = "match"
    FAKE_ENGINE.FACE_COUNT = 1


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
    conn = sqlite3.connect(current_db_path())
    try:
        row = conn.execute(sql, params).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def db_rows(sql: str, params: tuple = ()):
    conn = sqlite3.connect(current_db_path())
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
    """A synthetic image; real face content is irrelevant because the engine is stubbed."""
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
    confirmed: bool = False,
):
    """One punch. No password: the bearer token is the credential.

    ``confirmed`` answers the early clock-out question. It defaults to off, because a
    helper that confirmed everything would stop the gate from ever being observed; a test
    that clocks out a short shift says so, and gets the refusal first if it does not.

    The form used to carry the worker's password as well, and this helper used to be
    where every test typed it. ``/attendance/verify`` no longer takes one, so a helper
    that still sent it would be describing a field the server ignores - and a suite that
    passes whatever it likes in an ignored field stops being evidence of anything.
    ``user_id`` stays because the endpoint still echoes it and still refuses a subject
    that is not the token's owner.
    """
    data = {
        "worker_id": user_id,
        "action": action,
        "location_input": coordinates,
    }
    if confirmed:
        data["confirm_early_checkout"] = "1"
    return client.post(
        "/api/v1/attendance/verify",
        data=data,
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


def use_auto_close(client, *, notify_hours: float = 7.5) -> dict:
    """Put the shift rules into the arrangement where the automatic close ends the day.

    The close stands down when the overtime line sits above the paid day, because a shift
    closed at 8 h could never be observed crossing a line at 8.1 h (see
    ``shift_hours.day_end_rules``). So a test of the close has to say which arrangement it
    means, and the shipped one is not it. 7.5 h is *below* the 8 h paid day: the alert fires
    first for anyone who ran long, and the close then ends the standard day - the two rules
    cooperating, which is the arrangement the close tests are about.

    Applied through the same endpoint the console posts to, so the rules the scans read are
    the rules an operator would have stored.
    """
    response = client.post(
        "/api/v1/admin/shift_rules",
        headers=bearer(ADMIN),
        json={"overtime_notify_hours": notify_hours},
    )
    assert response.status_code == 200, response.text[:300]
    rules = response.json()["rules"]
    assert rules["day_end"]["close_defers"] is False, (
        "the helper means to configure the arrangement where the close acts"
    )
    return rules


def load_second_app_instance():
    """Import ``main.py`` a second time, simulating a second ASGI worker process.

    Two uvicorn workers share one SQLite file but each keeps its own module-level state -
    the face engine's pool and queue, the rate-limit buckets, whatever the app caches for
    itself. ``USER_CACHE``, the per-process password dictionary that used to make a rotation
    invisible to every other worker, is no longer one of them: identity is read from the
    database on every request. That is the property this helper exists to demonstrate -
    ``test_session_integrity`` loads a second instance and requires a rotated password to be
    seen by both. Importing runs no database work - ``init_db()`` moved into the app's
    ``lifespan``, and the old import-time ``enforce_11h_cutoff()`` is gone - so the second
    instance only has to be pointed at the same reference/photo directories the first one uses.

    Only those two need setting on the new module: ``punch_frames`` and ``quick_links`` are
    process-wide singletons, so the second instance reads the very same - already redirected -
    globals the first one does.
    """
    import importlib.util

    name = f"main_second_worker_{uuid.uuid4().hex[:8]}"
    spec = importlib.util.spec_from_file_location(name, MAIN_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.LOCAL_REFS_DIR = str(REFS_DIR)
    module.WORKER_PHOTOS_DIR = str(PHOTOS_DIR)
    return module


#: No face-model stub is needed any more: the app imports as ONNX Runtime code and builds no
#: session until a punch runs, so ``import main`` in a clean interpreter costs no framework and
#: this observes the real import rather than one behind a fake. It used to install a fake
#: ``deepface`` so the probe did not load TensorFlow - a crutch the ONNX swap removed.
APP_IMPORT_PROBE = "import main; print('APP_IMPORTED_OK')"


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
