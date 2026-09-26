"""One resampler for both sides of a face comparison.

WHY THIS EXISTS
---------------
A template is an image of a person; a punch is another image of the same person; the thing
that makes them comparable at all is that they are *both* built the same way. They were not.
The punch chain capped its frame at 1280 px, the two console enrollment endpoints capped
theirs at 800, and three more enrollment paths - the registration link, the invite accept and
a row inside a bulk ZIP - resized not at all and embedded whatever the phone sent. A
resampler is not neutral: it decides what the detector finds and what the alignment crop is
taken from, and a distance measured between two frames that were resampled differently is a
measurement of *that difference* as much as of the face.

So the decode hint and the resize moved into one function, ``uploads.face_frame``, and this
suite pins the two halves of the claim:

1. **The chain behaves.** A photo inside the ingestion boundary is downscaled to the
   configured long edge with its aspect ratio kept, a small one is passed through untouched,
   and exactly one resampler touches it.
2. **Nothing bypasses it.** A source check (parsed, not grepped - see ``_bypasses``), because
   the failure it prevents is invisible at runtime: embedding a full-resolution frame works
   perfectly and simply measures something other than the punch it is compared to. That is how
   three enrollment paths came to sit outside the chain in the first place.
3. **The boundary holds.** A photo above it is refused, from its header, with a code and a
   sentence that say which limit was crossed.

WHY THERE IS A BOUNDARY AT ALL
------------------------------
The chain used to accept a 12 MP photo and handle it with a libjpeg decode hint. The hint
keeps the memory down and is why it was there - but it resamples on a power-of-two grid, so
where the frame landed depended on how big the *file* was: ~1008 px for a 12 MP source, 1280
for a 1.2 MP one, out of the same face. Two different resamplings of one person, compared to
each other by a cosine distance whose decision margin is ~0.0036, against a measured drift of
up to ~0.0088. A punch could cross the line because of the size of the upload.

So the hint is gone and the input is bounded instead: one decode path for everything that is
accepted, and a refusal for what is not. The refusal is not a formality - a phone's own
camera app produces 12 MP, so the file-picker paths in the client have to downscale before
they upload, and the limit is published in ``uploads.policy()`` for exactly that reason.
"""

from __future__ import annotations

import ast
import io
import json
import os
import subprocess
import sys

import numpy as np
import pytest
from fastapi import HTTPException
from PIL import Image, ImageOps

import config
import harness
import uploads

#: The modules that build a frame for a face model. ``uploads.py`` owns the chain and is not
#: scanned; ``biometrics`` (the reference selfie), ``punch_frames`` (punch evidence) and
#: ``branding`` (a logo) resize their own *artifacts*, none of which is ever fed to a model.
FACE_FRAME_MODULES = (
    "main.py",
    "quick_links.py",
    "offline_sync.py",
    "enrollment.py",
    "enroll_workers.py",
)


