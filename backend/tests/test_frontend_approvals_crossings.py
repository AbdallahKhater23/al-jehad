"""The Approvals tab: the shift that has *not ended* yet, and the two answers to it.

WHY THIS EXISTS
---------------
The crossing was moved out of the Alerts tab and into the Approvals queue, because a shift past
the overtime line while it is still running is a question rather than a notice: reading it
answers nothing, and the answer changes what the rest of the shift is paid for. The backend half
of that lives in ``test_overtime_crossing_queue.py`` - two endpoints, one append-only decision.
This file is the half that only a browser can fail:

1. opening the Approvals tab reads **both** endpoints, and the live crossings are drawn *above*
   the review queue (they are the items with a worker still on site);
2. each card carries the two numbers a decision rests on - what the shift has been paid so far
   and what is being held past the line - plus the site, the clock-in and the reason the shift
   is still open at all (``close_defers``: nothing else will end it, and an open shift refuses
   the worker's next clock-in);
3. **Authorise** posts the ceiling and the note to *that worker's* accept endpoint, and a blank
   ceiling sends no ``authorised_hours`` at all, so the server's default (the hours worked so
   far) is the only default in the system;
4. **Decline** requires a reason before anything is sent - it is what the record keeps to answer
   "why was my overtime refused" - and posts to the decline endpoint, not the accept one;
5. a ceiling that is not a number is refused with a toast rather than a round trip;
6. an answered crossing shows **who answered it and with what**, and offers no second form: the
   first answer is the one kept, so the card must not look like it is still asking;
7. a crossings read that fails leaves the review queue on screen - two questions, two reads, and
   one must not take the other down;
8. the tab's own wording is translated in all four languages.

Node is optional; without it these skip rather than fail.
"""

from __future__ import annotations

import pytest

import frontend_vm

pytestmark = pytest.mark.skipif(frontend_vm.NODE is None, reason="node is not installed")

