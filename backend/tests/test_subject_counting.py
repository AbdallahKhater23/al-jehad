"""Who is in the frame: the count behind "more than one face is in the photo".

WHY THIS SUITE EXISTS
---------------------
Two rules in this application are enforced by *counting faces*, and both of them cost a worker
their clock-in when the count is wrong:

* the punch path refuses a frame with more than one face (``compare_faces_sync``), because a
  clock-in must not guess which person it is scoring;
* enrollment refuses one too, because the reference template is built from that frame.

A detector does not report people, it reports *responses*, and the difference shows up exactly at
the gate:

* **one face, two boxes.** YuNet driven at an unusual scale - this deployment raises the punch
  frame ceiling to 1280, well above the scale its heads are anchored for - and any detector run
  over overlapping tiles can fire twice on one face. The count then says "two people".
* **a speck counted as a person.** A 12 px face-shaped region of a poster, a wall photograph or a
  phone screen is not the person at the gate, and cannot be cropped into the pipeline's own
  112 px alignment template at all. The count said "two people" anyway.

Both produce a sentence about a person who is not there ("More than one face is in the photo. You
have to be the only person in the frame."), which a worker at a gate cannot act on. What this suite
pins is that the count stops lying **without** relaxing the rule that matters: two detections that
are each subject-sized are two people, and the punch is still refused.

The last section runs the real decision paths - ``main.compare_faces_sync`` and
``enrollment._embed_image`` - against a seeded template, so the fix is proven where it is used
rather than only where it is defined.
"""

from __future__ import annotations

import face_detector
import harness
from harness import MOALLEM


# ---------------------------------------------------------------------------
# helpers: detections in the shapes the pipeline actually passes around
# ---------------------------------------------------------------------------
def _face(width: int, height: int, x: int = 0, y: int = 0, *, embedding=None, score: float = 0.9):
    """One engine entry: an embedding, a score and - the point - the detection's geometry."""
    return {
        "embedding": embedding if embedding is not None else [0.1] * 8,
        "face_confidence": score,
        "facial_area": {"x": x, "y": y, "w": width, "h": height},
    }


def _sizes(report: face_detector.SubjectReport) -> list[int]:
    return [int(face["facial_area"]["w"]) for face in report.faces]


# ---------------------------------------------------------------------------
# 1. the counting rule
# ---------------------------------------------------------------------------
def test_the_narrowest_subject_is_tied_to_the_alignment_template():
    """The floor is a fact about this pipeline's crop, not a number somebody liked."""
    assert 16 <= face_detector.MIN_SUBJECT_PX < face_detector.ALIGNED_SIZE, (
        "a detection below the alignment template's usable floor cannot be the subject of a "
        "punch, and one above the template would refuse the very face it exists to crop"
    )


def test_one_face_found_twice_is_one_person():
    """The same face, overlapping and nested: containment is what catches the nested case."""
    overlapping = face_detector.subject_detections(
        [_face(200, 240, 100, 100), _face(190, 230, 110, 105)]
    )
    nested = face_detector.subject_detections(
        [_face(200, 240, 100, 100), _face(120, 150, 130, 120)]
    )

    assert overlapping.count == 1 and overlapping.merged == 1, overlapping.summary()
    assert nested.count == 1 and nested.merged == 1, nested.summary()
    assert _sizes(overlapping) == [200], "the larger box is the face; the other was the same face"
    assert _sizes(nested) == [200]


def test_a_speck_of_a_face_is_not_a_person():
    report = face_detector.subject_detections([_face(200, 240, 0, 0), _face(12, 14, 900, 40)])

    assert report.count == 1, report.summary()
    assert report.specks == [12]
    assert _sizes(report) == [200]


def test_a_frame_of_specks_has_no_usable_face():
    """The honest answer is "no face", so the worker is asked to step closer."""
    report = face_detector.subject_detections([_face(10, 12), _face(6, 8)])

    assert report.count == 0
    assert sorted(report.specks) == [6, 10]
    assert "no subject-sized face" in report.summary()