def _jpeg(size: tuple[int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (120, 130, 140)).save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()


#: Inside the boundary (2.8 MP, edge 2000) and larger than the frame: the ordinary case, a
#: photo bigger than the 1280 px frame that is resampled down to it.
BIG = _jpeg((2000, 1400))

#: 4.19 MP of area on an edge that is exactly at the limit, so only the pixel rule fires.
OVER_PIXELS = (2048, 2048)

#: 2.1 MP - comfortably under the pixel limit - with 2100 px on the long edge.
OVER_EDGE = (2100, 1000)


# ---------------------------------------------------------------------------
# 1. the chain itself
# ---------------------------------------------------------------------------
def test_a_big_photo_is_downscaled_to_the_configured_edge_with_its_ratio_kept():
    source = Image.open(io.BytesIO(BIG))
    frame = uploads.face_frame(BIG, field="selfie")

    assert frame.mode == "RGB"
    assert max(frame.size) <= config.settings.face_frame_max_px
    assert max(frame.size) < max(source.size), "the frame is the size the phone sent"
    source_ratio = source.size[0] / source.size[1]
    assert abs(frame.size[0] / frame.size[1] - source_ratio) < 0.01, frame.size


def test_a_photo_smaller_than_the_ceiling_is_passed_through_untouched():
    """``thumbnail`` only ever shrinks: a 320 px frame must not be blown up to the ceiling.

    Upscaling would not add a pixel of real detail and would change what the detector sees
    for every old phone in the field, so it is worth pinning separately from the cap.
    """
    frame = uploads.face_frame(_jpeg((320, 240)), field="selfie")

    assert frame.size == (320, 240)


def test_the_settings_drive_both_the_boundary_and_the_resize(monkeypatch):
    """The chain passes the boundary down and applies its own ceiling - from the settings.

    Two numbers, two jobs: the boundary decides whether the upload is accepted at all, and
    the ceiling decides how big the frame a model sees is. A chain that took either from a
    literal would ignore the environment the rest of the application is configured by.
    """
    seen: list[dict] = []
    real = uploads.decode_photo

    def recording(data, **kwargs):
        seen.append(kwargs)
        return real(data, **kwargs)

    monkeypatch.setattr(uploads, "decode_photo", recording)
    monkeypatch.setattr(
        uploads,
        "settings",
        config.settings.model_copy(
            update={
                "face_frame_max_px": 640,
                "face_frame_max_pixels": 2_000_000,
                "face_frame_max_edge_px": 1024,
            }
        ),
    )

    frame = uploads.face_frame(_jpeg((800, 600)), field="selfie")

    assert seen == [{"field": "selfie", "max_pixels": 2_000_000, "max_edge": 1024}]
    assert max(frame.size) <= 640


def test_raising_the_setting_never_shrinks_the_frame(monkeypatch):
    """The number is a ceiling, so a larger one can only produce a larger frame.

    This is the property that makes the setting safe to tune on a live deployment: it is a
    ceiling on cost, not a target size, and the single resample only ever shrinks.
    """
    frames = {}
    for edge in (640, 1280):
        monkeypatch.setattr(
            uploads, "settings", config.settings.model_copy(update={"face_frame_max_px": edge})
        )
        frames[edge] = uploads.face_frame(BIG, field="selfie")

    assert max(frames[640].size) <= 640
    assert max(frames[640].size) < max(frames[1280].size) <= 1280


def test_both_sides_of_a_comparison_are_built_from_the_same_bytes():
    """The same photo, enrolled and punched, is the same pixels - which is the whole point.

    ``field`` only names the photo in a refusal message, so it must not reach the chain.
    """
    enrolled = uploads.face_frame(BIG, field="enrollment photo")
    punched = uploads.face_frame(BIG, field="selfie")

    assert np.array_equal(np.asarray(enrolled), np.asarray(punched))


# ---------------------------------------------------------------------------
# 2. nothing bypasses the chain
# ---------------------------------------------------------------------------
def _is_uploads(node: ast.AST) -> bool:
    return isinstance(node, ast.Name) and node.id == "uploads"


def _bypasses(source: str) -> list[str]:
    """Hand-built face frames in ``source`` - parsed, so prose is not a bypass.

    Two shapes are reported, and they are the two ways a path can end up outside the chain:

    * ``uploads.decode_photo(...)`` - the decode hint without the resize, which is exactly
      how three enrollment paths embedded a full-resolution frame.
    * ``<frame>.thumbnail(...)`` - a resize without the hint, which is how the console's
      enrollment endpoints came to embed a *smaller* frame than the punch they were compared
      against. A module that wants either half wants ``uploads.face_frame``.
    """
    found = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr == "decode_photo" and _is_uploads(node.func.value):
            found.append(f"uploads.decode_photo at line {node.lineno}")
        elif node.func.attr == "thumbnail":
            found.append(f".thumbnail at line {node.lineno}")
    return found


def test_the_scanner_catches_both_ways_out_of_the_chain():
    """The guard is a source check, so it has to fail on the code it exists to find."""
    source = (
        "def handler(blob):\n"
        "    image = uploads.decode_photo(blob, field='selfie')\n"
        "    image.thumbnail((800, 800))\n"
        "    return image\n"
    )

    assert _bypasses(source) == ["uploads.decode_photo at line 2", ".thumbnail at line 3"]


def test_no_module_builds_a_face_frame_outside_the_shared_chain():
    """Every path that feeds a face model must go through ``uploads.face_frame``.

    A source check rather than a runtime one, because a bypass is not an error: it produces a
    template and a decision that both look fine, and only differ by the resampler that made
    them. The prompt for this test is three such paths, none of which anything ever failed on.
    """
    sources = {
        name: (harness.BACKEND_DIR / name).read_text(encoding="utf-8")
        for name in FACE_FRAME_MODULES
    }
    missing = [name for name, source in sources.items() if "uploads.face_frame(" not in source]
    assert not missing, f"these modules no longer build a face frame: {missing}"

    offenders = {
        name: found for name, source in sources.items() if (found := _bypasses(source))
    }
    assert offenders == {}, offenders


def test_the_chain_decodes_only_through_the_upload_policy(monkeypatch):
    """``face_frame`` must not open the bytes itself.

    ``decode_photo`` is where the *declared* pixel count is refused before any pixels exist,
    and where the type sniffing and the coded refusals live. A chain that reached for
    ``Image.open`` directly would work perfectly and be a second door into the decoder, so
    this pins the delegation rather than the outcome.
    """
    calls: list[tuple[bytes, dict]] = []
    sentinel = Image.new("RGB", (16, 16))

    def fake_decode(data, **kwargs):
        calls.append((data, kwargs))
        return sentinel

    monkeypatch.setattr(uploads, "decode_photo", fake_decode)

    frame = uploads.face_frame(b"bytes that never reach PIL", field="selfie")

    assert calls and calls[0][0] == b"bytes that never reach PIL"
    assert calls[0][1]["field"] == "selfie"
    assert frame is sentinel, "the chain decoded a second time instead of using the policy's image"


# ---------------------------------------------------------------------------
# 3. one number, one name, and the old name still answers
# ---------------------------------------------------------------------------
def test_the_setting_is_read_from_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("ENV_FILE", str(tmp_path / "absent.env"))
    monkeypatch.delenv("FACE_FRAME_MAX_PX", raising=False)
    monkeypatch.setenv("FACE_FRAME_MAX_PX", "704")

    assert config.build_settings().face_frame_max_px == 704


def test_the_old_variable_name_still_sizes_the_frame(monkeypatch, tmp_path):
    """The setting used to be ``PUNCH_SELFIE_MAX_PX``, when it only sized punches.

    A deployment that set it chose that number deliberately, and it does not stop being a
    valid choice because the setting grew a wider meaning - so the old name is still read,
    and quietly keeps the value it asked for rather than reverting to the default.
    """
    monkeypatch.setenv("ENV_FILE", str(tmp_path / "absent.env"))
    monkeypatch.setenv("PUNCH_SELFIE_MAX_PX", "960")
    monkeypatch.delenv("FACE_FRAME_MAX_PX", raising=False)

    assert config.build_settings().face_frame_max_px == 960

    monkeypatch.setenv("FACE_FRAME_MAX_PX", "704")
    assert config.build_settings().face_frame_max_px == 704, "the current name must win"


def test_the_setting_is_the_punch_ceiling_the_framing_coach_assumes():
    """A regression guard on the value, because it is a *facial-recognition* parameter.

    The working-distance band (see the framing coach) needs a face to be ~112 px for the
    alignment template, which 640 could not hold beyond 0.7 m. 1280 was chosen for that, and
    a change here is a change to how far away a worker can stand and still match.
    """
    assert config.settings.face_frame_max_px == 1280


# ---------------------------------------------------------------------------
# 4. the ingestion boundary - what may be uploaded at all
# ---------------------------------------------------------------------------
def test_a_photo_above_the_pixel_limit_is_refused_with_the_sentence_a_client_acts_on():
    with pytest.raises(HTTPException) as refused:
        uploads.face_frame(_jpeg(OVER_PIXELS), field="selfie")

    assert refused.value.status_code == 422
    detail = refused.value.detail
    assert detail["error_code"] == uploads.ERR_RESOLUTION_TOO_HIGH
    assert detail["message"] == (
        "The selfie resolution exceeds the 4 MP limit (2048x2048). "
        "Please downscale or capture at standard resolution."
    )
    assert (detail["width"], detail["height"]) == OVER_PIXELS


def test_a_photo_above_the_long_edge_limit_names_the_limit_it_crossed():
    """2.1 MP is *under* the pixel limit, so a message about megapixels would be a lie."""
    with pytest.raises(HTTPException) as refused:
        uploads.face_frame(_jpeg(OVER_EDGE), field="selfie")

    detail = refused.value.detail
    assert detail["error_code"] == uploads.ERR_RESOLUTION_TOO_HIGH
    assert "2100x1000" in detail["message"] and "2048 px" in detail["message"]
    assert "MP limit" not in detail["message"]


def test_the_boundary_is_answered_from_the_header_without_decoding_anything(monkeypatch):
    """The refusal has to cost a header read, not a 12 MB bitmap - that is its whole point.

    A stand-in image that declares 20 MP and raises if anything asks it for pixels: an
    implementation that decoded first and checked afterwards would fail here rather than in
    production, where the failure is memory rather than an assertion.
    """

    class Undecodable:
        size = (5000, 4000)  # 20 MP: under the bomb ceiling, over the boundary

        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

        def convert(self, *args, **kwargs):  # pragma: no cover - the refusal must come first
            raise AssertionError("the photo was decoded before the boundary was checked")

    opener = Undecodable()
    monkeypatch.setattr(uploads, "_open_photo", lambda source: opener)

    with pytest.raises(HTTPException) as refused:
        uploads.face_frame(b"bytes that are never decoded", field="selfie")

    assert refused.value.detail["error_code"] == uploads.ERR_RESOLUTION_TOO_HIGH
    # The descriptor goes back before the refusal does. ``Image.open`` is lazy, so a refusal
    # raised on the declared size - the point of checking it early - otherwise leaves PIL
    # holding the file, and on Windows an open file cannot be unlinked: the spooled upload of
    # every oversized photo would sit there until the hourly sweep, silently (that is not
    # reachable on Linux, where unlinking an open file works, which is why this is pinned here
    # and by the endpoint-level test in ``test_punch_spool.py``).
    assert opener.closed, "the source handle was left open, so the upload cannot be removed"


def _header_only_png(width: int, height: int) -> bytes:
    """A PNG that *declares* a size and carries no pixels.

    Cheaper than building a 49 MP image to be refused, and a sharper test of the claim: the
    header is where both ceilings are answered from, so a file with nothing but a header is
    exactly the input they have to handle.
    """
    import struct
    import zlib

    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))

    header = chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    # An IDAT is present but empty so that ``Image.open`` parses the container (it reads the
    # chunk stream at open) and reports the declared size. Nothing here ever decodes: the
    # ceiling is answered from the header, which is the claim being tested.
    return (
        b"\x89PNG\r\n\x1a\n"
        + header
        + chunk(b"IDAT", zlib.compress(b""))
        + chunk(b"IEND", b"")
    )