HARNESS = r"""
// --- the server's answers, faked ----------------------------------------

// Three crossings: one nobody has answered (Ana, still on site, past the line); one answered by
// the head administrator with a ceiling that still covers the shift - the case that must stop
// asking; and one whose ceiling the shift has since worked past, which is a question again.
// ``needs_answer`` is the server's own rule (``overtime.authorisation_covers``) arriving on the
// wire, because the client must not re-derive it from the figures.
const CROSSINGS = [
    {
        worker_id: '1', worker_name: 'Ana Ruiz', role: 'worker',
        site_name: 'Downtown Tower A', clock_in_time: '2026-09-22 07:00:00',
        crossed_at: '2026-09-22 15:36:00', waiting_seconds: 1800,
        elapsed_hours: 9.5, paid_hours: 9.0, break_hours: 0.5,
        threshold_hours: 8.1, regular_hours: 8.0, overtime_hours: 1.0,
        basis: 'paid', close_defers: true, decision: null,
        needs_answer: true, unauthorised_hours: null
    },
    {
        worker_id: '600', worker_name: 'Bilal Khan', role: 'moallem',
        site_name: 'Riverside Depot', clock_in_time: '2026-09-22 06:30:00',
        crossed_at: '2026-09-22 15:06:00', waiting_seconds: 3600,
        elapsed_hours: 9.0, paid_hours: 8.5, break_hours: 0.5,
        threshold_hours: 8.1, regular_hours: 8.0, overtime_hours: 0.5,
        basis: 'paid', close_defers: true,
        decision: {
            id: 3, decision: 'authorised', authorised_hours: 9.5,
            recorded_hours_at_decision: 8.5, decided_by: '5000',
            decided_by_name: 'Head Admin',
            decided_at: '2026-09-22 15:30:00', note: 'the pour ran long'
        },
        needs_answer: false, unauthorised_hours: null
    },
    {
        worker_id: '601', worker_name: 'Rania Haddad', role: 'worker',
        site_name: 'Harbour Gate', clock_in_time: '2026-09-22 05:00:00',
        crossed_at: '2026-09-22 13:36:00', waiting_seconds: 7200,
        elapsed_hours: 13.0, paid_hours: 12.5, break_hours: 0.5,
        threshold_hours: 8.1, regular_hours: 8.0, overtime_hours: 4.5,
        basis: 'paid', close_defers: false,
        decision: {
            id: 7, decision: 'authorised', authorised_hours: 10.75,
            recorded_hours_at_decision: 10.5, decided_by: '1000',
            decided_by_name: 'Head Admin',
            decided_at: '2026-09-22 15:00:00', note: 'the pour ran long'
        },
        needs_answer: true, unauthorised_hours: 1.75
    }
];

// One review row, so the tab has both halves to draw.
const REVIEWS = [{
    id: 42, worker_id: '1', name: 'Ana Ruiz', role: 'worker',
    site_name: 'Downtown Tower A', action: 'Clock Out', timestamp: '2026-09-22 16:00:00',
    hours: 9.0, score: 0.31, status: 'Pending Review', status_code: 'pending_review',
    flag_reason: 'past the paid day', overtime_hours: 1.0, source: 'online',
    liveness_class: null, liveness_score: null, punch_frame: null, frame_url: null,
    match_verdict: 'match', awaiting_approval: true
}];

let crossings = CROSSINGS.slice();
let crossingsFail = null;        // {status, detail} for the next crossings read
let decisionFailure = null;      // {status, detail} for the next answer
const decisions = [];
const reviewReads = [];

function urlOf(init) { return init && init.url ? init.url : ''; }

function responders(url, init) {
    const answer = /\/admin\/overtime\/crossings\/([^/]+)\/(accept|decline)$/.exec(String(url));
    if (answer) {
        decisions.push({
            worker_id: answer[1],
            answer: answer[2],
            body: JSON.parse((init && init.body) || '{}')
        });
        if (decisionFailure) {
            const failure = decisionFailure;
            decisionFailure = null;
            return { status: failure.status, body: { detail: failure.detail } };
        }
        return {
            status: 200,
            body: {
                worker_id: answer[1], decision: answer[2] === 'accept' ? 'authorised' : 'declined',
                authorised_hours: 9.5, recorded_hours_at_decision: 9.0,
                decided_by: '1000', decided_at: '2026-09-22 16:05:00',
                note: null, already_decided: false
            }
        };
    }
    if (/\/admin\/overtime\/crossings$/.test(String(url))) {
        if (crossingsFail) {
            const failure = crossingsFail;
            crossingsFail = null;
            return { status: failure.status, body: { detail: failure.detail } };
        }
        return { status: 200, body: crossings.slice() };
    }
    if (/\/admin\/pending_reviews$/.test(String(url))) {
        reviewReads.push(String(url));
        return { status: 200, body: REVIEWS.slice() };
    }
    return { status: 200, body: {} };
}

// --- reading the rendered page -----------------------------------------

function textOf(markup) {
    return markup.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();
}

//: One decision's facts, as ``[label, value]`` pairs.
//
// Matched as the *pair* of spans the facts grid emits: the stats grid above it uses the same
// label class, and reading those as evidence is how a card about a ceiling comes to look as
// though it was decided at 9.00 h.
function factsIn(markup) {
    const pattern = /<span class="ops-stat-label">([^<]*)<\/span>\s*<span class="ui-fact-value">([^<]*)<\/span>/g;
    const facts = [];
    let match;
    while ((match = pattern.exec(markup)) !== null) facts.push([match[1], match[2]]);
    return facts;
}

function cardsOf(markup) {
    const cards = markup.match(/<article class="ui-card[^"]*"[\s\S]*?<\/article>/g) || [];
    return cards.map((card) => ({
        id: (/data-crossing="([^"]*)"/.exec(card) || [])[1],
        review_id: (/data-review="([^"]*)"/.exec(card) || [])[1],
        has_evidence: /data-crossing-evidence="/.test(card),
        facts: factsIn(card),
        has_accept: /data-crossing-accept="/.test(card),
        has_decline: /data-crossing-decline="/.test(card),
        has_ceiling: /data-crossing-hours="/.test(card),
        has_note: /data-crossing-note="/.test(card),
        close_defers: /data-crossing-holds-open="/.test(card),
        outgrown: /data-crossing-outgrown="/.test(card),
        outgrown_text: (/data-crossing-outgrown="[^"]*">([\s\S]*?)<\/p>/.exec(card) || [])[1],
        answer: (/data-crossing-answer="[^"]*">([\s\S]*?)<\/p>/.exec(card) || [])[1],
        answer_note: (/data-crossing-answer-note="[^"]*">([\s\S]*?)<\/p>/.exec(card) || [])[1],
        text: textOf(card)
    }));
}

function crossingsOf(markup) {
    return cardsOf(markup).filter((card) => card.id !== undefined);
}

function toasts(env) {
    return env.evaluate(
        "(document.getElementById('toastRoot').__children || []).map((el) => el.textContent)"
    );
}

function render(env) {
    return env.evaluate("document.getElementById('adminContent').innerHTML");
}

function consoleEnv() {
    crossings = CROSSINGS.slice();
    crossingsFail = null;
    decisionFailure = null;
    decisions.length = 0;
    reviewReads.length = 0;
    const env = boot();
    env.setResponder(responders);
    env.evaluate("State.saveUser(" + JSON.stringify({
        id: '1000', name: 'Head Admin', role: 'head_admin', token: 'tok-admin'
    }) + ")");
    return env;
}

const results = {};

// 1. the tab reads both queues and draws the live crossings, with the queue beneath them
{
    const env = consoleEnv();
    results.tabs = env.evaluate("ADMIN_TABS.map((tab) => [tab.id, tab.key, tab.group, tab.icon])");
    await env.evaluate("UI.renderAdminTab('Approvals')");
    const markup = render(env);
    const cards = crossingsOf(markup);
    results.page = {
        flag: markup.indexOf('data-approvals="true"') >= 0,
        title_shown: markup.indexOf('data-crossings-title="true"') >= 0,
        hint_shown: textOf(markup).indexOf('past the overtime line right now') >= 0,
        cards: cards,
        crossing_before_queue:
            markup.indexOf('data-crossing="1"') >= 0 &&
            markup.indexOf('data-crossing="1"') < markup.indexOf('data-review="42"'),
        crossings_read: env.requests.filter((r) => /\/admin\/overtime\/crossings$/.test(r.url)).length,
        reviews_read: reviewReads.length,
        unanswered_text: textOf((/<article class="ui-card[^"]*" data-crossing="1"[\s\S]*?<\/article>/.exec(markup) || [])[0] || ''),
        answered_text: textOf((/<article class="ui-card[^"]*" data-crossing="600"[\s\S]*?<\/article>/.exec(markup) || [])[0] || '')
    };
}

// 2. authorising: the ceiling and the note go to that worker's accept endpoint
{
    const env = consoleEnv();
    await env.evaluate("UI.renderAdminTab('Approvals')");
    env.evaluate("document.getElementById('crossingHours-1').value = '10.5'");
    env.evaluate("document.getElementById('crossingNote-1').value = 'the pour ran long'");
    await env.evaluate("UI_MODULES.handleCrossing('1', true)");
    results.authorise = {
        calls: decisions.slice(),
        toast: toasts(env).slice(-1)[0],
        repainted: env.requests.filter((r) => /\/admin\/overtime\/crossings$/.test(r.url)).length > 1
    };
}

// 3. a blank ceiling sends no number at all: the server's default is the only default
{
    const env = consoleEnv();
    await env.evaluate("UI.renderAdminTab('Approvals')");
    env.evaluate("document.getElementById('crossingNote-1').value = 'as agreed'");
    await env.evaluate("UI_MODULES.handleCrossing('1', true)");
    results.blank_ceiling = { calls: decisions.slice() };
}

// 4. declining needs a reason, and goes to the decline endpoint
{
    const env = consoleEnv();
    await env.evaluate("UI.renderAdminTab('Approvals')");
    const before = decisions.length;
    await env.evaluate("UI_MODULES.handleCrossing('1', false)");
    const withoutReason = decisions.length - before;
    env.evaluate("document.getElementById('crossingNote-1').value = 'no overtime was agreed'");
    await env.evaluate("UI_MODULES.handleCrossing('1', false)");
    results.decline = {
        posted_without_reason: withoutReason,
        calls: decisions.slice(),
        toast: toasts(env).slice(-1)[0]
    };
}

// 5. a ceiling that is not a number never leaves the browser
{
    const env = consoleEnv();
    await env.evaluate("UI.renderAdminTab('Approvals')");
    const before = decisions.length;
    env.evaluate("document.getElementById('crossingHours-1').value = 'abc'");
    env.evaluate("document.getElementById('crossingNote-1').value = 'typo'");
    await env.evaluate("UI_MODULES.handleCrossing('1', true)");
    env.evaluate("document.getElementById('crossingHours-1').value = '-3'");
    await env.evaluate("UI_MODULES.handleCrossing('1', true)");
    results.bad_ceiling = {
        posted: decisions.length - before,
        toast: toasts(env).slice(-1)[0]
    };
}

// 6. the delegated binding: a tap reaches the right worker and the right answer
//
// A spy container, built *inside* the VM (a closure cannot cross ``evaluate``): it records the
// selectors the tab binds and hands back buttons whose listeners are captured here, so the
// listener itself - not the stub DOM's ``click()``, which dispatches nothing - is what fires.
{
    const env = consoleEnv();
    env.evaluate(`
        __selectors = [];
        __bound = [];
        __spy = {
            innerHTML: '',
            querySelectorAll: (selector) => {
                __selectors.push(selector);
                const make = (id, attribute) => ({
                    getAttribute: (name) => (name === attribute ? String(id) : null),
                    addEventListener: (type, handler) => {
                        if (type === 'click') __bound.push({ id: id, attribute: attribute, handler: handler });
                    }
                });
                if (selector === '[data-crossing-accept]') return [make('1', 'data-crossing-accept')];
                if (selector === '[data-crossing-decline]') return [make('600', 'data-crossing-decline')];
                return [];
            }
        };
    `);
    await env.evaluate("UI_MODULES.renderApprovals(__spy)");
    env.evaluate("document.getElementById('crossingNote-1').value = 'bound, not inline'");
    // The listener the browser would fire for a tap on worker 1's Authorise button.
    await env.evaluate("__bound[0].handler()");
    results.binding = {
        selectors: env.evaluate("__selectors.slice()"),
        bound: env.evaluate("__bound.map((entry) => entry.attribute + ':' + entry.id)"),
        calls: decisions.slice()
    };
}

// 6b. nothing the crossings added carries an inline handler
//
// Measured on the *crossings section* only. The review cards and the empty state already carry
// inline handlers from before this section existed, and ``test_frontend_xss`` pins that count
// across the codebase; the rule this section has to keep is that it adds none of its own.
{
    const env = consoleEnv();
    await env.evaluate("UI.renderAdminTab('Approvals')");
    const markup = render(env);
    const section = (markup.match(
        /<p class="ui-section-note" data-crossings-title="true">[\s\S]*?(?=<article class="ui-card[^"]*" data-review=)/
    ) || [])[0] || '';
    results.inline = {
        occurrences: (section.match(/onclick=/g) || []).length,
        captured_chars: section.length,
        answers_present: /data-crossing-accept="/.test(section) && /data-crossing-decline="/.test(section)
    };
}

// 7. an answered crossing offers no second form, and shows who decided
{
    const env = consoleEnv();
    await env.evaluate("UI.renderAdminTab('Approvals')");
    const cards = crossingsOf(render(env));
    const answered = cards.filter((card) => card.id === '600')[0];
    const unanswered = cards.filter((card) => card.id === '1')[0];
    results.answered = {
        answer: answered.answer,
        answer_note: answered.answer_note,
        has_ceiling: answered.has_ceiling,
        has_accept: answered.has_accept,
        has_decline: answered.has_decline,
        unanswered_has_ceiling: unanswered.has_ceiling,
        unanswered_has_decline: unanswered.has_decline
    };
    // The fallback, drawn straight from the card: an administrator whose account has gone leaves
    // a decision the record still holds by id, and the card shows the id rather than a blank.
    results.orphan = env.evaluate(
        'UI_MODULES.crossingCardHtml(' +
        JSON.stringify({
            worker_id: '9', worker_name: 'Orphan Case', paid_hours: 8.5, overtime_hours: 0.5,
            clock_in_time: '2026-09-22 06:00:00', site_name: 'Riverside Depot',
            decision: {
                id: 4, decision: 'declined', authorised_hours: 8.0,
                recorded_hours_at_decision: 8.5, decided_by: '404040404', decided_at: null,
                note: null
            }
        }) + ')'
    );
}

// 7b. a ceiling the shift has outgrown is a question again, with the figure being asked about
//
// The server says so (``needs_answer``), and the card has to hold three things at once: the answer
// that was given (an outgrown ceiling is not a forgotten one), how much nobody has authorised, and
// the form to cover it - otherwise the second question has no way to be answered from the console.
{
    const env = consoleEnv();
    await env.evaluate("UI.renderAdminTab('Approvals')");
    const cards = crossingsOf(render(env));
    const outgrown = cards.filter((card) => card.id === '601')[0];
    const covered = cards.filter((card) => card.id === '600')[0];
    // The same figures as the outgrown crossing above, with the server's verdict left off: a card
    // that worked the rule out for itself would draw the form here, and that is the second rule
    // this test exists to forbid.
    const noField = env.evaluate(
        'UI_MODULES.crossingCardHtml(' +
        JSON.stringify({
            worker_id: '9', worker_name: 'Orphan Case', paid_hours: 8.5, overtime_hours: 0.5,
            clock_in_time: '2026-09-22 06:00:00', site_name: 'Riverside Depot',
            decision: {
                id: 4, decision: 'authorised', authorised_hours: 8.0,
                recorded_hours_at_decision: 8.0, decided_by: '1000', decided_at: null,
                note: null
            }
        }) + ')'
    );
    results.outgrown = {
        outgrown_answer: outgrown.answer,
        outgrown_note: outgrown.outgrown_text,
        outgrown_has_ceiling: outgrown.has_ceiling,
        outgrown_has_accept: outgrown.has_accept,
        outgrown_has_decline: outgrown.has_decline,
        covered_has_ceiling: covered.has_ceiling,
        covered_has_accept: covered.has_accept,
        covered_outgrown: covered.outgrown,
        unreported_has_ceiling: /data-crossing-hours="/.test(noField),
        unreported_outgrown: /data-crossing-outgrown="/.test(noField),
        unreported_answer: /data-crossing-answer="/.test(noField)
    };

    // A refusal, drawn straight from the card: the same evidence block, and deliberately no
    // ceiling fact - it authorised nothing, and its stored ceiling is the paid day it kept.
    const declined = env.evaluate(
        'UI_MODULES.crossingCardHtml(' +
        JSON.stringify({
            worker_id: '9', worker_name: 'Refused Case', paid_hours: 9.2, overtime_hours: 1.2,
            clock_in_time: '2026-09-22 05:00:00', site_name: 'Harbour Gate',
            decision: {
                id: 8, decision: 'declined', authorised_hours: 8.0,
                recorded_hours_at_decision: 9.2, decided_by: '1000',
                decided_by_name: 'Head Admin', decided_at: '2026-09-22 15:00:00',
                note: 'no overtime was agreed'
            },
            needs_answer: false
        }) + ')'
    );
    const page = crossingsOf(render(env));
    results.decision_evidence = {
        labels: {
            ceiling: env.evaluate("I18n.__('crossingsDecisionCeiling')"),
            recorded: env.evaluate("I18n.__('crossingsDecisionRecorded')"),
            by: env.evaluate("I18n.__('crossingsDecisionBy')")
        },
        covered: page.filter((card) => card.id === '600')[0].facts,
        outgrown: page.filter((card) => card.id === '601')[0].facts,
        unanswered_has_evidence: page.filter((card) => card.id === '1')[0].has_evidence,
        unanswered: page.filter((card) => card.id === '1')[0].facts,
        declined: factsIn(declined)
    };
}

// 8. a crossings read that fails leaves the review queue on screen
{
    const env = consoleEnv();
    crossingsFail = { status: 500, detail: 'the queue could not be read' };
    await env.evaluate("UI.renderAdminTab('Approvals')");
    const markup = render(env);
    results.degraded = {
        queue_still_drawn: markup.indexOf('data-review="42"') >= 0,
        crossings_section_absent: markup.indexOf('data-crossings-title="true"') < 0
    };
}

// 9. an empty crossings list draws no section at all - and keeps the queue
{
    const env = consoleEnv();
    crossings = [];
    await env.evaluate("UI.renderAdminTab('Approvals')");
    const markup = render(env);
    results.empty = {
        section_absent: markup.indexOf('data-crossings-title="true"') < 0,
        queue_still_drawn: markup.indexOf('data-review="42"') >= 0
    };
}

// 10. the tab's own wording, in each language
{
    const env = consoleEnv();
    const label = (lang) => {
        env.evaluate("I18n.setLang('" + lang + "')");
        return env.evaluate(
            "[I18n.__('crossingsTitle'), I18n.__('crossingsAccept'), I18n.__('crossingsDecline')," +
            " I18n.__('crossingsAuthorised'), I18n.__('crossingsDeclinedBy')," +
            " I18n.__('crossingsPastCeiling'), I18n.__('crossingsDecisionCeiling')," +
            " I18n.__('crossingsDecisionRecorded'), I18n.__('crossingsDecisionBy')]"
        );
    };
    results.wording = { en: label('en'), ar: label('ar'), hi: label('hi'), ur: label('ur') };
    env.evaluate("I18n.setLang('en')");
}
"""


