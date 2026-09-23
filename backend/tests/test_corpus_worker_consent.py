"""Per-worker consent for calibration capture: the opt-in, its record, and what it gates.

WHY THIS FILE EXISTS
--------------------
The corpus is a biometric store, and its live captures used to rely on a single deployment-wide
switch: an operator set ``CALIBRATION_CAPTURE_ENABLED`` and every worker's punch frame was kept.
That is a decision made *about* people, not *by* them. The fix is a second switch, per worker,
recorded in its own append-only table (``corpus_capture_consents``, migration 23) with the
decision - who, when, from where, and in which direction - written to ``audit_log`` as well.

What is pinned here, in the order the failure would happen:

* the capture gate is the *intersection* of the two switches - deployment AND worker - and a
  missing consent row reads as no;
* a grant is a row with the actor and provenance on it, visible to the worker who gave it and
  to an administrator planning a derivation;
* a withdrawal is the newest decision, and it stops future captures without erasing history
  (that is what ``corpus.purge`` is for, deliberately);
* the table cannot be rewritten: UPDATE and DELETE are refused at the database level, so the
  consent history is evidence rather than a current value with a comment.
"""

from __future__ import annotations

import sqlite3

import pytest

import corpus
import harness
from config import settings
from database import db


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
class _StubDetector:
    """One face, the same geometry every time - the punch path's detector, stubbed."""

    def __call__(self, image):
        return [
            {
                "landmarks": [
                    [150.0, 120.0], [162.0, 120.0], [156.0, 126.0], [152.0, 132.0], [160.0, 132.0],
                ],
                "facial_area": {"x": 140, "y": 110, "w": 100, "h": 110},
                "confidence": 0.97,
            }
        ]


def _stub_punch_detector(monkeypatch) -> None:
    import face_detector

    monkeypatch.setattr(face_detector, "detect_landmarks", _StubDetector())
    monkeypatch.setattr(face_detector, "fingerprint", lambda: "c" * 16)
    monkeypatch.setattr(face_detector, "active_pipeline", lambda: "yunet-2023mar")


def _grant(worker_id: str = harness.WORKER, *, granted: bool = True) -> None:
    """Write a consent decision directly, the way the endpoint would (minus the HTTP layer)."""
    with db(write=True) as conn:
        corpus.record_consent(
            conn, worker_id=worker_id, granted=granted, actor_id=worker_id, note="test"
        )


def _image():
    from PIL import Image

    return Image.new("RGB", (640, 480), (120, 120, 120))


# ---------------------------------------------------------------------------
# the gate: two switches, not one
# ---------------------------------------------------------------------------
def test_capture_is_refused_without_the_workers_opt_in_even_when_the_deployment_enables_it(monkeypatch):
    """The deployment switch alone must not capture: that was the defect this feature fixes."""
    _stub_punch_detector(monkeypatch)
    monkeypatch.setattr(settings, "calibration_capture_enabled", True)

    assert corpus.maybe_capture_punch(_image(), worker_id=harness.WORKER, verdict="approved") is None
    assert corpus.sidecars() == [], "a punch without the worker's consent was captured"


def test_capture_records_a_worker_granted_consent_basis(monkeypatch):
    """Both switches on: the capture exists, and its basis names the worker's own decision."""
    _stub_punch_detector(monkeypatch)
    monkeypatch.setattr(settings, "calibration_capture_enabled", True)
    _grant()

    capture_id = corpus.maybe_capture_punch(_image(), worker_id=harness.WORKER, verdict="approved")
    assert capture_id, "a consenting worker's punch was not captured"
    record = {r.capture_id: r for r in corpus.sidecars()}[capture_id]
    assert record.consent == corpus.WORKER_CONSENT
    assert record.consent != corpus.LIVE_CONSENT, (
        "a per-worker capture must not carry the old deployment-wide basis"
    )


def test_a_withdrawal_is_the_newest_decision_and_stops_future_captures(monkeypatch):
    """Granted, then withdrawn: the newest row wins, and no new capture appears."""
    _stub_punch_detector(monkeypatch)
    monkeypatch.setattr(settings, "calibration_capture_enabled", True)
    _grant()
    assert corpus.worker_consent(harness.WORKER) is True

    _grant(granted=False)
    assert corpus.worker_consent(harness.WORKER) is False

    assert corpus.maybe_capture_punch(_image(), worker_id=harness.WORKER, verdict="approved") is None
    assert corpus.sidecars() == [], "a withdrawn worker's punch was still captured"


def test_a_worker_who_never_answered_and_a_blank_worker_id_are_both_a_no(monkeypatch):
    """Strictness by default: missing data is no consent, and no worker is nobody to ask."""
    _stub_punch_detector(monkeypatch)
    monkeypatch.setattr(settings, "calibration_capture_enabled", True)
    _grant(worker_id="somebody_else")

    assert corpus.worker_consent(harness.WORKER) is False
    assert corpus.worker_consent(None) is False
    assert corpus.maybe_capture_punch(_image(), worker_id=None, verdict="approved") is None


