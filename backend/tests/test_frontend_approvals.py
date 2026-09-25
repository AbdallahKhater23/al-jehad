"""The two answers on the Approvals card, and which endpoint each one calls.

THE BUG THIS GUARDS
-------------------
``handleApproval(logId, action)`` took an ``action`` argument and never read it. Both
buttons posted to ``/admin/approve_review``:

    onclick="UI_MODULES.handleApproval(${idArg}, 'approve')"
    onclick="UI_MODULES.handleApproval(${idArg}, 'reject')"   // ... and this one approved

So pressing **Reject** approved the shift at its full recorded hours. A worker was paid
overtime their manager had just refused, the card disappeared, and the only evidence of the
refusal was a click that had done the opposite of what the button said. The API could not
catch it: the request it received was a perfectly valid approval.

A backend test cannot see this either - the server was behaving correctly, and the
``note`` was even being sent. What was wrong was *which URL the console chose*. That only
exists in the browser, so it is asserted here.

The suite also pins the shape of each request, because the two decisions are not
symmetrical: an approval is optional-noted and carries the legacy ``admin_id`` field, and a
refusal must carry a reason or it must not be sent at all.

Node is optional; without it these skip rather than fail.
"""

from __future__ import annotations

import pytest

import frontend_vm

pytestmark = pytest.mark.skipif(frontend_vm.NODE is None, reason="node is not installed")

