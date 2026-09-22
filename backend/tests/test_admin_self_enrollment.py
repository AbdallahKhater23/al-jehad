"""An administrator putting their *own* face on the clock.

WHY THIS EXISTS
---------------
``POST /attendance/verify`` has always accepted any authenticated role, so a normal
administrator who works a site could reach the punch card. A punch needs a stored template,
though, and every path that produced one put somebody else in the middle:

* ``POST /admin/enroll`` - written for an administrator enrolling *someone*, and requiring a
  ``worker_id`` this endpoint deliberately does not have;
* an enrollment link - which has to be sent to a phone and opened there;
* the console's create-user form - which only runs while the account is being created.

So the console could show an administrator the clock and never let them use it, and the only
fix was to ask the head administrator to take their photo. ``POST /worker/me/enroll`` is the
missing path: the subject is the token - there is no id in the request to point at anybody
else - and the role list is exactly the roles that reach the console *and* work a shift.

What is pinned here:

1. an ``admin`` with no template can register one and then clock in, which is the whole
   point, asserted through a real punch rather than only through the files on disk;
2. nobody can name a subject: an extra id in the body is ignored, and the template it writes
   is the caller's;
3. the roles that may not do this are refused (401 without a credential, 403 with the wrong
   one), and their templates are untouched;
4. a replacement says so - in the response, in the audit log's before-image - because
   "enrolled a face" and "replaced the face this account was using" are different events;
5. the enrollment liveness policy runs on what is sent, so a printed photo cannot become a
   permanent template just because the capture came from the console;
6. the punch's own refusal tells each role where *its* fix is - an administrator's is the
   console, a worker's is their administrator.
"""

from __future__ import annotations

import re

import pytest

import harness
import liveness
from harness import (
    ADMIN,
    HEAD_ADMIN,
    MOALLEM,
    WORKER,
    bearer,
    db_rows,
    db_scalar,
    jpeg_bytes,
)

SELF_ENROLL = "/api/v1/worker/me/enroll"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def enroll_self(client, user_id, *, image=None, extra_form=None, headers=None):
    """One console capture: a photo, and (for the tampering test) whatever else is sent."""
    return client.post(
        SELF_ENROLL,
        data=extra_form or None,
        files={"photo": ("photo.jpg", image if image is not None else jpeg_bytes(), "image/jpeg")},
        headers=headers if headers is not None else bearer(user_id),
    )


@pytest.fixture(autouse=True)
def _hand_the_templates_back():
    """Whatever this suite takes off disk, it puts back when the test ends.

    ``drop_template`` deletes the *seeded* reference for an account the rest of the suite also
    uses, and the reference directory outlives the database reset - so a file removed here is
    missing in every later test of the same process, including suites in other files that
    expect the seeded administrator to be able to clock in. That is a failure of somebody
    else's test, which is the expensive kind: it reads as a regression in the feature under
    test and sends you reading code that is fine.

    "This account has no face" is a state this suite arranges, not a change it is entitled to
    make permanent. Snapshot the bytes before, write them back after.
    """
    saved = {}
    for user_id in (ADMIN, WORKER, MOALLEM, HEAD_ADMIN):
        for path in (harness.reference_path(user_id), harness.stored_photo_path(user_id)):
            if path is not None and path.exists():
                saved[path] = path.read_bytes()
    yield
    for path, data in saved.items():
        path.write_bytes(data)


def drop_template(user_id: str) -> None:
    """Take the seeded template away, so an enrollment has to write a new one.

    The reference directory outlives the database reset, so "this account has no face" has
    to be arranged by the test that needs it - the same reason ``test_biometric_identity``
    keeps its own blocks of ids. The fixture above is what makes the arrangement temporary.
    """
    for existing in (harness.reference_path(user_id), harness.stored_photo_path(user_id)):
        if existing is not None and existing.exists():
            existing.unlink()


def template_version(user_id: str):
    return db_scalar("SELECT COALESCE(template_version, 0) FROM users WHERE id = ?", (user_id,))


# ---------------------------------------------------------------------------
# 1. the request, end to end
# ---------------------------------------------------------------------------
def test_an_admin_with_no_template_registers_one_and_can_then_clock_in(client):
    """The feature, asserted at the level the person experiences it: punch, refusal, enroll,
    punch. A template on disk is the mechanism; the clock-in is the request.
    """
    drop_template(ADMIN)
    assert not harness.template_exists(ADMIN)
    version_before = template_version(ADMIN)

    # Where they are today: reaching the clock and being turned away for want of a template.
    refused = harness.clock_in(client, ADMIN, headers=bearer(ADMIN))
    assert refused.status_code == 404, refused.text[:300]

    response = enroll_self(client, ADMIN)

    assert response.status_code == 200, response.text[:300]
    body = response.json()
    assert body["worker_id"] == ADMIN, "the response must name the caller, not a parameter"
    assert body["template_replaced"] is False, "there was no template to replace"
    assert harness.template_exists(ADMIN), "no template was written"
    assert harness.stored_photo_path(ADMIN) is not None, "the reference selfie is part of the record"
    assert db_scalar("SELECT enrolled_at FROM users WHERE id = ?", (ADMIN,)), (
        "the roster reads enrolled_at, so an enrollment that does not set it reads as missing"
    )
    assert template_version(ADMIN) == (version_before or 0) + 1

    # ... and now the clock works, which is the only reason this path exists.
    punched = harness.clock_in(client, ADMIN, headers=bearer(ADMIN))
    assert punched.status_code == 200, punched.text[:300]


