"""The worker's own half of calibration capture, from the phone.

WHY THIS FILE EXISTS
--------------------
The backend has accepted a per-worker capture consent since migration 23: the decision is written
to ``corpus_capture_consents`` (append-only) and to ``audit_log``, and capture is refused unless
the newest row is a grant *and* the deployment switch is on. What did not exist was any way for a
worker to make that decision. The endpoints were reachable only by an HTTP client, which for the
person whose face is kept is the same as having no way at all - so a deployment that enabled
capture for a week collected nothing, and every test of the consent *record* still passed.

That is the gap this file closes, and the reason it is a frontend test rather than an API one: the
API half is already pinned in ``test_corpus_worker_consent.py``. What can only be checked here is
what the worker actually sees and touches:

1. the card states the *server's* answer, read from ``GET /worker/me/corpus/consent`` - not a
   local flag, and not optimism;
2. with no answer yet (the read failed, or has not landed) there is **no button**: offering an
   "I agree" control whose write the page has not proven it can make is asking somebody to
   consent to something that may not be recorded;
3. agreeing POSTs ``{granted: true}``, and withdrawing POSTs ``{granted: false}`` to the *same*
   endpoint - a worker who wants out must not have to find a second screen;
4. a refused write leaves the card showing what the server still believes, so the switch cannot
   read "on" for a decision nobody recorded;
5. the wording names the photo. A toggle labelled "help improve the service" is not consent to
   keep somebody's photograph, and the note has to say who cannot see the answer and that it can
   be stopped - the three facts a person needs to agree knowingly;
6. every string the card uses exists in all four language tables, because a worker reading the
   Arabic or Hindi build must be able to understand what they are agreeing to.

Node is optional; without it these skip rather than fail.
"""

from __future__ import annotations

import re

import pytest

import frontend_vm

pytestmark = pytest.mark.skipif(frontend_vm.NODE is None, reason="node is not installed")

CONSENT_PATH = "/worker/me/corpus/consent"

