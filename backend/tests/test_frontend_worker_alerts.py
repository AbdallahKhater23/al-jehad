"""The worker's own inbox: the notices the system writes, on the phone they are for.

WHY THIS EXISTS
---------------
The application has written worker notices for a while - ``overtime.scan_overtime`` records
a crossing, ``overtime.scan_auto_close`` records the day it closed for somebody - and
``GET /worker/me/notifications`` has answered with them since it was added. Nothing in the
frontend ever *asked*. So the record existed, the endpoint worked, the backend suite proved
both, and the person the notice was about could not read a word of it: the only thing that
tried to reach them was a push, and a push needs a service worker this frontend does not
have, a permission nobody had prompted for, and VAPID keys a deployment may not carry.

What can only be checked here, and not against the API:

1. the count is *read* - once, from the endpoint that owns it - and painted in both places
   it appears: the badge on the handset's alerts tab and the band above the clock button,
   from one number, so the two can never disagree;
2. the clock panel is where it appears, and above the button rather than below it, because
   the notice the system writes about a shift exists precisely because the *next* clock-out
   refuses the worker;
3. marking one notice read sends the id **in the query string**, which is where the route
   reads it - a JSON body it never parses is answered exactly like "no id", so it would mark
   the whole inbox read and destroy the one piece of information this feature carries;
4. a notice is text, not markup: a title arriving with ``<b>`` in it must not become bold in
   the middle of somebody's inbox;
5. an unread notice is a button and a read one is not, because reading is the only action a
   notice has - a card that looks tappable and does nothing is worse than one that plainly
   does not;
6. a count that cannot be read leaves the punch card alone. The clock panel is the one
   screen that has to work with no signal, and a badge is not worth an error card;
7. returning to the tab re-reads the count, so a notice read on *another* device - a push
   tapped on the worker's other phone - clears the badge here too. The service worker's
   ``message`` only covers a tap in this tab, so ``focus``/``visibilitychange`` is the only
   way this one hears about it.

Node is optional; without it these skip rather than fail. The same journey in a real browser
- the badge actually painted on the tab, the banner actually leading to the inbox - is pinned
in ``test_signed_in_sessions_in_a_browser.py``, because a stub DOM keeps markup as a string
and cannot answer "what did the reader see".
"""

from __future__ import annotations

import html as html_module
import re

import pytest

import frontend_vm

pytestmark = pytest.mark.skipif(frontend_vm.NODE is None, reason="node is not installed")