def test_the_subject_is_the_token_and_not_a_form_field(client):
    """An extra id in the body must be ignored, not obeyed.

    This is the difference between this endpoint and ``/admin/enroll``, where the id is a
    parameter an administrator is trusted to choose. Here a caller cannot name a subject at
    all, so the only template it can write is its own - and a request trying to name the
    worker must leave that worker's face exactly as it was.
    """
    drop_template(ADMIN)
    victim_before = harness.reference_path(WORKER).read_bytes()
    victim_version = template_version(WORKER)

    response = enroll_self(client, ADMIN, extra_form={"worker_id": WORKER, "user_id": WORKER})

    assert response.status_code == 200, response.text[:300]
    assert response.json()["worker_id"] == ADMIN, "a form field chose the subject"
    assert harness.reference_path(WORKER).read_bytes() == victim_before, (
        "another account's template was rewritten by a request that named it"
    )
    assert template_version(WORKER) == victim_version
    assert harness.template_exists(ADMIN)


# ---------------------------------------------------------------------------
# 2. who may not
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "user_id",
    [WORKER, MOALLEM, HEAD_ADMIN],
    ids=["worker", "moallem", "head_admin"],
)
def test_only_the_role_that_clocks_in_from_the_console_may_do_this(client, user_id):
    """A worker's face is issued by the company; a head administrator works no rota.

    A template is a credential: every punch by the account is judged against it, so a
    caller who could mint one could mint the face their own clock-ins used. That is why
    this is not the open self-service door a worker's own stats are, and why the list is
    the roles that reach the console and work a shift - not one of those two.
    """
    version_before = template_version(user_id)

    response = enroll_self(client, user_id)

    harness.assert_denied(
        response,
        endpoint=SELF_ENROLL,
        detail=f"a {user_id} registered their own reference photo",
    )
    assert template_version(user_id) == version_before, "a refused enrollment still wrote a face"


def test_an_anonymous_caller_cannot_enroll_anybody(client):
    response = enroll_self(client, "", headers={})

    harness.assert_denied(
        response,
        endpoint=SELF_ENROLL,
        detail="an unauthenticated caller registered a reference photo",
    )


# ---------------------------------------------------------------------------
# 3. a replacement says so
# ---------------------------------------------------------------------------
def test_replacing_a_template_is_reported_and_kept_on_the_record(client):
    """The seeded admin already has a template, so this is the replacement case.

    Both halves are asserted because they answer different questions: the response tells
    whoever just tapped the shutter, and the audit before-image is what a head
    administrator reads a month later - and it has to say that a *face* was replaced, not
    merely that something was enrolled.
    """
    assert harness.template_exists(ADMIN), "this test is about a replacement; seed a template"
    version_before = template_version(ADMIN)

    response = enroll_self(client, ADMIN)

    assert response.status_code == 200, response.text[:300]
    assert response.json()["template_replaced"] is True
    assert template_version(ADMIN) == (version_before or 0) + 1

    audit = db_rows(
        "SELECT actor_id, actor_role, before_json, after_json FROM audit_log "
        "WHERE action = 'biometric_self_enroll' ORDER BY id DESC LIMIT 1"
    )
    assert len(audit) == 1, "an enrollment that replaces a face is not an unrecorded event"
    actor_id, actor_role, before_json, after_json = audit[0]
    assert (str(actor_id), actor_role) == (ADMIN, "admin"), "the actor must be the account itself"
    assert '"template_replaced": true' in (before_json or ""), before_json
    assert '"source": "console"' in (after_json or ""), after_json


def test_the_enrollment_is_announced_to_the_admins(client):
    """Somebody registering their own face is the kind of event an operator wants to hear
    about - it is a credential being minted, and it is not the one the deployment expects.
    """
    enroll_self(client, ADMIN)

    rows = db_rows(
        "SELECT title, body FROM admin_notifications WHERE kind = 'enrollment_completed' "
        "ORDER BY id DESC LIMIT 1"
    )
    assert rows, "no notification was written"
    title, body = rows[0]
    assert ADMIN in str(body), f"the notification must name whose face it was: {body!r}"
    assert "own face" in str(title).lower(), title


