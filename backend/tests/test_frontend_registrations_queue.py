"""The console's registrations queue, exercised for real.

WHY THIS EXISTS
---------------
``test_walk_up_registration`` covers the intake end to end on the server: the switch, the
validation order, the held account, the queue cap, the face filed at submission, and the one
status change an administrator makes. What it cannot see is the reviewer: whether anybody can
*reach* the queue, read a face before deciding, approve somebody, and be told what to hand over
now that the account is theirs. That is this file.

Five properties, and each one is a decision rather than a rendering detail:

1. **the tab is a console screen, for the console's audience.** It is offered to an
   administrator (the route behind it is ``admin_only``), it sits with the other people
   screens, and it draws a glyph of its own.
2. **the queue is the server's, oldest first.** The read asks for ``pending_approval`` and
   renders the rows in the order they arrived, because the applicant at the front is the one
   being phoned about.
3. **the photograph is fetched, never embedded.** An ``<img src>`` cannot carry a token, so
   a URL that worked in a ``src`` would be a URL that worked for anybody - and forty
   applications in one response would be forty faces. One request, this session's
   credential, on the reviewer's own tap.
4. **the account a decision was about survives the row that produced it.** A decided account
   leaves the queue by definition, so what the administrator has to hand over - the id and the
   contact the sign-in route matches on - is kept on the screen (and in the answer the server
   sent) rather than in a toast that scrolls away. A refusal needs a reason here, and the server
   answers whether the face came with it.
5. **every sentence exists in all four language tables.** A string that reaches three of
   them is a blank line for the reader of the fourth.

Node is optional; without it these skip rather than fail.
"""

from __future__ import annotations

import html as html_module
import re
from pathlib import Path

import pytest

import frontend_vm

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FRONTEND = PROJECT_ROOT / "frontend"
BACKEND = PROJECT_ROOT / "backend"

pytestmark = pytest.mark.skipif(frontend_vm.NODE is None, reason="node is not installed")