def test_the_bomb_ceiling_still_answers_before_the_boundary_does():
    """Two different refusals for two different situations, and the code tells them apart.

    A 49 MP file is not a worker with a big camera; it is the case ``MAX_PIXELS`` exists for,
    and it keeps its own error code so an operator reading a log can tell the two apart.
    """
    with pytest.raises(HTTPException) as refused:
        uploads.face_frame(_header_only_png(7000, 7000), field="selfie")

    assert refused.value.status_code == 422
    assert refused.value.detail["error_code"] == uploads.ERR_TOO_MANY_PIXELS


def test_the_chain_resamples_exactly_once_for_every_accepted_upload():
    """No decode hint may come back: it resamples on a power-of-two grid.

    Pinned in the source rather than by outcome, because the hint's effect is a *different
    frame* for a large upload and looks perfectly reasonable in isolation - which is how it
    survived this long. One resample, one resampler, for both sides of the comparison.
    """
    source = (harness.BACKEND_DIR / "uploads.py").read_text(encoding="utf-8")

    assert ".draft(" not in source, "a coarse libjpeg decode hint is back in the chain"
    assert source.count(".thumbnail(") == 1, "the chain should resize in exactly one place"


def test_the_boundary_sits_between_a_canvas_capture_and_a_phone_camera():
    """The two real upload shapes, stated so that moving the boundary has to face them.

    A browser canvas capture is 1280x960 and must never be refused. A phone's own camera app
    produces 12 MP, so the file-picker paths in the client have to downscale first - the
    refusal says so, and the numbers are published for a client to obey.
    """
    settings = config.settings

    assert 1280 * 960 <= settings.face_frame_max_pixels
    assert 1280 <= settings.face_frame_max_edge_px
    assert settings.face_frame_max_pixels < 12_000_000
    assert settings.face_frame_max_edge_px < 4032


