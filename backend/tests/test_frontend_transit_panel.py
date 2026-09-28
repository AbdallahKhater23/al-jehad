"""The worker's clock panel on the road: what it says, and the one tap it offers.

WHY THIS EXISTS
---------------
An off-site Clock Out is refused by the server (see ``test_transit_pending_shift``) - the one
shift whose end the field cannot decide. A refusal with no visible way out is a dead end for
the person holding the phone, so the panel has to do two things the backend cannot do for it:

1. **Say the state.** A worker looking at a shift whose site reads "In Transit" has been told
   nothing about why the site looks like that, why Clock Out may be refused, or what arriving
   will do. The note carries it.
2. **Offer the way out.** ``Ask an administrator to close my shift`` - one tap, one request,
   and the answer says the shift is *still running* until somebody acts, which is the part a
   worker would otherwise learn by tapping Clock Out again.

The button only exists on the road: a confirmed shift keeps the panel it always had, which is
asserted here too, because a control that appears for everybody is a control nobody reads.

Node is optional; without it these skip rather than fail. The button is bound through a
delegated ``data-`` hook rather than an inline ``onclick``, because this file's inline-handler
budget may only fall (see ``test_frontend_xss``).
"""

from __future__ import annotations

import pytest

import frontend_vm

pytestmark = pytest.mark.skipif(frontend_vm.NODE is None, reason="node is not installed")

HARNESS = r"""
// --- the server's answers, faked ----------------------------------------

const stats = {
    worker_id: '600',
    total_hours: 0,
    regular_hours: 0,
    pending_hours: 0,
    overtime_hours: 0,
    overtime_notify_hours: 8.1,
    break_minutes: 30,
    break_after_hours: 4,
    paid_day_hours: 8.0,
    on_site_day_hours: 8.5,
    auto_close_at_regular: 1,
    flagged_for_review: false,
    active_session: {
        site_name: 'In Transit',
        clock_in_time: '2026-09-28 05:00:00',
        late_flag: null,
        seconds_on_site: 3600,
        in_transit: true
    }
};

const requestCalls = [];

function responders(url, init) {
    const method = (init && init.method) || 'GET';
    if (url.indexOf('/worker/me/request_checkout') >= 0) {
        requestCalls.push({ url: String(url), method: method, headers: init && init.headers });
        return {
            status: 200,
            body: {
                status: 'requested',
                already_asked: false,
                message: 'Your request was sent. An administrator will close this shift.',
                open_hours: 1.0,
                site_name: 'In Transit'
            }
        };
    }
    if (url.indexOf('/worker/me/stats') >= 0) return { status: 200, body: stats };
    if (url.indexOf('/admin/active_sessions') >= 0) return { status: 200, body: [] };
    return { status: 200, body: {} };
}

function workerEnv(active) {
    stats.active_session = active;
    requestCalls.length = 0;
    const env = boot();
    env.setResponder(responders);
    env.evaluate('State.saveUser(' + JSON.stringify({
        id: '600', name: 'Seed Lead', role: 'moallem', token: 'tok-600'
    }) + ')');
    return env;
}

/** The panel, drawn the way the tab draws it. */
async function drawPanel(env) {
    await env.evaluate("WORKER_MODULES.renderClockPanel(document.getElementById('workerDashboard'))");
    return env.evaluate("document.getElementById('workerDashboard').innerHTML");
}

/** A click event aimed at the request button, for the delegated listener. */
function clickOn(selector) {
    return {
        target: { closest: (wanted) => (wanted === selector ? { selector: selector } : null) }
    };
}

const results = {};

// 1. on the road: the note, the button, and the one request the button makes
{
    const env = workerEnv({
        site_name: 'In Transit',
        clock_in_time: '2026-09-28 05:00:00',
        late_flag: null,
        seconds_on_site: 3600,
        in_transit: true
    });
    const markup = await drawPanel(env);

    results.pending = {
        // The note is a data-attribute, not a class: the wording and the styling both move.
        note: markup.indexOf('data-transit-pending') >= 0,
        title: env.evaluate("I18n.__('transitPendingTitle')"),
        body: env.evaluate("I18n.__('transitPendingBody')"),
        says_waiting: markup.indexOf(env.evaluate("I18n.__('transitPendingTitle')")) >= 0,
        says_ask: markup.indexOf(env.evaluate("I18n.__('transitPendingBody')")) >= 0,
        // The whole point of the state: the way out is on screen before the tap that needs it.
        button: markup.indexOf('data-request-checkout') >= 0,
        button_label: env.evaluate("I18n.__('transitRequestCheckout')"),
        button_labelled: markup.indexOf(env.evaluate("I18n.__('transitRequestCheckout')")) >= 0,
        // The primary action is unchanged: from inside a fence, Clock Out is how the trip is
        // confirmed and the day closed. The request is the second thing, not a replacement.
        clock_button: markup.indexOf("handleClock('Clock Out')") >= 0
    };

    const returned = env.evaluate(
        "document.getElementById('workerDashboard').__handlers.click(" +
        "({ target: { closest: (wanted) => (wanted === '[data-request-checkout]' ? {} : null) } }))"
    );
    // The listener hands the request back, so the wait below is the *request settling* rather
    // than a sleep that might be too short - and a ``thenable`` is the evidence of that, since
    // an async function resolves to ``undefined`` and the value alone cannot tell them apart.
    results.clicked = !!(returned && typeof returned.then === 'function');
    await returned;
    results.request = {
        calls: requestCalls.length,
        url: requestCalls.length ? requestCalls[0].url : null,
        method: requestCalls.length ? requestCalls[0].method : null,
        authorized: requestCalls.length ? (requestCalls[0].headers || {})['Authorization'] : null,
        toast: env.evaluate(
            "(document.getElementById('toastRoot').__children || []).map((el) => el.textContent).join(' | ')"
        ),
        answered: env.evaluate("I18n.__('transitRequestSent')")
    };
    results.host_bound_once = env.evaluate(
        "!!document.getElementById('workerDashboard').__checkoutBound"
    );
}

// 2. a shift that has been confirmed: none of it
{
    const env = workerEnv({
        site_name: 'Downtown Tower A',
        clock_in_time: '2026-09-28 05:00:00',
        late_flag: null,
        seconds_on_site: 3600,
        in_transit: false
    });
    const markup = await drawPanel(env);
    results.confirmed = {
        note: markup.indexOf('data-transit-pending') >= 0,
        button: markup.indexOf('data-request-checkout') >= 0,
        clock_button: markup.indexOf("handleClock('Clock Out')") >= 0
    };

    // Painting a confirmed shift is not a request, and the delegated listener is the only
    // thing that could make one - the button it looks for is not in this markup, so ``closest``
    // resolves nothing on a real tap. (A synthetic event that *claims* a match would only be
    // asserting the stub, which is why the click is not faked here at all.)
    results.confirmed_requests = requestCalls.length;
}
"""