HARNESS = r"""
// --- the server's answers, faked ----------------------------------------

//  Two applications, and the second one's name is markup: a full name is free text an
//  applicant typed into a public form, and it is rendered inside a card.
const PENDING = [
    {
        id: '7', status: 'pending_approval', full_name: 'Nadia Saleh', phone: '+965 555 0101',
        email: 'nadia@example.com', requested_role: 'worker',
        work_details: 'Formwork, six years on tower sites.',
        created_at: '2026-09-25 06:40:00', has_photo: true, photo_bytes: 4096
    },
    {
        id: '8', status: 'pending_approval', full_name: '<img src=x onerror=alert(1)>Omar',
        phone: '', email: 'omar@example.com', requested_role: 'moallem',
        work_details: '', created_at: '2026-09-26 05:10:00', has_photo: true, photo_bytes: 4096
    }
];

//: The account the fake approval is about. Deliberately not one of the fixture's accounts: this
//: is a number that only exists in the answer, which is what makes it worth asserting.
const MINTED = '1042';

let queueFails = null;          // a status to answer the queue read with, or null
let photoMissing = false;       // answer the photograph with 404
let approveWritesTemplate = true;
//: The contact the fake approval answers with. It comes back in the *answer* because the queue
//: row that held it has left the queue the moment it is approved - and it is the second of the
//: three credentials a first sign-in is typed with.
let approveContact = { email: 'nadia@example.com', phone: '+965 555 0101' };

//: The link's state, as the fake server reports it. ``open`` is the ordinary case; the other two
//: are the two ways it can be shut and the console is required to tell them apart, because
//: "nobody has opened this yet" and "somebody closed it this morning" are the same state and
//: different sentences. Both are states this console can leave.
let intakeReason = 'open';
let intakeFails = null;         // a status to answer the intake read with, or null
const intakePosts = [];         // every intake write, in order

function intakeBody() {
    return {
        status: 'success',
        accepting: intakeReason === 'open',
        reason: intakeReason,
        deployment_enabled: intakeReason !== 'closed_by_default',
        decided: intakeReason === 'closed_by_console',
        updated_at: null,
        updated_by: intakeReason === 'closed_by_console' ? '1000' : null
    };
}
let rejectDestroysPhoto = true;
let decisionFails = null;       // a status to answer a decision with, or null
const decisions = [];           // every decision posted, in order
const reads = [];               // every queue read, path included

function queueBody(pending, requests) {
    return {
        status: 'success', enabled: true, pending: pending, requests: requests,
        count: requests.length
    };
}

function responders(url, init) {
    const path = url.split('/api/v1')[1] || url;
    if (/\/admin\/registrations\/\d+\/photo$/.test(path)) {
        if (photoMissing) return { status: 404, body: { detail: 'This request has no photo on file.' } };
        return { status: 200, body: {}, contentType: 'image/jpeg' };
    }
    if (/\/admin\/registrations\/\d+\/(approve|reject)$/.test(path)) {
        const requestId = parseInt(path.split('/')[3], 10);
        const action = path.split('/').pop();
        decisions.push({ path: path, body: JSON.parse(init.body) });
        if (decisionFails) {
            return {
                status: decisionFails,
                body: { detail: { error_code: 'already_reviewed', message: 'Another administrator reviewed this request first.' } }
            };
        }
        if (action === 'approve') {
            return {
                status: 200,
                body: {
                    status: 'success', user_id: MINTED,
                    name: 'Nadia Saleh', role: 'worker', template_written: approveWritesTemplate,
                    email: approveContact.email, phone: approveContact.phone,
                    message: MINTED + ' is approved: Nadia Saleh can clock in from now on.'
                }
            };
        }
        return {
            status: 200,
            body: {
                status: 'success', user_id: String(requestId),
                photo_destroyed: rejectDestroysPhoto,
                message: 'The account was refused and its face has been destroyed.'
            }
        };
    }
    if (/\/admin\/registrations\/intake$/.test(path)) {
        if (intakeFails) return { status: intakeFails, body: { detail: 'Database is locked.' } };
        if (init && String(init.method || 'GET').toUpperCase() === 'POST') {
            const wanted = JSON.parse(init.body);
            intakePosts.push({ path: path, body: wanted });
            // The *answer* is the state, not the request, and the console draws what came back -
            // so the fake server's job is to move the switch the way the real one does: this
            // route writes the position, and the position is what decides, whatever the
            // deployment's own default was.
            intakeReason = wanted.open ? 'open' : 'closed_by_console';
        }
        return { status: 200, body: intakeBody() };
    }
    if (path.indexOf('/admin/registrations') >= 0) {
        reads.push(path);
        if (queueFails) return { status: queueFails, body: { detail: 'Database is locked.' } };
        // ``limit=1`` is the badge's read: the count it carries is the queue's own total, which
        // is why the badge can be right while the page it fetched is one row long.
        const badgeRead = /limit=1(&|$)/.test(path);
        return { status: 200, body: queueBody(PENDING.length, badgeRead ? PENDING.slice(0, 1) : PENDING) };
    }
    return { status: 200, body: {} };
}

// --- reading the rendered page -------------------------------------------

function textOf(markup) {
    return markup.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();
}

/** Every application card, in the order the screen drew them. */
function cardsOf(markup) {
    const cards = markup.match(/<article class="ui-card[^"]*" data-registration="[^"]*"[\s\S]*?<\/article>/g) || [];
    return cards.map((card) => ({
        id: (/data-registration="([^"]*)"/.exec(card) || [])[1],
        waiting: /class="ui-badge[^"]*"/.test(card),
        danger: card.indexOf('is-danger') >= 0,
        photo: /data-registration-photo="([^"]*)"/.exec(card) ? true : false,
        approve: /data-registration-approve="([^"]*)"/.exec(card) ? true : false,
        reject: /data-registration-reject="([^"]*)"/.exec(card) ? true : false,
        note: /id="registrationNote-[^"]*"/.test(card),
        text: textOf(card)
    }));
}

function toasts(env) {
    return env.evaluate("(document.getElementById('toastRoot').__children || []).map((el) => el.textContent)");
}

/** The tab's own screen, which is the pane the shell paints into. */
function render(env) {
    return env.evaluate("document.getElementById('adminContent').innerHTML");
}

function consoleEnv() {
    queueFails = null;
    photoMissing = false;
    approveWritesTemplate = true;
    approveContact = { email: 'nadia@example.com', phone: '+965 555 0101' };
    intakeReason = 'open';
    intakeFails = null;
    intakePosts.length = 0;
    rejectDestroysPhoto = true;
    decisionFails = null;
    decisions.length = 0;
    reads.length = 0;
    const env = boot();
    env.setResponder(responders);
    env.evaluate("State.saveUser(" + JSON.stringify({
        id: '1000', name: 'Head Admin', role: 'head_admin', token: 'tok-admin'
    }) + ")");
    env.evaluate("I18n.setLang('en')");
    return env;
}

/**
 * A stand-in for the two nav buttons a console really has.
 *
 * The nav is markup inside a string, so the stub DOM has no node to paint a badge onto -
 * and the badge is the one place the count becomes something an operator sees, so it is
 * worth asserting rather than merely running. This records what the paint appends and
 * answers ``querySelector`` with whatever that was, which is exactly how the app's own
 * ``paintRegistrationsBadge`` asks for it.
 */
const NAV_BUTTON = `
    window.__navButton = {
        __badge: null, __attrs: {},
        querySelector() { return this.__badge; },
        appendChild(child) { this.__badge = child; },
        setAttribute(name, value) { this.__attrs[String(name)] = String(value); },
        removeAttribute(name) { delete this.__attrs[String(name)]; }
    };
    document.querySelectorAll = (selector) => (
        String(selector).indexOf('Registrations') >= 0 ? [window.__navButton] : []
    );
`;

const results = {};

// 1. the console has the tab, and it is offered to an administrator
{
    const env = consoleEnv();
    results.tabs = env.evaluate("ADMIN_TABS.map((tab) => [tab.id, tab.key, tab.group, tab.icon, !!tab.rootOnly])");
    results.nav = env.evaluate("adminNavGroups().reduce((all, group) => all.concat(group.tabs.map((tab) => tab.id)), [])");
    results.visible = env.evaluate("adminVisibleTabs().map((tab) => tab.id)");
}

// 2. opening it reads the pending queue and draws it oldest first
{
    const env = consoleEnv();
    await env.evaluate("UI.renderAdminTab('Registrations')");
    const markup = render(env);
    results.queue = {
        page_flag: markup.indexOf('data-registrations="true"') >= 0,
        count: (/data-registrations-count="(\d+)"/.exec(markup) || [])[1],
        count_text: textOf((/<p class="ui-section-note" data-registrations-count="\d+">([\s\S]*?)<\/p>/.exec(markup) || [])[0] || ''),
        cards: cardsOf(markup),
        reads: reads.slice(),
        closed_note: markup.indexOf('data-registrations-closed') >= 0,
        // The name an applicant typed is text, not markup - the second one is an <img> tag.
        escaped: markup.indexOf('&lt;img src=x onerror=alert(1)&gt;') >= 0,
        raw_markup_injected: markup.indexOf('<img src=x onerror=alert(1)>') >= 0,
        bare_photo_url: /<img[^>]*src="[^"]*\/admin\/registrations/.test(markup),
        inline_handler: /onclick="[^"]*handleRegistration/.test(markup)
    };
}

// 3. an empty queue says so, and a closed intake says so too, without hiding the queue
{
    const env = consoleEnv();
    env.setResponder((url, init) => {
        const path = url.split('/api/v1')[1] || url;
        if (path.indexOf('/admin/registrations') >= 0) {
            return { status: 200, body: { status: 'success', enabled: false, pending: 0, requests: [], count: 0 } };
        }
        return { status: 200, body: {} };
    });
    await env.evaluate("UI.renderAdminTab('Registrations')");
    const markup = render(env);
    results.empty = {
        flag: markup.indexOf('data-registrations-empty="true"') >= 0,
        cards: cardsOf(markup).length,
        text: textOf(markup),
        closed: env.evaluate("I18n.__('registrationsClosed')"),
        shows_closed: markup.indexOf('data-registrations-closed="true"') >= 0
    };
}

// 4. the photograph, fetched with the session's token
{
    const env = consoleEnv();
    await env.evaluate("UI.renderAdminTab('Registrations')");
    await env.evaluate("UI_MODULES.showRegistrationPhoto('7')");
    const fetched = env.requests.filter((r) => /\/admin\/registrations\/7\/photo$/.test(r.url))[0];
    results.photo = {
        called: !!fetched,
        url: fetched ? fetched.url.replace(/^https?:\/\/[^/]*\/api\/v1/, '') : null,
        authorized: fetched ? fetched.headers.Authorization : null,
        src: env.evaluate("String(document.getElementById('registrationPhoto7').src || '')"),
        visible: env.evaluate("!document.getElementById('registrationPhoto7').classList.contains('hidden')")
    };
}

// 5. a photograph that is gone is stated, not shown as a broken image
{
    const env = consoleEnv();
    photoMissing = true;
    await env.evaluate("UI.renderAdminTab('Registrations')");
    await env.evaluate("UI_MODULES.showRegistrationPhoto('7')");
    results.photo_gone = {
        toast: toasts(env).join(' | '),
        src: env.evaluate("String(document.getElementById('registrationPhoto7').src || '')"),
        gone_sentence: env.evaluate("I18n.__('registrationsPhotoGone')"),
        failed_sentence: env.evaluate("I18n.__('registrationsPhotoFailed')")
    };
}

// 6. approving sends the note and shows the id the server minted
{
    const env = consoleEnv();
    await env.evaluate("UI.renderAdminTab('Registrations')");
    env.evaluate("document.getElementById('registrationNote-7').value = 'Hired for the B site'");
    await env.evaluate("UI_MODULES.handleRegistration('7', 'approve')");
    const markup = render(env);
    results.approve = {
        posted: decisions.slice(),
        minted: (/data-registration-minted="([^"]*)"/.exec(markup) || [])[1],
        minted_id: textOf((/<p class="ui-fact-value" data-registration-minted-id>([\s\S]*?)<\/p>/.exec(markup) || [])[0] || ''),
        warning: markup.indexOf('data-registration-template-warning') >= 0,
        receipt: textOf((/<div class="ui-card is-ok"[\s\S]*?<\/div>/.exec(markup) || [])[0] || ''),
        // What the console has to hand over, and the decision it kept to draw it from.
        signin: (/data-registration-signin="([^"]*)"/.exec(markup) || [])[1],
        signin_text: textOf((/<p class="ui-note" data-registration-signin="[^"]*">([\s\S]*?)<\/p>/.exec(markup) || [])[0] || ''),
        signin_expected: env.evaluate("I18n.__('registrationsSignIn').replace('{id}', '1042').replace('{contact}', '+965 555 0101 \u00b7 nadia@example.com')"),
        approved_sentence: env.evaluate("I18n.__('registrationsApproved').replace('{id}', '1042').replace('{name}', 'Nadia Saleh')"),
        page_text: textOf(markup),
        last: env.evaluate("UI_MODULES._registrationsLast"),
        toast: toasts(env).join(' | '),
        queue_reads: reads.slice()
    };
}

// 6b. ...and an application that gave no contact at all is told to leave that box empty
{
    const env = consoleEnv();
    approveContact = { email: '', phone: '' };
    await env.evaluate("UI.renderAdminTab('Registrations')");
    await env.evaluate("UI_MODULES.handleRegistration('7', 'approve')");
    const markup = render(env);
    results.no_contact = {
        signin_text: textOf((/<p class="ui-note" data-registration-signin="[^"]*">([\s\S]*?)<\/p>/.exec(markup) || [])[0] || ''),
        signin_expected: env.evaluate("I18n.__('registrationsSignInNoContact').replace('{id}', '1042')"),
        generic_sentence: env.evaluate("I18n.__('registrationsSignIn')")
    };
}

// 7. an approval whose face reference could not be stored is reported as what it is
{
    const env = consoleEnv();
    approveWritesTemplate = false;
    await env.evaluate("UI.renderAdminTab('Registrations')");
    await env.evaluate("UI_MODULES.handleRegistration('7', 'approve')");
    const markup = render(env);
    results.no_template = {
        minted: (/data-registration-minted="([^"]*)"/.exec(markup) || [])[1],
        warning: markup.indexOf('data-registration-template-warning="true"') >= 0,
        warning_text: textOf((/<p class="ui-note is-warn" data-registration-template-warning="true">([\s\S]*?)<\/p>/.exec(markup) || [])[0] || ''),
        toast: toasts(env).join(' | '),
        sentence: env.evaluate("I18n.__('registrationsTemplateWarning').replace('{id}', '1042')")
    };
}

// 8. refusing needs a reason, and then destroys the photograph
{
    const env = consoleEnv();
    await env.evaluate("UI.renderAdminTab('Registrations')");
    await env.evaluate("UI_MODULES.handleRegistration('8', 'reject')");
    const blocked = {
        posted: decisions.length,
        toast: toasts(env).join(' | '),
        needs_note: env.evaluate("I18n.__('registrationsRejectNeedsNote')")
    };
    env.evaluate("document.getElementById('registrationNote-8').value = 'No formwork experience'");
    await env.evaluate("UI_MODULES.handleRegistration('8', 'reject')");
    const markup = render(env);
    results.refuse = {
        blocked: blocked,
        posted: decisions.slice(),
        refused_card: (/data-registration-refused="([^"]*)"/.exec(markup) || [])[1],
        toast: toasts(env).join(' | ')
    };
}

// 9. a decision somebody else already made re-reads the queue rather than leaving a dead button
{
    const env = consoleEnv();
    await env.evaluate("UI.renderAdminTab('Registrations')");
    const before = reads.length;
    decisionFails = 409;
    env.evaluate("document.getElementById('registrationNote-7').value = 'Hired'");
    await env.evaluate("UI_MODULES.handleRegistration('7', 'approve')");
    results.conflict = {
        reads_before: before,
        reads_after: reads.length,
        toast: toasts(env).join(' | ')
    };
}

// 10. the waiting count reaches the tab's badge
{
    const env = consoleEnv();
    env.evaluate(NAV_BUTTON);
    await env.evaluate("UI.refreshRegistrationsBadge(true)");
    const painted = env.evaluate("({count: State.registrationsWaiting, text: window.__navButton.__badge ? window.__navButton.__badge.textContent : null, label: window.__navButton.__attrs['aria-label'] || null, hidden: window.__navButton.__badge ? window.__navButton.__badge.getAttribute('aria-hidden') : null})");
    // ...and a queue that empties drops the numeral rather than leaving a stale figure behind.
    env.evaluate("State.registrationsWaiting = 0");
    env.evaluate("UI.paintRegistrationsBadge()");
    results.badge = {
        painted: painted,
        badge_read: reads.slice(),
        after_zero: env.evaluate("window.__navButton.__attrs['aria-label'] || null")
    };
}

// 8b. a photograph the server could not remove is not reported as destroyed
{
    const env = consoleEnv();
    rejectDestroysPhoto = false;
    await env.evaluate("UI.renderAdminTab('Registrations')");
    env.evaluate("document.getElementById('registrationNote-8').value = 'Not this trade'");
    await env.evaluate("UI_MODULES.handleRegistration('8', 'reject')");
    const markup = render(env);
    results.refuse_kept = {
        refused_card: (/data-registration-refused="([^"]*)"/.exec(markup) || [])[1],
        notice: textOf((/<div class="ui-card is-warn" data-registration-refused[\s\S]*?<\/div>/.exec(markup) || [])[0] || ''),
        destroy_sentence: env.evaluate("I18n.__('registrationsRejected').replace('{request}', '8')"),
        kept_sentence: env.evaluate("I18n.__('registrationsPhotoKept').replace('{request}', '8')"),
        toast: toasts(env).join(' | ')
    };
}

// 11. a read that fails says what failed, and offers the one control that fixes it
{
    const env = consoleEnv();
    queueFails = 503;
    await env.evaluate("UI.renderAdminTab('Registrations')");
    const markup = render(env);
    results.failure = {
        error: markup.indexOf('class="ui-error"') >= 0,
        message: textOf(markup),
        retry: markup.indexOf("UI.renderAdminTab('Registrations')") >= 0,
        cards: cardsOf(markup).length
    };
}

// 12. every sentence the screen can say exists in all four translation tables
{
    const env = consoleEnv();
    results.i18n = {
        langs: env.evaluate("Object.keys(TRANSLATIONS).sort()"),
        keys: env.evaluate(
            "Object.keys(TRANSLATIONS.en).filter((key) => key.indexOf('registrations') === 0).sort()"
        ),
        missing: env.evaluate(
            "Object.keys(TRANSLATIONS.en).filter((key) => key.indexOf('registrations') === 0)"
            + ".map((key) => [key, Object.keys(TRANSLATIONS).filter("
            + "(lang) => !(key in TRANSLATIONS[lang])).join(',')])"
            + ".filter((row) => row[1] !== '')"
        ),
        hint_missing: env.evaluate(
            "Object.keys(TRANSLATIONS).filter((lang) => !('hintRegistrations' in TRANSLATIONS[lang])).join(',')"
        ),
        label: env.evaluate("I18n.__('registrations')")
    };
}

// 13. the link's own switch: the state it is drawn in, and the one action it offers
{
    const env = consoleEnv();
    await env.evaluate("UI.renderAdminTab('Registrations')");
    const openMarkup = render(env);
    // Closing the link is a real decision, so the console is then asked to draw what the server
    // answered rather than what it asked for.
    await env.evaluate("UI_MODULES.toggleRegistrationsIntake('close')");
    const closedMarkup = render(env);
    // The card, cut at the sentence that closes it rather than at the first closing tag: the
    // card is nested (a spread, a row of two facts), so a ``</div>``-terminated match stops
    // before the reason line - which is the half this is about.
    const cardText = (markup) => textOf((/<div class="ui-card" data-registrations-intake="[^"]*"[\s\S]*?<\/p>/.exec(markup) || [])[0] || '');
    results.intake = {
        open_state: (/data-registrations-intake-state="([^"]*)"/.exec(openMarkup) || [])[1],
        open_card: (/data-registrations-intake="([^"]*)"/.exec(openMarkup) || [])[1],
        open_action: (/data-registration-intake="([^"]*)"/.exec(openMarkup) || [])[1],
        open_text: cardText(openMarkup),
        open_sentence: env.evaluate("I18n.__('registrationsIntakeWhyOpen')"),
        closed_state: (/data-registrations-intake-state="([^"]*)"/.exec(closedMarkup) || [])[1],
        closed_action: (/data-registration-intake="([^"]*)"/.exec(closedMarkup) || [])[1],
        closed_text: cardText(closedMarkup),
        closed_sentence: env.evaluate("I18n.__('registrationsIntakeWhyClosedByConsole')"),
        posts: intakePosts.slice(),
        toast: toasts(env).join(' | ')
    };
}

// 14. a deployment that ships closed is a link the console can open - which is the whole point
{
    const env = consoleEnv();
    intakeReason = 'closed_by_default';
    await env.evaluate("UI.renderAdminTab('Registrations')");
    const closing = render(env);
    // ...and the tap reaches the server, whose answer is then what the card is drawn from.
    await env.evaluate("UI_MODULES.toggleRegistrationsIntake('open')");
    const opened = render(env);
    results.intake_default_off = {
        state: (/data-registrations-intake-state="([^"]*)"/.exec(closing) || [])[1],
        // ``data-registration-intake`` is the button; ``data-registrations-intake`` is the card.
        lever: (/data-registration-intake="([^"]*)"/.exec(closing) || [])[1],
        text: textOf((/<div class="ui-card" data-registrations-intake="[^"]*"[\s\S]*?<\/p>/.exec(closing) || [])[0] || ''),
        sentence: env.evaluate("I18n.__('registrationsIntakeWhyClosedByDefault')"),
        posts: intakePosts.slice(),
        opened_state: (/data-registrations-intake-state="([^"]*)"/.exec(opened) || [])[1],
        opened_action: (/data-registration-intake="([^"]*)"/.exec(opened) || [])[1]
    };
}

// 15. a switch whose state could not be read is left off the screen rather than guessed at
{
    const env = consoleEnv();
    intakeFails = 503;
    await env.evaluate("UI.renderAdminTab('Registrations')");
    const markup = render(env);
    results.intake_unreadable = {
        card: markup.indexOf('data-registrations-intake=') >= 0,
        lever: markup.indexOf('data-registration-intake=') >= 0,
        // The queue's own closed note is drawn from the queue read, so the screen still says
        // whether the link is accepting - there is just no control on it.
        queue_drawn: cardsOf(markup).length
    };
}
"""


