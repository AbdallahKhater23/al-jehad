"""The console's Alerts tab: the queue, the answer, and the reason.

WHY THIS EXISTS
---------------
The backend writes administrator alerts and, until this tab, none of them had a screen. What
can only break *in the browser* is exactly what this file exercises:

1. the tab is on the rail, in the operations group, and opening it reads the alert queue;
2. the queue leads with what is **waiting on somebody** - unanswered first, then the tone, then
   the newest - because the one row that needs an answer must not be below the fold;
3. answering posts the reason to *that* alert's endpoint, and the reason is required before
   anything is sent (the server refuses an empty one; this saves a round trip on a phone);
4. a ``409`` - somebody answered first - is shown as the server's sentence, and the card is left
   usable rather than frozen;
5. an answered alert shows who accepted it and why, and offers no second form;
6. **marking read is a separate act**: an informational alert can be cleared without looking
   like an acceptance of a forced start;
7. text the server wrote is text, not markup: an alert about a site called ``<b>`` must not turn
   bold in the middle of the queue;
8. the tab's own chrome is translated in all four languages, while the alert's sentence is the
   server's (English) wording, like every other ``admin_notifications`` row.

Node is optional; without it these skip rather than fail.
"""

from __future__ import annotations

import pytest

import frontend_vm

pytestmark = pytest.mark.skipif(frontend_vm.NODE is None, reason="node is not installed")

