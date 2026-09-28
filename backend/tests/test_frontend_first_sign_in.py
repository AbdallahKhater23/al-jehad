"""The first screen a hired worker ever sees, and the number it has to hand over.

WHY THIS EXISTS
---------------
``registrations.approve_registration`` writes a worker notice the moment it mints an account -
the id, and the three things a first sign-in is typed with - and ``POST /auth/login`` hands that
notice over while it is still unread (``notifications.worker_welcome``). Both halves are pinned
against the API in ``test_walk_up_registration.py``.

What an API test cannot see is whether any of it reaches the person. A notice in the inbox is a
row behind a tab; the worker it is for has never seen this application, does not know there is a
tab, and is standing at a gate. So the sign-in answer is where the card comes from, and this
suite is about the card:

1. the welcome rides in the *session*, not in a request - and it is therefore read on the first
   paint, before anything has been fetched, and it is still there after a reload;
2. it leads the clock panel: above the shift, above the review alert and above the inbox band,
   because it is the only thing on that screen about the account rather than about today;
3. the id is drawn as a *fact* as well as being inside the server's sentence. The reader has
   never been told this id before and cannot look it up, so it does not live only in prose;
4. the sentence itself is the server's own notice body, printed verbatim - one copy of the
   instructions, not a second one in the markup that can drift from it. It is also text and not
   markup, which is load-bearing here rather than tidy: that body interpolates the contact the
   *applicant typed on a public form*;
5. acknowledging it marks **that** notice read, in the query string where the route reads it,
   and re-reads the count rather than decrementing it - the badge came from the server;
6. a dismissal that cannot land still clears the card. The card is the acknowledgement of
   something the worker has now read on screen; what a failed write costs is the notice staying
   unread, which is exactly what brings the card back;
7. an account no approval minted - one an administrator typed, or a worker who has already read
   theirs - gets the clock panel alone.

Node is optional; without it these skip rather than fail.
"""

from __future__ import annotations

import html as html_module
import re

import pytest

import frontend_vm

pytestmark = pytest.mark.skipif(frontend_vm.NODE is None, reason="node is not installed")