@pytest.fixture(scope="module")
def results() -> dict:
    return frontend_vm.run(HARNESS)


def test_the_tab_is_on_the_rail_and_offered_to_an_administrator(results):
    tabs = {tab[0]: tab for tab in results["tabs"]}
    assert "Registrations" in tabs, f"the console has no Registrations tab: {list(tabs)}"
    assert tabs["Registrations"][1] == "registrations", "the tab is labelled, not left as a key"
    assert tabs["Registrations"][2] == "navGroupPeople", (
        "an intake queue is about people, not about the deployment's configuration"
    )
    assert tabs["Registrations"][3] == "registrations", "the tab draws a glyph of its own"
    assert tabs["Registrations"][4] is False, (
        "the queue is a site administrator's work - the route behind it is admin_only"
    )
    # The rail and the phone strip flatten the same groups, so this is both of them.
    assert "Registrations" in results["nav"]
    assert "Registrations" in results["visible"]


def test_the_queue_is_read_pending_and_drawn_in_the_order_it_arrived(results):
    queue = results["queue"]
    assert queue["page_flag"] is True
    assert queue["reads"], "the tab rendered without reading the queue"
    assert all(read.startswith("/admin/registrations") for read in queue["reads"]), queue["reads"]
    # One read for the queue panel. The badge's own count read is a different call with a
    # different limit (see ``test_the_waiting_count_reaches_the_tab_badge``).
    panel = [read for read in queue["reads"] if "limit=200" in read]
    assert len(panel) == 1, queue["reads"]
    assert "status=pending_approval" in panel[0], (
        "the work to do is the pending queue, not the archive, and the status is the server's "
        "own constant - the endpoint answers 400 for anything it does not know"
    )
    assert not queue["closed_note"], "intake is open in this answer"
    # Oldest first, straight from the server: the card order is the response order.
    assert [card["id"] for card in queue["cards"]] == ["7", "8"]
    assert queue["count"] == "2"
    assert "2 waiting on a decision" in queue["count_text"]