HARNESS = r"""
// --- the server's answers, faked ----------------------------------------

// The shift's own status, as ``/worker/me/stats`` really answers it: every field the clock
// panel reads for its card is here, so "the card still drew itself" is a real assertion.
const STATS = {
    active_session: null,
    total_hours: 12.5,
    overtime_notify_hours: 8.1,
    break_minutes: 30,
    break_after_hours: 4,
    paid_day_hours: 8,
    auto_close_at_regular: '1',
    flagged_for_review: false
};

// Three notices: a closed day, a crossing, and a decision somebody answered - one of them
// already read, and a title carrying markup, which is what any value off the wire may be.
let notices = [];

function loadNotices() {
    notices = [
        {
            id: 41, kind: 'shift_auto_closed',
            title: 'Your shift was closed at the paid day',
            body: '8.50h on site - 0.50h unpaid break = 8.00h payable. Clock in again when you start your next shift.',
            created_at: '2026-09-19 16:04:00', read: false, delivered: false
        },
        {
            id: 40, kind: 'overtime_crossed',
            title: '<b>Overtime</b> started',
            body: 'You have passed 8.10h today. The hours past the paid day are held for review.',
            created_at: '2026-09-18 17:20:00', read: false, delivered: true
        },
        {
            id: 39, kind: 'overtime_authorised',
            title: 'Your extra time is authorised up to 12 h',
            body: 'Head Admin authorised up to 12 h of paid work for your shift on Downtown Tower A.',
            created_at: '2026-09-11 17:05:00', read: true, delivered: true
        }
    ];
}

//: Every read request the page made, as the server would have parsed it.
let reads = [];
//: Flip to make the notice endpoint fail - the no-signal case.
let alertsBroken = false;

function unreadNotices() { return notices.filter((notice) => !notice.read); }

function responders(url, init) {
    // First, because the read route's own path also contains the inbox's.
    if (url.indexOf('/worker/me/notifications/read') >= 0) {
        reads.push({ url: url.split('/api/v1')[1], method: (init && init.method) || 'GET' });
        return { status: 200, body: { status: 'success', marked: 1 } };
    }
    if (url.indexOf('/worker/me/notifications') >= 0) {
        if (alertsBroken) return { status: 503, body: { detail: 'Database is locked.' } };
        // ``unread_only`` is the clock panel's cheap question; the inbox asks for the list.
        const rows = url.indexOf('unread_only=true') >= 0 ? unreadNotices().slice(0, 1) : notices;
        return {
            status: 200,
            body: { worker_id: '601', unread: unreadNotices().length, notifications: rows, push: { enabled: false } }
        };
    }
    if (url.indexOf('/worker/me/stats') >= 0) return { status: 200, body: STATS };
    if (url.indexOf('/worker/me/logs') >= 0) return { status: 200, body: [] };
    return { status: 200, body: {} };
}

function textOf(markup) {
    return markup.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();
}

function countOf(markup, needle) {
    return markup.split(needle).length - 1;
}

/**
 * The notice cards in a list, in the order they appear.
 *
 * Split on the attribute rather than matched with a tag-aware regex: a read card is a
 * ``<div>`` whose own body contains nested ``<div>``s, so a non-greedy ``</div>`` would stop
 * inside the card and read the wrong half of it. Each chunk runs from one card's opening tag
 * to the next card's, so its *first* id, tag and unread flag are the card's own.
 */
function cardsOf(markup, attr) {
    const marker = 'data-' + attr + '="';
    return markup.split(marker).slice(1).map((chunk) => ({
        id: (/^([^"]*)/.exec(chunk) || [])[1],
        unread: chunk.indexOf('data-alert-unread') >= 0
    }));
}

/** The opening tag of every card, in order - which is what tells a button from a card. */
function tagsOf(markup, attr) {
    const pattern = new RegExp('<(button|div)[^>]*data-' + attr + '="', 'g');
    const tags = [];
    let match;
    while ((match = pattern.exec(markup)) !== null) tags.push(match[1]);
    return tags;
}

function toasts(env) {
    return env.evaluate("(document.getElementById('toastRoot').__children || []).map((el) => el.textContent)");
}

function workerEnv() {
    loadNotices();
    reads.length = 0;
    alertsBroken = false;
    const env = boot();
    env.setResponder(responders);
    env.evaluate("localStorage.setItem('layoutOverride', 'mobile')");
    env.evaluate("State.saveUser(" + JSON.stringify({
        id: '601', name: 'Bilal Khan', role: 'worker', token: 'tok-worker'
    }) + ")");
    return env;
}

const results = {};

// 1. the count is read, once, and painted in both places it appears
{
    const env = workerEnv();
    await env.evaluate("UI.renderWorkerMobile()");
    const card = env.evaluate("document.getElementById('workerDashboard').innerHTML");
    results.badge = {
        tab_ids: env.evaluate("UI.workerTabIds"),
        // The rendered bar is a string in this DOM, so the badge is proved against the bar
        // as it is *drawn* from the count; the painted one is the browser suite's job.
        redrawn: env.evaluate("UI.workerTabBarHtml()"),
        count: env.evaluate("WORKER_MODULES.alertCount"),
        banner: env.evaluate("document.getElementById('workerAlertsBanner').innerHTML"),
        // Where the band's host sits in the card, and where the button it warns about is.
        // The band's *words* are in its own element below: this DOM keeps a view as the
        // string the app wrote, so only the markup's own order can be read from it.
        banner_host_at: card.indexOf('workerAlertsBanner'),
        button_at: card.indexOf('handleClock'),
        asked: env.requests.map((request) => request.url.split('/api/v1')[1])
    };
}

// 2. the inbox itself: newest first, the server's sentences, kinds as chips
{
    const env = workerEnv();
    await env.evaluate("UI.renderWorkerView('alerts')");
    const markup = env.evaluate("document.getElementById('workerAlerts').innerHTML");
    results.inbox = {
        markup: markup,
        text: textOf(markup),
        ids: cardsOf(markup, 'alert').map((card) => card.id),
        unread: cardsOf(markup, 'alert').map((card) => card.unread),
        tags: tagsOf(markup, 'alert'),
        new_chips: countOf(markup, '>New<'),
        mark_read_labels: countOf(markup, 'Mark as read'),
        raw_tag: markup.indexOf('<b>') >= 0,
        escaped_tag: markup.indexOf('&lt;b&gt;') >= 0,
        asked: env.requests.map((request) => request.url.split('/api/v1')[1]),
        count: env.evaluate("WORKER_MODULES.alertCount"),
        host_is_the_one_rendered_into: env.evaluate(
            "WORKER_MODULES._alertsHost === document.getElementById('workerAlerts')"
        )
    };
}

// 3. marking one read: the id where the route reads it, and both badges updated from it
{
    const env = workerEnv();
    await env.evaluate("UI.renderWorkerMobile()");
    await env.evaluate("UI.renderWorkerView('alerts')");
    await env.evaluate("WORKER_MODULES.markAlertRead(41)");
    const markup = env.evaluate("document.getElementById('workerAlerts').innerHTML");
    results.marked_one = {
        reads: reads.slice(),
        tags: tagsOf(markup, 'alert'),
        unread: cardsOf(markup, 'alert').map((card) => card.unread),
        mark_read_labels: countOf(markup, 'Mark as read'),
        count: env.evaluate("WORKER_MODULES.alertCount"),
        banner: env.evaluate("document.getElementById('workerAlertsBanner').innerHTML"),
        redrawn: env.evaluate("UI.workerTabBarHtml()"),
        toast: toasts(env).slice(-1)[0]
    };
}

// 4. marking the whole inbox read: no id at all, which is what "all of them" means
{
    const env = workerEnv();
    await env.evaluate("UI.renderWorkerMobile()");
    await env.evaluate("UI.renderWorkerView('alerts')");
    await env.evaluate("WORKER_MODULES.markAllAlertsRead()");
    const markup = env.evaluate("document.getElementById('workerAlerts').innerHTML");
    results.marked_all = {
        reads: reads.slice(),
        unread: cardsOf(markup, 'alert').map((card) => card.unread),
        tags: tagsOf(markup, 'alert'),
        mark_all_button: markup.indexOf('data-mark-all-alerts') >= 0,
        count: env.evaluate("WORKER_MODULES.alertCount"),
        banner: env.evaluate("document.getElementById('workerAlertsBanner').innerHTML"),
        redrawn: env.evaluate("UI.workerTabBarHtml()"),
        toast: toasts(env).slice(-1)[0]
    };
}

// 5. an inbox with nothing in it at all: the empty state, and chrome that claims nothing
{
    const env = workerEnv();
    notices.length = 0;
    await env.evaluate("UI.renderWorkerMobile()");
    await env.evaluate("UI.renderWorkerView('alerts')");
    const markup = env.evaluate("document.getElementById('workerAlerts').innerHTML");
    results.nothing_waiting = {
        inbox: textOf(markup),
        empty_state: markup.indexOf('ui-empty') >= 0,
        mark_all_button: markup.indexOf('data-mark-all-alerts') >= 0,
        banner: env.evaluate("document.getElementById('workerAlertsBanner').innerHTML"),
        redrawn: env.evaluate("UI.workerTabBarHtml()"),
        count: env.evaluate("WORKER_MODULES.alertCount")
    };
}

// 6. the count cannot be read: the punch card is untouched, and nothing is claimed
{
    const env = workerEnv();
    alertsBroken = true;
    await env.evaluate("UI.renderWorkerMobile()");
    const card = env.evaluate("document.getElementById('workerDashboard').innerHTML");
    results.broken = {
        card: card,
        clock_button: card.indexOf('handleClock') >= 0,
        leaked_error: card.indexOf('Database is locked.') >= 0,
        banner: env.evaluate("document.getElementById('workerAlertsBanner').innerHTML"),
        redrawn: env.evaluate("UI.workerTabBarHtml()"),
        count: env.evaluate("WORKER_MODULES.alertCount"),
        toasts: toasts(env)
    };
}

// 7. every new string is in every table, with its placeholder
{
    const env = workerEnv();
    const KEYS = [
        'alerts', 'alertsMine', 'alertsIntro', 'alertsEmpty', 'alertsEmptyHint', 'alertsNew',
        'alertsMarkRead', 'alertsMarkAll', 'alertsMarkedRead', 'alertsMarkedAll',
        'alertsUnreadOne', 'alertsUnreadMany', 'alertsOpen', 'alertsSent',
        'alertKind_overtime_crossed', 'alertKind_shift_auto_closed',
        'alertKind_overtime_authorised', 'alertKind_overtime_declined'
    ];
    results.translations = env.evaluate(`(function () {
        const KEYS = ${JSON.stringify(KEYS)};
        return {
            present_in_english: KEYS.filter((key) => !(key in TRANSLATIONS.en)),
            missing: Object.keys(TRANSLATIONS).map((lang) => [
                lang,
                KEYS.filter((key) => !(key in TRANSLATIONS[lang])).join(',')
            ]),
            placeholders: KEYS.filter((key) => {
                const holder = /\{\w+\}/.exec(TRANSLATIONS.en[key]);
                if (!holder) return false;
                return Object.keys(TRANSLATIONS).some((lang) => TRANSLATIONS[lang][key].indexOf(holder[0]) < 0);
            })
        };
    })()`);
}

// 7b. the decision chips: the two kinds an answer arrives under, and a kind nobody knows yet
{
    const env = workerEnv();
    results.decision_labels = {
        authorised: env.evaluate("WORKER_MODULES.alertKindLabel('overtime_authorised')"),
        declined: env.evaluate("WORKER_MODULES.alertKindLabel('overtime_declined')"),
        // The fallback the chip shares with every other code label in this app: a kind this
        // build has never heard of reads as opened-up words rather than as nothing at all.
        unknown: env.evaluate("WORKER_MODULES.alertKindLabel('overtime_authorised_by_someone_new')")
    };
}

// 8. coming back to the tab re-reads the count, so a read made elsewhere clears the badge
{
    const env = workerEnv();
    await env.evaluate("UI.renderWorkerMobile()");
    // Boot registers the return-to-tab watcher. Its own first paint reads the count too, so
    // the requests are cleared after it - this case is about the read a *return* causes.
    await env.evaluate("UI.init()");
    await new Promise((resolve) => setTimeout(resolve, 0));
    env.requests.length = 0;
    // The worker taps the push on their other phone: the server now has nothing unread.
    notices.forEach((notice) => { notice.read = true; });
    // Both events one return produces, and twice, because ``visibilitychange`` and ``focus``
    // both fire and only one read may come of it.
    env.fireWindowEvent('focus');
    env.fireWindowEvent('focus');
    await new Promise((resolve) => setTimeout(resolve, 0));
    const redrawn = env.evaluate("UI.workerTabBarHtml()");
    results.returned_to_tab = {
        bound: env.evaluate("UI._unreadResyncBound"),
        asked: env.requests.map((request) => request.url.split('/api/v1')[1]),
        count: env.evaluate("WORKER_MODULES.alertCount"),
        redrawn: redrawn,
        badge_gone: redrawn.indexOf('data-alert-count') < 0,
        banner: env.evaluate("document.getElementById('workerAlertsBanner').innerHTML")
    };
    // A tab nobody is signed into asks nothing: the badge belongs to the handset's session.
    env.evaluate("State.clearUser()");
    env.fireWindowEvent('focus');
    await new Promise((resolve) => setTimeout(resolve, 0));
    results.returned_to_tab.asked_after_logout = env.requests.length;
}
"""


