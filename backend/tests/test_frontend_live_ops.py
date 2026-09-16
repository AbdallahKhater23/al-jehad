"""The Live Ops board: the shift an operator has to act on, in one glance.

WHAT THIS GUARDS
----------------
The Live Ops tab used to be one plain table (name / site / clock-in / button) and
a force-in panel above it. An admin opening it could not answer the three
questions the screen exists for:

* how many people are on site right now, and over how many sites;
* which shift has been open long enough to need a decision (the paid day, or the
  hour the server alerts on at clock-out);
* where is *that* person, in a list of forty names.

So this suite pins the redesign, not its markup: the four figures, the state of
each shift derived from ``shift_rules``, filtering, sorting, the empty and failed
states, the live timers' lifecycle, and the accessibility floor the board keeps
(a sentence in the live region, a word beside every colour, no emoji icons).

The properties a redesign is most likely to get wrong quietly are asserted
directly:

* **the arithmetic agrees with the payslip** - the unpaid break is charged only
  to a shift long enough to have contained one, and both thresholds are compared
  against *paid* hours, so 9 h on site (8.5 paid) crosses the day while 8 h
  (7.5 paid) does not;
* **an operator setting is honoured** - with ``auto_close_at_regular`` off, a
  shift past the paid day is not labelled as closing, because nothing is going to
  close it;
* **the board repaints only when the data changed** - a poll that repaints
  anyway is what steals focus from the search box and closes the panel someone
  is filling in.
"""

from __future__ import annotations

import re

import pytest

import frontend_vm
from harness import PROJECT_ROOT

STYLE = (PROJECT_ROOT / "frontend" / "style.css").read_text(encoding="utf-8", errors="ignore")

#: ``regular_hours`` 8 with an overtime line *below* it: a legitimate retune, and
#: the only way all three states appear on one board - past the overtime line
#: (7.5 paid) before the paid day (8) is reached. Under the shipped numbers
#: (8.1 > 8) the closing state always wins, which the arithmetic tests below pin.
RETUNED = {
    "regular_hours": 8.0,
    "overtime_notify_hours": 7.5,
    "break_minutes": 30.0,
    "break_after_hours": 4.0,
    "auto_close_at_regular": 1,
}

SHIPPED = {
    "regular_hours": 8.0,
    "overtime_notify_hours": 8.1,
    "break_minutes": 30.0,
    "break_after_hours": 4.0,
    "auto_close_at_regular": 1,
}