@pytest.fixture(scope="module")
def results() -> dict:
    return frontend_vm.run(HARNESS)


def test_the_approvals_tab_reads_both_queues_and_draws_the_live_crossings(results):
    tabs = {tab[0]: tab for tab in results["tabs"]}
    assert "Approvals" in tabs, [tab[0] for tab in results["tabs"]]

    page = results["page"]
    assert page["flag"] is True
    assert page["crossings_read"] >= 1, "opening the Approvals tab does not read the live crossings"
    assert page["reviews_read"] >= 1, "the review queue is no longer read by the tab that owns it"
    assert page["title_shown"] and page["hint_shown"], (
        "the section does not say what these items are or what authorising one means"
    )
    assert page["crossing_before_queue"], (
        "a worker who is still on site is below the shifts that have already ended"
    )

    ids = [card["id"] for card in page["cards"]]
    assert ids == ["1", "600", "601"], ids
    unanswered = [card for card in page["cards"] if card["id"] == "1"][0]
    assert unanswered["has_ceiling"] and unanswered["has_note"], (
        "there is no way to state a ceiling or a reason on an unanswered crossing"
    )
    assert unanswered["has_accept"] and unanswered["has_decline"], (
        "an unanswered crossing offers neither answer"
    )
    assert "Ana Ruiz" in page["unanswered_text"], page["unanswered_text"]
    assert "Downtown Tower A" in page["unanswered_text"], page["unanswered_text"]
    # The two numbers the decision rests on: what has been paid, and what is being held.
    assert "9.00" in page["unanswered_text"], page["unanswered_text"]
    assert "1.00" in page["unanswered_text"], page["unanswered_text"]
    assert unanswered["close_defers"], (
        "the card does not say that nothing else will end this shift - the operator is left to "
        "discover that the worker cannot clock in again"
    )