def test_the_boundary_is_configurable_from_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("ENV_FILE", str(tmp_path / "absent.env"))
    monkeypatch.setenv("FACE_FRAME_MAX_PIXELS", "5000000")
    monkeypatch.setenv("FACE_FRAME_MAX_EDGE_PX", "2560")

    settings = config.build_settings()

    assert settings.face_frame_max_pixels == 5_000_000
    assert settings.face_frame_max_edge_px == 2560


def test_the_policy_publishes_the_boundary_a_client_has_to_obey():
    """Enforced *and* published: a client cannot downscale to a limit it cannot read."""
    policy = uploads.policy()

    assert policy["face_frame_max_pixels"] == config.settings.face_frame_max_pixels
    assert policy["face_frame_max_edge_px"] == config.settings.face_frame_max_edge_px


# ---------------------------------------------------------------------------
# 5. one buffer per decode, and no handle left on the upload
# ---------------------------------------------------------------------------
# ``ImageOps.exif_transpose`` allocates a full-resolution copy of the photo *whether or not*
# it rotates anything, and that allocation lands while the decoder's own buffers are still
# live - so it, and not the later ``convert``, is what sets the peak of a decode. On a 3.1 MP
# upload at the default ceiling, asking for it only when there is something to rotate takes
# one frame from ~38 MB to ~26 MB; the measurements are in
# ``docs/FACE_VERIFICATION_MEMORY_REPORT.md``.
#
# This needs a test rather than a comment because the extra copy is *invisible*: the frame is
# byte-identical either way, so nothing about the output can catch its return - only the call
# can, and only while the photo is upright.
# ---------------------------------------------------------------------------
def _oriented_jpeg(size: tuple[int, int], orientation: int) -> bytes:
    """A JPEG carrying an EXIF orientation tag, the way a phone camera writes one."""
    buffer = io.BytesIO()
    exif = Image.Exif()
    exif[0x0112] = orientation
    Image.new("RGB", size, (120, 130, 140)).save(buffer, format="JPEG", quality=85, exif=exif)
    return buffer.getvalue()