HARNESS = """
// The four people on site: one past the paid day, one past the overtime line and
// flagged late, two on an ordinary morning. Clock-ins are relative to one ``NOW``
// captured at the top, so the derived states do not depend on when the suite is
// run - and so two reads of the same stored shift produce the same signature.
const NOW = Date.now();

function ago(hours) {
    const at = new Date(NOW - hours * 3600 * 1000);
    const pad = (value) => String(value).padStart(2, '0');
    return `${at.getFullYear()}-${pad(at.getMonth() + 1)}-${pad(at.getDate())} ` +
        `${pad(at.getHours())}:${pad(at.getMinutes())}:${pad(at.getSeconds())}`;
}

const RETUNED = __RETUNED__;
const SHIPPED = __SHIPPED__;

let rules = RETUNED;
let sessionsFail = false;
let renamed = false;

function sessionsNow() {
    return [
        { worker_id: 'w1', name: renamed ? 'Youssef Adel Renamed' : 'Youssef Adel', site_name: 'Downtown Tower A', clock_in_time: ago(10), role: 'worker', late_flag: 0 },
        { worker_id: 'w2', name: 'Mona Samir', site_name: 'New Capital Zone B', clock_in_time: ago(8.25), role: 'moallem', late_flag: 1 },
        { worker_id: 'w3', name: 'Amr Kamal', site_name: 'Downtown Tower A', clock_in_time: ago(6), role: 'worker', late_flag: 0 },
        { worker_id: 'w4', name: 'Hana Fouad', site_name: 'New Capital Zone B', clock_in_time: ago(2), role: 'worker', late_flag: 0 }
    ];
}

function responders(url) {
    if (url.indexOf('/admin/active_sessions') >= 0) {
        return sessionsFail
            ? { status: 500, body: { detail: 'Database is locked.' } }
            : { status: 200, body: sessionsNow() };
    }
    if (url.indexOf('/admin/shift_rules') >= 0) return { status: 200, body: rules };
    if (url.indexOf('/admin/users') >= 0) {
        return { status: 200, body: [
            { id: 'w1', name: 'Youssef Adel', role: 'worker', status: 'active' },
            { id: 'w9', name: 'Layla Hassan', role: 'worker', status: 'active' },
            { id: 'a1', name: 'Seed Admin', role: 'head_admin', status: 'active' }
        ] };
    }
    if (url.indexOf('/admin/sites') >= 0) {
        return { status: 200, body: [{ site_name: 'Downtown Tower A' }, { site_name: 'New Capital Zone B' }] };
    }
    return { status: 200, body: {} };
}

const ADMIN = { id: '5000', name: 'Seed Head Admin', role: 'head_admin' };

function bootBoard(options) {
    const opts = options || {};
    rules = opts.rules || RETUNED;
    sessionsFail = !!opts.fail;
    renamed = !!opts.renamed;
    const env = boot();
    env.setResponder(responders);
    env.evaluate('State.saveUser(' + JSON.stringify({ ...ADMIN, token: 'tok-5000' }) + ')');
    return env;
}

// --- reading what was rendered -------------------------------------------
//
// The stub DOM does not parse innerHTML, so the assertion surface is the markup
// string the renderer returned - and for the parts the board repaints in place
// (only the board and the stats are ever replaced) the element the stub hands
// back, which keeps whatever the app assigned to it.

function rendered(env) {
    return env.evaluate("document.getElementById('adminContent').innerHTML");
}

function boardPane(env) {
    return env.evaluate("document.getElementById('liveOpsBoard').innerHTML");
}

function rowOf(markup, id) {
    const match = new RegExp('<tr[^>]*data-session="' + id + '"[\\\\s\\\\S]*?</tr>').exec(markup);
    return match ? match[0] : '';
}

function sessionOrder(markup) {
    return (markup.match(/data-session="([^"]*)"/g) || []).map((attr) => /"([^"]*)"/.exec(attr)[1]);
}

function rowIds(env) {
    return env.evaluate("UI_MODULES.liveOpsRows(UI_MODULES._liveOps).map((row) => row.session.worker_id)");
}

function elapsedOf(markup, id) {
    const match = /data-fact="elapsed"[^>]*>([^<]*)</.exec(rowOf(markup, id));
    return match ? match[1].trim() : null;
}

function stateOf(markup, id) {
    const match = /data-state="([^"]*)"/.exec(rowOf(markup, id));
    return match ? match[1] : null;
}

function statOf(markup, key) {
    const match = new RegExp('data-stat="' + key + '"[^>]*>([^<]*)<').exec(markup);
    return match ? match[1].trim() : null;
}

/** The derived fields of one shift, straight from the board's own arithmetic. */
function facts(hoursAgo, policy) {
    return env.evaluate(
        'UI_MODULES.liveOpsFacts(' + JSON.stringify({ clock_in_time: ago(hoursAgo), late_flag: 0 }) +
        ', ' + JSON.stringify(policy) + ')'
    );
}

const env = bootBoard();
const results = {};

// 1. the board, as an operator first sees it
{
    await env.evaluate("UI.renderAdminTab('Live Ops')");
    const markup = rendered(env);
    results.board = {
        asked_for: env.requests.map((r) => r.url.replace(/^https?:\\/\\/[^/]*\\/api\\/v1/, '')).sort(),
        has_table: markup.indexOf('data-live-ops-table') >= 0,
        has_caption: markup.indexOf('<caption') >= 0,
        has_live_region: /id="liveOpsStatus"[^>]*role="status"[^>]*aria-atomic="true"/.test(markup),
        status_sentence: env.evaluate("UI_MODULES.liveOpsStatusSentence(UI_MODULES._liveOps)"),
        status_time: env.evaluate("UI_MODULES.liveOpsStatusTime(UI_MODULES._liveOps)"),
        status_time_outside_live_region: /id="liveOpsStatus"[^>]*>[\\s\\S]*?<\\/span>\\s*<span class="ops-status-time"/.test(markup),
        on_site: statOf(markup, 'on-site'),
        longest: statOf(markup, 'longest'),
        late: statOf(markup, 'late'),
        over: statOf(markup, 'over'),
        filter_note: env.evaluate("UI_MODULES.liveOpsFilterNoteHtml(UI_MODULES._liveOps)"),
        order: sessionOrder(markup),
        aria_sort: /aria-sort="(ascending|descending)"/.test(markup),
        icons_are_svg: /<svg/.test(markup),
        emoji: (markup.match(/[\\u{1F300}-\\u{1FAFF}\\u{2190}-\\u{21FF}\\u{2600}-\\u{27BF}]/gu) || []),
        states: { w1: stateOf(markup, 'w1'), w2: stateOf(markup, 'w2'), w3: stateOf(markup, 'w3'), w4: stateOf(markup, 'w4') },
        elapsed: { w1: elapsedOf(markup, 'w1'), w2: elapsedOf(markup, 'w2'), w4: elapsedOf(markup, 'w4') },
        w2_has_late_badge: rowOf(markup, 'w2').indexOf('is-late') >= 0,
        w1_names_a_close_time: /closes at \\d{2}:\\d{2}/.test(rowOf(markup, 'w1')),
        w3_has_no_close_time: /closes at/.test(rowOf(markup, 'w3')) === false,
        rows_have_force_out: (markup.match(/data-force-out="/g) || []).length,
        // Every element the one-second tick rewrites has to be able to answer
        // "how long has this shift run?" on its own - a fact element without a
        // start is recomputed from undefined and repaints a healthy row as
        // "clock-in unreadable" a second after it renders.
        facts_without_a_start: (markup.match(/data-fact="(elapsed|bar|state)"[^>]*>/g) || [])
            .filter((tag) => tag.indexOf('data-start="') < 0),
        fact_elements: (markup.match(/data-fact="(elapsed|bar|state)"/g) || []).length,
        panel_is_disclosure: /<details[^>]*id="liveOpsForceIn"/.test(markup),
        panel_has_note: markup.indexOf('data-force-in-note') >= 0,
        panel_offers_the_free_worker: markup.indexOf('<option value="w9">') >= 0,
        panel_hides_the_one_on_shift: markup.indexOf('<option value="w1">') < 0,
        panel_hides_the_admin: markup.indexOf('<option value="a1">') < 0
    };
}

// 2. the arithmetic, under the shipped numbers
{
    const onDay = facts(8, SHIPPED);        // 8 h on site, 7.5 h paid
    const pastDay = facts(9, SHIPPED);      // 9 h on site, 8.5 h paid
    const short = facts(2, SHIPPED);        // under the break threshold
    results.facts = {
        on_day: { state: onDay.state, paid_hours: onDay.paidSeconds / 3600, percent: onDay.percent, closes: !!onDay.closesAt },
        past_day: { state: pastDay.state, paid_hours: pastDay.paidSeconds / 3600, closes: !!pastDay.closesAt },
        short: { state: short.state, paid_hours: short.paidSeconds / 3600 },
        short_is_paid_as_worked: short.paidSeconds === short.seconds,
        unreadable: env.evaluate("UI_MODULES.liveOpsFacts({ clock_in_time: 'not a date' }, {})").state,
        on_site_is_what_the_row_shows: env.evaluate("UI_MODULES.liveOpsDuration(8 * 3600)")
    };
}

// 2b. a shift past the overtime line but not yet past the paid day
{
    const over = facts(8, RETUNED);         // 7.5 h paid, line at 7.5, day at 8
    results.over_line = {
        state: over.state,
        paid_hours: over.paidSeconds / 3600,
        paid_line: env.evaluate(
            'UI_MODULES.liveOpsPaidLineHtml(UI_MODULES.liveOpsFacts(' +
            JSON.stringify({ clock_in_time: ago(8), late_flag: 0 }) + ', ' + JSON.stringify(RETUNED) + '))'
        )
    };
}

// 2c. an operator who turned auto-close off gets no "this closes" claim
{
    const relaxed = facts(10, { ...SHIPPED, auto_close_at_regular: 0 });
    results.auto_close_off = { state: relaxed.state, closes: !!relaxed.closesAt };
}

// 3. search and the site chips
{
    const env2 = bootBoard();
    await env2.evaluate("UI.renderAdminTab('Live Ops')");
    env2.evaluate("UI_MODULES.setLiveOpsQuery('new capital')");
    results.search = {
        rows: rowIds(env2),
        note: env2.evaluate("UI_MODULES.liveOpsFilterNoteHtml(UI_MODULES._liveOps)"),
        query_kept_in_state: env2.evaluate("UI_MODULES._liveOpsQuery + '|' + State.liveOpsQuery"),
        only_the_board_repainted: env2.evaluate("UI_MODULES._liveOpsRun")
    };

    env2.evaluate("UI_MODULES.setLiveOpsQuery('nobody by this name')");
    const none = env2.evaluate("UI_MODULES.liveOpsBoardHtml(UI_MODULES._liveOps)");
    results.search_none = {
        empty_state: none.indexOf('data-empty="filtered"') >= 0,
        has_clear: none.indexOf('data-clear-filters') >= 0,
        no_rows: sessionOrder(none).length === 0,
        note: env2.evaluate("UI_MODULES.liveOpsFilterNoteHtml(UI_MODULES._liveOps)")
    };

    env2.evaluate("UI_MODULES.clearLiveOpsFilters()");
    results.search_cleared = { rows: rowIds(env2) };

    env2.evaluate("UI_MODULES.setLiveOpsSite('Downtown Tower A')");
    // The chips are what ``liveOpsChipsHtml`` paints into the toolbar - the same
    // function, read back after the filter moved, so this is the state a screen
    // reader would get (the stub DOM cannot be asked about aria-pressed itself).
    const chips = env2.evaluate("UI_MODULES.liveOpsChipsHtml(UI_MODULES._liveOps)");
    results.site_filter = {
        rows: rowIds(env2),
        note: env2.evaluate("UI_MODULES.liveOpsFilterNoteHtml(UI_MODULES._liveOps)"),
        chip_pressed_markup: /data-site-chip="Downtown Tower A"[^>]*aria-pressed="true"/.test(chips),
        all_chip_unpressed: /data-site-chip=""[^>]*aria-pressed="false"/.test(chips),
        chips: chips
    };
    env2.evaluate("UI_MODULES.clearLiveOpsFilters()");
    results.site_filter_cleared = { rows: rowIds(env2) };
}

// 4. sorting: longest first by default, then name, then the toggle
{
    const env3 = bootBoard();
    await env3.evaluate("UI.renderAdminTab('Live Ops')");
    const longest = rowIds(env3);
    env3.evaluate("UI_MODULES.setLiveOpsSort('name')");
    const byName = rowIds(env3);
    // From another column, the On-site header starts at its natural order...
    env3.evaluate("UI_MODULES.setLiveOpsSort('elapsed')");
    const backToLongest = rowIds(env3);
    // ...and re-tapping it flips the direction.
    env3.evaluate("UI_MODULES.setLiveOpsSort('elapsed')");
    const newest = rowIds(env3);
    env3.evaluate("UI_MODULES.setLiveOpsSort('elapsed')");
    results.sorting = { longest, by_name: byName, back_to_longest: backToLongest, newest, again: rowIds(env3) };
}

// 5. nobody on shift: an explanation and one way forward, not a blank panel
{
    const env4 = bootBoard();
    env4.setResponder((url) => {
        if (url.indexOf('/admin/active_sessions') >= 0) return { status: 200, body: [] };
        return responders(url);
    });
    await env4.evaluate("UI.renderAdminTab('Live Ops')");
    const markup = rendered(env4);
    results.nobody = {
        empty_state: markup.indexOf('data-empty="nobody"') >= 0,
        cta: markup.indexOf('data-force-in-cta') >= 0,
        which_empty_state: env4.evaluate("UI_MODULES.liveOpsBoardHtml(UI_MODULES._liveOps).indexOf('data-empty=\\"nobody\\"') >= 0"),
        on_site: statOf(markup, 'on-site'),
        longest: statOf(markup, 'longest'),
        status_sentence: env4.evaluate("UI_MODULES.liveOpsStatusSentence(UI_MODULES._liveOps)"),
        panel_still_there: markup.indexOf('data-force-in-note') >= 0
    };
}

// 6. a board that could not be read says so, and offers the retry
{
    const env5 = bootBoard({ fail: true });
    await env5.evaluate("UI.renderAdminTab('Live Ops')");
    const markup = rendered(env5);
    results.failure = {
        error_block: markup.indexOf('ops-error') >= 0,
        retry: markup.indexOf("UI.renderAdminTab('Live Ops')") >= 0,
        server_text_shown: markup.indexOf('Database is locked.') >= 0,
        timers_started: env5.evaluate("[UI_MODULES._liveOpsTick, UI_MODULES._liveOpsPoll]")
    };
}

// 7. the phone layout says the same thing as the table
{
    const env6 = bootBoard();
    await env6.evaluate("UI.renderAdminTab('Live Ops')");
    const cards = env6.evaluate("UI_MODULES.liveOpsCardsHtml(UI_MODULES.liveOpsRows(UI_MODULES._liveOps))");
    results.cards = {
        cards: (cards.match(/data-session="/g) || []).length,
        has_table: cards.indexOf('<table') >= 0,
        closing_named: cards.indexOf('is-closing') >= 0,
        over_named: cards.indexOf('is-over') >= 0,
        late_named: cards.indexOf('is-late') >= 0,
        force_out: (cards.match(/data-force-out="/g) || []).length,
        elapsed_present: cards.indexOf('data-fact="elapsed"') >= 0
    };
    const mobile = env6.evaluate("UI_MODULES.liveOpsSortSelectHtml()");
    results.mobile_sort = { has_select: mobile.indexOf('id="liveOpsSort"') >= 0, options: (mobile.match(/<option/g) || []).length };
}

// 8. the timers start with the board and die with it
{
    const env7 = bootBoard();
    await env7.evaluate("UI.renderAdminTab('Live Ops')");
    const started = env7.evaluate("[UI_MODULES._liveOpsTick !== null, UI_MODULES._liveOpsPoll !== null]");
    await env7.evaluate("UI.renderAdminTab('Sites')");
    const afterSwitch = env7.evaluate("[UI_MODULES._liveOpsTick, UI_MODULES._liveOpsPoll]");
    await env7.evaluate("UI.renderAdminTab('Live Ops')");
    await env7.evaluate("UI.logout()");
    results.lifecycle = {
        started,
        after_switch: afterSwitch,
        after_logout: env7.evaluate("[UI_MODULES._liveOpsTick, UI_MODULES._liveOpsPoll]")
    };
}

// 9. the poll: silent when nothing changed, a repaint when something did
{
    const env8 = bootBoard();
    await env8.evaluate("UI.renderAdminTab('Live Ops')");
    // A search that is half-typed is exactly the state a careless repaint destroys.
    env8.evaluate("UI_MODULES.setLiveOpsQuery('youssef')");
    env8.evaluate("UI_MODULES._liveOpsProbe = UI_MODULES._liveOps");
    env8.evaluate("UI_MODULES._liveOpsPaneBefore = document.getElementById('liveOpsBoard').innerHTML");
    await env8.evaluate("UI_MODULES.pollLiveOps()");
    results.poll = {
        state_untouched: env8.evaluate("UI_MODULES._liveOps === UI_MODULES._liveOpsProbe"),
        pane_unchanged: env8.evaluate(
            "document.getElementById('liveOpsBoard').innerHTML === UI_MODULES._liveOpsPaneBefore"
        ),
        pane_has_rows: env8.evaluate("document.getElementById('liveOpsBoard').innerHTML.indexOf('data-session') >= 0"),
        query_kept: env8.evaluate("UI_MODULES._liveOpsQuery"),
        reads: env8.requests.filter((r) => r.url.indexOf('/admin/active_sessions') >= 0).length
    };

    renamed = true;
    await env8.evaluate("UI_MODULES.pollLiveOps()");
    results.poll_after_change = {
        state_replaced: env8.evaluate("UI_MODULES._liveOps !== UI_MODULES._liveOpsProbe"),
        repainted_with_the_new_name: env8.evaluate(
            "document.getElementById('liveOpsBoard').innerHTML.indexOf('Renamed') >= 0"
        ),
        query_still_kept: env8.evaluate("UI_MODULES._liveOpsQuery")
    };
}

// 10. every new string exists in all three languages
{
    results.translations = JSON.parse(env.evaluate(
        "JSON.stringify(Object.keys(TRANSLATIONS).map((lang) => [lang," +
        "  Object.keys(TRANSLATIONS[lang]).filter((key) => key.indexOf('liveOps') === 0).length," +
        "  Object.keys(TRANSLATIONS.en).filter((key) => key.indexOf('liveOps') === 0 && !(key in TRANSLATIONS[lang])).join(',')]))"
    ));
    // Every placeholder the board substitutes must exist in every language, or an
    // Arabic operator reads a literal "{count}". The *set* is compared, not the
    // count: one language substituting one key fewer is exactly the bug.
    results.placeholders = JSON.parse(env.evaluate(
        "JSON.stringify(Object.keys(TRANSLATIONS).map((lang) => [lang," +
        "  Object.keys(TRANSLATIONS[lang]).filter((key) => key.indexOf('liveOps') === 0 && " +
        "  /\\\\{(workers|sites|time|paid|day|shown|total|count)\\\\}/.test(TRANSLATIONS[lang][key])).sort().join(',')]))"
    ));
}
"""