HARNESS = r"""
// --- the server's answers, faked ----------------------------------------

// One shift past the overtime threshold and one unconfirmed clock-in: the two kinds of
// review, which have *opposite* rejections.
const APPROVALS = [
    {
        id: 12, worker_id: '600', name: 'Ana Torrez', site_name: 'Downtown Tower A',
        action: 'Clock Out', timestamp: '2026-09-12 18:00:00', hours: 9.0, score: 0.1,
        status: 'Pending Overtime Approval', status_code: 'pending_overtime',
        flag_reason: null, overtime_hours: 1.0
    },
    {
        id: 900001, worker_id: '1', name: 'Seed Worker', site_name: 'Downtown Tower A',
        action: 'Clock In', timestamp: '2026-09-12 07:00:00', hours: 0.0, score: 0.5,
        status: 'pending_review', status_code: 'pending_review',
        flag_reason: 'outside every site', overtime_hours: null
    },
    // The signed-in administrator's own long shift - ``worker_id`` is the reader's own id on
    // purpose. It is on the queue like anybody else's (``test_admin_shift_visibility``), and it
    // is the one row on this page whose decision is not the reader's to make.
    {
        id: 77, worker_id: '1000', name: 'Site Admin', role: 'admin', site_name: 'Downtown Tower A',
        action: 'Clock Out', timestamp: '2026-09-12 19:00:00', hours: 10.0, score: 0.2,
        status: 'Pending Overtime Approval', status_code: 'pending_overtime',
        flag_reason: null, overtime_hours: 2.0
    }
];

const ADMIN = { id: '1000', name: 'Site Admin', role: 'admin', token: 'tok-1000' };

function responders(url) {
    if (url.includes('/admin/pending_reviews')) return { status: 200, body: APPROVALS };
    return { status: 200, body: {} };
}

async function approvalsEnv() {
    const env = boot();
    env.setResponder(responders);
    env.evaluate('State.saveUser(' + JSON.stringify(ADMIN) + ')');
    await env.evaluate("UI.renderAdminTab('Approvals')");
    return env;
}

// The card's note box is a detached node in this DOM (the tab paints from a string), so
// the field the handler reads is the one ``getElementById`` hands back - the same element
// the handler will read. Writing to it is how a person typing into it is modelled.
function typeNote(env, logId, text) {
    env.evaluate(
        'document.getElementById(' + JSON.stringify('note-' + logId) + ').value = ' + JSON.stringify(text)
    );
}

/** What the handler decided to send: one entry per request, in order. */
function posts(env) {
    return env.requests
        .filter((r) => r.method === 'POST')
        .map((r) => {
            let body = null;
            try { body = JSON.parse(r.body); } catch (err) { body = r.body; }
            return { url: r.url.replace(/^https?:\/\/[^/]*\/api\/v1/, ''), body };
        });
}

/** The last thing the page told the reader, if anything. */
function lastToast(env) {
    return env.evaluate(
        "(function () { const root = document.getElementById('toastRoot');" +
        " const kids = root.__children || [];" +
        " return kids.length ? kids[kids.length - 1].textContent : null; })()"
    );
}

const results = {};

// 1. the card offers both answers, wired to the right action
{
    const env = await approvalsEnv();
    const markup = env.evaluate("document.getElementById('adminContent').innerHTML");
    results.card = {
        // The overtime shift is the one with a decision to make about hours.
        has_approve: markup.indexOf('data-approve="12"') >= 0,
        has_reject: markup.indexOf('data-reject="12"') >= 0,
        note_box: markup.indexOf('id="note-12"') >= 0,
        // The button's own argument is what the handler branches on, so a swap here would
        // send the right request for the wrong button.
        reject_wired: /handleApproval\(12, 'reject'\)/.test(markup),
        approve_wired: /handleApproval\(12, 'approve'\)/.test(markup),
        // Both kinds of review are on the board.
        both_kinds: markup.indexOf('data-review="900001"') >= 0,
        // An administrator's own shift: drawn, and undecidable here. The reader *is* that
        // administrator, which is the sharper case of the same rule.
        peer_on_the_board: markup.indexOf('data-review="77"') >= 0,
        peer_has_approve: markup.indexOf('data-approve="77"') >= 0,
        peer_has_reject: markup.indexOf('data-reject="77"') >= 0,
        peer_has_note_box: markup.indexOf('id="note-77"') >= 0
    };
}

// 2. REJECTING A SHIFT REFUSES IT - the regression this suite exists for
{
    const env = await approvalsEnv();
    typeNote(env, 12, 'no overtime was authorised for this shift');
    await env.evaluate("UI_MODULES.handleApproval(12, 'reject')");
    results.reject = {
        posts: posts(env),
        // Nothing may have been approved: that was the bug.
        approved_anything: posts(env).some((p) => p.url.includes('/admin/approve_review')),
        // ... and the queue is re-read, so the card actually leaves the screen.
        refetched: env.requests.filter((r) => r.url.includes('/admin/pending_reviews')).length,
        toast: lastToast(env)
    };
}

// 3. a refusal with no reason decides nothing, and says why
{
    const env = await approvalsEnv();
    typeNote(env, 12, '   ');
    await env.evaluate("UI_MODULES.handleApproval(12, 'reject')");
    results.reject_without_reason = {
        posts: posts(env),
        toast: lastToast(env),
        expected_toast: env.evaluate("I18n.__('approvalsRejectNeedsNote')"),
        refetched: env.requests.filter((r) => r.url.includes('/admin/pending_reviews')).length
    };
}

// 4. approving still approves, with the note it was given
{
    const env = await approvalsEnv();
    typeNote(env, 12, 'authorised 15 min of overtime');
    await env.evaluate("UI_MODULES.handleApproval(12, 'approve')");
    results.approve = {
        posts: posts(env),
        toast: lastToast(env),
        expected_toast: env.evaluate("I18n.__('approvalsDecided')")
    };
}

// 5. the other kind of review is refused the same way, and it is the server that decides
//    what "rejected" means for the hours
{
    const env = await approvalsEnv();
    typeNote(env, 900001, 'outside every site');
    await env.evaluate("UI_MODULES.handleApproval(900001, 'reject')");
    results.unconfirmed = { posts: posts(env) };
}

// 6. the hint describes both meanings, and every new string exists in all three tables
{
    const env = await approvalsEnv();
    results.hint = env.evaluate(`(function () {
        const KEYS = ['approvalsHint', 'approvalsNotePlaceholder', 'approvalsRejected',
                      'approvalsDecided', 'approvalsRejectNeedsNote'];
        return {
            english: I18n.__('approvalsHint'),
            placeholder: I18n.__('approvalsNotePlaceholder'),
            present_in_english: KEYS.filter((key) => !(key in TRANSLATIONS.en)),
            missing: Object.keys(TRANSLATIONS).map((lang) => [
                lang,
                KEYS.filter((key) => !(key in TRANSLATIONS[lang])).join(',')
            ])
        };
    })()`);
}
"""


@pytest.fixture(scope="module")
def results():
    return frontend_vm.run(HARNESS)


