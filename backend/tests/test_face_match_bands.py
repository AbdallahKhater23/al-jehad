"""Where a face distance becomes a verdict, and what the lines were derived from.

The defect these tests exist for: the approve and refuse lines used to be two literals in
``main.py`` - 0.40 and 0.60, DeepFace's published numbers for *DeepFace's* crop - and nothing
recorded what they had been measured against. Two crops later, a punch scored under YuNet was
still being read against a band derived for MTCNN's rectangle, and the only thing keeping the
review queue meaningful was that both numbers happened to sit inside the new window.

So the lines are not literals any more. They belong to a ``(pipeline, model)`` pair, they are
recomputed from the boundaries they were measured against, and a pipeline this build can run
without one is a build that refuses to serve rather than one that guesses. The tests below
hold each of those three statements up: the rule reproduces the shipped numbers, every
pipeline that can really run is covered, and a distance is decided by the band rather than by
a comparison spelled out at a call site.

WHAT THIS FILE CANNOT SEE
-------------------------
The boundaries themselves are measurements, not assertions - a test cannot re-derive 0.676
from a corpus this repository does not ship. What it can do, and does, is refuse a line that
has drifted away from the boundary it claims to have come from.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import replace

import pytest

import face_detector
import harness
from harness import MOALLEM, bearer, clock_in


#: Every band this build can score with, by pipeline name. Collection-time, because a
#: parametrised test has to name its cases before it runs - and a band added or removed is
#: exactly what these tests exist to notice.
BAND_KEYS = sorted(face_detector.BANDS)


def _band_rows():
    """``(label, band)``: the fixture table this suite scores with, then the shipped one.

    Both halves are held to the same invariants on purpose. ``face_detector.BANDS`` in a test is
    ``harness.install_test_band``'s calibration; ``harness.SHIPPED_BANDS`` is that table as a
    *deployment* loads it, copied before the fixture replaced it. A file that only ever checked
    the first keeps passing while the second is empty - which it did, until the startup gate
    refused to open the port.
    """
    rows = [(key, face_detector.BANDS[key]) for key in BAND_KEYS]
    return rows + [(f"shipped:{key}", harness.SHIPPED_BANDS[key]) for key in sorted(harness.SHIPPED_BANDS)]


# ---------------------------------------------------------------------------
# 1. the rule reproduces the shipped numbers
# ---------------------------------------------------------------------------
def test_every_band_is_what_its_own_evidence_derives():
    """The lines are outputs of ``derived()``, not numbers typed beside it.

    This is the test that makes the table re-derivable rather than re-typed: editing ``approve``
    without editing the boundary behind it, or the reverse, fails here. A crop change therefore
    cannot move a line without leaving the measurement that justifies it.
    """
    for key, band in _band_rows():
        assert (band.approve, band.review) == band.derived(), (
            f"{key}: the shipped lines are not what the rule produces from "
            f"genuine_ceiling={band.genuine_ceiling} / impostor_floor={band.impostor_floor}"
        )


def test_each_line_keeps_a_stated_margin_to_the_boundary_it_came_from():
    """Above every genuine pair this deployment produced; below every impostor pair measured.

    The exact placement of both lines - including ``IMPOSTOR_MARGIN`` and the narrow-window
    clamp - is pinned by ``test_every_band_is_what_its_own_evidence_derives``, since the lines
    are defined as ``derived()``'s output. What is asserted here is the *safety* invariant that
    survives either shape of band: the approve line clears the highest same-person distance, and
    no line reaches the closest different-people distance.
    """
    for key, band in _band_rows():
        assert band.approve > band.genuine_ceiling, (
            f"{key}: the approve line sits at or below the highest same-person distance measured "
            f"for this pipeline ({band.genuine_ceiling}) - that is a false rejection designed in"
        )
        assert band.approve < band.impostor_floor, (
            f"{key}: the approve line reaches the closest observed impostor "
            f"({band.impostor_floor}), so the wrong person would be let in"
        )
        assert band.review <= band.impostor_floor, (
            f"{key}: the refuse line is at or above the closest observed impostor "
            f"({band.impostor_floor}), so a confirmed impostor would be sent for review "
            "instead of refused"
        )
        assert band.approve <= band.review, (
            f"{key}: the two lines are inverted, so no capture is ever classified at all - the "
            "rule is supposed to order them (see ``MatchBand.derived``)"
        )
        assert band.basis(), f"{key}: a band with no derivation is the defect this table replaced"


def test_the_boundaries_are_reported_with_the_numbers_a_reviewer_needs():
    """``basis()`` names both boundaries and both margins, because the lines are unreadable alone."""
    for key, band in _band_rows():
        basis = band.basis()
        assert f"{band.genuine_ceiling:.3f}" in basis and f"{band.impostor_floor:.3f}" in basis, basis
        assert f"{band.approve:.2f}" in basis and f"{band.review:.2f}" in basis, basis


# ---------------------------------------------------------------------------
# 2. a distance is decided by the band
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("key", BAND_KEYS)
def test_the_boundaries_decide_as_documented(key):
    """Inclusive at both lines, exact at the top of each band, and one step past them.

    Both shapes of band are covered: a two-line band routes the window between its lines to a
    human, and a one-line band - a measured window too narrow for ``IMPOSTOR_MARGIN`` to
    separate - has no window at all, so a hair above the line is already a refusal rather than a
    review. Asserting the two-line behaviour against a one-line band is how a suite starts
    demanding a review queue the corpus cannot support.
    """
    band = face_detector.BANDS[key]
    assert band.classify(0.0) == face_detector.MATCH_APPROVED
    assert band.classify(band.approve) == face_detector.MATCH_APPROVED
    assert band.classify(band.review + 0.001) == face_detector.MATCH_REFUSED
    assert band.classify(99.9) == face_detector.MATCH_REFUSED
    if band.approve < band.review:
        assert band.classify(band.approve + 0.001) == face_detector.MATCH_REVIEW
        assert band.classify(round((band.approve + band.review) / 2, 4)) == face_detector.MATCH_REVIEW
        assert band.classify(band.review) == face_detector.MATCH_REVIEW
    else:
        assert band.classify(band.approve + 0.001) == face_detector.MATCH_REFUSED


def _band_from(ceiling: float, floor: float, evidence: str) -> face_detector.MatchBand:
    """A band *carrying* the lines the rule derives, which is how every real band is built.

    ``derived()`` returns its two lines; it does not write them back into the record, because the
    record is frozen. A seed built with ``approve=0.0, review=0.0`` therefore still has *those*
    lines when ``classify`` is called on it, and a test that classifies the seed is asking a band
    with no lines at all: everything above 0.0 comes back refused, and an assertion that expects
    an approval fails while one that expects a refusal passes for the wrong reason. The tool and
    the harness both construct a band the two-step way, so this does too.
    """
    seed = face_detector.MatchBand(
        model="Facenet",
        approve=0.0,
        review=0.0,
        genuine_ceiling=ceiling,
        impostor_floor=floor,
        evidence=evidence,
    )
    approve, review = seed.derived()
    return replace(seed, approve=approve, review=review)


def test_a_window_too_narrow_for_the_margin_yields_a_one_line_band_never_an_inverted_one():
    """The clamp, on the real window that produced it and on a window that does not need it.

    ``0.4964 / 0.5377`` is the genuine ceiling and impostor floor this project's own FaceNet
    corpus measures - the window the *shipped* band was derived from. The two placement rules -
    geometric centre for approve, ``IMPOSTOR_MARGIN`` below the floor for review - need the floor
    to be about ``IMPOSTOR_MARGIN ** 2`` times the ceiling, which those numbers are not, so the
    unclamped rule returns a review line *below* the approve line. The clamp turns that into a
    one-line band; the second half of the test is what stops the clamp from turning every band
    into one, since a window with room for the margin keeps a review tier that a middling capture
    is routed to.
    """
    narrow = _band_from(
        0.4964, 0.5377, "a real measured window, narrower than the margin rule can separate"
    )
    assert (narrow.approve, narrow.review) == (0.50, 0.50)
    assert narrow.classify(0.50) == face_detector.MATCH_APPROVED
    assert narrow.classify(0.51) == face_detector.MATCH_REFUSED
    assert "no review window" in narrow.basis(), narrow.basis()

    wide = _band_from(0.20, 0.65, "a window the margin rule separates comfortably")
    assert (wide.approve, wide.review) == (0.35, 0.50)
    assert wide.approve < wide.review
    assert wide.classify(0.50) == face_detector.MATCH_REVIEW
    assert wide.classify(0.51) == face_detector.MATCH_REFUSED
    assert "no review window" not in wide.basis(), wide.basis()


# ---------------------------------------------------------------------------
# 3. the lines follow the pipeline that would really run
# ---------------------------------------------------------------------------
def test_every_pipeline_this_build_can_run_has_a_band():
    """The binding. ``active()`` can answer two names, and both have to be covered.

    ``PIPELINE`` and ``FALLBACK_PIPELINE`` are the only pipelines a template can be written
    with or scored under, so the table's keys must be exactly those two - no more (an orphan
    band is a line nobody re-derives when its crop leaves) and no fewer (a name with no band is
    a build that cannot score). This is the assertion that makes bumping ``PIPELINE`` without
    measuring a red suite rather than an inheritance.

    The table read here is populated by ``harness.install_test_band``. The fixture exists because
    a test deployment has no corpus to measure and no real detector - every embedding it will see
    comes from ``FAKE_ENGINE`` - so it supplies a calibration through the same rule a real one
    uses (see that function for why the fixture and a measurement are different things). The
    coverage a *deployment* needs is asserted separately, against ``harness.SHIPPED_BANDS``.
    """
    import face_engine

    assert set(face_detector.BANDS) == {face_detector.PIPELINE, face_detector.FALLBACK_PIPELINE}
    for pipeline, band in _band_rows():
        assert band.model == face_engine.FACE_MODEL, (
            f"{pipeline}: the band records the {band.model!r} embedding, this build scores with "
            f"{face_engine.FACE_MODEL!r} - the numbers were measured in another space"
        )


def test_the_table_a_deployment_loads_covers_the_pipeline_the_gate_runs():
    """The gate's own requirement, on the table a boot reads rather than on the fixture.

    Every other test here scores against ``harness.install_test_band``, which is what keeps the
    suite runnable and also what hid this: the shipped table was emptied when the VGG bands were
    retired, the fixture kept the suite green for as long as nobody looked, and the first thing
    to notice was the startup gate refusing to serve a real deployment. ``face_match_band`` is
    FATAL with no degraded mode - a build that cannot order the two lines can only approve
    against another crop's numbers or refuse every punch - so the requirement belongs in the
    suite the derivation tool tells an operator to run afterwards.

    The fallback is deliberately **not** required. ``FALLBACK_PIPELINE`` is selected only while
    the live crop's detector model is missing or fails its fingerprint, and a band for it would
    have to be measured through a crop this build cannot currently produce: the tool embeds via
    ``face_detector.detect_and_align``, the live pipeline's own detector, so a run labelled with
    any other pipeline's name would put a measurement taken on one crop behind another crop's
    name - the defect this table was introduced to end, one paste closer to production. An
    uncovered fallback is therefore a *stated* state rather than a hole, and what makes it safe
    is the refusal itself: a host on that crop does not score, and ``readiness`` says so.
    """
    shipped = harness.SHIPPED_BANDS
    assert face_detector.PIPELINE in shipped, (
        f"the shipped face_detector.BANDS has no band for {face_detector.PIPELINE!r}, the pipeline "
        "this build writes templates with: the startup gate refuses to serve, and the fixture "
        "band in tests/harness.py hides that from every other test in this file. Derive it with "
        "tools/derive_facenet_band.py against a labelled corpus and paste the MatchBand in"
    )
    orphans = sorted(set(shipped) - {face_detector.PIPELINE, face_detector.FALLBACK_PIPELINE})
    assert not orphans, (
        f"the shipped table carries bands for {orphans}, which are not pipelines this build can "
        "select: a line for a crop nobody produces is a line nobody re-derives when it leaves"
    )


#: Labels the derivation tool must refuse: every pipeline it could be asked for except the one
#: this build really runs. ``FALLBACK_PIPELINE`` is in here on purpose rather than as a
#: hypothetical - ``active()`` names it right beside the live pipeline, so covering the
#: deploy-without-a-detector case by labelling a measurement with it is the natural next move -
#: and it is dropped only when the real detector is unavailable, because then it *is* the live
#: pipeline and measuring it is the point. Read from ``REAL_FACE_DETECTOR`` because the harness
#: has stubbed the detector this file's in-process answer would be read from.
REAL_LIVE_PIPELINE = (
    face_detector.PIPELINE
    if harness.REAL_FACE_DETECTOR["available"]()[0]
    else face_detector.FALLBACK_PIPELINE
)
MISLABELS = [
    label
    for label in (face_detector.FALLBACK_PIPELINE, face_detector.PIPELINE, "yunet-2099")
    if label != REAL_LIVE_PIPELINE
]


@pytest.mark.parametrize("label", MISLABELS)
def test_the_derivation_tool_refuses_to_label_a_crop_it_did_not_measure(tmp_path, label):
    """The tool's own refusal, because the next ``--pipeline mtcnn`` looks like the right answer.

    Every embedding the tool produces comes from ``face_detector.detect_and_align``, the *live*
    pipeline's own detector, so a band carrying another pipeline's name would be one crop's
    numbers behind another crop's name - and nothing downstream could tell, because the numbers
    look measured and the crop they came from is not in them. That is the defect this table was
    emptied to end, arriving by the tool that exists to fill it.

    Refused before the corpus is read, since the label is a claim about the measurement and no
    amount of labelled data can make it true: the stub below is deliberately not a face. A test
    that had to reach detection to see the refusal would be testing the corpus, not the flag.
    """
    corpus = tmp_path / "corpus" / "alice"
    corpus.mkdir(parents=True)
    (corpus / "01.jpg").write_bytes(b"not a face")

    completed = subprocess.run(
        [
            sys.executable,
            "tools/derive_facenet_band.py",
            "--corpus",
            str(tmp_path / "corpus"),
            "--pipeline",
            label,
        ],
        cwd=str(harness.BACKEND_DIR),
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert completed.returncode == 2, completed.stdout + completed.stderr
    assert f"{label!r} names a crop this build cannot embed through" in completed.stderr, completed.stderr
    assert f"Measure {REAL_LIVE_PIPELINE!r}" in completed.stderr, completed.stderr
    assert "Paste into" not in completed.stdout, "a refused run must emit nothing paste-ready"


def test_the_table_is_written_out_rather_than_built_from_the_constants(monkeypatch):
    """A bumped pipeline name must find *nothing*, not a band that grew with it.

    The one design mistake worth a test of its own: a table keyed by ``PIPELINE`` would answer
    for whatever the constant was changed to, so the bump would silently keep the previous
    crop's lines - the exact defect this table was introduced to end. Found by this file, and
    it is why the keys are literals.
    """
    monkeypatch.setattr(face_detector, "PIPELINE", "yunet-2099")
    with pytest.raises(face_detector.UnknownPipelineError):
        face_detector.active_band()
    assert set(face_detector.BANDS) == {face_detector.FALLBACK_PIPELINE, "yunet-2023mar"}


def test_the_live_pipeline_chooses_the_band(monkeypatch):
    """A host without the detector model scores against *that* pipeline's lines.

    Both bands follow the same rule, and they are still different objects with different
    evidence: separate measurements, separate lines. A single shared band would pass the rule
    test above and fail here.
    """
    monkeypatch.setattr(face_detector, "_ensure", lambda: (True, ""))
    available = face_detector.active_band()
    assert available is face_detector.band_for(face_detector.PIPELINE)

    monkeypatch.setattr(face_detector, "_ensure", lambda: (False, "no model"))
    fallback = face_detector.active_band()
    assert fallback is face_detector.band_for(face_detector.FALLBACK_PIPELINE)
    assert fallback is not available
    assert (fallback.impostor_floor, fallback.genuine_ceiling) != (
        available.impostor_floor,
        available.genuine_ceiling,
    ), "each band has to carry the boundaries it was measured against, not the live crop's"


def test_a_pipeline_without_a_band_is_refused_rather_than_defaulted():
    """No default, because a default is the inherited number this whole change is about."""
    with pytest.raises(face_detector.UnknownPipelineError) as caught:
        face_detector.band_for("yunet-2099")
    assert "yunet-2099" in str(caught.value)

    # The other way to reach the same state without renaming anything: a model swap keeps the
    # pipeline's name, and the lines measured for the old embedding must not come with it.
    with pytest.raises(face_detector.UnknownPipelineError) as swapped:
        face_detector.band_for(face_detector.PIPELINE, "ArcFace")
    assert "ArcFace" in str(swapped.value) and "Facenet" in str(swapped.value)


def test_the_startup_gate_refuses_to_serve_without_a_band(monkeypatch):
    """FATAL, and it must say which pipeline - the operator's next move is the measurement."""
    import readiness

    check = readiness._check_face_match_band({})
    assert check.name == "face_match_band"
    assert check.tier == readiness.TIER_FATAL
    assert check.ok is True, check.detail
    # The derivation, not just the verdict: the number an operator wants when a review queue
    # moves is what the line was measured against.
    assert "genuine ceiling" in check.detail and check.value["approve"] == face_detector.active_band().approve

    monkeypatch.setattr(face_detector, "PIPELINE", "yunet-2099")
    broken = readiness._check_face_match_band({})
    assert broken.ok is False and broken.tier == readiness.TIER_FATAL
    assert "yunet-2099" in broken.detail, broken.detail