@pytest.fixture(scope="module")
def results() -> dict:
    body = HARNESS.replace("__RETUNED__", repr(RETUNED).replace("'", '"')).replace(
        "__SHIPPED__", repr(SHIPPED).replace("'", '"')
    )
    return frontend_vm.run(body)


# ---------------------------------------------------------------------------
# What the operator sees
# ---------------------------------------------------------------------------
def test_the_board_asks_for_the_shift_list_the_roster_the_sites_and_the_rules(results):
    asked = results["board"]["asked_for"]
    for path in ("/admin/active_sessions", "/admin/users", "/admin/sites", "/admin/shift_rules"):
        assert path in asked, f"the board is missing {path}: {asked}"


def test_the_board_leads_with_the_figures_an_operator_came_for(results):
    board = results["board"]
    assert board["on_site"] == "4"
    assert board["longest"] == "10h 0m", "the longest shift is the one that needs a decision"
    assert board["late"] == "1"
    assert board["over"] == "2", "past the paid day, plus past the overtime line"
    assert board["filter_note"] == "4 open shifts"


def test_the_worst_shift_is_the_first_row(results):
    """Newest-first buries exactly the row an operator opened the board to find."""
    assert results["board"]["order"] == ["w1", "w2", "w3", "w4"], results["board"]["order"]


