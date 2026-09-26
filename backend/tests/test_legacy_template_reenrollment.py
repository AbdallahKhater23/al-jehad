"""The re-enrollment contract: what a worker with a legacy template is told, as fields.

WHY THIS EXISTS
---------------
A site that has been running since before the embedder changed has templates on disk that this
build cannot score: 4096-float ``VGG-Face`` vectors where a 128-float FaceNet vector is
expected, and JSON lists that record nothing about what produced them. Those files are a fact
about the deployment, not a bug to fix on the spot, so the requirement is that they are
*answered*: a worker is refused with a sentence that names the fix (a photograph, taken by an
administrator), and a client is told in fields it can switch on which screen to show.

Two names for two different things, which is the whole design:

* ``status = NEEDS_REENROLLMENT`` - the *route*: this punch cannot be fixed by retaking the
  photo, so do not send the worker back to the camera.
* ``reason`` - the *diagnosis*: a different embedding version (a dimension or model change,
  which also covers the legacy 4096-float vectors) or a different crop (a template written
  before the detector was replaced, or with no provenance at all). Same fix, different cause,
  and an operator clearing a worklist needs to tell them apart.

A third cause is not about the vector at all and still needs the same photograph: a template
filed under the account id the old naming scheme used (``STALE_LEGACY_NAME``). It routes as the
*version* one - both codes say "this record predates a change in the system" - and carries its
own diagnosis, so a client that enumerates two codes never meets a third.

The unhandled-exception half of the requirement is pinned where it can be reached honestly, by
punching against a planted legacy template: ``tests/test_biometric_identity.py``.
"""

from __future__ import annotations

import pytest

import biometrics
import main


def test_the_status_and_the_reason_are_frozen_because_a_client_keys_on_them():
    """A client ships against these strings, so they are a wire contract, not copy."""
    assert biometrics.STATUS_NEEDS_REENROLLMENT == "NEEDS_REENROLLMENT"
    assert biometrics.REASON_STALE_TEMPLATE_VERSION == "stale_template_version"
    assert biometrics.REASON_STALE_TEMPLATE_PIPELINE == "stale_template_pipeline"


def test_every_stale_reason_has_a_status_and_a_reason():
    """Total over the reasons ``stale_reason`` can return: a new one cannot go unmapped.

    The table is the reason this is a table: a fifth reason added to ``stale_reason`` without
    an entry here would fall through to the fail-safe (re-enroll), which is the right default
    and the wrong *silence* - this test makes the omission visible.
    """
    assert set(biometrics.REENROLLMENT_STATUSES) == {
        biometrics.STALE_UNREADABLE,
        biometrics.STALE_NO_PROVENANCE,
        biometrics.STALE_OTHER_PIPELINE,
        biometrics.STALE_OTHER_MODEL,
        biometrics.STALE_LEGACY_NAME,
    }
    for reason, (status, code) in biometrics.REENROLLMENT_STATUSES.items():
        assert status == biometrics.STATUS_NEEDS_REENROLLMENT, reason
        assert code in {
            biometrics.REASON_STALE_TEMPLATE_VERSION,
            biometrics.REASON_STALE_TEMPLATE_PIPELINE,
        }, reason


@pytest.mark.parametrize(
    "reason, expected",
    [
        # A dimension or a model change: the two vectors are not comparable at all, which is
        # what the legacy 4096-float VGG-Face template is.
        (biometrics.STALE_UNREADABLE, biometrics.REASON_STALE_TEMPLATE_VERSION),
        (biometrics.STALE_OTHER_MODEL, biometrics.REASON_STALE_TEMPLATE_VERSION),
        # The same width, a different part of the face.
        (biometrics.STALE_NO_PROVENANCE, biometrics.REASON_STALE_TEMPLATE_PIPELINE),
        (biometrics.STALE_OTHER_PIPELINE, biometrics.REASON_STALE_TEMPLATE_PIPELINE),
        # Not a version or a crop of the *vector* at all, and still a record from before a
        # change in the system: a face filed under an account id rather than the immutable one.
        (biometrics.STALE_LEGACY_NAME, biometrics.REASON_STALE_TEMPLATE_VERSION),
    ],
)
def test_the_diagnosis_distinguishes_a_version_change_from_a_crop_change(reason, expected):
    assert biometrics.reenrollment_status(reason)["reason"] == expected


def test_a_usable_template_adds_nothing_to_a_response():
    """So a caller can merge this into any body unconditionally."""
    assert biometrics.reenrollment_status(None) == {}


def test_an_unknown_reason_is_answered_as_needs_re_enrollment_rather_than_as_nothing():
    """The fail-safe direction, and the one that matters.

    A template this build cannot score needs a photograph; a missing entry that read as
    "nothing wrong" would send the worker back to a camera that cannot help them.
    """
    assert biometrics.reenrollment_status("something_new") == {
        "status": biometrics.STATUS_NEEDS_REENROLLMENT,
        "reason": biometrics.REASON_STALE_TEMPLATE_VERSION,
        "stale_reason": "something_new",
    }


def test_the_diagnosis_travels_beside_the_routing_code():
    payload = biometrics.reenrollment_status(biometrics.STALE_NO_PROVENANCE)

    assert set(payload) == {"status", "reason", "stale_reason"}
    assert payload["stale_reason"] == biometrics.STALE_NO_PROVENANCE


def test_the_punch_endpoint_switches_on_the_code_the_table_is_keyed_by():
    """``main`` names the refusal code once, and the mapping table uses that name.

    The punch endpoint adds the re-enrollment fields when it sees ``REFERENCE_STALE_CODE``, so
    a literal in either place would be a second thing to keep in step.
    """
    error_code, message = main.FACE_FRAME_REFUSALS[main.FACE_REFERENCE_STALE]

    assert error_code == main.REFERENCE_STALE_CODE
    assert "enroll" in message.lower() and "administrator" in message.lower()


def test_the_stale_refusal_is_not_reported_as_a_server_fault():
    """It is the worker's administrator's job, not a 500, and the sentence says so."""
    error_code, _ = main._frame_refusal(main.FACE_REFERENCE_STALE)

    assert error_code != main.FACE_CHECK_FAILED[0]


def test_a_misfiled_template_routes_to_the_same_screen_with_its_own_diagnosis():
    """A second error string for one code, and the string has to be a true diagnosis.

    Both are stored on the punch row for an operator to read afterwards, so "predates the current
    face pipeline" cannot stand in for a template that may have been written yesterday and filed
    wrongly. What must *not* differ is the code: the worker is routed to the same re-enrollment,
    and a client that knows one refusal knows this one.
    """
    error_code, message = main.FACE_FRAME_REFUSALS[main.FACE_REFERENCE_MISFILED]

    assert error_code == main.REFERENCE_STALE_CODE, (error_code, message)
    assert main.FACE_REFERENCE_MISFILED != main.FACE_REFERENCE_STALE
    assert "enroll" in message.lower() and "administrator" in message.lower(), message

    payload = biometrics.reenrollment_status(biometrics.STALE_LEGACY_NAME)
    assert payload == {
        "status": biometrics.STATUS_NEEDS_REENROLLMENT,
        "reason": biometrics.REASON_STALE_TEMPLATE_VERSION,
        "stale_reason": biometrics.STALE_LEGACY_NAME,
    }, payload