@pytest.fixture(scope="module")
def results() -> dict:
    return frontend_vm.run(HARNESS)


def test_an_off_site_shift_is_explained_on_the_card(results):
    """The worker is told what state the shift is in, in their own language, before tapping."""
    pending = results["pending"]
    assert pending["note"] is True, "an unconfirmed shift has to say so on the card"
    assert pending["says_waiting"] is True
    assert pending["says_ask"] is True
    assert pending["title"] and pending["body"], "the note is not an empty band"


def test_the_card_offers_the_request_the_refusal_points_at(results):
    """The way out exists before the refusal, and it is labelled as the request it sends."""
    pending = results["pending"]
    assert pending["button"] is True
    assert pending["button_labelled"] is True
    assert pending["clock_button"] is True, "Clock Out is still how an arrival is confirmed"


def test_the_request_is_one_post_to_the_shift_the_worker_is_on(results):
    """One tap, one authenticated POST, and the answer repeated to the worker."""
    request = results["request"]
    assert results["clicked"] is True, "the handler hands its request back"
    assert request["calls"] == 1, "one tap is one request"
    assert request["url"].endswith("/worker/me/request_checkout")
    assert request["method"] == "POST"
    assert request["authorized"] == "Bearer tok-600"
    assert request["toast"].strip(), "the worker has to see that it was sent"
    assert results["host_bound_once"] is True, "the listener is bound once per host"


def test_a_confirmed_shift_carries_neither_the_note_nor_the_button(results):
    """A control that shows for everybody is a control nobody reads - and a request nobody needs."""
    confirmed = results["confirmed"]
    assert confirmed["note"] is False
    assert confirmed["button"] is False
    assert confirmed["clock_button"] is True, "the ordinary shift still closes the ordinary way"
    assert results["confirmed_requests"] == 0, "a confirmed shift cannot raise a request"