def test_each_shift_is_labelled_with_what_it_has_crossed(results):
    states = results["board"]["states"]
    assert states["w1"] == "closing", "10 h on site is past the paid day"
    assert states["w2"] == "over", "7.75 paid hours is past the 7.5 h overtime line"
    assert states["w3"] == "on" and states["w4"] == "on"
    assert results["board"]["w2_has_late_badge"], "the late arrival is named, not just coloured"


def test_a_closing_shift_names_the_moment_it_closes(results):
    board = results["board"]
    assert board["w1_names_a_close_time"], (
        "an operator deciding whether to leave a shift alone needs the time the "
        "system will close it"
    )
    assert board["w3_has_no_close_time"], "and a shift on an ordinary day must not claim one"


def test_every_live_fact_can_recompute_itself_from_the_dom(results):
    """The tick reads its inputs off the element it is about to change.

    The first version put the clock-in time only on the elapsed span, so the
    state badge - which the same tick also rewrites - was recomputed from
    ``undefined`` and painted "clock-in unreadable" over every row one second
    after it rendered. Only a browser showed it; this is the guard.
    """
    board = results["board"]
    assert board["fact_elements"] >= 12, "the board should have facts to tick"
    assert board["facts_without_a_start"] == [], (
        f"these tick targets cannot recompute themselves: {board['facts_without_a_start']}"
    )


