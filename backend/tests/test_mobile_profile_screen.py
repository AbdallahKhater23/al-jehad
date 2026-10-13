"""The profile screen's destructive controls, tapped in a real browser.

WHY THIS EXISTS
---------------
The handset's *Delete my account* row is the only control in the product that destroys a
person's account, and until now it was covered by a typecheck and by a build that bakes the
right host name into it. Neither of those presses it. The screen paints its markup from a
template string into ``#profile-root`` and hangs one delegated ``click`` listener off that
element (``screens/profile/profile.ts``), and the handler starts an ``async`` function with
``void`` - so a tap that goes nowhere, or a rejection inside the handler, is *silent*. A
control that is drawn and dead looks exactly like a working one, which is the worst possible
property for this row: a worker would tap it, watch nothing happen, and go on believing
their account was gone - or, in the other direction, a tap with no guard behind it would
take the account.

So these are the assertions a typecheck cannot make, on the real DOM, with real taps:

* the row asks for the password before it can remove anything, and the sheet says what will
  be kept and that the step cannot be undone;
* confirming with an empty field removes nothing and says so - the guard against a mis-tap;
* *Keep my account*, and the back key, both close the sheet without a request leaving the
  device;
* with the password typed, exactly one ``POST /auth/delete_account`` goes out carrying that
  password and the session's bearer token - and only then does the session leave both the
  app's memory and the phone's storage;
* a refusal (401) leaves the sheet open with a message, leaves the account alone, and leaves
  the worker signed in;
* and the *other* destructive control on the same screen, *Sign out*, still asks first and
  honours *Cancel*, so the harness is not one test wide.

HOW THIS IS DRIVEN
------------------
``mobile_screen_harness`` bundles the real screen out of its own tree with esbuild and
serves it to a real Chromium; see that module for what it does and does not do. ``fetch`` is
scripted and every request is recorded, so both directions are asserted: what the screen
asked for, and what it did afterwards. ``Preferences`` is Capacitor's own web implementation
over this browser's ``localStorage``, so "the session is gone from the phone" is a statement
about real storage rather than about a stub.

Node, esbuild and a Chromium are all optional here: without any of them the module skips and
says which one is missing.
"""

from __future__ import annotations

import dataclasses

import pytest

from mobile_screen_harness import (
    PROFILE,
    WORKER_SESSION,
    Handset,
    bundle,
    phone,
    serve,
    skip_reason,
    write_site,
)

pytestmark = pytest.mark.skipif(
    skip_reason() is not None,
    reason=skip_reason() or "",
)

#: The password the suite types. A literal rather than a fixture, because it is *this*
#: screen's own contract that the string on the keyboard is the string in the request body -
#: and a password that came from anywhere but the test would hide a screen that sent
#: something else.
PASSWORD = "correct horse battery staple"

#: The device the phone is holding when the screen opens: a registered device with a key
#: epoch, which is the state a working handset is in. Answered for every test, because
#: "the screen would not paint without it" is a fact about the app, not the subject here.
REGISTERED_DEVICE = {
    "device_id": "dev-a1b2c3",
    "worker_id": WORKER_SESSION["user"]["id"],
    "key_epoch": 3,
    "device_key": "a2V5LW1hdGVyaWFs",
}

#: A route that answers every ``POST /auth/delete_account`` with 200, for the tests that get
#: as far as confirming. The message is the server's own, and the screen prefers it.
DELETED = {"message": "Your account has been deleted."}

WORKER_ID = WORKER_SESSION["user"]["id"]

#: The sheet is gone only once it has animated out: ``sheet.ts`` removes the node 220ms
#: after it starts closing, so "the sheet closed" is this expression rather than a property.
NO_SHEET = "document.querySelector('.hand-sheet') === null"
A_SHEET = "document.querySelector('.hand-sheet') !== null"


# ---------------------------------------------------------------------------
# The site, served once for the module
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def screens_site(tmp_path_factory) -> object:
    """The profile screen, bundled and laid out as a site the browser can open.

    Built once per module: esbuild takes seconds, and every test in the file mounts the same
    bundle. The bundle goes through the app's own entry conventions - see
    ``mobile_screen_harness.bundle`` - so what runs here is the screen's real source, not a
    copy of it.
    """
    built = tmp_path_factory.mktemp("mobile_dom")
    return write_site(built, bundle((PROFILE,), built / "app.mjs"))


