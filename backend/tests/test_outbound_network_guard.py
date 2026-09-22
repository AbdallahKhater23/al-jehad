"""No test run may reach the outside world - and the guard has to actually cover the exits.

WHY THIS EXISTS
---------------
``harness.install_outbound_guard`` replaces ``requests.post`` and ``requests.get``, and its
docstring justifies that with a live hazard: "today ``send_whatsapp_alert()`` falls back to
*hardcoded* Twilio credentials and would otherwise place a real API call (and message a personal
number) from a test run".

**That hazard is gone and the prose has not caught up.** ``main.send_whatsapp_alert`` now
performs no I/O at all ("Removed. External messaging is gone"), and the only outbound HTTP left
in the application is ``push.send_push`` - through ``pywebpush``, which is an *optional*
package that is not installed here. So the guard currently intercepts nothing in the running
application, and it is exactly the kind of protection that quietly stops mattering: nobody
notices that it is now defence-in-depth rather than the live safeguard.

Which is why the load-bearing test in this file is not about the recorder. It is about
**coverage**: the guard only replaces two functions of one library, so its promise ("no real
call may ever leave a test run") is a statement about *what the application can reach the
network with*. That is a property of the source, and nothing was checking it. Add ``httpx``,
``aiohttp`` or a bare ``urllib.request`` somewhere, and the guard silently covers ninety percent
of the exits while the suite keeps passing. The scan below is that check.

THE OTHER THING PINNED HERE
---------------------------
``OUTBOUND`` is append-only: nothing in ``reset_database`` clears it, and the two suites that
use it assert on a *delta* or filter by URL. That is a deliberate, undocumented idiom, and a
test that starts asserting ``not OUTBOUND.calls`` would be asserting about every test that ran
before it in this process - so the idiom is pinned rather than left to be rediscovered.
"""

from __future__ import annotations

import re
import warnings
from pathlib import Path

import pytest

import harness
import push

#: Clients that can open a connection. ``urllib.parse`` is deliberately absent: it parses URLs
#: and cannot connect to anything.
NETWORK_CLIENTS: tuple[str, ...] = (
    "requests",
    "httpx",
    "aiohttp",
    "urllib.request",
    "urllib3",
    "http.client",
    "socket",
    "pycurl",
    "websocket",
    "pywebpush",
    "smtplib",
    "ftplib",
)

#: Every (module, client) pair that may exist, and why. Keyed by the *pair* rather than the
#: module on purpose: a module-level whitelist would say "push.py may talk to the network" and
#: then accept a second, uncovered client added to it - which is the first draft of this test,
#: caught by mutating push.py to import httpx and watching it pass.
KNOWN_EXITS: dict[tuple[str, str], str] = {
    ("push.py", "pywebpush"): (
        "the only outbound HTTP in the application: pywebpush calls requests internally, which is "
        "the pair install_outbound_guard replaces. Covered."
    ),
    ("serve.py", "socket"): (
        "a UDP socket probe (`connect(('8.8.8.8', 80))`) that sends no payload - it asks the OS "
        "which interface routes outbound so it can print a phone-reachable LAN address. It is the "
        "deployment entry point and no test imports it."
    ),
}

#: Not the application: two superseded scripts that talk to a live server on import and are kept
#: out of collection by ``pytest.ini`` (see ``testpaths``/``norecursedirs`` there).
NOT_THE_APPLICATION: tuple[str, ...] = ("test_load.py", "test_login.py")


# ---------------------------------------------------------------------------
# the mechanism
# ---------------------------------------------------------------------------


def test_the_guard_is_installed_before_any_test_body_runs():
    """Pinned without calling ``install_outbound_guard``: conftest's session fixture does it.

    A test that installed the guard itself would pass even if nothing installed it for the rest
    of the suite, which is the property that matters.
    """
    import requests

    assert requests.post is harness.OUTBOUND, "requests.post is not the harness recorder"
    assert requests.get is harness.OUTBOUND, "requests.get is not the harness recorder"


def test_a_call_that_would_need_the_network_is_answered_by_the_recorder():
    """``.invalid`` cannot resolve, so a real call would raise ``ConnectionError``.

    The stub answering 201 instead is the proof that no socket was involved - not a mock's
    promise that it was replaced.
    """
    import requests

    before = len(harness.OUTBOUND.calls)
    response = requests.post(
        "https://api.invalid/v1/Messages.json",
        data={"To": "+10000000000", "Body": "a message that must never be placed"},
        timeout=5,
    )

    assert response.status_code == 201
    assert response.json() == {"sid": "SM_test_stub"}
    recorded = harness.OUTBOUND.calls[before:]
    assert [call["url"] for call in recorded] == ["https://api.invalid/v1/Messages.json"]
    assert recorded[0]["kwargs"]["data"]["To"] == "+10000000000", (
        "the recorder dropped the payload, so a test that asserts \"no credential was sent\" "
        "would be reading an empty dictionary and passing"
    )