# ---------------------------------------------------------------------------
# 4. the verdict path asks the band
# ---------------------------------------------------------------------------
@pytest.mark.regression
def test_a_punch_scored_past_the_review_line_is_refused_not_merely_flagged(client, jpeg, app_module, monkeypatch):
    """The behaviour the derived refuse line changes, end to end through a real punch.

    Under the inherited band (0.60) a 0.55 distance was logged as pending_review and the
    worker kept their shift. Measured, no impostor pair on any corpus this deployment could
    build sits below 0.676, so the 0.50-0.60 zone holds nothing but unmeasured risk - and a
    punch there is now the honest answer, a 422 that asks for another photograph.

    422, never 401: a mismatch is a fact about this frame, not about the session.
    """
    assert face_detector.active_band().review < 0.55, "premise of this test: 0.55 is past the refuse line"
    monkeypatch.setattr(
        app_module,
        "compare_faces_sync",
        lambda *a, **k: {"verified": False, "distance": 0.55, "error": None},
    )
    response = clock_in(client, MOALLEM, image=jpeg, headers=bearer(MOALLEM))
    assert response.status_code == 422, f"expected a refusal, got {response.status_code}: {response.text[:200]}"
    assert response.json()["detail"]["error_code"] == "face_mismatch"


@pytest.mark.regression
def test_a_punch_inside_the_review_band_is_still_logged_not_refused(client, jpeg, app_module, monkeypatch):
    """The other half: the review band still exists, and it still keeps the shift.

    Without this, tightening the refuse line could be tightened all the way to the approve
    line - every ambiguous capture refused at the gate - and the first test would not notice.
    """
    band = face_detector.active_band()
    middling = round((band.approve + band.review) / 2, 4)
    monkeypatch.setattr(
        app_module,
        "compare_faces_sync",
        lambda *a, **k: {"verified": False, "distance": middling, "error": None},
    )
    response = clock_in(client, MOALLEM, image=jpeg, headers=bearer(MOALLEM))
    assert response.status_code == 200, response.text[:200]
    assert response.json()["status"] == "flagged", response.json()