def test_the_live_timers_read_as_hours_and_minutes(results):
    elapsed = results["board"]["elapsed"]
    assert elapsed["w1"] == "10h 0m", elapsed
    assert elapsed["w2"] == "8h 15m", elapsed
    assert elapsed["w4"] == "2h 0m", elapsed
    assert results["facts"]["on_site_is_what_the_row_shows"] == "8h 0m"


def test_the_table_is_a_table_a_screen_reader_can_use(results):
    board = results["board"]
    assert board["has_table"] and board["has_caption"], "a data table needs a caption and headers"
    assert board["aria_sort"], "the sorted column has to say so"
    assert board["rows_have_force_out"] == 4, "one action per person, reachable from the row"


# ---------------------------------------------------------------------------
# The arithmetic
# ---------------------------------------------------------------------------
def test_paid_hours_are_what_the_payslip_will_say(results):
    facts = results["facts"]
    assert facts["on_day"]["paid_hours"] == pytest.approx(7.5, abs=0.01), (
        "8 h on site less the 30-minute unpaid break is 7.5 h paid"
    )
    assert facts["on_day"]["state"] == "on", "and 7.5 paid hours is not overtime"
    assert facts["on_day"]["percent"] == 94, (
        "the bar is paid hours against the paid day: 7.5 of 8 h, so an ordinary "
        "shift does not read as nearly over"
    )
    assert facts["past_day"]["paid_hours"] == pytest.approx(8.5, abs=0.01)
    assert facts["past_day"]["state"] == "closing" and facts["past_day"]["closes"] is True


