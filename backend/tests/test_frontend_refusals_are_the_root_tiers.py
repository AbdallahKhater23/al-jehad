"""The refused-punch triage is the root tier's screen, and the Approvals tab agrees with that.

WHY THIS SUITE EXISTS
---------------------
The section moved tiers twice over. On the server the three routes left ``/admin/refused_*``
for ``/developer/refused-punches*``, and ``test_refused_punch_triage`` pins both halves of that
(a root-tier read, an administrator refusal, and the old path gone). What no backend test can
see is the console: a section that kept asking the removed URL would be a screenful of 403s on
every administrator's Approvals tab, with the failure surfacing as an empty panel rather than as
a request nobody should have made. A backend test asserts what the server answers; this one
asserts what the page *asks*, which is the only place the difference exists.

Node is optional; without it these skip rather than fail.
"""

from __future__ import annotations

import pytest

import frontend_vm

pytestmark = pytest.mark.skipif(frontend_vm.NODE is None, reason="node is not installed")

HARNESS = r"""
// --- the server's answers, faked ----------------------------------------

// One refusal, drawn the way the developer route shapes it.
const REFUSALS = [
    {
        id: 4, worker_id: '418', name: 'Worker 418', site_name: 'Downtown Tower A',
        action: 'clock_out', error_code: 'face_mismatch', score: 0.4123,
        pipeline: 'yunet-2023mar', created_at: '2026-09-20 08:00:00',
        frame_url: '/api/v1/developer/refused-punches/4/frame'
    }
];

const ADMIN = { id: '1000', name: 'Site Admin', role: 'admin', token: 'tok-1000' };
const HEAD_ADMIN = { id: '5000', name: 'Head Admin', role: 'head_admin', token: 'tok-5000' };
const DEVELOPER = { id: '309010401073', name: 'Developer', role: 'developer', token: 'tok-dev' };

function responders(url) {
    if (url.includes('/admin/pending_reviews')) return { status: 200, body: [] };
    if (url.includes('/admin/overtime/crossings')) return { status: 200, body: [] };
    if (url.includes('/developer/refused-punches')) return { status: 200, body: REFUSALS };
    // What the server would answer if the console still asked the old path: the section would
    // paint empty on every administrator's board and nobody would know why.
    if (url.includes('/admin/refused')) {
        return { status: 403, body: { detail: 'Requires one of these roles: developer.' } };
    }
    return { status: 200, body: {} };
}

/** The Approvals tab, rendered for one role. */
async function approvalsAs(who) {
    const env = boot();
    env.setResponder(responders);
    env.evaluate('State.saveUser(' + JSON.stringify(who) + ')');
    await env.evaluate("UI.renderAdminTab('Approvals')");
    return env;
}

/** Every path the page asked for, with the origin and the API prefix stripped. */
function asked(env) {
    return env.requests.map((r) => r.url.replace(/^https?:\/\/[^/]*\/api\/v1/, ''));
}

const results = {};

// 1. an administrator's board: the queue, and no refused-punch section at all
{
    const env = await approvalsAs(ADMIN);
    const markup = env.evaluate("document.getElementById('adminContent').innerHTML");
    results.admin = {
        asked: asked(env),
        has_section: markup.indexOf('data-refusals-title') >= 0,
        has_card: markup.indexOf('data-refusal="4"') >= 0,
        has_clear: markup.indexOf('data-refusal-clear') >= 0
    };
}

// 2. the same board for the head administrator - the move is off both roles, not one
{
    const env = await approvalsAs(HEAD_ADMIN);
    results.head_admin = {
        asked: asked(env),
        has_section: env.evaluate(
            "document.getElementById('adminContent').innerHTML.indexOf('data-refusals-title') >= 0"
        )
    };
}

// 3. the root tier: the section is drawn, from the route it now lives on
{
    const env = await approvalsAs(DEVELOPER);
    const markup = env.evaluate("document.getElementById('adminContent').innerHTML");
    results.developer = {
        asked: asked(env),
        has_section: markup.indexOf('data-refusals-title') >= 0,
        has_card: markup.indexOf('data-refusal="4"') >= 0,
        // The evidence itself, which is the whole point of the section: the distance, and the
        // way to the picture it was measured from. The URL is not in the markup - the frame is
        // fetched on demand, so a day of refusals does not carry every picture whether or not
        // anybody looked - so the control is what is asserted, not the path behind it.
        has_score: markup.indexOf('0.4123') >= 0,
        has_frame_button: markup.indexOf('data-refusal-show-frame="4"') >= 0
    };
}

// 4. the frame and the clear follow the section: both ask the developer route, never the old one
{
    const env = await approvalsAs(DEVELOPER);
    await env.evaluate("UI_MODULES.handleRefusalClear('4')");
    results.actions = {
        posts: env.requests
            .filter((r) => r.method === 'POST')
            .map((r) => r.url.replace(/^https?:\/\/[^/]*\/api\/v1/, '')),
        frame_url: env.evaluate("UI_MODULES.refusalCardHtml(" + JSON.stringify(REFUSALS[0]) + ")")
            .match(/data-refusal-show-frame="\d+"/) ? 'wired' : 'missing'
    };
}
"""


@pytest.fixture(scope="module")
def results() -> dict:
    return frontend_vm.run(HARNESS)


def test_an_administrators_board_neither_asks_nor_draws_the_triage(results):
    """Absent, not empty: the request is what would make this a screenful of 403s.

    A section that renders nothing from a refused read looks exactly like a day with no
    refusals, which is the failure mode this asserts rather than a cosmetic one.
    """
    admin = results["admin"]
    assert not admin["has_section"], "the refused-punch section is still on an administrator's board"
    assert not admin["has_card"] and not admin["has_clear"], admin
    asked = admin["asked"]
    assert not any("refused" in url for url in asked), (
        f"an administrator's Approvals tab asked for the triage surface: {asked}"
    )


def test_the_head_administrator_is_moved_off_it_too(results):
    """Both roles the section was taken from, because it was on both of their boards."""
    head = results["head_admin"]
    assert not head["has_section"], "the section is still on the head administrator's board"
    assert not any("refused" in url for url in head["asked"]), head["asked"]


def test_the_root_tier_still_gets_the_screen_and_the_evidence(results):
    dev = results["developer"]
    assert dev["has_section"], "the root tier lost the triage section"
    assert dev["has_card"], "the refusal is not drawn for the role the surface moved to"
    assert dev["has_score"], "the score - the evidence the section exists for - is not on the card"
    assert dev["has_frame_button"], dev
    asked = dev["asked"]
    assert any(url.startswith("/developer/refused-punches") for url in asked), asked
    assert not any(url.startswith("/admin/refused") for url in asked), (
        f"the console still asks the path the server removed: {asked}"
    )


def test_the_clear_posts_to_the_developer_route_and_nowhere_else(results):
    posts = results["actions"]["posts"]
    assert posts, "clearing a refusal sent nothing"
    assert posts[0] == "/developer/refused-punches/4/clear", posts
    assert all("/admin/refused" not in url for url in posts), posts
    assert results["actions"]["frame_url"] == "wired", results["actions"]