def test_every_card_carries_the_applicant_the_waiting_and_the_two_answers(results):
    cards = {card["id"]: card for card in results["queue"]["cards"]}
    first = cards["7"]
    assert first["waiting"] is True, "an application waiting on a person says how long"
    assert first["photo"] and first["approve"] and first["reject"] and first["note"]
    assert "Nadia Saleh" in first["text"]
    assert "Worker" in first["text"], "the requested role is shown as words, not as a code"
    assert "#7" in first["text"], "the request's own number, which the decision is filed under"
    assert "Formwork, six years on tower sites." in first["text"]
    assert "+965 555 0101" in first["text"]
    # A contact line with no phone falls back to what is there rather than to an empty row.
    assert "omar@example.com" in cards["8"]["text"]


def test_what_an_applicant_typed_is_text_and_the_photo_is_not_a_url(results):
    queue = results["queue"]
    assert queue["escaped"] is True, "a name with markup in it is escaped like any other text"
    assert queue["raw_markup_injected"] is False
    assert queue["bare_photo_url"] is False, (
        "an <img src> cannot carry a token, so a src that worked would work for anybody"
    )
    assert queue["inline_handler"] is False, (
        "the answers are bound with delegated listeners, not with an inline attribute"
    )


def test_the_photograph_is_fetched_with_the_session_token(results):
    photo = results["photo"]
    assert photo["called"] is True, "the reviewer asked to see the face and nothing was fetched"
    assert photo["url"] == "/admin/registrations/7/photo"
    assert photo["authorized"] == "Bearer tok-admin"
    assert photo["src"] == "blob:stub", "the bytes become an object URL only this page can use"
    assert photo["visible"] is True