HARNESS = r"""
// --- the server's answers, faked ----------------------------------------

// Four alerts, one per interesting state: a forced start nobody has answered (critical, with
// the reason the operator typed), an answered retention sweep, an informational note from a
// worker, and one whose text is markup because a site name is free text somebody typed.
const ALERTS = [
    {
        id: 71, kind: 'startup_override', severity: 'critical',
        title: 'Startup gate overridden',
        body: "The server started with failing checks ['schema_current']. Reason given: rollback drill.",
        payload: '{"reason": "rollback drill"}', dedupe_key: 'startup_override:rollback drill',
        created_at: '2026-09-21 08:10:00', read_at: null, read_by: null,
        acknowledged_at: null, acknowledged_by: null, acknowledgement_note: null
    },
    {
        id: 70, kind: 'retention_sweep', severity: 'info',
        title: 'Retention sweep completed',
        body: 'Erased 412 read notifications older than 180 days.',
        payload: null, dedupe_key: 'retention:2026-09-20',
        created_at: '2026-09-20 03:00:00', read_at: '2026-09-20 07:00:00', read_by: '1000',
        acknowledged_at: '2026-09-20 07:05:00', acknowledged_by: '1000',
        acknowledgement_note: 'Expected: the nightly sweep, and the counts are in the log.'
    },
    {
        id: 69, kind: 'push_undelivered', severity: 'warning',
        title: 'Worker notifications went undelivered',
        body: '3 worker notification(s) passed the 15-minute push window without being delivered.',
        payload: '{"notices": 3, "no_device": 1, "with_device": 2}', dedupe_key: 'push_undelivered:2026-09-21 08',
        created_at: '2026-09-21 08:00:00', read_at: null, read_by: null,
        acknowledged_at: null, acknowledged_by: null, acknowledgement_note: null
    },
    {
        id: 68, kind: 'worker_note', severity: 'info',
        title: '<img src=x onerror=alert(1)>Password stopped working',
        body: 'Bilal Khan wrote: <b>the app</b> refuses my password.',
        payload: null, dedupe_key: 'note:12',
        created_at: '2026-09-19 06:00:00', read_at: null, read_by: null,
        acknowledged_at: null, acknowledged_by: null, acknowledgement_note: null
    }
];

let alertsEmpty = false;
let ackFailure = null;      // {status, detail} to answer the next acknowledgement with
const acknowledgements = [];
const reads = [];

function responders(url, init) {
    if (/\/admin\/notifications\/\d+\/acknowledge$/.test(url)) {
        acknowledgements.push({ url: url.split('/api/v1')[1], body: JSON.parse(init.body) });
        if (ackFailure) {
            const failure = ackFailure;
            ackFailure = null;
            return { status: failure.status, body: { detail: failure.detail } };
        }
        return {
            status: 200,
            body: { status: 'success', message: 'Notification acknowledged.', notification: { id: 71 } }
        };
    }
    if (/\/admin\/notifications\/\d+\/read$/.test(url)) {
        const id = parseInt(url.match(/notifications\/(\d+)\/read$/)[1], 10);
        reads.push({ url: url.split('/api/v1')[1], method: (init && init.method) || 'GET' });
        // The fake server *stores* the read, so the queue the next repaint draws is the state
        // the action produced rather than the state it started from.
        ALERTS.forEach((alert) => {
            if (alert.id === id) { alert.read_at = '2026-09-21 10:00:00'; alert.read_by = '1000'; }
        });
        return { status: 200, body: { status: 'success' } };
    }
    if (url.indexOf('/admin/notifications') >= 0) {
        if (alertsEmpty) return { status: 200, body: { unread: 0, unacknowledged: 0, notifications: [] } };
        const unacknowledged = ALERTS.filter((alert) => !alert.acknowledged_at).length;
        return {
            status: 200,
            body: { unread: 2, unacknowledged: unacknowledged, notifications: ALERTS }
        };
    }
    return { status: 200, body: {} };
}

// --- reading the rendered page -----------------------------------------

function textOf(markup) {
    return markup.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();
}

/** Every alert card, read the way an administrator sees them. */
function cardsOf(markup) {
    const cards = markup.match(/<article class="ui-card[^"]*"[\s\S]*?<\/article>/g) || [];
    return cards.map((card) => ({
        id: (/data-alert="([^"]*)"/.exec(card) || [])[1],
        severity: (/data-alert-severity="([^"]*)"/.exec(card) || [])[1],
        waiting: /data-unacknowledged="1"/.test(card),
        acknowledged_chip: /data-alert-acknowledged-chip/.test(card),
        waiting_chip: /data-alert-waiting-chip/.test(card),
        has_note_box: /data-alert-note="/.test(card),
        has_ack_button: /data-alert-acknowledge="/.test(card),
        has_read_button: /data-alert-mark-read="/.test(card),
        acknowledged_by: (/data-alert-acknowledged-by="true">([\s\S]*?)<\/p>/.exec(card) || [])[1],
        reason: (/data-alert-acknowledgement-note="true">([\s\S]*?)<\/p>/.exec(card) || [])[1],
        body: (/data-alert-body="true">([\s\S]*?)<\/p>/.exec(card) || [])[1],
        text: textOf(card)
    }));
}

function toasts(env) {
    return env.evaluate("(document.getElementById('toastRoot').__children || []).map((el) => el.textContent)");
}

function render(env) {
    return env.evaluate("document.getElementById('adminContent').innerHTML");
}

function consoleEnv() {
    alertsEmpty = false;
    ackFailure = null;
    acknowledgements.length = 0;
    reads.length = 0;
    const env = boot();
    env.setResponder(responders);
    env.evaluate("State.saveUser(" + JSON.stringify({
        id: '1000', name: 'Head Admin', role: 'head_admin', token: 'tok-admin'
    }) + ")");
    return env;
}

const results = {};

// 1. the tab is on the rail, and opening it reads the queue
{
    const env = consoleEnv();
    results.tabs = env.evaluate("ADMIN_TABS.map((tab) => [tab.id, tab.key, tab.group, tab.icon])");
    await env.evaluate("UI.renderAdminTab('Alerts')");
    const markup = render(env);
    results.queue = {
        cards: cardsOf(markup),
        page_flag: markup.indexOf('data-alerts="true"') >= 0,
        count_flag: (/data-alerts-count="(\d+)"/.exec(markup) || [])[1],
        count_text: textOf((/<p class="ui-section-note" data-alerts-count="\d+">([\s\S]*?)<\/p>/.exec(markup) || [])[0] || ''),
        hint_shown: textOf(markup).indexOf('Reading one is not accepting it') >= 0,
        requests: env.requests.filter((r) => r.url.indexOf('/admin/notifications') >= 0).length
    };
}

// 2. answering posts the reason to that alert's own endpoint
{
    const env = consoleEnv();
    await env.evaluate("UI.renderAdminTab('Alerts')");
    env.evaluate("document.getElementById('alertNote71').value = 'Checked the ledger by hand.'");
    await env.evaluate("UI_MODULES.acknowledgeAlert(71)");
    results.acknowledge = {
        calls: acknowledgements.slice(),
        toast: toasts(env).slice(-1)[0],
        repainted: env.requests.filter((r) => r.url.indexOf('/admin/notifications') >= 0).length > 1
    };
}

// 3. a reason is required before anything is sent - empty and whitespace
{
    const env = consoleEnv();
    await env.evaluate("UI.renderAdminTab('Alerts')");
    const before = acknowledgements.length;
    await env.evaluate("UI_MODULES.acknowledgeAlert(71)");
    env.evaluate("document.getElementById('alertNote71').value = '   '");
    await env.evaluate("UI_MODULES.acknowledgeAlert(71)");
    results.guard = {
        posted: acknowledgements.length - before,
        toast: toasts(env).slice(-1)[0]
    };
}

// 4. somebody answered first: the server's sentence, and the card stays usable
{
    const env = consoleEnv();
    await env.evaluate("UI.renderAdminTab('Alerts')");
    ackFailure = {
        status: 409,
        detail: 'Notification 71 was already acknowledged by 5000 on 2026-09-21 09:00:00.'
    };
    env.evaluate("document.getElementById('alertNote71').value = 'Second opinion'");
    await env.evaluate("UI_MODULES.acknowledgeAlert(71)");
    results.conflict = {
        toast: toasts(env).slice(-1)[0],
        buttons_enabled: env.evaluate(
            "Array.from(document.querySelectorAll('[data-alert=\"71\"] button')).every((b) => !b.disabled)"
        ),
        success_toast_absent: toasts(env).every((text) => text.indexOf('Acknowledged.') !== 0)
    };
}

// 5. marking read is a separate act, and it is not offered once the alert is read
{
    const env = consoleEnv();
    await env.evaluate("UI.renderAdminTab('Alerts')");
    const before = acknowledgements.length;
    const beforeMarkup = render(env);
    await env.evaluate("UI_MODULES.markAlertRead(68)");
    // The repaint the action triggers is not awaited by the action itself (the same shape the
    // review cards use), so the queue is drawn explicitly here - what is being asserted is what
    // the *data* now offers, not which microtask painted it.
    await env.evaluate("UI.renderAdminTab('Alerts')");
    const cards = cardsOf(render(env));
    results.mark_read = {
        calls: reads.slice(),
        acknowledgements: acknowledgements.length - before,
        offered_before: cardsOf(beforeMarkup).filter((card) => card.id === '68')[0].has_read_button,
        offered_on_unread: cards.filter((card) => card.id === '68')[0].has_read_button,
        offered_on_read: cards.filter((card) => card.id === '70')[0].has_read_button,
        toast: toasts(env).slice(-1)[0]
    };
}

// 6. an answered alert shows who accepted it, why, and no second form
{
    const env = consoleEnv();
    await env.evaluate("UI.renderAdminTab('Alerts')");
    const cards = cardsOf(render(env));
    const answered = cards.filter((card) => card.id === '70')[0];
    results.answered = {
        acknowledged_by: answered.acknowledged_by,
        reason: answered.reason,
        has_note_box: answered.has_note_box,
        has_ack_button: answered.has_ack_button,
        acknowledged_chip: answered.acknowledged_chip
    };
}

// 7. the empty state, and text from the server stays text
{
    const env = consoleEnv();
    alertsEmpty = true;
    await env.evaluate("UI.renderAdminTab('Alerts')");
    const emptyMarkup = render(env);
    results.empty = {
        flag: emptyMarkup.indexOf('data-alerts-empty="true"') >= 0,
        cards: cardsOf(emptyMarkup).length,
        text: textOf(emptyMarkup)
    };

    const env2 = consoleEnv();
    await env2.evaluate("UI.renderAdminTab('Alerts')");
    const markup = render(env2);
    results.escaping = {
        markup_absent: markup.indexOf('<img src=x') < 0 && markup.indexOf('<b>the app</b>') < 0,
        // Escaped, and *visible*: a queue that dropped the angle brackets would be safe and
        // useless - the alert is about a note whose subject somebody typed.
        escaped_title: markup.indexOf('&lt;img src=x onerror=alert(1)&gt;Password stopped working') >= 0,
        escaped_body: markup.indexOf('&lt;b&gt;the app&lt;/b&gt; refuses my password') >= 0
    };
}

// 8. the buttons are bound rather than inlined, and a click reaches that alert's endpoint
{
    const env = consoleEnv();
    env.evaluate("document.getElementById('alertNote71').value = 'Bound, not inline.'");
    // A container the binder can walk. The stub DOM's elements have no ``querySelectorAll``,
    // so ``bindAlertControls`` is handed one that does - which is exactly the seam under test:
    // the selectors it looks for, the attribute it reads the id from, and the method it calls.
    // (An inline ``onclick`` would make this untestable *and* cost a CSP allowance.) Everything
    // that touches the page's own globals happens inside the page's context; what comes back is
    // the selectors it asked for and the ids it read off the cards.
    results.binding = await env.evaluate(`(async () => {
        const selectors = [];
        const fired = [];
        const button = (id) => ({
            getAttribute: (name) => (name === 'data-alert-acknowledge' ? String(id) : null),
            addEventListener: (type, handler) => {
                if (type === 'click') fired.push({ id: id, handler: handler });
            }
        });
        const container = {
            querySelectorAll: (selector) => {
                selectors.push(selector);
                return selector === '[data-alert-acknowledge]' ? [button(71), button(69)] : [];
            }
        };
        UI_MODULES.bindAlertControls(container);
        // The listener the browser would fire on a tap; async, so a test has to await it.
        await fired[0].handler();
        return { selectors: selectors.slice(), bound: fired.map((entry) => entry.id) };
    })()`);
    results.binding.calls = acknowledgements.slice();
}

// 9. the tab's own wording, in each language, with the severity codes as the fallback
{
    const env = consoleEnv();
    // The fallbacks first, while the language is still English: a code this build does not know
    // has no translation to fall back to, and that is what is being pinned.
    const unknown = env.evaluate("UI_MODULES.alertSeverityLabel({severity: 'catastrophe'})");
    const missing = env.evaluate("UI_MODULES.alertSeverityLabel({})");
    const label = (lang) => {
        env.evaluate("I18n.setLang('" + lang + "')");
        return env.evaluate(
            "[I18n.__('adminAlerts'), I18n.__('adminAlertsAcknowledge'), I18n.__('hintAlerts')," +
            " UI_MODULES.alertSeverityLabel({severity: 'critical'})]"
        );
    };
    results.wording = {
        en: label('en'),
        ar: label('ar'),
        hi: label('hi'),
        ur: label('ur'),
        unknown_severity: unknown,
        missing_severity: missing
    };
    env.evaluate("I18n.setLang('en')");
}
"""