@pytest.fixture(scope="module")
def base_url(browser, screens_site):
    """The site's address. Depends on ``browser`` so a machine without one skips, not errors."""
    with serve(screens_site) as running:
        yield running


@pytest.fixture
def handset(browser, base_url, tmp_path) -> Handset:
    """A phone, the profile screen mounted on it, and the device already registered.

    The screen is asserted to have *painted* before any test body runs: ``PROFILE.mounted``
    is the delete row itself, so a screen that drew a header and then threw while building
    its cards fails here rather than in an assertion about a tap that had no target.
    """
    with phone(browser, base_url) as tab:
        tab.route("/attendance/devices/register", REGISTERED_DEVICE)
        mounted = tab.open(PROFILE, session=WORKER_SESSION)
        assert mounted.painted, (
            "the profile screen never painted the row this file exists to tap, so no "
            f"assertion below could mean anything.\n{tab.describe()}"
        )
        assert tab.page_errors == [], (
            f"the profile screen threw while drawing itself.\n{tab.describe()}"
        )
        assert tab.stored_session(), (
            "the session was not written to the phone's storage, so no 'signed out' "
            f"assertion below would prove anything.\n{tab.describe()}"
        )
        yield tab


def open_the_delete_sheet(handset: Handset) -> dict:
    """Tap the row and return the sheet, asserting it is the one that was asked for.

    Every test in this file starts the same way, and the sheet's identity is worth asserting
    once here rather than five times below: a tap on the wrong row, or a sheet that opened
    with the wrong name, is not a variant of the flow under test.

    The tap is asserted on its own, before anything about the sheet, because "the control is
    drawn and dead" is the failure this whole file exists for - and a raw driver timeout
    from the next line would hide it.
    """
    assert handset.exists('[data-action="delete-account"]'), (
        f"the delete row is not on the screen.\n{handset.describe()}"
    )
    assert handset.tap("delete-account"), (
        "the delete row is on the screen and could not be tapped. A control that is drawn "
        f"and dead looks exactly like this.\n{handset.describe()}"
    )
    assert handset.wait(A_SHEET), (
        f"tapping the delete row opened no sheet.\n{handset.describe()}"
    )
    sheet = handset.sheet()
    assert sheet["title"] == "Delete my account", (
        f"tapping the row opened {sheet['title']!r}, not the question it asks.\n{handset.describe()}"
    )
    return sheet


# ---------------------------------------------------------------------------
# The question
# ---------------------------------------------------------------------------
def test_the_delete_row_asks_for_the_password_and_removes_nothing_by_itself(handset):
    """The row opens a question. It does not remove an account, and it says what will go.

    Three halves, and all three are the contract. The sheet asks for the password because a
    stolen session must not be enough to remove an account any more than it is enough to
    seize one. It names what is *kept* - the shifts already worked - because that is the
    part a worker cannot reconstruct afterwards. And it asks without answering: opening the
    question must not itself send anything, which is the difference between a confirm step
    and a destructive control with a dialog in front of it.
    """
    sheet = open_the_delete_sheet(handset)

    # It asks for the password, and it is a password field.
    assert [field["type"] for field in sheet["inputs"]] == ["password"], (
        f"the sheet asks with {sheet['inputs']!r}\n{handset.describe()}"
    )
    assert sheet["inputs"][0]["id"] == "del-password", sheet["inputs"]

    # Both answers are offered, the destructive one is drawn as dangerous, and the safe one
    # is there to be taken.
    assert [button["label"] for button in sheet["buttons"]] == [
        "Delete my account",
        "Keep my account",
    ], f"the sheet offers {sheet['buttons']!r}\n{handset.describe()}"
    assert sheet["buttons"][0]["danger"] is True, sheet["buttons"]
    assert sheet["buttons"][1]["danger"] is False, sheet["buttons"]

    # What will happen, before anything is asked - including what is kept.
    assert "cannot be undone" in sheet["body"], sheet["body"]
    assert "Shifts you have already worked are kept" in sheet["body"], sheet["body"]
    assert "signing key" in sheet["body"], sheet["body"]

    # And the question alone removed nothing.
    assert handset.requests_to("/auth/delete_account") == [], (
        f"opening the question already sent a request.\n{handset.describe()}"
    )
    assert handset.in_memory_session() is not None, handset.describe()
    assert handset.signed_out() == 0, handset.describe()