def test_two_people_are_still_two_people():
    """The rule the application enforces must survive the fix, untouched."""
    report = face_detector.subject_detections([_face(200, 240, 0, 0), _face(180, 210, 500, 20)])

    assert report.count == 2, report.summary()
    assert report.merged == 0 and report.specks == []
    assert _sizes(report) == [200, 180], "largest first: the subject is the near face"


def test_boxes_that_merely_touch_are_not_merged():
    """Merging is for one face found twice, not for two heads side by side."""
    report = face_detector.subject_detections([_face(200, 240, 0, 0), _face(90, 110, 170, 100)])
    assert report.count == 2, report.summary()


def test_detections_with_no_geometry_are_still_counted():
    """A caller that reports a face without a box must not have it silently dropped."""
    entries = [{"embedding": [0.2] * 8, "face_confidence": 0.9}]

    report = face_detector.subject_detections(entries)

    assert report.count == 1, "an un-measurable detection is a face the caller meant to report"
    assert report.merged == 0 and report.specks == []


def test_the_summary_names_what_was_ignored():
    """A log line saying "1 face" where the detector saw three hides the whole diagnosis."""
    report = face_detector.subject_detections(
        [_face(200, 240), _face(196, 236, 6, 6), _face(11, 13, 900, 40)]
    )
    text = report.summary()
    assert "1 subject-sized face(s) [200x240@0.90]" in text
    assert "1 duplicate box(es) merged" in text
    assert "1 speck(s) ignored [11]" in text


# ---------------------------------------------------------------------------
# 2. the punch path: the rule where it decides a worker's clock-in
# ---------------------------------------------------------------------------
def _seeded_embedding(monkeypatch, app_module) -> tuple[list[float], str]:
    """A template for a seeded worker, and the path the punch path will read it from."""
    import biometrics

    path = harness.seed_reference(MOALLEM)
    reference = biometrics.read_reference(str(path))
    return list(reference.embedding), str(path)


def test_a_punch_with_one_face_reported_twice_is_still_scored(app_module, monkeypatch):
    import face_engine
    import main

    embedding, reference_path = _seeded_embedding(monkeypatch, app_module)
    seen = {}

    def fake_represent(image, **kwargs):
        seen["faces"] = [
            _face(200, 240, 100, 100, embedding=embedding),
            _face(190, 230, 110, 105, embedding=embedding),
            _face(11, 13, 900, 40, embedding=[0.9] * 8, score=0.61),
        ]
        return seen["faces"]

    monkeypatch.setattr(face_engine.ENGINE, "represent_direct", fake_represent)

    result = main.compare_faces_sync(reference_path, None)

    assert result["error"] is None, (
        f"one face in two boxes plus a speck refused the punch: {result['error']}"
    )
    assert result["verified"] is True
    assert len(seen["faces"]) == 3, "premise: the detector really did report three detections"


def test_a_punch_with_two_real_faces_is_still_refused(app_module, monkeypatch):
    import face_engine
    import main

    embedding, reference_path = _seeded_embedding(monkeypatch, app_module)
    monkeypatch.setattr(
        face_engine.ENGINE,
        "represent_direct",
        lambda image, **kwargs: [
            _face(200, 240, 100, 100, embedding=embedding),
            _face(180, 210, 500, 120, embedding=[0.3] * 8),
        ],
    )

    result = main.compare_faces_sync(reference_path, None)

    assert result["verified"] is False
    assert result["error"] == "Multiple faces detected.", (
        "two subject-sized faces is two people, and the punch must not be scored"
    )
    assert result["faces"] == 2
    assert "2 subject-sized face(s) [200x240@0.90, 180x210@0.90]" in result["face_count_detail"], (
        "the record carries the shape and the score, so a logged refusal can be read without the frame"
    )