def test_a_short_shift_is_paid_as_worked(results):
    facts = results["facts"]
    assert facts["short"]["paid_hours"] == pytest.approx(2.0, abs=0.01), (
        "a two-hour shift cannot have contained an unpaid break"
    )
    assert facts["short_is_paid_as_worked"] is True


def test_a_shift_whose_clock_in_cannot_be_read_is_not_guessed_at(results):
    assert results["facts"]["unreadable"] == "unknown"


def test_past_the_overtime_line_is_not_yet_the_end_of_the_day(results):
    """The two thresholds are separate, and only one of them closes a shift."""
    over = results["over_line"]
    assert over["state"] == "over", over
    assert over["paid_hours"] == pytest.approx(7.5, abs=0.01)
    assert "closes at" not in over["paid_line"], (
        f"a shift before the paid day must not claim a closing time: {over['paid_line']}"
    )


def test_turning_auto_close_off_removes_the_closing_claim(results):
    """Nothing is going to close that shift, so the board must not say it will."""
    assert results["auto_close_off"] == {"state": "over", "closes": False}, results["auto_close_off"]


# ---------------------------------------------------------------------------
# Finding one person
# ---------------------------------------------------------------------------
def test_search_narrows_the_board_and_keeps_what_was_typed(results):
    search = results["search"]
    assert search["rows"] == ["w2", "w4"], "both New Capital Zone B shifts, longest first"
    assert search["note"] == "Showing 2 of 4"
    assert search["query_kept_in_state"] == "new capital|new capital", (
        "the query survives a repaint, in the module and in State"
    )


