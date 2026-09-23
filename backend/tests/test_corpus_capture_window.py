"""Calibration capture as a *period*: the window that ends it, and the frames only it can collect.

WHY THIS FILE EXISTS
--------------------
Two things stood between "capture is on" and a corpus that holds the small-face regime, and neither
was the switch:

1. **A collection period with no end.** ``CALIBRATION_CAPTURE_ENABLED`` is a boolean and an operator's
   request is a duration ("for a week"). Left as a calendar reminder, the deployment keeps collecting
   faces until somebody remembers to stop it - which is exactly the failure the switch's own comment
   warns about. ``CALIBRATION_CAPTURE_UNTIL`` makes the end a property of the deployment, and a value
   that cannot be read answers **closed**, because the only other reading of an unreadable deadline is
   "indefinitely", applied to a biometric store.

2. **The frames were never offered to the capture path.** A distant worker's punch is refused at the
   face check - and that refusal happens *before* the capture hook, so the one frame class the coverage
   question is about was discarded by control flow. Worse, the hook re-detected at the pipeline's own
   reach, so even when it ran it could only ever store faces the punch had already found: a corpus of
   successful punches is by construction the regime *above* the detector's limit. The refusal path now
   calls in, and the capture looks again at twice the reach.

What is pinned here, in the order the failure would happen:

* the window's four states, and that ``malformed`` and ``closed`` both stop capture;
* a capture taken inside a window is recorded, and one taken after it is not - with consent and the
  switch both still on, so the window is demonstrably the thing that stopped it;
* the refusal path stores an unlabelled specimen with the refusal named on it, at the widened reach
  and with provenance that says so - and nothing at all when neither pass finds a single face.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import corpus
import harness
from config import settings


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
class _StubDetector:
    """One face with the geometry a usable capture needs - the punch path's detector, stubbed."""

    def __init__(self, found: bool = True) -> None:
        self._found = found

    def __call__(self, image):
        if not self._found:
            return []
        return [
            {
                "landmarks": [
                    [150.0, 120.0], [162.0, 120.0], [156.0, 126.0], [152.0, 132.0], [160.0, 132.0],
                ],
                "facial_area": {"x": 140, "y": 110, "w": 100, "h": 110},
                "confidence": 0.97,
            }
        ]


def _stub_punch_detector(monkeypatch, *, found: bool = True) -> None:
    import face_detector

    monkeypatch.setattr(face_detector, "detect_landmarks", _StubDetector(found=found))
    monkeypatch.setattr(face_detector, "fingerprint", lambda: "c" * 16)
    monkeypatch.setattr(face_detector, "active_pipeline", lambda: "yunet-2023mar")


def _stub_widened_detector(monkeypatch, *, found: bool = True) -> None:
    """The wider pass, as ``detector_640`` yields it: a small face with a box and landmarks."""
    import detector_640

    class _SmallFace:
        box = (100.0, 90.0, 22.0, 24.0)
        landmarks = [
            (104.0, 98.0), (118.0, 98.0), (111.0, 104.0), (106.0, 110.0), (116.0, 110.0),
        ]
        score = 0.81

    class _Detector:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __call__(self, image):
            return [_SmallFace()] if found else []

    monkeypatch.setattr(detector_640, "build_detector", _Detector)


def _grant(worker_id: str = harness.WORKER) -> None:
    from database import db

    with db(write=True) as conn:
        corpus.record_consent(conn, worker_id=worker_id, granted=True, actor_id=worker_id, note="t")


def _image():
    from PIL import Image

    return Image.new("RGB", (640, 480), (120, 120, 120))


def _open_window(monkeypatch, *, hours: float = 24 * 7) -> None:
    """The switch on, a week left: the state an operator sets for a collection period."""
    monkeypatch.setattr(settings, "calibration_capture_enabled", True)
    until = datetime.now() + timedelta(hours=hours)
    monkeypatch.setattr(settings, "calibration_capture_until", until.isoformat() + "Z")


# ---------------------------------------------------------------------------
# the window's own states
# ---------------------------------------------------------------------------
def test_the_window_reads_as_off_when_the_switch_is_off(monkeypatch):
    monkeypatch.setattr(settings, "calibration_capture_enabled", False)
    monkeypatch.setattr(settings, "calibration_capture_until", "")

    window = corpus.capture_window()
    assert window.state == "off"
    assert window.open is False


def test_a_switch_with_no_deadline_is_open_ended_and_open(monkeypatch):
    """The pre-window behaviour, kept: an operator may still say "no end date" deliberately."""
    monkeypatch.setattr(settings, "calibration_capture_enabled", True)
    monkeypatch.setattr(settings, "calibration_capture_until", "")

    window = corpus.capture_window()
    assert window.state == "open_ended"
    assert window.open is True
    assert window.until == ""