def test_an_answered_crossing_shows_the_answer_and_no_second_form(results):
    answered = results["answered"]
    assert answered["answer"], "the card does not say what was decided"
    assert "9.50" in answered["answer"], answered["answer"]
    # Who answered it, by name: the server resolves the id it stores, because an operator reading
    # a queue should not be sent to a database to find out who is on the line.
    assert "Head Admin" in answered["answer"], answered["answer"]
    assert "5000" not in answered["answer"], (
        "the card shows the raw user id instead of the name resolved for it"
    )
    assert answered["answer_note"] and "the pour ran long" in answered["answer_note"], (
        "the reason the decision was made is not on the card, so the record has to be queried "
        "to find out why somebody's overtime was authorised"
    )
    # ... and the fallback: a decision whose account is gone is shown by id, not dropped.
    assert "404040404" in results["orphan"], results["orphan"]
    assert not answered["has_accept"] and not answered["has_decline"], (
        "an answered crossing still offers the answers, so the first decision looks unsettled"
    )
    assert not answered["has_ceiling"], "an answered crossing still asks for a ceiling"
    assert answered["unanswered_has_ceiling"] and answered["unanswered_has_decline"], (
        "the unanswered crossing lost its controls, so nothing on the page can be answered"
    )


def test_a_ceiling_the_shift_outgrew_asks_again_with_the_figure(results):
    """The second question the server is waiting on has to be answerable from the console.

    A ceiling authorises the hours it named and no more, so a shift that keeps working past it is
    asking again - and the card carries both halves at once: the answer that was given, the hours
    beyond it that nobody has authorised, and the form to cover them.
    """
    outgrown = results["outgrown"]
    assert outgrown["outgrown_answer"], "the card lost the answer it is extending"
    assert "10.75" in outgrown["outgrown_answer"], outgrown["outgrown_answer"]
    assert outgrown["outgrown_note"] and "1.75" in outgrown["outgrown_note"], (
        "the card does not say how much is unauthorised, so the operator is asked to name a "
        f"ceiling with no idea what it has to cover: {outgrown}"
    )
    assert outgrown["outgrown_has_ceiling"], (
        "the hours past the ceiling are a question with no way to answer it from the console"
    )
    assert outgrown["outgrown_has_accept"] and outgrown["outgrown_has_decline"], outgrown

    # ... and the answer that still covers its shift stays settled. Both are "answered" crossings;
    # drawing the form for both is how a decision that is still good looks like an open question.
    assert not outgrown["covered_has_ceiling"] and not outgrown["covered_has_accept"], outgrown
    assert not outgrown["covered_outgrown"], (
        "the covered crossing says hours are unauthorised when they are not"
    )