HARNESS = r"""
// --- the server's answers, faked ----------------------------------------

//: What an approval wrote, still unread: the id, the contact the sign-in route will match, and
//: which password to bring. Word for word the shape ``registrations._approved_notice`` builds.
const WELCOME = {
    notice_id: 812,
    kind: 'account_approved',
    title: 'Your worker id is 431',
    body: 'Your application to work here was approved as worker, and your worker id is 431. '
        + 'To sign in for the first time, use id 431, the phone number you gave us (+965 555 0177), '
        + 'and the password you chose when you applied.',
    created_at: '2026-09-28 08:41:00'
};

//: What ``/auth/login`` answers with under ``welcome``. ``null`` once it has been read, and for
//: every account that was not minted by an approval.
let welcomeWaiting = null;
//: The inbox rows, kept in step with it: the band above the button counts the same notice the
//: card is made of, so the two must not be able to disagree.
let notices = [];
//: Every mark-read the page made, as the route would have parsed it.
let reads = [];
//: Flip to make the mark-read fail - the no-signal case.
let readBroken = false;

const STATS = {
    active_session: null, total_hours: 0, overtime_notify_hours: 8.1, break_minutes: 30,
    break_after_hours: 4, paid_day_hours: 8, auto_close_at_regular: '1', flagged_for_review: false
};

function freshWelcome() { return Object.assign({}, WELCOME); }

function dropNotice(id) {
    notices = notices.filter((notice) => Number(notice.id) !== Number(id));
}

function responders(url, init) {
    // First, because the read route's own path contains the inbox's.
    if (url.indexOf('/worker/me/notifications/read') >= 0) {
        reads.push({ url: url.split('/api/v1')[1], method: (init && init.method) || 'GET' });
        if (readBroken) return { status: 503, body: { detail: 'Database is locked.' } };
        const match = /notification_id=(\d+)/.exec(url);
        if (match) dropNotice(match[1]);
        return { status: 200, body: { status: 'success', marked: 1 } };
    }
    if (url.indexOf('/worker/me/notifications') >= 0) {
        return { status: 200, body: {
            worker_id: '431', unread: notices.length,
            notifications: url.indexOf('unread_only=true') >= 0 ? notices.slice(0, 1) : notices,
            push: { enabled: false }
        } };
    }
    if (url.indexOf('/auth/login') >= 0) {
        return { status: 200, body: {
            status: 'success',
            user: { id: '431', name: 'Nadia Haddad', role: 'worker', email: '', phone: '+965 555 0177' },
            welcome: welcomeWaiting,
            token: 'tok-new-worker', access_token: 'tok-new-worker', token_type: 'bearer',
            expires_at: '2026-10-28T08:41:00', expires_in: 2592000
        } };
    }
    if (url.indexOf('/worker/me/stats') >= 0) return { status: 200, body: STATS };
    if (url.indexOf('/worker/me/logs') >= 0) return { status: 200, body: [] };
    return { status: 200, body: {} };
}

/** A fresh world: one unread welcome waiting, and nothing else in the inbox. */
function world(welcome) {
    welcomeWaiting = welcome === undefined ? freshWelcome() : welcome;
    reads.length = 0;
    readBroken = false;
    notices = welcomeWaiting ? [{
        id: welcomeWaiting.notice_id, kind: welcomeWaiting.kind, title: welcomeWaiting.title,
        body: welcomeWaiting.body, created_at: welcomeWaiting.created_at, read: false, delivered: false
    }] : [];
}

function newEnv(options) {
    const env = boot(options);
    env.setResponder(responders);
    // The handset, which is where a worker signs in: the band above the button is drawn on a
    // phone and nowhere else, and ``Device.isMobile`` is read live from this key.
    env.evaluate("localStorage.setItem('layoutOverride', 'mobile')");
    return env;
}

/** The sign-in screen, filled in and submitted the way the browser does. */
async function signIn(env) {
    env.evaluate('UI.renderApp()');
    env.evaluate("document.getElementById('userId').value = '431'");
    env.evaluate("document.getElementById('email').value = '+965 555 0177'");
    env.evaluate("document.getElementById('password').value = 'site-attendance-2026'");
    const handler = (env.evaluate("document.getElementById('loginForm')").__handlers || {}).submit;
    if (!handler) throw new Error('the login form registered no submit handler');
    await handler({ preventDefault() {}, target: { querySelector: () => ({ disabled: false }) } });
    // The handler paints the portal without awaiting itself; a tick settles it before the
    // assertions paint the handset layout on purpose and read it.
    await new Promise((resolve) => setTimeout(resolve, 0));
    await new Promise((resolve) => setTimeout(resolve, 0));
}

/** The mobile shell, settled, with the clock panel painted into it. */
async function clockPanel(env) {
    await env.evaluate('UI.renderWorkerMobile()');
    await new Promise((resolve) => setTimeout(resolve, 0));
    return env.evaluate("document.getElementById('workerDashboard').innerHTML");
}

/** Tap the card's one control, through the delegated listener the module binds. */
async function tapDismiss(env) {
    return env.evaluate(`
        (async () => {
            const host = document.getElementById('workerWelcome');
            const handler = (host.__handlers || {}).click;
            if (!handler) return 'no-handler';
            await handler({ target: { closest: (sel) => (sel === '[data-worker-welcome-dismiss]' ? {} : null) } });
            return 'tapped';
        })()
    `);
}

function welcomeHost(env) { return env.evaluate("document.getElementById('workerWelcome').innerHTML"); }

function storedWelcome(env) {
    return env.evaluate("((JSON.parse(localStorage.getItem('session')) || {}).user || {}).welcome");
}

function toasts(env) {
    return env.evaluate("(document.getElementById('toastRoot').__children || []).map((el) => el.textContent)");
}

function textOf(markup) {
    return markup.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();
}

const results = {};

// 1. the sign-in answer itself: the card is on the first screen, above everything, with the id
{
    world();
    const env = newEnv();
    await signIn(env);
    const panel = await clockPanel(env);
    const card = welcomeHost(env);
    results.first_sign_in = {
        login_fields: env.requests
            .filter((request) => request.url.indexOf('/auth/login') >= 0)
            .map((request) => Object.keys(JSON.parse(request.body || '{}'))),
        in_session: env.evaluate("State.user.welcome && State.user.welcome.notice_id"),
        persisted: (storedWelcome(env) || {}).notice_id,
        // Nothing is fetched for the card: it came in with the answer, so the panel it leads
        // was painted without waiting for anything.
        asked_for_it: env.requests.map((request) => request.url.split('/api/v1')[1])
            .filter((url) => url && url.indexOf('welcome') >= 0),
        card: card,
        text: textOf(card),
        panel: panel,
        card_at: panel.indexOf('workerWelcome'),
        hero_at: panel.indexOf('hand-hero'),
        button_at: panel.indexOf('handleClock'),
        banner_at: panel.indexOf('workerAlertsBanner'),
        id_value: (/hand-welcome-id-value[^>]*>([^<]*)</.exec(card) || [])[1] || null,
        unread: env.evaluate('WORKER_MODULES.alertCount'),
        banner: env.evaluate("document.getElementById('workerAlertsBanner').innerHTML")
    };
}

// 2. the sentence is the server's own, escaped, and never the page's second copy of it
{
    world(Object.assign(freshWelcome(), {
        body: '<b>Welcome</b> 431 - sent to <script>alert(1)</script> by the applicant.'
    }));
    const env = newEnv();
    await signIn(env);
    await clockPanel(env);
    const card = welcomeHost(env);
    results.note_is_verbatim = {
        card: card,
        raw_tag: card.indexOf('<b>') >= 0 || card.indexOf('<script>') >= 0,
        escaped_tag: card.indexOf('&lt;b&gt;') >= 0,
        // The words, tags stripped and entities resolved, by the Python helper below: what a
        // reader gets is the sentence they were sent, not the escapes it travelled in.
        text: textOf(card)
    };
}

// 3. acknowledging it reads that notice - by id, in the query string - and both badges follow
{
    world();
    const env = newEnv();
    await signIn(env);
    await clockPanel(env);
    results.dismissed = {
        tapped: await tapDismiss(env),
        reads: reads.slice(),
        card: welcomeHost(env),
        in_session: env.evaluate('State.user.welcome'),
        persisted: storedWelcome(env),
        unread: env.evaluate('WORKER_MODULES.alertCount'),
        banner: env.evaluate("document.getElementById('workerAlertsBanner').innerHTML"),
        redrawn: env.evaluate('UI.workerTabBarHtml()'),
        toasts: toasts(env)
    };
}

// 4. a write that cannot land: the card still goes, and nothing is claimed about the badge
{
    world();
    const env = newEnv();
    await signIn(env);
    await clockPanel(env);
    readBroken = true;
    results.dismissal_refused = {
        tapped: await tapDismiss(env),
        reads: reads.slice(),
        card: welcomeHost(env),
        in_session: env.evaluate('State.user.welcome'),
        unread: env.evaluate('WORKER_MODULES.alertCount'),
        banner: env.evaluate("document.getElementById('workerAlertsBanner').innerHTML"),
        toasts: toasts(env)
    };
}

// 5. an account no approval minted: no card, and the panel is the one every worker gets
{
    world(null);
    const env = newEnv();
    await signIn(env);
    const panel = await clockPanel(env);
    results.no_welcome = {
        in_session: env.evaluate('State.user.welcome === null'),
        host: welcomeHost(env),
        hero_at: panel.indexOf('hand-hero'),
        button_at: panel.indexOf('handleClock'),
        unread: env.evaluate('WORKER_MODULES.alertCount'),
        asks: env.requests.map((request) => request.url.split('/api/v1')[1])
            .filter((url) => url && url.indexOf('/worker/me/notifications') === 0)
    };
}

// 6. a reload: the card rides the stored session, so it is still there until it is read
{
    world();
    const env = newEnv({ session: {
        id: '431', name: 'Nadia Haddad', role: 'worker', token: 'tok-new-worker',
        welcome: freshWelcome()
    } });
    const panel = await clockPanel(env);
    results.reload = {
        signed_in: env.evaluate('!!State.user'),
        card: welcomeHost(env),
        id_value: (/hand-welcome-id-value[^>]*>([^<]*)</.exec(welcomeHost(env)) || [])[1] || null,
        hero_at: panel.indexOf('hand-hero'),
        card_at: panel.indexOf('workerWelcome'),
        asked_for_login: env.requests.filter((request) => request.url.indexOf('/auth/login') >= 0).length
    };
}

// 7. every string the card uses exists in every table, with its placeholder
{
    const env = newEnv();
    results.translations = env.evaluate(`(function () {
        const KEYS = ${JSON.stringify([
            'welcomeTitle', 'welcomeSignedIn', 'welcomeWorkerId', 'welcomeDismiss', 'welcomeKept',
        ])};
        return {
            english: KEYS.filter((key) => !(key in TRANSLATIONS.en)),
            missing: Object.keys(TRANSLATIONS).map((lang) => [
                lang, KEYS.filter((key) => !(key in TRANSLATIONS[lang])).join(',')
            ]),
            placeholders: KEYS.filter((key) => {
                const holder = /\{\w+\}/.exec(TRANSLATIONS.en[key]);
                if (!holder) return false;
                return Object.keys(TRANSLATIONS).some((lang) => TRANSLATIONS[lang][key].indexOf(holder[0]) < 0);
            })
        };
    })()`);
}
"""