def test_a_confirmation_without_the_password_removes_nothing(handset):
    """The confirm button is not the destructive act; the password is.

    This is the assertion that would catch a screen that dropped its own guard - the empty
    field is confirmed, and nothing leaves the device. The message on screen is part of the
    contract too: a worker who tapped confirm and saw nothing at all would tap it again.
    """
    open_the_delete_sheet(handset)
    assert handset.tap_sheet_button("Delete my account"), (
        f"the sheet offered no way to confirm.\n{handset.describe()}"
    )
    assert handset.wait("!!document.querySelector('[data-error]:not([hidden])')"), (
        f"confirming an empty password said nothing at all.\n{handset.describe()}"
    )

    sheet = handset.sheet()
    assert sheet["error"] == "Enter your password to confirm.", (
        f"confirming an empty field said {sheet['error']!r}\n{handset.describe()}"
    )
    assert handset.requests_to("/auth/delete_account") == [], (
        f"an empty password still sent a request.\n{handset.describe()}"
    )
    assert handset.sheet_open(), "the sheet closed as though it had done something"
    assert handset.in_memory_session() is not None and handset.stored_session(), (
        "the phone was signed out by a confirmation that should have failed.\n"
        f"{handset.describe()}"
    )
    assert handset.signed_out() == 0, handset.describe()


# ---------------------------------------------------------------------------
# The ways out that are not deletion
# ---------------------------------------------------------------------------
def test_keeping_the_account_closes_the_sheet_and_leaves_the_phone_signed_in(handset):
    """The safe answer to a destructive question has to work, and has to do nothing else."""
    open_the_delete_sheet(handset)
    assert handset.tap_sheet_button("Keep my account"), (
        f"the sheet offered no way to keep the account.\n{handset.describe()}"
    )
    assert handset.wait(NO_SHEET), (
        f"*Keep my account* left the sheet open.\n{handset.describe()}"
    )

    assert handset.requests_to("/auth/delete_account") == [], (
        f"*Keep my account* sent a request.\n{handset.describe()}"
    )
    assert handset.in_memory_session() == WORKER_SESSION, handset.describe()
    assert len(handset.stored_session()) == 4, (
        f"the phone's stored session changed: {handset.stored_session()}\n{handset.describe()}"
    )
    assert handset.signed_out() == 0, handset.describe()


def test_the_back_key_closes_the_delete_sheet(handset):
    """A sheet a worker cannot back out of is a trap, so the back key is a way out.

    A phone's back gesture is what Escape stands in for here; the sheet's own contract is
    that a dismissible one closes on it (``components/sheet.ts``). The delete sheet stays
    dismissible on purpose - the *question* can be abandoned, and only the answer commits.
    """
    open_the_delete_sheet(handset)
    handset.press("Escape")
    assert handset.wait(NO_SHEET), f"Escape left the sheet open.\n{handset.describe()}"

    assert handset.requests_to("/auth/delete_account") == [], handset.describe()
    assert handset.in_memory_session() is not None, handset.describe()
    assert handset.signed_out() == 0, handset.describe()


# ---------------------------------------------------------------------------
# The deletion itself
# ---------------------------------------------------------------------------
def test_confirming_with_the_password_removes_the_account_and_the_phone_s_session(handset):
    """The whole flow, end to end: one request, with the password, and then a wiped phone.

    Four things travel together here and only one of them is the request. The request has to
    carry the password *the worker typed* and the session's own bearer token - a delete that
    went out unauthenticated would be refused by the server and read, on the phone, as a
    network problem. On the answer, the sheet closes, the worker is told it happened, the
    in-memory session is gone, the phone's storage is empty of the session, and the screen
    tells whoever mounted it to send the worker back to sign-in.
    """
    handset.route("/auth/delete_account", DELETED)
    open_the_delete_sheet(handset)
    handset.fill("#del-password", PASSWORD)
    assert handset.tap_sheet_button("Delete my account"), (
        f"the sheet offered no way to confirm.\n{handset.describe()}"
    )
    assert handset.wait("window.MobileScreens.store.getState().session === null"), (
        f"confirming with the password did not end the session.\n{handset.describe()}"
    )

    sent = handset.requests_to("/auth/delete_account", method="POST")
    assert len(sent) == 1, f"the deletion went out {len(sent)} times: {sent}\n{handset.describe()}"
    assert sent[0]["body"] == {"current_password": PASSWORD}, (
        f"the request carried {sent[0]['body']!r}, not the password that was typed.\n"
        f"{handset.describe()}"
    )
    assert sent[0]["headers"].get("Authorization") == f"Bearer {WORKER_SESSION['token']}", (
        f"the deletion was not authenticated as the worker: {sent[0]['headers']!r}\n"
        f"{handset.describe()}"
    )

    assert handset.in_memory_session() is None, (
        f"the app is still holding a session after the account was removed.\n{handset.describe()}"
    )
    assert handset.stored_session() == {}, (
        f"the phone is still holding the session's keys: {handset.stored_session()}\n"
        f"{handset.describe()}"
    )
    assert handset.signed_out() == 1, (
        f"the screen never told the app to sign the worker out (called {handset.signed_out()} "
        f"times).\n{handset.describe()}"
    )
    assert {"kind": "ok", "text": DELETED["message"]} in handset.toasts(), (
        f"the worker was not told what happened: {handset.toasts()!r}\n{handset.describe()}"
    )
    assert handset.wait(NO_SHEET), (
        f"the sheet stayed open over a screen the worker has left.\n{handset.describe()}"
    )
    assert handset.page_errors == [] and handset.rejections() == [], (
        f"the flow produced an unhandled failure.\n{handset.describe()}"
    )