HARNESS = r"""
// --- the server's answers, faked ----------------------------------------

//: What ``GET /worker/me/corpus/consent`` answers. Flipped by the scenarios.
let consentState = { granted: false, history: [], capture_enabled: true };
//: Every POST the page made to the consent route: the parsed bodies.
let posted = [];
//: When true, the write fails and the state must not move.
let writeBroken = false;

function responders(url, init) {
    if (url.indexOf('C/' + 'worker/me/corpus/consent') >= 0 || url.indexOf('/corpus/consent') >= 0) {
        if ((init && init.method || 'GET').toUpperCase() === 'POST') {
            // Recorded before the failure branch: the request still reached the server, which is
            // why the worker is owed an honest answer about it either way.
            posted.push(JSON.parse(init.body));
            if (writeBroken) return { status: 503, body: { detail: 'unreachable' } };
            consentState = { granted: JSON.parse(init.body).granted, history: [], capture_enabled: true };
            return { status: 200, body: { granted: consentState.granted } };
        }
        return { status: 200, body: consentState };
    }
    if (url.indexOf('/worker/me/stats') >= 0) {
        return { status: 200, body: {
            active_session: null, total_hours: 0, overtime_notify_hours: 8.1, break_minutes: 30,
            break_after_hours: 4, paid_day_hours: 8, auto_close_at_regular: '1', flagged_for_review: false
        } };
    }
    if (url.indexOf('/worker/me/logs') >= 0) return { status: 200, body: [] };
    if (url.indexOf('/worker/me/notifications') >= 0) return { status: 200, body: { unread: 0, notifications: [] } };
    return { status: 200, body: {} };
}

async function consentEnv({ state = null, broken = false } = {}) {
    posted.length = 0;
    writeBroken = broken;
    consentState = state || { granted: false, history: [], capture_enabled: true };
    const env = boot();
    env.setResponder(responders);
    env.evaluate("localStorage.setItem('layoutOverride', 'mobile')");
    env.evaluate("State.saveUser(" + JSON.stringify({ id: '601', name: 'Bilal Khan', role: 'worker', token: 'tok-worker' }) + ")");
    return env;
}

/** The card's markup, painted the way the profile tab paints it. */
function card(env) {
    env.evaluate("document.getElementById('corpusConsent').innerHTML = WORKER_MODULES.corpusConsentHtml()");
    return env.evaluate("document.getElementById('corpusConsent').innerHTML");
}

/** Tap the card's one control, through the delegated listener the app binds. */
async function tap(env) {
    card(env);
    env.evaluate("WORKER_MODULES.bindCorpusConsent(document.getElementById('corpusConsent'))");
    return env.evaluate(`
        (async () => {
            const host = document.getElementById('corpusConsent');
            const control = host.innerHTML.match(/data-corpus-consent="([a-z]+)"/);
            if (!control) return 'no-control';
            const button = { disabled: false, getAttribute: () => control[1] };
            await host.__handlers.click({ target: { closest: (sel) => (sel === '[data-corpus-consent]' ? button : null) } });
            return control[1];
        })()
    `);
}

const results = {};

// 1. not agreed: the card says so, names the photo, and offers the opt-in
{
    const env = await consentEnv({ state: { granted: false, history: [], capture_enabled: true } });
    await env.evaluate("WORKER_MODULES.initCorpusConsent()");
    results.off = {
        card: card(env),
        state: env.evaluate("WORKER_MODULES._corpusConsent && WORKER_MODULES._corpusConsent.granted"),
        asked: env.requests.map((r) => r.url.split('/api/v1')[1]).filter((u) => u && u.indexOf('/corpus') >= 0)
    };
}

// 2. agreed: the card reads the server's answer
{
    const env = await consentEnv({ state: { granted: true, history: [{ granted: true }], capture_enabled: true } });
    await env.evaluate("WORKER_MODULES.initCorpusConsent()");
    results.on = { card: card(env), control: card(env).match(/data-corpus-consent="([a-z]+)"/)[1] };
}

// 3. no answer yet: no button to press
{
    const env = await consentEnv();
    env.setResponder(() => ({ status: 503, body: { detail: 'unreachable' } }));
    await env.evaluate("WORKER_MODULES.initCorpusConsent()");
    const markup = card(env);
    results.unknown = {
        card: markup,
        has_control: markup.indexOf('data-corpus-consent') >= 0,
        state: env.evaluate("WORKER_MODULES._corpusConsent")
    };
}

// 4. agreeing: one POST to the consent route with granted true, then the server's state
{
    const env = await consentEnv({ state: { granted: false, history: [], capture_enabled: true } });
    await env.evaluate("WORKER_MODULES.initCorpusConsent()");
    const control = await tap(env);
    results.grant = { control, posted: posted.slice(), card: card(env) };
}

// 5. withdrawing: the SAME endpoint, granted false, no administrator in the loop
{
    const env = await consentEnv({ state: { granted: true, history: [{ granted: true }], capture_enabled: true } });
    await env.evaluate("WORKER_MODULES.initCorpusConsent()");
    const control = await tap(env);
    results.withdraw = {
        control,
        posted: posted.slice(),
        routes: env.requests.map((r) => r.url.split('/api/v1')[1]).filter((u) => u && u.indexOf('/corpus') >= 0)
    };
}

// 6. a refused write: the card must not claim a decision that was not recorded
{
    const env = await consentEnv({ state: { granted: false, history: [], capture_enabled: true }, broken: true });
    await env.evaluate("WORKER_MODULES.initCorpusConsent()");
    await tap(env);
    results.broken = {
        posted: posted.slice(),
        card: card(env),
        claims_on: env.evaluate("!!(WORKER_MODULES._corpusConsent && WORKER_MODULES._corpusConsent.granted)")
    };
}

globalThis.__results = results;
"""


@pytest.fixture(scope="module")
def results():
    return frontend_vm.run(HARNESS)