def test_a_photograph_that_is_gone_says_so_rather_than_failing(results):
    gone = results["photo_gone"]
    assert gone["gone_sentence"] in gone["toast"], (
        f"a destroyed photograph is a fact about the record, not a load error: {gone['toast']}"
    )
    assert gone["failed_sentence"] not in gone["toast"], (
        "a 404 is the photograph having been destroyed, and the server says which it is"
    )
    assert gone["src"] == "", "nothing is shown that is not there"


def test_approving_posts_the_note_and_shows_the_id_it_minted(results):
    approve = results["approve"]
    assert len(approve["posted"]) == 1, approve["posted"]
    posted = approve["posted"][0]
    assert posted["path"] == "/admin/registrations/7/approve"
    assert posted["body"] == {"note": "Hired for the B site"}, posted["body"]
    # The account leaves the queue by definition, so the id it is known by has to outlive the
    # row - this is the number the worker signs in with.
    assert approve["minted"] == "1042"
    assert approve["minted_id"] == "1042"
    assert approve["approved_sentence"] in approve["page_text"], (
        f"the receipt does not say the account was approved: {approve['page_text']!r}"
    )
    assert approve["warning"] is False, "the face reference is on file in this answer"
    assert "1042" in approve["toast"]
    # And the queue was re-read afterwards, so the decided row is not left on screen.
    assert len(approve["queue_reads"]) >= 2, approve["queue_reads"]