@pytest.fixture(scope="module")
def results() -> dict:
    return frontend_vm.run(HARNESS)


def test_the_tab_is_on_the_rail_and_opening_it_reads_the_queue(results):
    tabs = {tab[0]: tab for tab in results["tabs"]}
    assert "Alerts" in tabs, f"the console has no Alerts tab: {[tab[0] for tab in results['tabs']]}"
    assert tabs["Alerts"][2] == "navGroupOperations", (
        "an alert queue belongs with the operations screens, not in the configuration group"
    )
    assert tabs["Alerts"][3] == "alerts", "the tab draws an icon of its own"

    queue = results["queue"]
    assert queue["page_flag"] is True
    assert queue["requests"] >= 1, "the tab rendered without reading the alert queue"
    assert queue["hint_shown"], "the screen does not say that reading is not accepting"


def test_the_queue_leads_with_what_nobody_has_answered(results):
    order = [card["id"] for card in results["queue"]["cards"]]
    assert order == ["71", "69", "68", "70"], (
        "the queue is not ordered unanswered-first, then by tone, then newest: " + str(order)
    )
    by_id = {card["id"]: card for card in results["queue"]["cards"]}
    assert by_id["71"]["severity"] == "critical"
    assert by_id["71"]["waiting"] is True and by_id["71"]["waiting_chip"] is True
    assert by_id["70"]["waiting"] is False and by_id["70"]["acknowledged_chip"] is True
    assert by_id["71"]["has_note_box"] and by_id["71"]["has_ack_button"]
    assert results["queue"]["count_flag"] == "3", "the count of unanswered alerts is wrong"
    assert "3 of 4" in results["queue"]["count_text"], results["queue"]["count_text"]