@pytest.fixture(scope="module")
def results():
    return frontend_vm.run(HARNESS)


def text_of(markup: str) -> str:
    """The words a reader gets: tags out, entities resolved, whitespace collapsed."""
    return re.sub(r"\s+", " ", html_module.unescape(re.sub(r"<[^>]*>", " ", markup))).strip()


def test_the_count_reaches_the_tab_and_the_clock_panel(results):
    """One number, read once, painted in the two places a worker can see it.

    Read once because it is one fact: two requests are two chances for the tab to say "2"
    while the band above the button says "1". Asked for cheaply - ``unread_only`` and one
    row - because the clock panel is not the screen to download a list onto, and it is a
    phone on site cellular that pays for it.
    """
    badge = results["badge"]
    assert badge["count"] == 2, badge
    assert [url for url in badge["asked"] if "notifications?" in url] == [
        "/worker/me/notifications?limit=1&unread_only=true"
    ], badge["asked"]
    assert "data-alert-count=\"2\"" in badge["redrawn"], badge["redrawn"]
    # On the alerts tab and nowhere else: a badge on Clock is a badge about the wrong screen.
    before_alerts = badge["redrawn"].split('data-worker-tab="alerts"')[0]
    assert "data-alert-count" not in before_alerts, badge["redrawn"]

    banner = badge["banner"]
    assert "2 new alerts" in text_of(banner), banner
    assert "Your shift was closed at the paid day" in text_of(banner), (
        f"the band has to say what is waiting, not only how much\n{banner}"
    )
    assert "See all" in text_of(banner), (
        f"the band is a control, so something in it has to say where it goes\n{banner}"
    )
    assert 0 <= badge["banner_host_at"] < badge["button_at"], (
        f"the band's host sits at {badge['banner_host_at']} and the button at "
        f"{badge['button_at']}: below the button, a worker meets the notice after the tap it "
        "is about"
    )