def test_a_refused_password_leaves_the_account_and_the_session_alone(handset):
    """A 401 is a refusal, not a deletion: the message is shown and nothing is torn down.

    The refusal is the one answer a destructive control must not mistake for success, and
    the transport retries it once after refreshing the session - so this also pins that the
    retry is bounded, that both attempts carry the same password the worker typed, and that
    the screen is still standing when they are both refused.
    """
    handset.route(
        "/auth/delete_account",
        {"detail": {"error_code": "invalid_password", "message": "That password is not right."}},
        status=401,
    )
    # The refresh is answered honestly: the session is still valid, its token is simply
    # stale - which is the case the transport retries, and the harder one for the screen.
    handset.route(
        "/auth/refresh",
        {
            "access_token": "the-fresh-token",
            "expires_at": "2027-02-01T00:00:00Z",
            "user": WORKER_SESSION["user"],
            "token_version": WORKER_SESSION["tokenVersion"],
        },
    )

    open_the_delete_sheet(handset)
    handset.fill("#del-password", "not-the-password")
    assert handset.tap_sheet_button("Delete my account"), (
        f"the sheet offered no way to confirm.\n{handset.describe()}"
    )
    assert handset.wait("!!document.querySelector('.hand-sheet [data-error]:not([hidden])')"), (
        f"a refused password said nothing at all.\n{handset.describe()}"
    )

    sheet = handset.sheet()
    assert sheet["error"] == "That password is not right.", (
        f"a refused password said {sheet['error']!r}\n{handset.describe()}"
    )
    assert sheet["open"], f"the sheet closed on a refusal.\n{handset.describe()}"
    assert [button["busy"] for button in sheet["buttons"]] == [False, False], (
        f"the confirm button was left in its loading state.\n{handset.describe()}"
    )

    attempts = handset.requests_to("/auth/delete_account", method="POST")
    assert len(attempts) == 2, (
        f"a refused deletion was attempted {len(attempts)} times; the transport retries a "
        f"401 once, after refreshing.\n{handset.describe()}"
    )
    assert [attempt["body"] for attempt in attempts] == [
        {"current_password": "not-the-password"}
    ] * 2, f"the retry did not carry the worker's own password: {attempts}\n{handset.describe()}"
    assert attempts[0]["headers"].get("Authorization") == f"Bearer {WORKER_SESSION['token']}"
    assert attempts[1]["headers"].get("Authorization") == "Bearer the-fresh-token", (
        f"the retry used the stale token.\n{handset.describe()}"
    )

    assert handset.in_memory_session() is not None, (
        f"a refused deletion signed the worker out.\n{handset.describe()}"
    )
    assert handset.signed_out() == 0, handset.describe()
    assert handset.page_errors == [] and handset.rejections() == [], handset.describe()