@pytest.fixture(scope="module")
def results() -> dict:
    return frontend_vm.run(HARNESS)


def text_of(markup: str) -> str:
    """The words a reader gets: tags out, entities resolved, whitespace collapsed."""
    return re.sub(r"\s+", " ", html_module.unescape(re.sub(r"<[^>]*>", " ", markup))).strip()


def test_the_first_screen_of_a_new_account_hands_the_number_over(results):
    """The sign-in answer is where it comes from, and it leads the only screen there is.

    Above the shift, above the review alert and above the inbox band, because it is the one
    thing on that panel about the account rather than about today - and because the reader,
    who has never seen this application before, is meeting all of it for the first time.
    """
    first = results["first_sign_in"]
    assert first["in_session"] == 812, first
    assert first["persisted"] == 812, (
        "the welcome has to ride the stored session, or a reload loses the only number this "
        f"worker has: {first['persisted']}"
    )
    assert first["asked_for_it"] == [], (
        f"the card is painted from the sign-in answer, not fetched: {first['asked_for_it']}"
    )
    # The three credentials are still the form's own business; nothing about the welcome travels
    # in the request.
    assert first["login_fields"] == [["user_id", "email_or_phone", "password"]], first["login_fields"]

    card = first["card"]
    assert card, "the first sign-in of a hired worker drew no welcome at all"
    assert "hand-welcome" in card, card
    assert first["id_value"] == "431", (
        f"the id is not drawn as a fact of its own: {first['card']}"
    )
    assert first["hero_at"] > first["card_at"] >= 0, (
        f"the welcome sits at {first['card_at']} and the shift card at {first['hero_at']}: below "
        "the shift is a card a worker meets after the thing they opened the app to do"
    )
    assert 0 <= first["card_at"] < first["button_at"], first
    assert 0 <= first["card_at"] < first["banner_at"], first

    text = first["text"]
    assert "Welcome" in text, text
    assert "Nadia Haddad" in text, text
    assert "431" in text, text
    assert "+965 555 0177" in text, text
    assert "Got it" in text, (
        f"nothing on the card says what acknowledging it does: {text}"
    )
    # The band above the button counts the same notice the card is made of, so the worker who
    # dismisses it does not leave a badge pointing at the thing they just read.
    assert first["unread"] == 1, first
    assert "1 new alert" in text_of(first["banner"]), first["banner"]