def test_a_deadline_in_the_future_is_open_and_counts_down(monkeypatch):
    _open_window(monkeypatch, hours=48)

    window = corpus.capture_window()
    assert window.state == "open"
    assert window.open is True
    # Two days, less whatever rounding the call cost.
    assert window.seconds_remaining is not None
    assert 47 * 3600 < window.seconds_remaining <= 48 * 3600


@pytest.mark.parametrize(
    "until",
    ["2020-01-01T00:00:00Z", "2026-10-0", "next tuesday", "2026-13-45T99:99:99Z"],
)
def test_a_closed_or_unreadable_deadline_stops_capture(monkeypatch, until):
    """Both states refuse, and for different reasons - which is why they are two states.

    ``closed`` is the end of a period; ``malformed`` is a typo. Collapsing them into one boolean
    would make a typo look like a deliberate stop, and a deliberate stop look like a typo.
    """
    monkeypatch.setattr(settings, "calibration_capture_enabled", True)
    monkeypatch.setattr(settings, "calibration_capture_until", until)

    window = corpus.capture_window()
    assert window.open is False, f"{until!r} left capture open"
    assert window.state == "closed"


def test_a_window_is_read_as_utc_whatever_shape_it_arrives_in(monkeypatch):
    """``Z``, an offset and a naive instant all name the same moment, so all three must agree."""
    monkeypatch.setattr(settings, "calibration_capture_enabled", True)
    mark = datetime.now(timezone.utc) + timedelta(days=3)
    texts = [
        mark.strftime("%Y-%m-%dT%H:%M:%S") + "Z",
        mark.isoformat(),
        mark.replace(tzinfo=None).isoformat(),
    ]

    seconds = []
    for text in texts:
        monkeypatch.setattr(settings, "calibration_capture_until", text)
        window = corpus.capture_window()
        assert window.open is True, text
        seconds.append(window.seconds_remaining)

    assert max(seconds) - min(seconds) <= 2, seconds


# ---------------------------------------------------------------------------
# what the window gates
# ---------------------------------------------------------------------------
def test_a_capture_inside_the_window_is_recorded(monkeypatch):
    _stub_punch_detector(monkeypatch)
    _open_window(monkeypatch)
    _grant()

    assert corpus.maybe_capture_punch(_image(), worker_id=harness.WORKER, verdict="approved")
    assert len(corpus.sidecars()) == 1


def test_a_capture_after_the_window_is_refused_with_both_other_switches_still_on(monkeypatch):
    """The window is the thing that stopped it: consent is granted and the deployment switch is on."""
    _stub_punch_detector(monkeypatch)
    _stub_widened_detector(monkeypatch)
    _open_window(monkeypatch, hours=-1)
    _grant()

    assert settings.calibration_capture_enabled is True
    assert corpus.worker_consent(harness.WORKER) is True
    assert corpus.maybe_capture_punch(_image(), worker_id=harness.WORKER, verdict="approved") is None
    assert corpus.sidecars() == [], "a capture landed after the window closed"


def test_an_unreadable_window_refuses_rather_than_collecting_indefinitely(monkeypatch):
    """The fail-closed reading, pinned: the other reading of a bad deadline is "forever"."""
    _stub_punch_detector(monkeypatch)
    monkeypatch.setattr(settings, "calibration_capture_enabled", True)
    monkeypatch.setattr(settings, "calibration_capture_until", "2026-10-0")
    _grant()

    assert corpus.maybe_capture_punch(_image(), worker_id=harness.WORKER, verdict="approved") is None
    assert corpus.sidecars() == []


def test_the_window_does_not_replace_the_workers_own_consent(monkeypatch):
    """A window is the deployment's half. It must not become a way to collect without the other."""
    _stub_punch_detector(monkeypatch)
    _open_window(monkeypatch)

    assert corpus.maybe_capture_punch(_image(), worker_id=harness.WORKER, verdict="approved") is None
    assert corpus.sidecars() == []


# ---------------------------------------------------------------------------
# the frames only the refusal path can collect
# ---------------------------------------------------------------------------
def test_a_refused_frame_is_stored_unlabelled_with_the_refusal_named_on_it(monkeypatch):
    """The small-face specimen: a frame the punch could not use, kept as evidence about reach.

    Unlabelled on purpose. ``approved`` is the only verdict that established an identity; a refusal
    is the *absence* of that claim, so naming the worker here would be the corpus inventing a label
    out of a successful login - the one thing ``LABEL_CARRYING_VERDICTS`` exists to prevent.
    """
    _stub_punch_detector(monkeypatch)
    _open_window(monkeypatch)
    _grant()

    capture_id = corpus.maybe_capture_punch(
        _image(), worker_id=harness.WORKER, verdict=None, refused_reason="No face detected."
    )
    assert capture_id, "the refused frame was not stored"

    record = {r.capture_id: r for r in corpus.sidecars()}[capture_id]
    assert record.identity is None, "a refused frame must not carry the punching worker as a label"
    assert record.consent == corpus.WORKER_CONSENT
    assert "refused=No face detected." in (record.note or "")
    assert record.quality is not None and record.quality.get("hard_case") is not None