def test_the_inbox_lists_what_the_backend_wrote(results):
    """Newest first, the server's own two sentences, and the kind as a chip."""
    inbox = results["inbox"]
    assert inbox["asked"] == ["/worker/me/notifications"], inbox["asked"]
    assert inbox["ids"] == ["41", "40", "39"], inbox["ids"]
    assert inbox["unread"] == [True, True, False], inbox["unread"]
    # An unread notice is the only one with something to do, so it is the only button.
    assert inbox["tags"] == ["button", "button", "div"], inbox["tags"]
    assert inbox["new_chips"] == 2, inbox["markup"]
    assert inbox["mark_read_labels"] == 2, inbox["markup"]
    assert "Your shift was closed at the paid day" in inbox["text"], inbox["text"]
    assert "8.00h payable" in inbox["text"], inbox["text"]
    # The decision notice, which is a third kind and needs no rendering of its own: the server's
    # title and body are what the inbox prints, so an answer somebody gave reads like any notice.
    assert "Your extra time is authorised up to 12 h" in inbox["text"], inbox["text"]
    # The two kinds are named, so a crossing reads differently from a closed day.
    assert "Day closed" in inbox["text"], inbox["text"]
    assert inbox["count"] == 2, inbox
    assert inbox["host_is_the_one_rendered_into"], (
        "a repaint after marking one read looks for the host it drew into"
    )