def test_the_card_prints_the_notices_own_sentence_and_never_its_markup(results):
    """One copy of the instructions, escaped on the way in.

    The body interpolates the contact the applicant typed into a **public form**, so a card that
    rendered it as markup would carry whatever they typed into every screen of this worker's
    account. It is also the reason the sentence is not re-written in the markup: two copies of
    "which id, which contact, which password" are two things that can disagree.
    """
    verbatim = results["note_is_verbatim"]
    assert verbatim["escaped_tag"], (
        f"the notice body's tag was not escaped on the way onto the card\n{verbatim['card']}"
    )
    assert not verbatim["raw_tag"], (
        f"the notice body became markup on the worker's first screen\n{verbatim['card']}"
    )
    assert "&lt;b&gt;Welcome&lt;/b&gt; 431" in verbatim["card"], verbatim["card"]
    # Still the server's sentence, entities and all - not a paragraph with a hole in it.
    assert "<b>Welcome</b> 431" in text_of(verbatim["card"]), verbatim["card"]


def test_acknowledging_it_reads_that_notice_and_moves_both_badges(results):
    """The id goes where the route reads it, and the count is re-read rather than decremented.

    ``POST /worker/me/notifications/read`` takes ``notification_id`` from the query string, so a
    body it never parses would mark the whole inbox read - the one mistake here that destroys
    information instead of showing it. And the number on the tab came from the server, so it is
    asked again instead of being worked out on this screen.
    """
    dismissed = results["dismissed"]
    assert dismissed["tapped"] == "tapped", dismissed
    assert dismissed["reads"] == [
        {"url": "/worker/me/notifications/read?notification_id=812", "method": "POST"}
    ], dismissed["reads"]
    assert dismissed["card"] == "", (
        f"the card is still on screen after being acknowledged\n{dismissed['card']}"
    )
    assert dismissed["in_session"] is None, dismissed
    assert dismissed["persisted"] is None, (
        "the session still carries the welcome, so the card returns on the next reload"
    )
    assert dismissed["unread"] == 0, dismissed
    assert dismissed["banner"] == "", dismissed["banner"]
    assert "data-alert-count" not in dismissed["redrawn"], dismissed["redrawn"]