def test_a_search_that_matches_nobody_says_so_and_offers_a_way_back(results):
    none = results["search_none"]
    assert none["empty_state"] and none["has_clear"] and none["no_rows"], none
    assert none["note"] == "Showing 0 of 4"
    assert results["search_cleared"]["rows"] == ["w1", "w2", "w3", "w4"]


def test_the_site_chips_filter_and_announce_which_one_is_on(results):
    site = results["site_filter"]
    assert site["rows"] == ["w1", "w3"], "only the Downtown Tower A shifts, longest first"
    assert site["note"] == "Showing 2 of 4"
    assert site["chip_pressed_markup"], "the pressed chip says so for a screen reader"
    assert site["all_chip_unpressed"], "and All sites is no longer pressed"
    for label, count in (("All sites", 4), ("Downtown Tower A", 2), ("New Capital Zone B", 2)):
        assert f"{label} ({count})" in site["chips"], (
            f"each chip carries how many are on that site: {site['chips']}"
        )
    assert results["site_filter_cleared"]["rows"] == ["w1", "w2", "w3", "w4"]


def test_sorting_cycles_through_longest_name_and_newest(results):
    sorting = results["sorting"]
    assert sorting["longest"] == ["w1", "w2", "w3", "w4"]
    assert sorting["by_name"] == ["w3", "w4", "w2", "w1"], sorting["by_name"]
    assert sorting["back_to_longest"] == ["w1", "w2", "w3", "w4"], (
        "tapping the On-site header from another column starts at longest, not newest"
    )
    assert sorting["newest"] == ["w4", "w3", "w2", "w1"], "re-tapping flips the direction"
    assert sorting["again"] == ["w1", "w2", "w3", "w4"], "and tapping again flips back"


# ---------------------------------------------------------------------------
# States
# ---------------------------------------------------------------------------
def test_nobody_on_shift_explains_itself_and_offers_the_force_in(results):
    nobody = results["nobody"]
    assert nobody["empty_state"], "an empty board is a state, not a blank panel"
    assert nobody["which_empty_state"], "and it is the nobody-on-site state, not 'no matches'"
    assert nobody["cta"], "it offers the one action that is possible"
    assert nobody["panel_still_there"], "the force-in panel stays reachable"
    assert nobody["on_site"] == "0"
    assert nobody["longest"] == "\u2014", "no shift means no longest shift, not 0h 0m"
    assert nobody["status_sentence"] == "On site now: 0. Sites with people: 0."


def test_a_board_that_could_not_be_loaded_says_so_and_offers_a_retry(results):
    failure = results["failure"]
    assert failure["error_block"] and failure["retry"], failure
    assert failure["server_text_shown"], "the reason is shown, not swallowed"
    assert failure["timers_started"] == [None, None], "a failed board must not poll forever"


def test_the_phone_layout_carries_the_same_facts_as_the_table(results):
    cards = results["cards"]
    assert cards["cards"] == 4 and not cards["has_table"]
    assert cards["closing_named"] and cards["over_named"] and cards["late_named"], (
        "colour is never the only signal"
    )
    assert cards["force_out"] == 4
    assert cards["elapsed_present"], "the phone gets the live timer too"
    assert results["mobile_sort"]["has_select"] and results["mobile_sort"]["options"] == 3, (
        "cards have no column headers, so the phone needs the sort control"
    )


# ---------------------------------------------------------------------------
# Live without flicker, and no leaks
# ---------------------------------------------------------------------------
def test_the_timers_start_with_the_board_and_stop_when_it_is_left(results):
    lifecycle = results["lifecycle"]
    assert lifecycle["started"] == [True, True], "the board starts both its tick and its poll"
    assert lifecycle["after_switch"] == [None, None], "switching tabs stops them"
    assert lifecycle["after_logout"] == [None, None], "logging out stops them"