def test_the_receipt_hands_over_the_id_the_contact_and_what_to_do_with_them(results):
    """Three credentials open a first sign-in, and the applicant has none of them written down.

    The id is minted by the approval, the contact is whatever they typed on a public form, and
    the password is one they chose and then never used - so the administrator who made the
    account is the only person who can hand all three over at once, and this receipt is where
    they read them. The queue row that held the contact has left the queue by definition, so the
    approval's answer has to carry it back; that is the ``email``/``phone`` pair above.
    """
    approve = results["approve"]
    assert approve["signin"] == "1042", "the line is tied to the id it is about"
    text = html_module.unescape(approve["signin_text"])
    assert text == approve["signin_expected"], (
        f"the receipt is not the sentence the table defines: {text!r}"
    )
    assert "1042" in text
    assert "+965 555 0101" in text and "nadia@example.com" in text, (
        f"the contact is the value the sign-in route matches, so it has to be on the card: {text!r}"
    )
    assert "password" in text
    # ...and it is drawn from the decision the console kept, not from the row it no longer has.
    assert approve["last"]["phone"] == "+965 555 0101", approve["last"]
    assert approve["last"]["email"] == "nadia@example.com", approve["last"]


def test_an_application_with_no_contact_is_told_to_leave_that_box_empty(results):
    """Both contact columns are optional, and the sign-in route matches whatever is stored.

    So the value that opens *this* account is an empty box - the same reason the login form's own
    email-or-phone input is deliberately not ``required``. An instruction that named a contact
    the applicant never gave would send them looking for one that does not exist.
    """
    no_contact = results["no_contact"]
    text = html_module.unescape(no_contact["signin_text"])
    assert text == no_contact["signin_expected"], (
        f"the receipt is not the sentence the table defines: {text!r}"
    )
    assert "1042" in text
    assert "empty" in text.lower(), f"nothing says what to do with the box: {text!r}"
    assert text != no_contact["generic_sentence"], (
        "an application with no contact was given the sentence that names one"
    )