def test_answering_sends_the_reason_to_that_alerts_own_endpoint(results):
    calls = results["acknowledge"]["calls"]
    assert len(calls) == 1, calls
    assert calls[0]["url"] == "/admin/notifications/71/acknowledge", (
        "the reason went somewhere other than the alert it was written about"
    )
    assert calls[0]["body"] == {"note": "Checked the ledger by hand."}
    assert results["acknowledge"]["toast"], "no confirmation was shown"
    assert results["acknowledge"]["repainted"], "the queue was not refreshed after the answer"


def test_a_reason_is_required_before_anything_is_sent(results):
    guard = results["guard"]
    assert guard["posted"] == 0, "an empty reason was posted to the server"
    assert guard["toast"], "the administrator was not told the reason is missing"


def test_a_conflict_shows_the_servers_sentence_and_leaves_the_card_usable(results):
    conflict = results["conflict"]
    assert "already acknowledged by 5000" in conflict["toast"], conflict["toast"]
    assert conflict["success_toast_absent"], "a refused answer was reported as accepted"
    assert conflict["buttons_enabled"], "the card was left disabled after a refusal"


def test_marking_read_is_a_separate_act(results):
    mark_read = results["mark_read"]
    assert [call["url"] for call in mark_read["calls"]] == ["/admin/notifications/68/read"]
    assert mark_read["calls"][0]["method"] == "POST", (
        "marking read must post: this endpoint changes a row"
    )
    assert mark_read["acknowledgements"] == 0, "marking read also acknowledged the alert"
    assert mark_read["offered_before"] is True, "an unread alert cannot be marked read"
    assert mark_read["offered_on_unread"] is False, (
        "the alert still offers 'mark read' after it was marked read"
    )
    assert mark_read["offered_on_read"] is False, "an alert that is already read offers it again"
    assert mark_read["toast"]