# ---------------------------------------------------------------------------
# 4. liveness: a live frame, not a photograph of one
# ---------------------------------------------------------------------------
class _FakeSession:
    """A stand-in for the ONNX session, as in ``test_phase02_liveness_and_shifts``.

    The model file is not in this repository and ``onnxruntime`` is not installed, so what
    is tested is the policy around it - which verdict leads to which decision.
    """

    def __init__(self, probabilities):
        import numpy as np
        import types

        self._probabilities = np.asarray(probabilities, dtype=np.float32).reshape(1, -1)
        self._types = types

    def get_inputs(self):
        return [self._types.SimpleNamespace(name="input", shape=[1, 3, 80, 80], type="tensor(float)")]

    def get_outputs(self):
        return [self._types.SimpleNamespace(name="output")]

    def run(self, output_names, feeds):  # noqa: ARG002 - mirrors the ORT signature
        return [self._probabilities]


#: Logits in the canonical [live, print_attack, replay_attack] order.
GENUINE = [5.0, -2.0, -3.0]
PRINTED = [-2.0, 5.0, -3.0]


def _install_liveness(monkeypatch, probabilities):
    monkeypatch.setattr(liveness, "get_session", lambda: _FakeSession(probabilities))


def test_a_printed_photo_cannot_become_a_permanent_template(monkeypatch, client):
    """The console's capture runs the enrollment policy, so the shutter is not a bypass.

    The path that accepts a file from disk - the create-user form, the roster import -
    deliberately does not pretend a file can be proven live. This one is a camera frame,
    which is exactly what the policy is built to judge, so it is judged.
    """
    from config import settings

    drop_template(ADMIN)
    monkeypatch.setattr(settings, "liveness_mode", "enforce")
    _install_liveness(monkeypatch, PRINTED)

    response = enroll_self(client, ADMIN)

    assert response.status_code == 422, response.text[:300]
    assert response.json()["detail"]["error_code"] == liveness.ERR_SPOOF
    assert not harness.template_exists(ADMIN), "a presentation attack wrote a template"
    assert db_scalar("SELECT COUNT(*) FROM audit_log WHERE action = 'biometric_self_enroll'") == 0


def test_a_genuine_frame_is_enrolled_and_the_verdict_recorded(monkeypatch, client):
    from config import settings

    drop_template(ADMIN)
    monkeypatch.setattr(settings, "liveness_mode", "enforce")
    _install_liveness(monkeypatch, GENUINE)

    response = enroll_self(client, ADMIN)

    assert response.status_code == 200, response.text[:300]
    assert harness.template_exists(ADMIN)
    after_json = db_scalar(
        "SELECT after_json FROM audit_log WHERE action = 'biometric_self_enroll' ORDER BY id DESC LIMIT 1"
    )
    assert '"verdict": "live"' in (after_json or ""), (
        "the verdict the template was enrolled under belongs on the record"
    )


# ---------------------------------------------------------------------------
# 5. the refusal at the gate points at the right fix
# ---------------------------------------------------------------------------
def test_the_punch_sends_each_role_to_its_own_fix(client):
    """One wall, two audiences.

    A worker cannot register a template for themselves - that is the company's act - so
    they are told to ask. Telling an administrator the same thing is a dead end with a
    phone number in it: they *can* register their own, two taps away, and the sentence
    they read is the only place the app gets to say so.
    """
    drop_template(ADMIN)
    drop_template(WORKER)

    admin_detail = harness.clock_in(client, ADMIN, headers=bearer(ADMIN)).json()["detail"]
    worker_detail = harness.clock_in(client, WORKER, headers=bearer(WORKER)).json()["detail"]

    assert "console" in admin_detail.lower(), admin_detail
    assert "contact your administrator" in worker_detail.lower(), worker_detail
    assert admin_detail != worker_detail


# ---------------------------------------------------------------------------
# 6. one list, two places
# ---------------------------------------------------------------------------
def test_the_console_offers_its_button_to_exactly_the_roles_that_may_use_it(app_module):
    """The button is drawn from the console's own list; the endpoint checks the server's.

    Two lists that mean the same thing are a drift waiting to happen - a role added to one
    gets either a button that always answers 403 or a capability nobody can reach. The
    console's list is ``UI.handsetRoles`` (the roles that run the console *and* work a
    shift), so this asserts the two are the same list rather than merely similar.
    """
    source = (harness.PROJECT_ROOT / "frontend" / "frontendjavascript.js").read_text(encoding="utf-8")
    match = re.search(r"handsetRoles:\s*\[([^\]]*)\]", source)
    assert match, "the console's handsetRoles list is gone; this test guards the pairing"
    console_roles = tuple(re.findall(r"'([a-z_]+)'", match.group(1)))

    assert console_roles == tuple(app_module.SELF_ENROLL_ROLES), (
        f"the console offers self-enrollment to {console_roles} and the server allows "
        f"{tuple(app_module.SELF_ENROLL_ROLES)}"
    )