def test_an_account_whose_face_could_not_be_stored_is_reported_as_one(results):
    missing = results["no_template"]
    assert missing["minted"] == "1042", "the account exists, so its id is still the answer"
    assert missing["warning"] is True
    assert missing["sentence"] in missing["warning_text"], missing["warning_text"]
    assert "1042" in missing["toast"], (
        "the toast has to name the account too: the operator's next step is to enroll it"
    )


def test_refusing_needs_a_reason_and_then_destroys_the_photograph(results):
    refuse = results["refuse"]
    assert refuse["blocked"]["posted"] == 0, "a refusal with no reason was sent anyway"
    assert refuse["blocked"]["needs_note"] in refuse["blocked"]["toast"]
    assert len(refuse["posted"]) == 1, refuse["posted"]
    posted = refuse["posted"][0]
    assert posted["path"] == "/admin/registrations/8/reject"
    assert posted["body"] == {"note": "No formwork experience"}
    assert refuse["refused_card"] == "8", "the receipt names the application that was refused"


def test_a_photograph_the_server_could_not_destroy_is_not_reported_as_destroyed(results):
    kept = results["refuse_kept"]
    assert kept["kept_sentence"] in kept["notice"], kept["notice"]
    assert kept["destroy_sentence"] not in kept["notice"], (
        "a refusal claims a face is gone while it is still on disk for somebody to find"
    )
    assert kept["kept_sentence"] in kept["toast"]
    assert kept["refused_card"] == "8"


def test_a_decision_somebody_else_made_re_reads_the_queue(results):
    conflict = results["conflict"]
    assert conflict["reads_after"] > conflict["reads_before"], (
        "a 409 left a button on screen that can only answer 409 again"
    )
    assert "Another administrator reviewed this request first." in conflict["toast"]


def test_the_waiting_count_reaches_the_tab_badge(results):
    badge = results["badge"]
    painted = badge["painted"]
    # The badge's read asks for one row and still learns the whole count - which is the
    # reason it can be right while the page it fetched is one application long.
    assert any("limit=1" in path for path in badge["badge_read"]), badge["badge_read"]
    # The same constant the queue panel asks for. A badge read with the other spelling of
    # pending is a 400 in a ``catch`` that keeps the last count, so the numeral would simply
    # never appear on a console nobody had opened that tab on.
    assert all("status=pending_approval" in path for path in badge["badge_read"]), badge["badge_read"]
    assert painted["count"] == 2
    assert painted["text"] == "2"
    assert "2" in painted["label"]
    assert badge["after_zero"] is None, "an empty queue still wore a numeral"


def test_a_failed_read_is_stated_with_the_control_that_fixes_it(results):
    failure = results["failure"]
    assert failure["error"] is True, "a failed read left no visible error"
    assert "Database is locked." in failure["message"], (
        f"the server's own reason is the point of the error line: {failure['message']}"
    )
    assert failure["retry"] is True, "the error offers no way to try again"
    assert failure["cards"] == 0, "a failed read drew rows from nothing"


def test_an_empty_queue_says_what_empty_means(results):
    empty = results["empty"]
    assert empty["flag"] is True
    assert empty["cards"] == 0
    assert empty["text"].strip(), "the empty state says nothing at all"
    # A closed intake is still a queue with work in it some days, so it is a note on the
    # screen rather than a reason to draw nothing.
    assert empty["shows_closed"] is True
    assert empty["closed"] in empty["text"], empty["text"]


def test_every_sentence_the_screen_says_exists_in_all_four_languages(results):
    """A string that reaches three tables is a blank line for the reader of the fourth."""
    i18n = results["i18n"]
    assert i18n["langs"] == ["ar", "en", "hi", "ur"], i18n["langs"]
    assert i18n["missing"] == [], f"keys missing from a translation table: {i18n['missing']}"
    assert i18n["hint_missing"] == "", "the tab's own hint is missing from a table"
    # Named, not counted: the tab key is the one a reader sees on the rail, and the rest are
    # what the screen says when it has something to say.
    assert "registrations" in i18n["keys"]
    for key in (
        "registrationsApprove",
        "registrationsReject",
        "registrationsRejectNeedsNote",
        "registrationsApproved",
        "registrationsTemplateWarning",
        "registrationsPhotoGone",
        "registrationsLast",
    ):
        assert key in i18n["keys"], f"{key} is gone from the screen's vocabulary"
    assert i18n["label"] == "Registrations", "the rail shows a key instead of a label"