def test_the_recorder_reports_urls_and_never_resets_itself(app_module):
    """The idiom, pinned: assert on a delta or by URL, never on emptiness.

    ``OUTBOUND.calls`` accumulates for the whole process, and ``reset_database`` does not touch
    it. A future test asserting ``not OUTBOUND.calls`` would be asserting about every earlier
    test in that worker - a failure that appears only under ``-n auto`` or a shuffled order.
    """
    before = len(harness.OUTBOUND.calls)
    harness.OUTBOUND("https://observability.example/beat")

    assert harness.OUTBOUND.urls()[-1] == "https://observability.example/beat"
    assert len(harness.OUTBOUND.calls) == before + 1, (
        "the recorder was cleared by the per-test reset; assert on a delta instead of depending "
        "on that"
    )


def test_a_library_that_calls_requests_internally_is_covered(monkeypatch):
    """The seam the guard really protects, exercised without pywebpush installed.

    ``push.send_push`` hands the message to pywebpush, which posts through ``requests`` - so the
    guard is one level *below* the call site. A stand-in that posts the same way pywebpush does
    proves that indirection is covered, rather than that the guard can see its own name.
    """
    import requests

    posted: list[str] = []

    def fake_webpush(*, subscription_info, data, vapid_private_key, vapid_claims):
        # Exactly the shape push.send_push calls, and exactly what pywebpush does next.
        response = requests.post(
            subscription_info["endpoint"],
            data=data,
            headers={"Authorization": f"vapid {vapid_claims['sub']}"},
        )
        posted.append(f"{response.status_code} {response.json()['sid']}")

    monkeypatch.setattr(push, "_load_webpush", lambda: (fake_webpush, RuntimeError))

    before = len(harness.OUTBOUND.calls)
    push.send_push(
        {"endpoint": "https://fcm.googleapis.com/fcm/send/abc"},
        {"title": "Your shift was closed", "body": "8.00h payable"},
    )

    assert posted == ["201 SM_test_stub"], posted
    assert harness.OUTBOUND.urls()[before:] == ["https://fcm.googleapis.com/fcm/send/abc"], (
        "a push notification reached the network without passing the recorder"
    )


# ---------------------------------------------------------------------------
# the coverage claim, which only the source can answer
# ---------------------------------------------------------------------------


def test_the_only_ways_out_of_the_application_are_a_named_short_list():
    """The guard replaces two functions of one library; this is what that has to cover.

    Fails when a module starts naming another client, because that is the moment the guard stops
    being complete - and the moment nothing else would notice.
    """
    found: set[tuple[str, str]] = set()
    for path in sorted(Path(harness.BACKEND_DIR).glob("*.py")):
        if path.name in NOT_THE_APPLICATION:
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        for client in NETWORK_CLIENTS:
            # Indentation-tolerant: pywebpush is imported *inside* a function, deliberately, so
            # a module-level-only scan would miss the one client the guard actually covers.
            if re.search(rf"^\s*(from|import)\s+{re.escape(client)}\b", source, re.MULTILINE):
                found.add((path.name, client))

    unexpected = sorted(found - set(KNOWN_EXITS))
    assert not unexpected, (
        f"{unexpected} name a network client the outbound guard does not cover. "
        "install_outbound_guard replaces requests.post/get only, so a call through anything else "
        "leaves the process for real. Either route it through requests, or add the pair to "
        "KNOWN_EXITS in this file with the reason it is safe - do not leave it unlisted, because "
        "then nothing in the suite knows the guard is incomplete."
    )

    missing = sorted(set(KNOWN_EXITS) - found)
    assert not missing, (
        f"{missing} are listed as network-capable but name no client any more; the list has gone "
        "stale and with it the claim that it covers every exit"
    )


def test_the_removed_alert_path_is_removed(app_module):
    """The guard's stated justification describes code that no longer exists.

    Pinned from both sides: the function performs no I/O (so it cannot be the reason the guard
    matters), and the guard is still what would catch it if it came back.
    """
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = app_module.send_whatsapp_alert("1", "Seed Worker", "Downtown Tower A", 0.5)

    assert result is False, "the removed alert path answered as if it had sent something"
    assert any(issubclass(item.category, DeprecationWarning) for item in caught), (
        "the removal is no longer announced, so a caller silently gets False"
    )

    before = len(harness.OUTBOUND.calls)
    import requests

    requests.post("https://api.twilio.com/2010-04-01/Accounts/x/Messages.json", data={})
    assert len(harness.OUTBOUND.calls) == before + 1, (
        "the guard no longer catches the call it was written for"
    )


def test_push_says_which_piece_is_missing_rather_than_failing_at_send_time():
    """An operator's answer to "why is push not working": the reason names the piece.

    ``pywebpush`` is optional, so this is also the pin on the current deployment truth - if a box
    installs it, this test says so instead of the case going untested.
    """
    usable, reason = push.transport_available()
    if usable:
        pytest.skip(f"pywebpush is installed here ({reason}); the guard covers the call path")
    assert "pywebpush" in reason or "VAPID" in reason or "PUSH_ENABLED" in reason, reason