def test_the_card_leaves_the_covering_rule_to_the_server(results):
    """No second rule in the browser: the server's field decides, not a figure comparison.

    An operator and a POST have to agree about whether a crossing is still answered, and the rule
    that decides it - a ceiling covers the shift up to the amount somebody named, with the
    tolerance ``overtime.COVER_TOLERANCE_HOURS`` - belongs to ``overtime.authorisation_covers``.
    A card that compared ``paid_hours`` against ``authorised_hours`` itself would be that rule
    stated twice, and the two would part company the first time either moved. The item below is
    exactly that shape: a decision the shift has worked past, and no verdict from the server on
    whether it still covers. It stays settled.
    """
    outgrown = results["outgrown"]
    assert outgrown["unreported_answer"], outgrown
    assert not outgrown["unreported_has_ceiling"], (
        "the card re-derived the rule: a decision with no server verdict was treated as outgrown "
        "because the paid figure is past the ceiling"
    )
    assert not outgrown["unreported_outgrown"], outgrown


def test_an_answer_is_shown_with_the_evidence_it_was_given_on(results):
    """The ceiling, the hours worked when it was given, and who gave it - not the verdict alone.

    The same standard the review half of this tab is held to. "Authorised up to 12 h" cannot tell
    a generous ceiling given at 8.5 h from a rubber-stamp given at 11.9 h, and that difference is
    what the next reader needs from the card rather than from a query: an operator about to
    extend it, the worker asking why their week moved, an auditor months later.
    """
    evidence = results["decision_evidence"]
    labels = evidence["labels"]
    assert all(labels.values()), labels
    assert evidence["covered"] == [
        [labels["ceiling"], "9.50"],
        [labels["recorded"], "8.50"],
        [labels["by"], "Head Admin"],
    ], evidence["covered"]
    # The outgrown ceiling carries the facts of *its* decision, not the one it replaced: 10.75
    # authorised when 10.50 had been worked. An extension whose evidence was the first decision's
    # would describe a judgement nobody made.
    assert evidence["outgrown"] == [
        [labels["ceiling"], "10.75"],
        [labels["recorded"], "10.50"],
        [labels["by"], "Head Admin"],
    ], evidence["outgrown"]
    # A crossing nobody has answered has no decision, so there is no evidence block to draw - and
    # the stats grid above it must not be mistaken for one.
    assert evidence["unanswered_has_evidence"] is False, evidence
    assert evidence["unanswered"] == [], evidence["unanswered"]
    # A refusal's evidence is the same block without a ceiling: it authorised nobody, and the paid
    # day it kept is not an "hours authorised" figure. What it has is the hours worked when it was
    # made, which is the whole of its evidence.
    assert evidence["declined"] == [
        [labels["recorded"], "9.20"],
        [labels["by"], "Head Admin"],
    ], evidence["declined"]