def test_an_upright_photo_is_not_transposed(monkeypatch):
    """The transpose is *asked for* only when the tag says the pixels need rotating.

    Counted rather than observed: the frame it returns is byte-identical to the one a
    transpose would produce, and the extra full-resolution buffer is the only difference -
    which no comparison of the output can see. That is how the copy got in and stayed.
    """
    calls: list[object] = []
    real = ImageOps.exif_transpose

    def counting(image, *args, **kwargs):
        calls.append(image)
        return real(image, *args, **kwargs)

    monkeypatch.setattr(ImageOps, "exif_transpose", counting)

    frame = uploads.face_frame(_jpeg((1600, 1200)), field="selfie")

    assert calls == [], "an upright photo was transposed, paying a full-resolution copy"
    assert frame.size == (1280, 960)


def test_an_exif_rotated_photo_still_comes_back_upright(monkeypatch):
    """The fast path must not be the one that skips a rotation the photo *needs*.

    Orientation 6 is the portrait-phone case: the sensor wrote landscape pixels and the tag
    is what makes them portrait. A template stored at the wrong rotation rejects its owner
    forever, so the *smaller* frame here (960x1280) is the correct answer, and the landscape
    one would be a bug that only ever surfaces as a worker who can never match.
    """
    calls: list[object] = []
    real = ImageOps.exif_transpose

    def counting(image, *args, **kwargs):
        calls.append(image)
        return real(image, *args, **kwargs)

    monkeypatch.setattr(ImageOps, "exif_transpose", counting)

    frame = uploads.face_frame(_oriented_jpeg((1600, 1200), 6), field="selfie")

    assert calls, "the rotation the EXIF tag asked for was skipped"
    assert frame.size == (960, 1280), "the frame was not transposed"
    assert max(frame.size) <= config.settings.face_frame_max_px


