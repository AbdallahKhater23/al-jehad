"""A punch's upload goes to disk, and no queued job holds a frame.

WHY THIS EXISTS
---------------
The punch used to read its photo into memory, decode it on the request path, and then submit
two jobs: the liveness check, and later the comparison. So a punch *waiting* for engine capacity
was holding its upload bytes (up to 5 MB) and its decoded pixels (~10 MB at 1280 px, because
both the RGB and the BGR view existed by then) - and a burst of ten punches on a 512 MB instance
was ten of those across twenty queue slots. The thing that made that instance fall over was
never the model; it was the frame every *waiting* punch kept alive.

What replaced it is one job per punch, handed a path:

* the request spools the upload to a temporary file a chunk at a time (``uploads.spool_photo``),
  under the same policy and with the same coded refusals as ``read_photo``
* ``main.judge_punch_frame`` decodes that file **inside the engine worker**, screens liveness,
  and compares - so at most ``capacity`` frames are alive however many punches are queued
* the frame comes back to the request for the only two things that need it, the evidence stored
  with the row and the calibration capture, both of which must be taken from the frame the models
  actually judged

This suite pins the parts that would come back silently: that the job is handed a *path* and no
pixels, that a punch is one job rather than two, that the temporary file is gone whatever the
answer was, that a punch refused before the photo never writes it at all, and that the spool
cannot be mistaken for somewhere this application keeps faces.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import numpy as np

import config
import uploads
from harness import MOALLEM, OUTSIDE_ALL_SITES, bearer, clock_in, jpeg_bytes


def _punch(client, monkeypatch, app_module, **punch):
    """Run one punch and report what was queued to the face engine, in order.

    ``run_async`` is watched rather than the job body, because what is under test is *what was
    queued*: arguments are recorded before the job runs, which is the moment a memory hold would
    be taken (and held for the whole wait). Each entry is ``(job name, arguments, whether the
    file the job was handed existed right then)`` - the existence is sampled *inside* the
    watcher, because by the time the punch has answered the file is meant to be gone.
    """
    queue: list[tuple[str, tuple, bool]] = []
    real = app_module.face_engine.ENGINE.run_async

    async def watched(fn, *args, **kwargs):
        path = args[1] if len(args) > 1 else None
        queue.append(
            (getattr(fn, "__name__", "anonymous"), args, bool(path) and os.path.isfile(path))
        )
        return await real(fn, *args, **kwargs)

    monkeypatch.setattr(app_module.face_engine.ENGINE, "run_async", watched)
    response = clock_in(client, MOALLEM, headers=bearer(MOALLEM), **punch)
    return response, queue


def _spooled() -> set[str]:
    try:
        return set(os.listdir(uploads.SPOOL_DIR))
    except FileNotFoundError:
        return set()


# ---------------------------------------------------------------------------
# 1. what the queue holds
# ---------------------------------------------------------------------------
def test_one_punch_is_one_job_holding_a_path_and_no_pixels(client, app_module, monkeypatch):
    """The whole point, stated exactly.

    A regression here is invisible at runtime - the punch still works, it just holds megabytes
    per waiting request again - so the shape is asserted rather than the outcome: *one* job,
    whose arguments are a template path (or ``None`` for an unenrolled worker) and the path of a
    file that exists on disk at submission time, with no array and no upload bytes anywhere in
    it.
    """
    response, queue = _punch(client, monkeypatch, app_module)
    assert response.status_code in (200, 422), response.text[:300]

    names = [name for name, _, _ in queue]
    assert names == ["judge_punch_frame"], (
        f"a punch submitted {names}: one punch is one unit of engine work"
    )
    _, arguments, on_disk = queue[0]
    reference, photo_path = arguments

    # The template, resolved by the request (a worker with nothing enrolled passes ``None`` and
    # the 404 is raised after the job, where it has always been).
    assert reference is None or (isinstance(reference, str) and os.path.isfile(reference)), (
        "the job was not handed a template it could read"
    )
    assert isinstance(photo_path, str), "the job was not handed a path"
    assert on_disk, "the path handed to a queued job did not exist when the worker would open it"
    assert os.path.dirname(os.path.abspath(photo_path)) == os.path.abspath(uploads.SPOOL_DIR), (
        "the queued file is not a spooled upload"
    )
    for argument in arguments:
        assert not isinstance(argument, np.ndarray), "an array of pixels was queued"
        assert not isinstance(argument, (bytes, bytearray)), "the uploaded bytes were queued"


def test_the_upload_is_removed_once_the_punch_has_answered(client, app_module, monkeypatch):
    response, queue = _punch(client, monkeypatch, app_module)
    assert response.status_code in (200, 422), response.text[:300]
    photo_path = queue[0][1][1]

    assert not os.path.exists(photo_path), "the punch left its upload on disk"


def test_a_refused_punch_takes_its_upload_with_it(client, app_module, monkeypatch):
    """A mismatch is still a refusal that ran a full pipeline - and still leaves nothing behind.

    This is the path most likely to forget: the request raises deep inside the handler, long
    after the job returned, and the upload has already been used by every stage.
    """
    monkeypatch.setattr(
        app_module, "compare_faces_sync", lambda *a, **k: {"verified": False, "distance": 0.9, "error": None}
    )
    response, queue = _punch(client, monkeypatch, app_module)
    assert response.status_code == 422, response.text[:300]
    assert response.json()["detail"]["error_code"] == "face_mismatch"
    photo_path = queue[0][1][1]

    assert not os.path.exists(photo_path), "a refused punch left its upload on disk"


def test_a_photo_that_will_not_decode_is_refused_with_its_own_code(client, app_module, monkeypatch):
    """The decode moved into the worker, and the refusal did not move with it.

    The type is decided from the leading bytes on the request path, so a JPEG header over
    garbage is accepted as a photo and then fails to decode *inside the job*. The client must
    still read the code that names their problem, and the temporary file must not survive the
    refusal that the file itself caused.
    """
    broken = b"\xff\xd8\xff" + b"this is not a JPEG body at all"
    response, queue = _punch(client, monkeypatch, app_module, image=broken)

    assert response.status_code == 422, response.text[:300]
    assert response.json()["detail"]["error_code"] == "unreadable_image"
    photo_path = queue[0][1][1]
    assert not os.path.exists(photo_path)


def test_a_selfie_above_the_ingestion_boundary_is_refused_with_the_code_a_client_acts_on(
    client, app_module
):
    """The ingestion boundary, where a worker actually meets it: a punch.

    2048x2048 is 4.19 MP - over the pixel limit, exactly on the edge limit - so this is the
    case the boundary exists for, and it is refused *inside* the engine worker (the boundary is
    read from the spooled file, which is what lets a waiting punch hold no pixels at all). Two
    things therefore have to hold that a unit test of ``face_frame`` cannot show: the client
    reads the coded refusal rather than a 500 from a job that raised, and the refused upload
    does not survive the refusal.
    """
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (2048, 2048), (120, 130, 140)).save(buffer, format="JPEG", quality=85)

    before = _spooled()
    response = clock_in(client, MOALLEM, headers=bearer(MOALLEM), image=buffer.getvalue())

    assert response.status_code == 422, response.text[:300]
    detail = response.json()["detail"]
    assert detail["error_code"] == uploads.ERR_RESOLUTION_TOO_HIGH, detail
    assert "4 MP" in detail["message"], detail
    assert (detail["width"], detail["height"]) == (2048, 2048), detail
    assert _spooled() == before, "the refused upload stayed on disk"


# ---------------------------------------------------------------------------
# 2. the refusals that never reach the file
# ---------------------------------------------------------------------------
def test_an_oversized_upload_is_refused_while_reading_and_leaves_nothing(client, app_module):
    """The size ceiling is enforced chunk by chunk, before the rest of the body arrives."""
    before = _spooled()
    response = clock_in(
        client, MOALLEM, headers=bearer(MOALLEM), image=b"\xff\xd8\xff" * 512 + b"x" * (6 * 1024 * 1024)
    )

    assert response.status_code == 413, response.text[:300]
    assert response.json()["detail"]["error_code"] == "photo_too_large"
    assert _spooled() <= before, "an upload that was refused for its size was left on disk"


def test_a_punch_refused_outside_the_geofence_never_writes_the_photo(client):
    """Cheap refusals stay cheap, and that includes not writing the body down.

    The geofence is checked before the upload is read - which is why the spool cannot live in a
    dependency: a request that is about to be refused for where it is must not spend disk on the
    way, or a phone that keeps retrying from the wrong place writes a file per attempt.
    """
    before = _spooled()
    response = clock_in(client, MOALLEM, headers=bearer(MOALLEM), coordinates=OUTSIDE_ALL_SITES)

    assert response.status_code == 403, response.text[:300]
    assert _spooled() <= before, "a punch refused for its location wrote the upload to disk anyway"


# ---------------------------------------------------------------------------
# 3. the frame that comes back
# ---------------------------------------------------------------------------
def test_the_evidence_frame_is_the_frame_the_models_judged(client, app_module, monkeypatch):
    """The stored evidence is the same frame the liveness model and the detector saw.

    The frame travels back out of the job precisely so that this stays true: the alternative - a
    second decode from the spooled file for the benefit of ``store_frame`` - would work, cost
    another decode on every punch, and quietly answer the coverage and triage questions with a
    slightly different image than the one that was judged.
    """
    shapes: list[tuple] = []
    real_inspect = app_module.liveness.inspect

    def watched_inspect(rgb, **kwargs):
        shapes.append(tuple(np.shape(rgb)))
        return real_inspect(rgb, **kwargs)

    monkeypatch.setattr(app_module.liveness, "inspect", watched_inspect)

    stored: list[tuple] = []
    real_store = app_module.punch_frames.store_frame

    def watched_store(image, name=None):
        stored.append(image.size)
        return real_store(image, name)

    monkeypatch.setattr(app_module.punch_frames, "store_frame", watched_store)

    response, _ = _punch(client, monkeypatch, app_module)
    assert response.status_code in (200, 422), response.text[:300]

    assert shapes, "no frame reached the liveness model"
    assert stored, "this punch stored no evidence frame"
    height, width, channels = shapes[0]
    assert channels == 3
    assert stored[0] == (width, height), (
        f"the evidence frame is {stored[0]} and the judged frame was {(width, height)}"
    )


# ---------------------------------------------------------------------------
# 4. the spool file itself
# ---------------------------------------------------------------------------
def test_a_spool_writes_the_body_in_chunks_and_keeps_only_the_header():
    """``spool_photo`` may never ask for the whole body.

    A single ``read()`` with no size is how the 2 GB POST that ``uploads.py`` exists to prevent
    would come back: the policy is enforced chunk by chunk, so a call that skips the chunks is a
    call that skips the ceiling. The upload stub here refuses to be read that way - it raises -
    which makes the assertion the strongest available without measuring the process's memory.
    """

    class Chunked:
        def __init__(self, blob: bytes) -> None:
            self._blob = blob
            self._at = 0
            self.calls: list[int] = []

        async def read(self, size: int = -1) -> bytes:
            self.calls.append(size)
            if size is None or size < 0:  # pragma: no cover - the failure this test is for
                raise AssertionError("the body was read in one piece, past the size ceiling")
            chunk = self._blob[self._at : self._at + size]
            self._at += size
            return chunk

    import asyncio

    blob = jpeg_bytes()
    upload = Chunked(blob)
    spool = asyncio.run(uploads.spool_photo(upload, field="selfie"))
    try:
        assert upload.calls and set(upload.calls) == {uploads.CHUNK_BYTES}
        assert os.path.getsize(spool.path) == len(blob)
        assert spool.size == len(blob) and spool.mime == "image/jpeg"
        # What was kept in memory is the signature, not the photo: the file is the source of
        # truth for everything after this.
        assert uploads.check_photo_type(blob[: uploads.SNIFF_BYTES], field="selfie") == "image/jpeg"
    finally:
        spool.discard()


def test_a_crashed_request_leaves_a_file_that_the_next_start_sweeps():
    """The one leftover a request cannot clean up after, and the pass that collects it.

    A file whose request died between spooling and discarding has no owner left, so the sweep is
    the second half of the cleanup. It is age-gated on purpose: several processes share this
    directory (a test run is several), and a sweep that took *everything* could delete the upload
    of a punch being verified in another process.
    """
    directory = uploads.spool_dir()
    stale = os.path.join(directory, "punch-left-by-a-crash.jpg")
    live = os.path.join(directory, "punch-in-flight.jpg")
    for path in (stale, live):
        with open(path, "wb") as handle:
            handle.write(b"\xff\xd8\xff")
    old = time.time() - uploads.SPOOL_STALE_SECONDS - 60
    os.utime(stale, (old, old))

    removed = uploads.sweep_spool_dir()

    assert removed >= 1
    assert not os.path.exists(stale), "a file from an interrupted request survived the sweep"
    assert os.path.exists(live), "the sweep removed a file a live request could still need"
    os.remove(live)
    # A request is seconds long; the floor has to be far above it for that safety to mean
    # anything.
    assert uploads.SPOOL_STALE_SECONDS > 60


def test_the_spool_is_not_a_directory_this_application_keeps_faces_in():
    """A temporary upload must not be mistakable for a stored one.

    Biometric templates, enrollment photos, punch evidence and quick-link photos all live in
    configured directories that ``retention`` sweeps and an operator inspects. The spool is the
    opposite - it is scoped to a single request and has no meaning after it - so it is kept
    outside every one of them, and a spooled file can never be read back as somebody's enrolled
    face by a tool that lists a directory.
    """
    spool = Path(uploads.SPOOL_DIR).resolve()
    for field in (
        "local_refs_dir",
        "worker_photos_dir",
        "punch_frames_dir",
        "quick_link_photos_dir",
        "calibration_corpus_dir",
    ):
        directory = Path(getattr(config.settings, field)).resolve()
        assert not spool.is_relative_to(directory), f"the spool is inside {field}"
        assert not directory.is_relative_to(spool), f"{field} is inside the spool"