def test_a_poll_that_finds_no_change_repaints_nothing(results):
    poll = results["poll"]
    assert poll["state_untouched"], "the board state is not replaced when nothing changed"
    assert poll["pane_has_rows"], "the board really is on screen (a control for the next assert)"
    assert poll["pane_unchanged"], "and nothing on screen moves"
    assert poll["query_kept"] == "youssef", "so a half-typed search survives the poll"
    assert poll["reads"] == 2, "one read for the render, one for the poll"


def test_a_poll_that_finds_a_change_repaints_only_the_board(results):
    changed = results["poll_after_change"]
    assert changed["state_replaced"], "a real change is picked up"
    assert changed["repainted_with_the_new_name"], (
        "the pane shows the new name without the renderer being re-run"
    )
    assert changed["query_still_kept"] == "youssef", "without resetting the filter"


# ---------------------------------------------------------------------------
# The accessibility floor, and the languages
# ---------------------------------------------------------------------------
def test_the_status_line_is_a_sentence_in_a_single_live_region(results):
    board = results["board"]
    assert board["has_live_region"], "the count has to be announced, not just drawn"
    assert board["status_sentence"] == "On site now: 4. Sites with people: 2."
    assert board["status_time"].startswith("Updated ")
    assert board["status_time_outside_live_region"], (
        "the freshness stamp sits outside the live region: a timestamp changing "
        "every 45 s is not news"
    )


def test_icons_are_vectors_and_never_emoji(results):
    assert results["board"]["icons_are_svg"], "the board draws its own icons"
    assert results["board"]["emoji"] == [], (
        f"an emoji renders differently on every phone and cannot be themed: {results['board']['emoji']}"
    )


def test_the_force_in_panel_is_a_disclosure_that_stays_in_the_document(results):
    board = results["board"]
    assert board["panel_is_disclosure"], "progressive disclosure keeps the board the subject"
    assert board["panel_has_note"], "and it still explains itself"
    assert board["panel_offers_the_free_worker"], "the worker with no shift is the point of it"
    assert board["panel_hides_the_one_on_shift"], (
        "someone already on shift is on the board; the panel is for the ones who are not"
    )
    assert board["panel_hides_the_admin"], "an administrator cannot be forced onto a site"


def test_every_new_string_exists_in_all_three_languages(results):
    counts = {lang: count for lang, count, _absent in results["translations"]}
    missing = {lang: absent for lang, _count, absent in results["translations"] if absent}
    assert missing == {}, f"untranslated Live Ops strings: {missing}"
    assert len(set(counts.values())) == 1, f"the languages disagree on the key set: {counts}"
    assert counts.get("en", 0) >= 39, f"the board lost its strings: {counts}"


def test_the_placeholders_every_language_substitutes_are_present(results):
    sets = {lang: keys for lang, keys in results["placeholders"]}
    assert len(set(sets.values())) == 1, (
        f"a language is missing a placeholder the board replaces: {sets}"
    )
    assert sets["en"].count("liveOps") >= 5, f"the board substitutes almost nothing: {sets['en']}"


def test_the_board_has_a_dark_variant_of_every_colour_token():
    """Dark mode is a token swap, so a missing override is a silent contrast bug.

    The board draws on a couple of dozen ``--ops-*`` tokens. If a colour token is
    defined only in the light theme, a dark-mode operator gets a light surface
    behind dark text, on the screen with the most text in the console. Every
    colour token the light theme sets is therefore re-declared for dark.
    """
    # ``html.dark,`` (with the comma) is the board's dark token block; the bare
    # ``html.dark { color-scheme: dark }`` rule above it is not.
    boundary = STYLE.index("html.dark,")
    light = set(re.findall(r"(--ops-[a-z0-9-]+):", STYLE[:boundary])) - {"--ops-mono"}
    dark = set(re.findall(r"(--ops-[a-z0-9-]+):", STYLE[boundary:]))
    assert light, "the board has no design tokens at all"
    assert light - dark == set(), f"tokens with no dark-theme value: {sorted(light - dark)}"


def test_the_motion_the_board_has_can_be_turned_off():
    """Every animation on the board has a still equivalent."""
    assert "prefers-reduced-motion" in STYLE, "the board animates with no way out"
    reduced = STYLE[STYLE.index("prefers-reduced-motion"):]
    for animated in (".ops-live-dot", ".ops-skeleton-bar", ".ops-progress > span"):
        assert animated in reduced, f"{animated} keeps moving under reduced motion"
    assert "animation: none" in reduced