def test_authorising_posts_the_ceiling_and_the_reason_to_that_workers_endpoint(results):
    calls = results["authorise"]["calls"]
    assert len(calls) == 1, calls
    assert calls[0]["worker_id"] == "1", calls[0]
    assert calls[0]["answer"] == "accept", calls[0]
    assert calls[0]["body"]["authorised_hours"] == 10.5, calls[0]
    assert calls[0]["body"]["note"] == "the pour ran long", calls[0]
    assert results["authorise"]["toast"], "the answer was sent without saying anything happened"
    assert results["authorise"]["repainted"], (
        "the tab did not re-read after the answer, so the card still asks the question"
    )


def test_a_blank_ceiling_sends_no_number_so_the_server_decides(results):
    calls = results["blank_ceiling"]["calls"]
    assert len(calls) == 1, calls
    assert calls[0]["answer"] == "accept", calls
    assert "authorised_hours" not in calls[0]["body"], (
        "a blank field sent a number, so the browser and the server can disagree about what a "
        f"blank field means: {calls[0]['body']}"
    )


def test_declining_requires_a_reason_and_uses_the_decline_endpoint(results):
    decline = results["decline"]
    assert decline["posted_without_reason"] == 0, (
        "a refusal was sent with no reason, which is what the record keeps"
    )
    calls = decline["calls"]
    assert len(calls) == 1, calls
    assert calls[0]["answer"] == "decline", calls[0]
    assert calls[0]["body"]["note"] == "no overtime was agreed", calls[0]
    assert "authorised_hours" not in calls[0]["body"], (
        "a refusal carried a ceiling, so the two answers are not distinguishable on the wire"
    )