def test_an_answered_alert_shows_who_accepted_it_and_why(results):
    answered = results["answered"]
    assert "1000" in answered["acknowledged_by"], answered["acknowledged_by"]
    assert "2026-09-20 07:05:00" in answered["acknowledged_by"]
    assert "the nightly sweep" in answered["reason"], answered["reason"]
    assert answered["has_note_box"] is False, "an answered alert offers a second answer"
    assert answered["has_ack_button"] is False
    assert answered["acknowledged_chip"] is True


def test_nothing_waiting_reads_as_nothing_waiting(results):
    empty = results["empty"]
    assert empty["flag"] is True, "the empty queue rendered cards"
    assert empty["cards"] == 0
    assert "Nothing to answer" in empty["text"], empty["text"]


def test_text_the_server_wrote_is_escaped(results):
    escaping = results["escaping"]
    assert escaping["markup_absent"], (
        "an alert whose text contains markup rendered it: the queue is an injection surface"
    )
    assert escaping["escaped_title"], "the worker's own words were dropped rather than shown as text"
    assert escaping["escaped_body"]


def test_the_buttons_are_bound_and_a_click_reaches_that_alerts_endpoint(results):
    """No inline handler, and the id comes off the card rather than out of a closure.

    The CSP still allows ``script-src-attr 'unsafe-inline'`` because the console builds its
    markup as strings; ``test_frontend_xss`` pins that count as never rising, and this is the
    behaviour on the other side of it - the listeners are attached after the paint, and each
    one reads its own card's id, so a repaint between paint and tap cannot misfire onto another
    alert.
    """
    binding = results["binding"]
    assert binding["selectors"] == [
        "[data-alert-acknowledge]",
        "[data-alert-mark-read]",
        "[data-alerts-live-ops]",
    ], binding["selectors"]
    assert binding["bound"] == [71, 69], "the acknowledge buttons were not bound"
    assert len(binding["calls"]) == 1, binding["calls"]
    assert binding["calls"][0]["url"] == "/admin/notifications/71/acknowledge", (
        "the click did not reach the alert whose card carried it: " + str(binding["calls"])
    )
    assert binding["calls"][0]["body"] == {"note": "Bound, not inline."}


def test_the_tabs_own_wording_ships_in_every_language(results):
    wording = results["wording"]
    english = wording["en"]
    assert english[0] == "Alerts"
    assert english[1] == "Acknowledge"
    assert english[3] == "Critical", "the severity code is not shown as words"
    for lang in ("ar", "hi", "ur"):
        values = wording[lang]
        assert all(value and value != key for value, key in zip(values, english[:3])), (
            f"{lang} shows the English keys for the Alerts tab: {values}"
        )
        assert values[3] != "Critical", f"{lang} shows the severity in English: {values[3]}"
    assert wording["unknown_severity"] == "Catastrophe", (
        "a severity this build does not know must read as words, not as a code"
    )
    assert wording["missing_severity"] == "Information", (
        "an alert with no severity is not an alert with a broken label"
    )