def test_the_decision_arrives_under_a_chip_of_its_own(results):
    """An answer is a notice of its own kind, and the inbox names it.

    The inbox is generic - the server's title and body are what it prints, so a decision notice
    needed no rendering of its own - but the *chip* is where this app says which of the things
    that can land here this one is. Authorised and declined are opposite statements, so they get
    opposite labels rather than one word covering both, and a worker scanning their inbox is not
    made to read a sentence to find out which of the two happened to them.
    """
    labels = results["decision_labels"]
    assert labels["authorised"], labels
    assert labels["authorised"] != "overtime_authorised", (
        f"the authorising kind has no label, so the chip prints the raw kind: {labels}"
    )
    assert labels["declined"] and labels["declined"] != labels["authorised"], labels
    assert labels["unknown"] and "_" not in labels["unknown"], (
        f"a kind this build has never heard of should read as opened-up words: {labels}"
    )

    inbox = results["inbox"]
    assert labels["authorised"] in inbox["text"], inbox["text"]
    assert "Your extra time is authorised up to 12 h" in inbox["text"], inbox["text"]
    assert "Head Admin authorised up to 12 h" in inbox["text"], inbox["text"]


def test_a_notice_is_text_and_never_markup(results):
    """A title that arrives with a tag in it is escaped like every other value off the wire."""
    inbox = results["inbox"]
    assert inbox["escaped_tag"], (
        f"the title's tag was not escaped on the way into the list\n{inbox['markup']}"
    )
    assert not inbox["raw_tag"], (
        f"a notice title became markup in the middle of somebody's inbox\n{inbox['markup']}"
    )
    # The escaped title is still a title: the reader gets the words, entities and all, rather
    # than the tag being dropped and the notice arriving with a hole in it.
    assert "&lt;b&gt;Overtime" in inbox["text"], inbox["text"]


def test_marking_one_read_sends_the_id_where_the_route_reads_it(results):
    """The query string, not a body.

    ``POST /worker/me/notifications/read`` takes ``notification_id`` as a plain parameter,
    which FastAPI reads from the query string. Posting it in a JSON body would be answered
    exactly like "no id" - every notice read, silently, which is the one mistake here that
    destroys information instead of showing it.
    """
    marked = results["marked_one"]
    assert marked["reads"] == [
        {"url": "/worker/me/notifications/read?notification_id=41", "method": "POST"}
    ], marked["reads"]
    assert marked["unread"] == [False, True, False], marked["unread"]
    assert marked["tags"] == ["div", "button", "div"], marked["tags"]
    assert marked["mark_read_labels"] == 1, "the label goes with the action it names"
    assert marked["count"] == 1, marked
    assert "1 new alert" in text_of(marked["banner"]), marked["banner"]
    assert "data-alert-count=\"1\"" in marked["redrawn"], marked["redrawn"]
    assert marked["toast"] == "Alert marked as read.", marked["toast"]