def test_a_ceiling_that_is_not_a_number_never_leaves_the_browser(results):
    assert results["bad_ceiling"]["posted"] == 0, results["bad_ceiling"]
    assert results["bad_ceiling"]["toast"], "a typo was refused silently"


def test_a_tap_reaches_the_right_worker_and_the_right_answer(results):
    binding = results["binding"]
    assert "[data-crossing-accept]" in binding["selectors"], binding["selectors"]
    assert "[data-crossing-decline]" in binding["selectors"], binding["selectors"]
    assert binding["bound"] == [
        "data-crossing-accept:1",
        "data-crossing-decline:600",
    ], binding["bound"]
    # The listener fired is the one the tab bound to worker 1's *accept* button, so the worker
    # and the answer both had to be carried by that button - a shared handler that guessed would
    # have posted for the wrong worker or the wrong answer.
    assert binding["calls"], "the tap on an unanswered crossing posted nothing"
    assert binding["calls"][0]["worker_id"] == "1", binding["calls"]
    assert binding["calls"][0]["answer"] == "accept", binding["calls"]


def test_the_crossing_cards_add_no_inline_handler(results):
    """``test_frontend_xss`` pins the count of inline handlers, and it only ever falls.

    Scoped to the section this change added: the review cards and the empty state carry inline
    handlers that predate it, and asserting over the whole page would be a test about somebody
    else's markup - vacuous when it passes for the wrong reason.
    """
    inline = results["inline"]
    assert inline["captured_chars"] > 200 and inline["answers_present"], (
        f"the crossings section was not captured, so this asserts nothing: {inline}"
    )
    assert inline["occurrences"] == 0, (
        "the crossings were wired with an inline handler; a CSP that forbids them would leave "
        "the two answers unclickable and nothing would say so"
    )


