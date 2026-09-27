"""The console's registrations queue, exercised for real.

WHY THIS EXISTS
---------------
``test_walk_up_registration`` covers the intake end to end on the server: the switch, the
validation order, the pending row, the queue cap, the photograph that is destroyed rather
than kept, and the approval that is the only thing which creates an account. What it cannot
see is the reviewer: whether anybody can *reach* the queue, read a face before deciding,
hire somebody, and be told the number they were hired under. That is this file.

Five properties, and each one is a decision rather than a rendering detail:

1. **the tab is a console screen, for the console's audience.** It is offered to an
   administrator (the route behind it is ``admin_only``), it sits with the other people
   screens, and it draws a glyph of its own.
2. **the queue is the server's, oldest first.** The read asks for ``PENDING_REVIEW`` and renders
   the rows in the order they arrived, because the applicant at the front is the one being
   phoned about.
3. **the photograph is fetched, never embedded.** An ``<img src>`` cannot carry a token, so
   a URL that worked in a ``src`` would be a URL that worked for anybody - and forty
   applications in one response would be forty faces. One request, this session's
   credential, on the reviewer's own tap.
4. **the id an approval minted survives the row that produced it.** An approved application
   leaves the queue by definition, so the number the new worker signs in with is kept on
   the screen (and in the answer the server sent) rather than in a toast that scrolls away.
   A refusal needs a reason here, and it destroys the photograph.
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
        id: 7, status: 'PENDING_REVIEW', full_name: 'Nadia Saleh', phone: '+965 555 0101',
        email: 'nadia@example.com', requested_role: 'worker',
        work_details: 'Formwork, six years on tower sites.', assigned_id: null,
        submitted_ip: '10.0.0.9', consent_version: '1', created_at: '2026-09-25 06:40:00',
        reviewed_by: null, reviewed_at: null, decision_note: null
    },
    {
        id: 8, status: 'PENDING_REVIEW', full_name: '<img src=x onerror=alert(1)>Omar',
        phone: '', email: 'omar@example.com', requested_role: 'moallem',
        work_details: '', assigned_id: null, submitted_ip: '10.0.0.10',
        consent_version: '1', created_at: '2026-09-26 05:10:00',
        reviewed_by: null, reviewed_at: null, decision_note: null
    }
];

//: The id the fake approval mints. Deliberately not one of the fixture's accounts: this is
//: a number that only exists in the answer, which is what makes it worth asserting.
const MINTED = '1042';

let queueFails = null;          // a status to answer the queue read with, or null
let photoMissing = false;       // answer the photograph with 404
let approveWritesTemplate = true;
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
                    status: 'success', request_id: requestId, worker_id: MINTED,
                    name: 'Nadia Saleh', role: 'worker', template_written: approveWritesTemplate,
                    message: 'Account ' + MINTED + ' created for Nadia Saleh.'
                }
            };
        }
        return {
            status: 200,
            body: {
                status: 'success', request_id: requestId, photo_destroyed: rejectDestroysPhoto,
                message: 'The request was refused and the photograph has been destroyed.'
            }
        };
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
        toast: toasts(env).join(' | '),
        queue_reads: reads.slice()
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
    assert "status=PENDING_REVIEW" in panel[0], (
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
    # The row is gone from the queue by definition, so the number it was hired under has to
    # outlive it - this is the id the worker signs in with.
    assert approve["minted"] == "1042"
    assert approve["minted_id"] == "1042"
    assert "Account 1042 was created for Nadia Saleh." in html_module.unescape(approve["receipt"])
    assert approve["warning"] is False, "the face reference was written in this answer"
    assert "1042" in approve["toast"]
    # And the queue was re-read afterwards, so the decided row is not left on screen.
    assert len(approve["queue_reads"]) >= 2, approve["queue_reads"]


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
    assert all("status=PENDING_REVIEW" in path for path in badge["badge_read"]), badge["badge_read"]
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
    fails in the console - as a screenful of "status must be one of PENDING_REVIEW, APPROVED,
    REJECTED or 'all'." for the administrator who opened the tab. The backend is the only
    authority on that vocabulary, so it is read here rather than assumed: the values the
    console puts in a ``status=`` parameter are checked against the constants the endpoint
    validates against.
    """
    console = (FRONTEND / "admin_modules.js").read_text(encoding="utf-8")
    server = (BACKEND / "registrations.py").read_text(encoding="utf-8")
    asked = set(re.findall(r"/admin/registrations\?status=([A-Z_]+)", console))
    assert asked, "the console does not name a status, so the read depends on the server's default"
    known = set(re.findall(r'^STATUS_[A-Z_]+ = "([A-Z_]+)"', server, re.MULTILINE))
    assert known, "the server's status vocabulary moved and this check found nothing to hold to"
    for value in sorted(asked):
        assert value in known, (
            f"the console asks for status={value}, which is not one of the endpoint's own "
            f"values ({', '.join(sorted(known))}): the queue screen would render "
            "'status must be one of ...' instead of the queue"
        )