def test_an_administrators_own_shift_is_drawn_without_a_decision_on_it(results):
    """``_guard_standard_admin``, one level out: the queue shows the row, the reader cannot settle it.

    Overtime for a peer is a standard admin's escalation, so this card is evidence - the face, the
    verdict, the hours - and there is nothing on it to type a decision into. The alternative, a
    button that always answers 403, is the trap the credentials screen already refuses to draw.
    The server enforces it (``approve_review``/``reject_review``); this is the surface agreeing.
    """
    card = results["card"]
    assert card["peer_on_the_board"], "the shift is on the queue like anybody else's"
    assert not card["peer_has_approve"], "approving a peer's overtime is not this admin's"
    assert not card["peer_has_reject"], "and refusing it is the same reach"
    assert not card["peer_has_note_box"], (
        "no reason box either: a decision that cannot be sent is not a decision"
    )
    # ... and the rows that *are* this admin's keep exactly what they had.
    assert card["has_approve"] and card["has_reject"] and card["note_box"]


def test_the_card_offers_both_answers(results):
    card = results["card"]
    assert card["has_approve"] is True
    assert card["has_reject"] is True
    assert card["note_box"] is True
    assert card["approve_wired"] is True, "the approve button must pass 'approve'"
    assert card["reject_wired"] is True, "the reject button must pass 'reject'"
    assert card["both_kinds"] is True, "both kinds of review are on the same board"


def test_rejecting_a_shift_refuses_it(results):
    """The regression: Reject must refuse, not approve."""
    reject = results["reject"]
    assert reject["approved_anything"] is False, (
        "the Reject button posted an approval - this is the bug that paid overtime "
        "a manager had refused"
    )
    assert len(reject["posts"]) == 1, reject["posts"]
    post = reject["posts"][0]
    assert post["url"] == "/admin/reject_review", post
    assert post["body"]["log_id"] == 12
    assert post["body"]["note"] == "no overtime was authorised for this shift"
    # A rejection is a new call site: identity is the bearer token, so it does not repeat
    # the field the server ignores.
    assert "admin_id" not in post["body"], "identity comes from the token, not the body"
    assert reject["refetched"] >= 2, "the queue must be re-read so the card leaves the screen"
    assert reject["toast"], "the administrator is told what happened"


def test_a_refusal_without_a_reason_is_not_sent(results):
    empty = results["reject_without_reason"]
    assert empty["posts"] == [], (
        "a refusal that reduces somebody's pay must not go to the server without a reason"
    )
    assert empty["toast"] == empty["expected_toast"], empty["toast"]
    assert empty["refetched"] == 1, "nothing was decided, so the queue was not re-read"


def test_approving_still_approves(results):
    approve = results["approve"]
    assert len(approve["posts"]) == 1, approve["posts"]
    post = approve["posts"][0]
    assert post["url"] == "/admin/approve_review", post
    assert post["body"]["log_id"] == 12
    assert post["body"]["note"] == "authorised 15 min of overtime"
    # Kept for the legacy client contract: the server ignores it, and dropping it here
    # would be an unrelated change to the endpoint that already worked.
    assert post["body"]["admin_id"] == "1000"
    assert approve["toast"] == approve["expected_toast"], approve["toast"]


def test_an_unconfirmed_punch_is_refused_by_the_same_route(results):
    """One button, two meanings - and the meaning is the server's to apply."""
    unconfirmed = results["unconfirmed"]
    assert len(unconfirmed["posts"]) == 1, unconfirmed["posts"]
    assert unconfirmed["posts"][0]["url"] == "/admin/reject_review"
    assert unconfirmed["posts"][0]["body"]["log_id"] == 900001


def test_the_hint_describes_both_kinds_of_refusal(results):
    """The stale hint is part of how the button came to mean the opposite.

    It said only that rejecting "records it as not worked" and that the note was optional -
    neither of which is true of an overtime shift, whose refusal keeps the standard paid day
    and requires a reason.
    """
    hint = results["hint"]
    assert "overtime" in hint["english"].lower(), hint["english"]
    assert "reason" in hint["english"].lower(), hint["english"]
    assert "optional" not in hint["placeholder"].lower(), hint["placeholder"]


def test_every_new_string_exists_in_every_language(results):
    hint = results["hint"]
    assert hint["present_in_english"] == [], hint["present_in_english"]
    # The tables this session loaded, read off the page rather than listed here: a language
    # added to the app is covered the moment its file is loaded, and a table that stops
    # arriving fails this instead of passing quietly.
    assert [lang for lang, _ in hint["missing"]] == ["en", "ar", "hi", "ur"], hint["missing"]
    # ``approvalsDecided`` was called by the toast and defined nowhere, so every approval
    # ended with the reader being shown the key name.
    assert all(missing == "" for _, missing in hint["missing"]), hint["missing"]