# ---------------------------------------------------------------------------
# what the worker sees
# ---------------------------------------------------------------------------
def test_the_card_states_the_servers_answer_and_asks_for_it(results):
    off = results["off"]
    assert off["asked"] == [CONSENT_PATH], (
        f"the card must read its state from {CONSENT_PATH}; it asked {off['asked']}"
    )
    assert off["state"] is False
    assert "data-corpus-consent=\"grant\"" in off["card"], (
        "a worker who has not agreed needs the control that lets them: " + off["card"]
    )


def test_the_wording_names_the_photo_and_that_it_can_be_stopped(results):
    """The three facts that make an agreement knowing: what is kept, who sees it, how to stop."""
    card_text = results["off"]["card"]
    assert "photo" in card_text.lower(), (
        "a toggle that does not say a photograph is kept is not consent to keep one: " + card_text
    )
    assert "Only you can decide this" in card_text, card_text
    assert "stop it at any time" in card_text, card_text


def test_the_card_follows_the_server_when_the_worker_has_agreed(results):
    on = results["on"]
    assert "You agreed" in on["card"], on["card"]
    assert on["control"] == "withdraw", (
        "an agreed card must offer the way out, not the way in: " + on["control"]
    )


def test_no_answer_yet_means_no_button(results):
    """A consent button whose write the page cannot make is a button to nowhere."""
    unknown = results["unknown"]
    assert unknown["state"] is None, unknown["state"]
    assert unknown["has_control"] is False, (
        "the card offered a consent control before the state was known: " + unknown["card"]
    )


# ---------------------------------------------------------------------------
# what the tap does
# ---------------------------------------------------------------------------
def test_agreeing_posts_the_grant_where_the_route_reads_it(results):
    grant = results["grant"]
    assert grant["control"] == "grant"
    assert grant["posted"] == [{"granted": True}], (
        f"the corpus consent route reads {{'granted': bool}}; the page sent {grant['posted']}"
    )
    assert "You agreed" in grant["card"], "the card did not follow the server after agreeing"


def test_withdrawing_is_the_same_endpoint_and_needs_no_administrator(results):
    withdraw = results["withdraw"]
    assert withdraw["control"] == "withdraw"
    assert withdraw["posted"] == [{"granted": False}], withdraw["posted"]
    assert set(withdraw["routes"]) == {CONSENT_PATH}, (
        f"withdrawal must not require a second screen; the page used {sorted(set(withdraw['routes']))}"
    )


def test_a_refused_write_does_not_leave_the_card_claiming_consent(results):
    broken = results["broken"]
    assert broken["posted"] == [{"granted": True}], broken["posted"]
    assert broken["claims_on"] is False, (
        "the card says the worker agreed, but the write was refused - a switch that lies about a "
        "consent record"
    )
    assert "Not agreed" in broken["card"], broken["card"]


# ---------------------------------------------------------------------------
# the strings, in every language the app ships
# ---------------------------------------------------------------------------
def test_every_string_the_card_uses_exists_in_all_four_tables():
    """A worker on the Arabic or Hindi build must be able to read what they are agreeing to."""
    keys = {"corpusTitle", "corpusChecking", "corpusNote", "corpusOnNote", "corpusOffNote",
            "corpusEnable", "corpusWithdraw", "corpusGranted", "corpusWithdrawn", "corpusUnavailable"}
    tables = {
        name: (frontend_vm.FRONTEND / name).read_text(encoding="utf-8")
        for name in ("i18n.js", "i18n.ar.js", "i18n.hi.js", "i18n.ur.js")
    }
    missing = {}
    for name, source in tables.items():
        absent = sorted(key for key in keys if f'"{key}"' not in source)
        if absent:
            missing[name] = absent
    assert not missing, f"the consent card would print a key instead of a sentence: {missing}"

    # And the card uses those keys and no others: a string added to the markup without a table
    # entry is the same failure, one release later.
    source = (frontend_vm.FRONTEND / "worker_modules.js").read_text(encoding="utf-8")
    start = source.index("corpusConsentHtml()")
    body = source[start:source.index("bindCorpusConsent(host)")]
    used = set(re.findall(r"I18n\.__\('([A-Za-z]+)'\)", body))
    assert used <= keys, f"the card asks for strings no table defines: {sorted(used - keys)}"