def test_a_crossings_read_that_fails_leaves_the_review_queue_on_screen(results):
    degraded = results["degraded"]
    assert degraded["queue_still_drawn"], (
        "a failed crossings read took the review queue off the screen: two questions, two reads, "
        "and one is not the other's error"
    )
    assert degraded["crossings_section_absent"], degraded


def test_an_empty_crossings_list_draws_no_section(results):
    empty = results["empty"]
    assert empty["section_absent"], "an empty section is drawn for a queue with nothing in it"
    assert empty["queue_still_drawn"], "the review queue disappeared with the crossings"


def test_the_sections_wording_is_translated_in_all_four_languages(results):
    wording = results["wording"]
    keys = (
        "crossingsTitle",
        "crossingsAccept",
        "crossingsDecline",
        "crossingsAuthorised",
        "crossingsDeclinedBy",
        "crossingsPastCeiling",
        "crossingsDecisionCeiling",
        "crossingsDecisionRecorded",
        "crossingsDecisionBy",
    )
    for lang in ("en", "ar", "hi", "ur"):
        values = wording[lang]
        assert len(values) == len(keys), (lang, values)
        for key, value in zip(keys, values):
            assert value and value != key, f"{lang}: {key} has no translation"
        assert values[0] != wording["en"][0] or lang == "en", (
            f"{lang}: the section title is not translated"
        )