def test_a_face_only_the_wide_pass_can_see_is_captured_as_a_widened_specimen(monkeypatch):
    """The point of the whole exercise: the pipeline found nothing, the wider pass did.

    A corpus built from successful punches can only hold faces *above* the pipeline's reach; this is
    the path that puts one below it in, with provenance that says which pass found it so nobody reads
    the record as "the punch detector saw this".
    """
    _stub_punch_detector(monkeypatch, found=False)
    _stub_widened_detector(monkeypatch)
    _open_window(monkeypatch)
    _grant()

    capture_id = corpus.maybe_capture_punch(_image(), worker_id=harness.WORKER, verdict="approved")
    assert capture_id, "the specimen the punch could not see was not stored"

    record = {r.capture_id: r for r in corpus.sidecars()}[capture_id]
    assert record.detector["input_size"] == corpus.WIDENED_REACH_INPUT_SIZE
    assert "reach=widened" in (record.note or "")
    assert record.identity == harness.WORKER, "an approved punch still carries its label"


def test_nothing_is_stored_when_neither_pass_finds_a_single_face(monkeypatch):
    """A frame with no landmarks is a frame, not a corpus entry - the store's own rule, kept."""
    _stub_punch_detector(monkeypatch, found=False)
    _stub_widened_detector(monkeypatch, found=False)
    _open_window(monkeypatch)
    _grant()

    assert corpus.maybe_capture_punch(_image(), worker_id=harness.WORKER, verdict="approved") is None
    assert corpus.sidecars() == []


# ---------------------------------------------------------------------------
# the operator's view
# ---------------------------------------------------------------------------
def test_a_punch_the_face_check_refuses_still_writes_a_specimen(client, jpeg, app_module, monkeypatch):
    """End to end, through the route: the refusal path is the one that can hold a distant frame.

    Before this, the refusal ended the request *above* the capture hook, so the frame class the
    coverage question is about could not enter the corpus at all - no switch, no consent and no
    corpus setting could change that, because the code path was never reached.
    """
    # The punch's own detector finds nothing - the distant worker - so the capture must fall
    # through to the wide pass. Stubbed explicitly rather than inherited, so this test states the
    # frame it is about instead of depending on the shared stub's geometry.
    _stub_punch_detector(monkeypatch, found=False)
    _stub_widened_detector(monkeypatch)
    _open_window(monkeypatch)
    _grant(harness.MOALLEM)
    monkeypatch.setattr(
        app_module,
        "compare_faces_sync",
        lambda *a, **k: {"verified": False, "distance": 99.9, "error": "No face detected."},
    )

    response = harness.clock_in(
        client, harness.MOALLEM, image=jpeg, headers=harness.bearer(harness.MOALLEM)
    )
    assert response.status_code == 400, response.text[:200]
    assert response.json()["detail"]["error_code"] == "face_not_found"

    records = corpus.sidecars()
    assert len(records) == 1, "the refused frame was not kept as a specimen"
    assert records[0].identity is None
    assert "refused=No face detected." in (records[0].note or "")


def test_a_refused_punch_is_not_captured_without_the_workers_consent(
    client, jpeg, app_module, monkeypatch
):
    """The refusal path is an extra store, not an exception to the consent rule."""
    _stub_punch_detector(monkeypatch, found=False)
    _stub_widened_detector(monkeypatch)
    _open_window(monkeypatch)
    monkeypatch.setattr(
        app_module,
        "compare_faces_sync",
        lambda *a, **k: {"verified": False, "distance": 99.9, "error": "No face detected."},
    )

    assert harness.clock_in(
        client, harness.MOALLEM, image=jpeg, headers=harness.bearer(harness.MOALLEM)
    ).status_code == 400
    assert corpus.sidecars() == [], "a refused frame was captured from a worker who never opted in"


def test_the_operator_can_read_the_window_beside_the_consents(app_module, client, monkeypatch):
    """An end date nobody can see is an end date nobody relies on."""
    _open_window(monkeypatch, hours=24 * 7)

    body = client.get("/api/v1/admin/corpus_consents", headers=harness.bearer(harness.HEAD_ADMIN)).json()
    assert body["capture_enabled"] is True
    assert body["capture_window"]["state"] == "open"
    assert body["capture_window"]["until"]
    assert body["capture_window"]["seconds_remaining"] > 0