def test_a_decoded_frame_leaves_no_handle_on_the_upload(tmp_path):
    """The property a spooled punch depends on: the file can go the moment the frame exists.

    ``main.verify_worker`` deletes its spool file as soon as the face job returns, and on
    Windows a file somebody still has open cannot be unlinked at all - so a frame that kept
    the descriptor would leave every punch's upload on disk until the hourly sweep, quietly.
    The *refusal* path is pinned by the header-only test above; this is the **success** path,
    which is the one whose frame outlives the request.
    """
    path = tmp_path / "upload.jpg"
    path.write_bytes(_jpeg((1600, 1200)))

    frame = uploads.face_frame(str(path), field="selfie")

    assert getattr(frame, "fp", None) is None, "the frame still holds the upload open"
    os.remove(path)  # would raise on Windows if the handle were still held
    frame.load()  # the pixels survived the file
    assert frame.size == (1280, 960)


# ---------------------------------------------------------------------------
# the peak, in bytes: one full-resolution buffer per decode
# ---------------------------------------------------------------------------
# The buffer count above is the exact, portable guard. This is the backstop that does not
# depend on knowing *which* operation allocated the buffer: it measures the peak of one
# decode and holds it against a second decode whose extra buffer is *required*, so a future
# change anywhere in the chain - a `.copy()`, a second `np.array`, a second decode - shows up
# as memory even though it leaves the frame byte-identical.
#
# Peak resident memory is a process-lifetime high-water mark and never comes back down, so
# the two measurements need two interpreters: in one process the second measurement would
# inherit the first's peak and always look free. And an absolute megabyte figure is a
# property of one allocator on one operating system, while this ships on another - which is
# why the assertion is a *comparison* between two runs on the same machine.
#
# The rotated photo is the calibration, and it is deliberately a *legitimate* path rather
# than a reconstruction of the old bug: its transpose is required, so its peak necessarily
# contains the extra full-resolution buffer and differs from the upright case by nothing else.
_DECODE_PEAK_PROBE = r'''
import ctypes, io, json, os, sys
sys.path.insert(0, os.path.abspath("."))
import numpy as np
from PIL import Image
import uploads


def peak_bytes():
    """Resident high-water mark, in bytes, on the three families this ships on."""
    if sys.platform == "win32":
        from ctypes import wintypes

        class PMC(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
            ]
        fn = ctypes.windll.psapi.GetProcessMemoryInfo
        fn.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
        fn.restype = wintypes.BOOL
        counters = PMC()
        counters.cb = ctypes.sizeof(counters)
        fn(ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(counters), ctypes.sizeof(counters))
        return int(counters.PeakWorkingSetSize)
    import resource
    rss = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return rss if sys.platform == "darwin" else rss * 1024   # KiB on Linux


mode = sys.argv[1]

if mode == "calibrate":
    # Can this allocator see one extra full-resolution buffer at all? Two 2048x1536 RGB
    # buffers, the second allocated while the first is live and nothing else happening. When
    # this measures ~0 the machine cannot answer the question, and the caller skips instead
    # of reading a level result as "the chain stopped allocating the buffer".
    held = Image.new("RGB", (2048, 1536))
    base = peak_bytes()
    extra = held.copy()
    print(json.dumps({"peak_above_base": peak_bytes() - base, "held": [held.size, extra.size]}))
    raise SystemExit(0)

rng = np.random.RandomState(0)
photo = Image.fromarray(rng.randint(0, 255, (1536, 2048, 3), dtype=np.uint8))
buffer = io.BytesIO()
exif = Image.Exif()
exif[0x0112] = int(mode)
photo.save(buffer, format="JPEG", quality=85, exif=exif)
data = buffer.getvalue()
del photo, buffer
# Built *before* the baseline is read, so the JPEG encoder's own peak is not charged to
# the decode.
base = peak_bytes()
frame = uploads.face_frame(data, field="selfie")
print(json.dumps({"peak_above_base": peak_bytes() - base, "size": list(frame.size)}))
'''