def test_the_status_the_console_asks_for_is_one_the_server_answers():
    """The queue read is the server's own status value, not the word this screen would choose.

    A frontend suite stubs the server, so a query string the console invented passes here and
    fails in the console - as a screenful of "status must be one of pending_approval, active,
    inactive or 'all'." for the administrator who opened the tab. The backend is the only
    authority on that vocabulary, so it is read here rather than assumed: the values the
    console puts in a ``status=`` parameter are checked against the constants the endpoint
    validates against.
    """
    console = (FRONTEND / "admin_modules.js").read_text(encoding="utf-8")
    server = (BACKEND / "registrations.py").read_text(encoding="utf-8")
    asked = set(re.findall(r"/admin/registrations\?status=([a-z_]+)", console))
    assert asked, "the console does not name a status, so the read depends on the server's default"
    known = set(re.findall(r'^STATUS_[A-Z0-9_]+ = "([a-z_]+)"', server, re.MULTILINE))
    assert known, "the server's status vocabulary moved and this check found nothing to hold to"
    for value in sorted(asked):
        assert value in known, (
            f"the console asks for status={value}, which is not one of the endpoint's own "
            f"values ({', '.join(sorted(known))}): the queue screen would render "
            "'status must be one of ...' instead of the queue"
        )


def test_the_intake_switch_is_drawn_with_its_state_and_one_action(results):
    """The one control on this screen, and the reason it is a control rather than a label.

    The public form is a URL the company prints once, so opening and closing it is an operator's
    decision on the day. What the card has to carry is both the *state* - the reader has to be
    able to tell open from closed without pressing anything - and *why*, because a link nobody
    has ever opened and a link somebody closed this morning need different things said about
    them.
    """
    intake = results["intake"]
    assert intake["open_state"] == "open", "the card is not drawn in the server's own state"
    assert intake["open_card"] == "open"
    assert intake["open_action"] == "close", (
        "an open link offers to close, and the attribute names the direction rather than the "
        "current position - a handler that read a position would send the opposite of the tap"
    )
    assert intake["open_sentence"] in intake["open_text"], (
        f"a state with no reason beside it is one an operator cannot act on: {intake['open_text']!r}"
    )


def test_closing_the_link_posts_the_switch_and_draws_what_came_back(results):
    """The answer is the state, and the screen is redrawn from it rather than from the request.

    The switch is written for a position and read back for what it now is, so a console that
    repeated what it asked for would report a form the server has not agreed to - and the two
    only have to disagree once (another administrator moving the same switch, a request the
    server refuses) for that to be a screen handing out a link that refuses everybody.
    """
    intake = results["intake"]
    assert intake["posts"], "the control posted nothing at all"
    posted = intake["posts"][0]
    assert posted["path"] == "/admin/registrations/intake", posted["path"]
    assert posted["body"] == {"open": False}, posted["body"]
    assert intake["closed_state"] == "closed_by_console", (
        "the link shut but the screen still says otherwise"
    )
    assert intake["closed_action"] == "open", "a closed link offers no way back"
    assert intake["closed_sentence"] in intake["closed_text"], intake["closed_text"]


def test_a_deployment_that_ships_closed_is_a_link_the_console_can_open(results):
    """The lever is drawn in the state where it is the *only* thing that can open the link.

    A deployment that has never run walk-up registration ships with the link shut, so this is
    the state the first applicant's page is refusing from - the page whose own sentence tells
    them to ask their site administrator to open it. A card that named the deployment instead of
    offering the lever is what made that sentence impossible to act on.
    """
    shipped = results["intake_default_off"]
    assert shipped["state"] == "closed_by_default"
    assert shipped["lever"] == "open", (
        "the one state where the console is the only way in drew no lever"
    )
    assert shipped["sentence"] in shipped["text"], shipped["text"]

    # The tap is a write to the same route as any other, and the card follows the answer.
    assert shipped["posts"], "the lever posted nothing at all"
    assert shipped["posts"][0]["body"] == {"open": True}, shipped["posts"][0]
    assert shipped["opened_state"] == "open", "the form opened and the card still says closed"
    assert shipped["opened_action"] == "close", "the card did not turn round with the state"


def test_a_switch_that_could_not_be_read_leaves_no_control_behind(results):
    """A state that could not be read is not a state to draw a lever in.

    The queue's own closed note is drawn from the *queue* read, so the screen still answers
    whether the link is accepting - there is simply no switch on it, which is the honest thing to
    show when the switch's position is unknown.
    """
    unreadable = results["intake_unreadable"]
    assert unreadable["card"] is False, "a card was drawn for a state the server never sent"
    assert unreadable["lever"] is False
    assert unreadable["queue_drawn"] == 2, "the queue itself was lost with the switch read"