def test_a_punch_whose_only_detections_are_specks_asks_for_a_face(app_module, monkeypatch):
    """"More than one face" would be a lie about people who are not in the picture."""
    import face_engine
    import main

    _embedding, reference_path = _seeded_embedding(monkeypatch, app_module)
    monkeypatch.setattr(
        face_engine.ENGINE,
        "represent_direct",
        lambda image, **kwargs: [_face(10, 12), _face(7, 9, 500, 40)],
    )

    result = main.compare_faces_sync(reference_path, None)

    assert result["error"] == "No face detected.", result


# ---------------------------------------------------------------------------
# 3. the enrollment path: the same rule, where a template is written
# ---------------------------------------------------------------------------
def test_enrollment_accepts_a_duplicated_box_and_ignores_a_speck(app_module, monkeypatch):
    import face_engine
    import enrollment

    embedding = [0.4] * 8
    monkeypatch.setattr(
        face_engine.ENGINE,
        "represent_direct",
        lambda image, **kwargs: [
            _face(200, 240, 100, 100, embedding=embedding),
            _face(190, 230, 112, 108, embedding=embedding),
            _face(12, 14, 900, 40, embedding=[0.7] * 8),
        ],
    )

    assert enrollment._embed_image(_array()) == embedding


def test_enrollment_still_refuses_two_faces(app_module, monkeypatch):
    import face_engine
    import enrollment

    monkeypatch.setattr(
        face_engine.ENGINE,
        "represent_direct",
        lambda image, **kwargs: [_face(200, 240, 0, 0), _face(180, 210, 500, 40)],
    )

    try:
        enrollment._embed_image(_array())
    except ValueError as exc:
        assert "multiple faces" in str(exc)
    else:  # pragma: no cover - the failure path is the assertion
        raise AssertionError("two faces at enrollment must not write a template")


def test_enrollment_refuses_a_frame_of_specks_as_having_no_face(app_module, monkeypatch):
    import face_engine
    import enrollment

    monkeypatch.setattr(
        face_engine.ENGINE,
        "represent_direct",
        lambda image, **kwargs: [_face(9, 11)],
    )

    try:
        enrollment._embed_image(_array())
    except ValueError as exc:
        assert "no face" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("a frame of specks must not write a template")


def _array():
    """Whatever ``face_array`` would hand the engine: the stub ignores it."""
    import numpy as np

    return np.zeros((8, 8, 3), dtype=np.uint8)


def test_a_second_person_at_the_gate_is_still_refused_end_to_end(
    client, jpeg, app_module, monkeypatch, caplog
):
    """Through the real endpoint: 400 ``multiple_faces``, and a log line that names the count.

    The status is worth pinning because it is *not* the mismatch's: a punch refused for two people
    answers 400 with ``multiple_faces``, while a punch that was scored and did not clear the band
    answers 422 with ``face_mismatch``. An operator reading a log of 422s is looking at a different
    problem from one reading a log of 400s, and the two used to be indistinguishable in a support
    call.
    """
    import logging

    import face_engine

    monkeypatch.setattr(
        face_engine.ENGINE,
        "represent_direct",
        lambda image, **kwargs: [_face(200, 240, 0, 0), _face(180, 210, 500, 40)],
    )

    with caplog.at_level(logging.WARNING, logger="attendance.api"):
        response = harness.clock_in(client, MOALLEM, image=jpeg, headers=harness.bearer(MOALLEM))

    assert response.status_code == 400, response.text[:300]
    body = response.json()["detail"]
    assert body["error_code"] == "multiple_faces", body
    assert "only person in the frame" in body["message"]

    text = "\n".join(record.getMessage() for record in caplog.records)
    assert f"frame refused for worker {MOALLEM}" in text, text
    assert "2 subject-sized face(s) [200x240@0.90, 180x210@0.90]" in text, text