def test_the_deployment_switch_still_gates(monkeypatch):
    """The worker's yes does not override the operator's off: the intersection, not the union."""
    _stub_punch_detector(monkeypatch)
    monkeypatch.setattr(settings, "calibration_capture_enabled", False)
    _grant()

    assert corpus.maybe_capture_punch(_image(), worker_id=harness.WORKER, verdict="approved") is None
    assert corpus.sidecars() == []


# ---------------------------------------------------------------------------
# the record: append-only, and readable
# ---------------------------------------------------------------------------
def test_the_consent_table_refuses_update_and_delete():
    """A consent history that UPDATE can rewrite is not a history. The triggers are the point."""
    _grant()
    with db(write=True) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "UPDATE corpus_capture_consents SET granted = 0 WHERE worker_id = ?",
                (harness.WORKER,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM corpus_capture_consents")


def test_the_history_reads_newest_first_with_its_directions():
    _grant(granted=True)
    _grant(granted=False)

    history = corpus.consent_history(harness.WORKER)
    assert [entry["granted"] for entry in history] == [False, True], (
        "history must read newest first"
    )
    assert history[0]["revoked"] is True, "a withdrawal stamps the end of the consent"
    assert history[1]["revoked"] is False


def test_consented_workers_names_only_live_grants():
    _grant(worker_id="w1", granted=True)
    _grant(worker_id="w2", granted=True)
    _grant(worker_id="w2", granted=False)
    _grant(worker_id="w3", granted=False)

    assert corpus.consented_workers() == {"w1"}, (
        "a withdrawn worker or a refusal must not read as coverage"
    )


# ---------------------------------------------------------------------------
# the endpoints: the worker's decision, on two records
# ---------------------------------------------------------------------------
def test_the_worker_grants_and_sees_their_own_consent(app_module, client):
    headers = harness.bearer(harness.WORKER)
    granted = client.post(
        "/api/v1/worker/me/corpus/consent",
        headers=headers,
        json={"granted": True, "note": "happy to help the camera"},
    )
    assert granted.status_code == 200, granted.text

    reading = client.get("/api/v1/worker/me/corpus/consent", headers=headers)
    assert reading.status_code == 200
    body = reading.json()
    assert body["granted"] is True
    assert body["history"][0]["note"] == "happy to help the camera"
    assert body["history"][0]["created_by"] == harness.WORKER


def test_the_withdrawal_endpoint_stops_the_capture_path(app_module, client, monkeypatch):
    headers = harness.bearer(harness.WORKER)
    assert client.post(
        "/api/v1/worker/me/corpus/consent", headers=headers, json={"granted": True}
    ).status_code == 200

    _stub_punch_detector(monkeypatch)
    monkeypatch.setattr(settings, "calibration_capture_enabled", True)
    assert corpus.maybe_capture_punch(_image(), worker_id=harness.WORKER, verdict="approved")

    assert client.post(
        "/api/v1/worker/me/corpus/consent", headers=headers, json={"granted": False, "note": "stop"}
    ).status_code == 200
    assert corpus.maybe_capture_punch(_image(), worker_id=harness.WORKER, verdict="approved") is None
    # The earlier capture is untouched - withdrawal is about the future; purge is about the past.
    assert len(corpus.sidecars()) == 1


def test_the_consent_decision_is_audited_with_the_actor(app_module, client):
    headers = harness.bearer(harness.WORKER)
    response = client.post(
        "/api/v1/worker/me/corpus/consent", headers=headers, json={"granted": True}
    )
    consent_id = response.json()["consent_id"]

    with db() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM audit_log WHERE action = 'corpus_capture_consent' "
            "AND entity_id = ? ORDER BY id DESC LIMIT 1",
            (str(consent_id),),
        ).fetchone()
    assert row is not None, "a consent with no audit row is an unauditable biometric basis"
    assert row["actor_id"] == harness.WORKER


def test_the_admin_surface_lists_live_grants_and_the_deployment_switch(app_module, client):
    headers = harness.bearer(harness.HEAD_ADMIN)
    assert client.post(
        "/api/v1/worker/me/corpus/consent",
        headers=harness.bearer(harness.WORKER),
        json={"granted": True},
    ).status_code == 200

    reading = client.get("/api/v1/admin/corpus_consents", headers=headers)
    assert reading.status_code == 200
    body = reading.json()
    assert body["consented_workers"] == [harness.WORKER]
    assert body["capture_enabled"] == bool(settings.calibration_capture_enabled)
    assert body["decisions"], "the decisions list should carry the rows just written"


def test_the_admin_surface_is_not_worker_readable(app_module, client):
    assert client.get(
        "/api/v1/admin/corpus_consents", headers=harness.bearer(harness.WORKER)
    ).status_code == 403


def test_the_consent_note_is_vetted_as_prose(app_module, client):
    refused = client.post(
        "/api/v1/worker/me/corpus/consent",
        headers=harness.bearer(harness.WORKER),
        json={"granted": True, "note": "<script>alert(1)</script>"},
    )
    assert refused.status_code == 422, "markup in a consent note is prose failure"