#: One 2048x1536 RGB photo, the size of the buffer a decode is allowed to allocate once.
_SOURCE_BUFFER = 2048 * 1536 * 3


def _decode_peak(mode: int | str) -> int:
    """Bytes of peak above baseline for one probe run, in a fresh interpreter.

    ``mode`` is an EXIF orientation to decode, or ``"calibrate"`` for the allocator
    sensitivity check.
    """
    result = subprocess.run(
        [sys.executable, "-c", _DECODE_PEAK_PROBE, str(mode)],
        cwd=harness.BACKEND_DIR,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if result.returncode != 0:
        pytest.skip(f"peak memory could not be measured here: {result.stderr.strip()[-200:]}")
    return int(json.loads(result.stdout.strip().splitlines()[-1])["peak_above_base"])


def test_an_upright_decode_does_not_pay_for_a_transpose_buffer():
    """One decode allocates one full-resolution buffer, and the peak is what says so.

    ``ImageOps.exif_transpose`` allocates a full-resolution copy of the photo, and it does so
    while the decoder's own buffers are still live - which is why it, and not the ``convert``
    that follows it, is the whole peak of a decode. Skipping it when the orientation says the
    pixels are already upright is worth ~12.5 MB per verification (measurements and method:
    ``docs/FACE_VERIFICATION_MEMORY_REPORT.md``).

    The regression this catches is invisible in the output - the frame is byte-identical
    either way - so it can only be seen in memory. A rotated photo is the calibration: its
    transpose is *required*, so its peak necessarily contains that extra buffer and differs
    from the upright case by nothing else. Holding one run against the other on the same
    machine is what makes this portable; an absolute megabyte figure is a property of one
    allocator on one operating system, and this ships on another.

    A third run establishes whether this allocator can see one extra full-resolution buffer
    *at all*, by allocating two 2048x1536 buffers with nothing else happening. Without that,
    a level result is ambiguous - "the allocator reused the memory" and "the chain stopped
    allocating it" measure the same - and the ambiguity would have to be resolved by skipping,
    which is also how a regression would hide. With it, an insensitive allocator skips (and
    ``test_an_upright_photo_is_not_transposed`` has the portable half covered) while a
    sensitive one that finds no difference at all reports a failure.
    """
    margin = _SOURCE_BUFFER // 4

    if _decode_peak("calibrate") <= margin:
        pytest.skip(
            "this allocator did not expose a single extra full-resolution buffer, so a "
            "regression would be invisible to this measurement"
        )

    upright = _decode_peak(1)
    rotated = _decode_peak(6)

    assert upright < rotated - margin, (
        f"an upright decode peaked at {upright} B against a rotated decode's {rotated} B - "
        f"inside a quarter of one {_SOURCE_BUFFER} B source buffer, on a machine that *can* "
        "see such a buffer - which means a full-resolution copy of the photo is being "
        "allocated again (docs/FACE_VERIFICATION_MEMORY_REPORT.md)"
    )