@pytest.mark.regression
def test_a_capture_at_the_approve_line_is_approved(client, jpeg, app_module, monkeypatch):
    """Inclusive at the top of the band, end to end: the line is a decision, not a warning."""
    monkeypatch.setattr(
        app_module,
        "compare_faces_sync",
        lambda *a, **k: {"verified": True, "distance": face_detector.active_band().approve, "error": None},
    )
    response = clock_in(client, MOALLEM, image=jpeg, headers=bearer(MOALLEM))
    assert response.status_code == 200, response.text[:200]
    assert response.json()["status"] == "success", response.json()


@pytest.mark.regression
def test_a_punch_this_build_cannot_score_is_answered_as_our_fault(client, jpeg, monkeypatch):
    """A build that bumped the pipeline and forgot the lines refuses, loudly, and blames itself.

    This is the failure the binding turns a silent one into: a template written by this build,
    whose pipeline has no band. Before the table existed the two inherited numbers would have
    been applied to a crop they were never measured for - wrong verdicts, no symptom. Now the
    check answers with the generic sentence (the worker can do nothing about a build) and a 500
    that says on the server side which pipeline is missing its measurement.
    """
    monkeypatch.setattr(face_detector, "PIPELINE", "yunet-2099")
    # The template this build would write: same account, same vector, provenance included. The
    # staleness check passes - the requirement is only that the crop is the live one.
    harness.seed_reference(MOALLEM)

    response = clock_in(client, MOALLEM, image=jpeg, headers=bearer(MOALLEM))
    assert response.status_code == 500, response.text[:200]
    assert response.json()["detail"]["error_code"] == "face_check_failed"
    assert "thresholds" not in response.text and "yunet-2099" not in response.text, (
        "the pipeline name is the operator's business, not the worker's"
    )