def test_marking_everything_read_sends_no_id_at_all(results):
    """ "All of them" is the absence of an id, and both badges go with it."""
    marked = results["marked_all"]
    assert marked["reads"] == [
        {"url": "/worker/me/notifications/read", "method": "POST"}
    ], marked["reads"]
    assert marked["unread"] == [False, False, False], marked["unread"]
    assert marked["tags"] == ["div", "div", "div"], marked["tags"]
    assert marked["count"] == 0, marked
    assert marked["banner"] == "", (
        f"a band with nothing behind it is a claim about a shift\n{marked['banner']}"
    )
    assert "data-alert-count" not in marked["redrawn"], marked["redrawn"]
    assert marked["mark_all_button"] is False, (
        "the button is only offered while there is something to mark"
    )
    assert marked["toast"] == "All alerts marked as read.", marked["toast"]


def test_an_empty_inbox_claims_nothing(results):
    """A worker nothing has ever happened to gets a sentence and no chrome at all.

    The other empty case - three notices, every one read - is ``marked_all`` above: the
    list stays (the record is the point of it) and the count does not.
    """
    nothing = results["nothing_waiting"]
    assert nothing["count"] == 0, nothing
    assert nothing["empty_state"], nothing["inbox"]
    assert "Nothing waiting." in nothing["inbox"], nothing["inbox"]
    assert nothing["mark_all_button"] is False, nothing["inbox"]
    assert nothing["banner"] == "", nothing["banner"]
    assert "data-alert-count" not in nothing["redrawn"], nothing["redrawn"]


def test_a_count_that_cannot_be_read_leaves_the_punch_card_alone(results):
    """The offline case, which is the case this screen exists for.

    A notice is the least important thing on the clock panel and the punch card is the most,
    so an error card where the clock should be is the one degradation not allowed here.
    Nothing is claimed either: a badge left over from the last successful read would say
    "you have read everything" on the strength of a request that never landed, and a toast
    about a badge is noise in the middle of a punch.
    """
    broken = results["broken"]
    assert broken["clock_button"], (
        f"the clock button is missing from the card a worker gets with no signal\n{broken['card']}"
    )
    assert not broken["leaked_error"], (
        f"the notice endpoint's failure is on the punch card\n{broken['card']}"
    )
    assert broken["banner"] == "", broken["banner"]
    assert broken["count"] == 0, broken
    assert "data-alert-count" not in broken["redrawn"], broken["redrawn"]
    assert broken["toasts"] == [], broken["toasts"]


def test_returning_to_the_tab_re_reads_the_badge(results):
    """A read made on another device is the one this tab has no way to hear about.

    ``UI.init`` registers the watcher; a return to the tab asks the server again instead of
    trusting the number painted before the phone was put down. Two events fire for one return
    and the pair still costs one request, which is asserted because a badge that fetched twice
    per unlock is a phone on site cellular paying twice for the same fact. A tab with no
    session asks nothing at all.
    """
    returned = results["returned_to_tab"]
    assert returned["bound"] is True, "UI.init never bound the return-to-tab watcher"
    assert returned["asked"] == ["/worker/me/notifications?limit=1&unread_only=true"], (
        f"a return to the tab should re-ask the unread count once: {returned['asked']}"
    )
    assert returned["count"] == 0, returned
    assert returned["badge_gone"], (
        f"the badge still shows a count after the notice was read elsewhere\n{returned['redrawn']}"
    )
    assert returned["banner"] == "", returned["banner"]
    assert returned["asked_after_logout"] == 1, (
        "a signed-out tab asked the server for a count it has no session to read"
    )


def test_every_new_string_exists_in_every_language(results):
    """A key present in English only renders as its own name on every phone that lacks it."""
    translations = results["translations"]
    assert translations["present_in_english"] == [], translations["present_in_english"]
    languages = [lang for lang, _ in translations["missing"]]
    assert languages == ["en", "ar", "hi", "ur"], languages
    assert all(missing == "" for _, missing in translations["missing"]), translations["missing"]
    assert translations["placeholders"] == [], (
        f"a {{placeholder}} only some tables carry renders the token itself: "
        f"{translations['placeholders']}"
    )