def test_a_dismissal_that_cannot_land_still_clears_the_card(results):
    """The card is the acknowledgement; the write is what keeps it from coming back.

    A phone on site cellular is allowed to lose this one request. What it costs is honest: the
    notice is still unread on the server, so the card is waiting the next time this account signs
    in - which is the behaviour read state is there for. What must not happen is the reverse: a
    card left sitting on the clock panel because a request failed, with the worker unable to
    clear it, and a badge silently decremented on the strength of a write that never landed.
    """
    refused = results["dismissal_refused"]
    assert refused["tapped"] == "tapped", refused
    assert len(refused["reads"]) == 1, refused["reads"]
    assert refused["card"] == "", (
        f"a failed write left the card on the clock panel\n{refused['card']}"
    )
    assert refused["in_session"] is None, refused
    assert refused["unread"] == 1, (
        f"the badge was moved by a write that never landed: {refused}"
    )
    assert "Database is locked." in " ".join(refused["toasts"]), refused["toasts"]


def test_an_account_no_approval_minted_gets_the_clock_panel_alone(results):
    """No welcome in the answer means no card - and nothing asked for on the chance of one."""
    none = results["no_welcome"]
    assert none["host"] == "", none["host"]
    assert none["in_session"] is True, none
    assert 0 <= none["hero_at"] < none["button_at"], none
    assert any(url.startswith("/worker/me/notifications") for url in none["asks"]), (
        f"the band above the button is still read for every worker: {none['asks']}"
    )


def test_a_reload_keeps_the_card_until_it_has_been_read(results):
    """The card is not a one-shot event on the sign-in screen - it is a session fact.

    A worker who reloads the page, or opens the app on the phone in their other pocket, is still
    somebody who has not been told their id. A per-device "shown once" flag would lose it exactly
    there; the session carries it, and reading the notice is the only thing that clears it.
    """
    reload = results["reload"]
    assert reload["signed_in"] is True, reload
    assert reload["asked_for_login"] == 0, (
        "a restored session should not sign in again to have a welcome"
    )
    assert reload["card"], "the restored session drew no welcome"
    assert reload["id_value"] == "431", reload["card"]
    assert 0 <= reload["card_at"] < reload["hero_at"], reload


def test_every_welcome_string_exists_in_every_language(results):
    """A key present in English only renders as its own name on every phone that lacks it."""
    translations = results["translations"]
    assert translations["english"] == [], translations["english"]
    languages = [lang for lang, _ in translations["missing"]]
    assert languages == ["en", "ar", "hi", "ur"], languages
    assert all(missing == "" for _, missing in translations["missing"]), translations["missing"]
    assert translations["placeholders"] == [], (
        f"a {{placeholder}} only some tables carry renders the token itself: "
        f"{translations['placeholders']}"
    )
