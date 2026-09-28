"""The clock panel of an account that is signed in and not yet approved.

WHY THIS EXISTS
---------------
A walk-up registration creates a real account and quarantines it: the applicant signs in
immediately, sees their own history and messages, and every punch is refused by the server
until an administrator approves them (see ``test_walk_up_registration``). What the server
cannot do is make that visible *before* the tap. A worker whose clock button looks like every
other worker's walks to a gate, takes a selfie, and is answered with a refusal that reads like
a fault in the app - and the one person who could explain it is the administrator they cannot
reach.

So the panel has to do two things:

1. **Say the state**, in the worker's own language, from the server's own answer. Read fresh on
   every render from ``/worker/me/stats``, not remembered from the sign-in: an approval can
   land while the phone is sitting on this screen, and a banner that outlived the state it
   describes is a worker told they cannot work when they can.
2. **Draw the button disabled** rather than letting the tap be answered. There is nothing the
   worker can do differently and nothing they did wrong - so the control is simply not offered,
   and the sentence above it is the whole explanation.

The third decision is the one this suite exists to hold: **the ordinary worker's panel is
unchanged.** A banner that appeared for everybody would be a banner nobody read.

Node is optional; without it these skip rather than fail.
"""

from __future__ import annotations

import pytest

import frontend_vm

pytestmark = pytest.mark.skipif(frontend_vm.NODE is None, reason="node is not installed")

HARNESS = r"""
// --- the server's answers, faked ----------------------------------------

const stats = {
    worker_id: '7',
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
    approval_status: 'pending_approval',
    active_session: null
};

const reads = [];

function responders(url, init) {
    if (url.indexOf('/worker/me/stats') >= 0) {
        reads.push(String(url));
        return { status: 200, body: stats };
    }
    if (url.indexOf('/worker/me/notifications') >= 0) return { status: 200, body: { notifications: [], unread: 0 } };
    return { status: 200, body: {} };
}

function workerEnv(approvalStatus) {
    stats.approval_status = approvalStatus;
    reads.length = 0;
    const env = boot();
    env.setResponder(responders);
    env.evaluate('State.saveUser(' + JSON.stringify({
        id: '7', name: 'New Worker', role: 'worker', token: 'tok-7'
    }) + ')');
    return env;
}

async function drawPanel(env) {
    await env.evaluate("WORKER_MODULES.renderClockPanel(document.getElementById('workerDashboard'))");
    return env.evaluate("document.getElementById('workerDashboard').innerHTML");
}

/** The clock button's own opening tag, so ``disabled`` is read off the button and not the page. */
function clockButtonTag(markup) {
    return (/<button[^>]*class="clock-button[^"]*"[^>]*>/.exec(markup) || [''])[0];
}

const results = {};

// 1. waiting for approval: the banner, and a button that cannot be tapped
{
    const env = workerEnv('pending_approval');
    const markup = await drawPanel(env);
    results.pending = {
        banner: markup.indexOf('data-approval-pending') >= 0,
        title: env.evaluate("I18n.__('accountPendingTitle')"),
        body: env.evaluate("I18n.__('accountPendingBody')"),
        says_waiting: markup.indexOf(env.evaluate("I18n.__('accountPendingTitle')")) >= 0,
        says_what_to_do: markup.indexOf(env.evaluate("I18n.__('accountPendingBody')")) >= 0,
        button_tag: clockButtonTag(markup),
        reads: reads.slice()
    };
}

// 2. approved: none of it
{
    const env = workerEnv('active');
    const markup = await drawPanel(env);
    results.approved = {
        banner: markup.indexOf('data-approval-pending') >= 0,
        button_tag: clockButtonTag(markup)
    };
}

// 3. a server too old to send the field: treated as an ordinary account, not as quarantined
{
    const env = workerEnv(undefined);
    const markup = await drawPanel(env);
    results.unreported = {
        banner: markup.indexOf('data-approval-pending') >= 0,
        button_tag: clockButtonTag(markup)
    };
}
"""


@pytest.fixture(scope="module")
def results() -> dict:
    return frontend_vm.run(HARNESS)


def test_a_waiting_account_is_told_so_on_the_card(results):
    """The state, in the reader's language, from the server's own answer."""
    pending = results["pending"]
    assert pending["banner"] is True, (
        "an account that cannot clock in looks exactly like one that can - the refusal is only "
        "discovered by walking to a gate and taking a selfie"
    )
    assert pending["title"] and pending["body"], "the banner is not an empty band"
    assert pending["says_waiting"] is True
    assert pending["says_what_to_do"] is True


def test_the_clock_button_is_not_offered_to_a_waiting_account(results):
    """Disabled rather than answered: there is nothing the worker could do differently."""
    tag = results["pending"]["button_tag"]
    assert "clock-button" in tag, f"the card stopped drawing the clock button: {tag!r}"
    assert "disabled" in tag, (
        f"a quarantined account is offered a live Clock In, and every tap is a refusal it can do "
        f"nothing about: {tag!r}"
    )


def test_an_approved_account_keeps_the_panel_it_always_had(results):
    """Read fresh on every render, so an approval landing mid-shift takes effect at once."""
    approved = results["approved"]
    assert approved["banner"] is False, "an approved worker is told they cannot work"
    assert "disabled" not in approved["button_tag"], (
        f"an approved worker's clock button is still disabled: {approved['button_tag']!r}"
    )


def test_an_old_server_that_does_not_report_the_field_is_not_a_quarantine(results):
    """Absent is *unknown*, not *pending*: the failure direction that keeps workers working."""
    unreported = results["unreported"]
    assert unreported["banner"] is False
    assert "disabled" not in unreported["button_tag"]
