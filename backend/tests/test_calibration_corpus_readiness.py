"""The calibration corpus, reported at every boot.

The provisional face-match band was derived from public portraits because nothing in the
deployment said "your own traffic is the corpus the tool is asking for" - and the operator
who asks "has it been a few days yet?" had no answer but a shell session on the server.
``readiness._check_calibration_corpus`` is that answer: advisory, never fatal, reporting in
the terms ``derive_facenet_band.py`` itself consumes so the check and a derivation run can
never disagree about what the corpus holds.

This suite pins each state the check can report:

* capture off - the honest "not collecting" answer, not a silent pass;
* collecting - the countdown, with the counts a human needs to judge the wait;
* ready - the derivation instruction, only when ``corpus.stats()`` says the floor is
  supportable;
* unreadable store - reported, never raised, because an advisory check that throws is a
  broken check, and a broken check must not hide the others;
* membership - the check is in ``readiness.CHECKS``, so a refactor that drops it fails here
  rather than silencing the boot log.
"""

from __future__ import annotations

import pytest

import readiness


@pytest.fixture()
def _capture_on(monkeypatch):
    """Capture enabled, with the corpus pointed somewhere the check never reads twice."""
    import config

    monkeypatch.setattr(config.settings, "calibration_capture_enabled", True)


def _corpus_check() -> readiness.Check:
    checks, _ = readiness.run_checks()
    matches = [check for check in checks if check.name == "calibration_corpus"]
    assert len(matches) == 1, "the check must run exactly once"
    return matches[0]


def _reported(check: readiness.Check) -> dict:
    """The check's structured report (``Check.value``), always a dict for this check."""
    assert isinstance(check.value, dict), check.value
    return check.value


def test_the_check_is_in_the_startup_surface():
    assert readiness._check_calibration_corpus in readiness.CHECKS, (
        "the corpus answer belongs in the boot report: a check that exists but never runs "
        "is silence with extra steps"
    )


def test_the_check_is_advisory_and_passes_when_capture_is_off():
    """Off is a legitimate configuration, so it is reported, not failed."""
    check = _corpus_check()
    assert check.tier == readiness.TIER_ADVISORY
    assert check.ok is True
    assert _reported(check).get("capture_enabled") is False
    assert "CALIBRATION_CAPTURE_ENABLED" in check.detail, check.detail


def test_collecting_reports_the_counts_a_decision_needs(_capture_on, monkeypatch, tmp_path):
    """The countdown names what it has, not just that it is short."""
    import corpus

    summary = corpus.stats()
    assert summary["can_support_floor"] is False, "the temp corpus must not be the live one"

    check = _corpus_check()
    report = _reported(check)
    assert check.ok is True
    assert report.get("capture_enabled") is True
    assert report.get("captures") == summary["captures"]
    assert report.get("impostor_pairs") == summary["impostor_pairs"]
    assert "collecting" in check.detail, check.detail
    assert "derive" not in check.detail.lower() or "back in a few days" in check.detail, (
        "a corpus short of the floor must not instruct a derivation"
    )


def test_ready_names_the_derivation_and_the_runbook(_capture_on, monkeypatch):
    """The moment the corpus can support a floor, the check says exactly what to run."""
    import corpus

    real_stats = corpus.stats

    def ready_stats(*, include_hard_cases=False, include_unlabelled=False):
        summary = real_stats()
        summary.update(
            {
                "captures": 40,
                "labelled": 40,
                "unlabelled": 0,
                "identities": 6,
                "genuine_pairs": 12,
                "impostor_pairs": 768,
                "can_support_floor": True,
            }
        )
        return summary

    monkeypatch.setattr(corpus, "stats", ready_stats)
    check = _corpus_check()
    assert check.ok is True
    assert _reported(check).get("can_support_floor") is True
    assert "derive_facenet_band.py" in check.detail, check.detail
    assert "RUNBOOK_FACE_BAND" in check.detail, check.detail
    assert "face_detector.BANDS" in check.detail, check.detail
    assert "40 labelled capture(s)" in check.detail, check.detail


def test_an_unreadable_store_is_reported_never_raised(_capture_on, monkeypatch):
    """An advisory check that throws becomes 'check raised' - which hides the reason."""
    import corpus

    def broken_stats():
        raise OSError("corpus dir vanished")

    monkeypatch.setattr(corpus, "stats", broken_stats)
    check = _corpus_check()
    assert check.ok is True, "an unreadable corpus is not a broken deployment"
    assert "could not be checked" in check.detail, check.detail
    assert "corpus dir vanished" in check.detail, check.detail


def test_the_check_agrees_with_what_a_derivation_would_read(_capture_on):
    """The numbers reported are corpus.stats()'s, in the terms the tool consumes."""
    import corpus

    summary = corpus.stats()
    check = _corpus_check()
    report = _reported(check)
    for key in ("captures", "labelled", "unlabelled", "identities", "genuine_pairs", "impostor_pairs"):
        assert report.get(key) == summary[key], key