# ---------------------------------------------------------------------------
# The other destructive control on the same screen
# ---------------------------------------------------------------------------
def test_signing_out_asks_first_and_honours_the_safe_answer(handset):
    """The harness is not one test wide: the same screen's other irreversible control.

    *Sign out* is not deletion, but it is the same shape of danger - a tap that ends a
    session - and it is built from the shared confirm dialog. It is here for two reasons: to
    prove a second control on this screen is driven the same way, and because the *Cancel*
    branch of a confirm dialog is the half that nothing else in the suite exercises. A
    dialog whose only working answer is the destructive one is the bug this catches.
    """
    assert handset.tap("signout"), (
        f"the sign-out control is on the screen and could not be tapped.\n{handset.describe()}"
    )
    assert handset.wait(A_SHEET), f"signing out asked nothing.\n{handset.describe()}"
    sheet = handset.sheet()
    assert sheet["title"] == "Sign out?", f"{sheet['title']!r}\n{handset.describe()}"
    assert [button["label"] for button in sheet["buttons"]] == ["Sign out", "Cancel"], (
        f"{sheet['buttons']!r}\n{handset.describe()}"
    )

    assert handset.tap_sheet_button("Cancel"), (
        f"the question offered no way to stay signed in.\n{handset.describe()}"
    )
    assert handset.wait(NO_SHEET), f"*Cancel* left the sheet open.\n{handset.describe()}"
    assert handset.in_memory_session() is not None, (
        f"*Cancel* signed the worker out.\n{handset.describe()}"
    )
    assert handset.stored_session(), handset.describe()
    assert handset.signed_out() == 0, handset.describe()

    # And now the answer that does it.
    assert handset.tap("signout"), handset.describe()
    assert handset.wait(A_SHEET), handset.describe()
    assert handset.tap_sheet_button("Sign out"), handset.describe()
    assert handset.wait("window.MobileScreens.store.getState().session === null"), (
        f"confirming the sign-out did not end the session.\n{handset.describe()}"
    )
    assert handset.stored_session() == {}, (
        f"the phone kept its session: {handset.stored_session()}\n{handset.describe()}"
    )
    assert handset.signed_out() == 1, handset.describe()
    assert {"kind": "ok", "text": "Signed out."} in handset.toasts(), (
        f"{handset.toasts()!r}\n{handset.describe()}"
    )
    assert handset.page_errors == [] and handset.rejections() == [], handset.describe()


# ---------------------------------------------------------------------------
# The harness itself
# ---------------------------------------------------------------------------
def test_the_screen_renders_what_the_device_answers(handset):
    """The harness's own guard: a scripted answer has to reach the screen as drawn text.

    Without this, every test above could pass against a mount that painted nothing but a row
    to tap. The device panel is the screen's one hard case - it is drawn from answers that
    arrive *after* the first paint (the registered device, the anchor it holds, the queue
    depth) - so the device the server answered with being in the drawn text is evidence that
    the screen booted, fetched, and repainted rather than merely laid out.
    """
    drawn = handset.text("#profile-root")
    assert REGISTERED_DEVICE["device_id"] in drawn, (
        f"the registered device the server answered with is not on the screen.\n{handset.describe()}"
    )
    assert f"#{REGISTERED_DEVICE['key_epoch']}" in drawn, (
        f"the signing key epoch is not on the screen.\n{handset.describe()}"
    )
    assert "Punches waiting to sync" in drawn, handset.describe()
    assert "Not registered" not in drawn, (
        f"the screen drew a device it did not register.\n{handset.describe()}"
    )
    assert WORKER_SESSION["user"]["name"] in drawn, (
        f"the screen is not drawing the session it was given.\n{handset.describe()}"
    )
    assert handset.requests_to("/attendance/devices/register", method="POST"), (
        f"the screen never asked the server to register this phone.\n{handset.describe()}"
    )


def test_the_harness_refuses_to_mount_a_screen_the_app_does_not_have(tmp_path):
    """A vacuity guard on the harness: a screen is built from the app's own tree or not at all.

    ``bundle`` names the module and the export out of ``src/ui``, so the two ways this could
    quietly stop testing anything are a renamed file and a renamed factory - and either one
    would leave a bundle that mounts nothing. esbuild is what refuses, and it names the path
    it could not resolve, so a rename reads as a rename rather than as eight mystery
    failures in the tests above.
    """
    ghost = dataclasses.replace(
        PROFILE,
        name="ghost",
        module="screens/ghost/ghost.ts",
        factory="createGhostScreen",
    )
    with pytest.raises(AssertionError) as refused:
        bundle((ghost,), tmp_path / "ghost.mjs")
    assert "screens/ghost/ghost.ts" in str(refused.value), str(refused.value)
