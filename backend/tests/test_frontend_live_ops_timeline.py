"""The Live Ops timeline: today's arrivals and departures, and the scrubber over them.

WHAT THIS GUARDS
----------------
The board answers "who is on site right now" - one moment, always the newest one. The
timeline answers the question underneath it: how the site *filled up and emptied*, as one
bar per shift from its clock-in to its clock-out, under a curve of how many people were on
site at any minute of the day. An operator can stand the scrubber anywhere in the day and
read the headcount at that moment.

Three properties are the ones a redesign gets wrong quietly:

* **the day's history is not paid for until it is asked for.** Closing shifts live in
  ``/admin/reports/shifts``, a whole day of timesheet rows, and the board must not fetch it
  to draw the moment it is already showing - the read happens once, when the timeline is
  first opened, and never on a plain board render;
* **the headcount is arithmetic, not markup.** The count at a minute is a pure function of
  the lanes, so it is asserted directly at fixed minutes rather than inferred from pixels -
  including the boundaries, where a bar is open at its own arrival and closed at its own
  departure;
* **the timeline and the list are the same region.** ``#liveOpsBoard`` draws one or the
  other, which is what lets the poll, Refresh and the fold keep working without knowing
  which reading is on screen.

The suite drives the shipped frontend through the stub DOM the other console suites use.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import frontend_vm
from harness import PROJECT_ROOT

#: The board's own files, for the one test that has to read the keys out of the source
#: rather than out of the rendered page.
MODULE = (PROJECT_ROOT / "frontend" / "admin_modules.js").read_text(encoding="utf-8", errors="ignore")


def _tables() -> dict[str, str]:
    """The words each language reads, as the union of every file that carries them.

    English may be split. The console's own strings were moved out of ``i18n.js`` into a
    console-only table the browser fetches beside it (``admin_i18n.js``), so "what English has"
    is every file that supplies English rather than one path - and a language's text is read
    from whatever files exist, so this holds for the split and for the arrangement before it.
    """
    paths = {
        "en": ["i18n.js", "admin_i18n.js"],
        "ar": ["i18n.ar.js"],
        "hi": ["i18n.hi.js"],
        "ur": ["i18n.ur.js"],
    }
    return {
        code: "\n".join(
            (PROJECT_ROOT / "frontend" / name).read_text(encoding="utf-8", errors="ignore")
            for name in names
            if (PROJECT_ROOT / "frontend" / name).exists()
        )
        for code, names in paths.items()
    }


TABLES = _tables()

HARNESS = r"""
// --- the day the board is looking at -------------------------------------
//
// Stamps are built on *today's* local date at a fixed wall-clock time, so the minutes the
// timeline derives are the same number whenever the suite runs: 06:00 is always minute 360.
// Nothing here reads the clock for its expectations - the arithmetic tests below are handed
// the minute they are about.

function pad(value) { return String(value).padStart(2, '0'); }

function at(hours, minutes, dayOffset) {
    const when = new Date();
    when.setDate(when.getDate() + (dayOffset || 0));
    return `${when.getFullYear()}-${pad(when.getMonth() + 1)}-${pad(when.getDate())} ` +
        `${pad(hours)}:${pad(minutes)}:00`;
}

function today() {
    const when = new Date();
    return `${when.getFullYear()}-${pad(when.getMonth() + 1)}-${pad(when.getDate())}`;
}

// Two shifts that finished today, and one still open: the smallest board that has a
// departure, an arrival and a bar that reaches the right edge.
const CLOSED = [
    { worker_id: 'w1', worker_name: 'Youssef Adel', site_name: 'Downtown Tower A', arrival_time: at(6, 0), timestamp: at(14, 0), hours: 7.5, arrival_verdict: 'on_time', arrival_minutes: 0, role: 'worker', status_code: 'approved', status: 'Approved' },
    { worker_id: 'w2', worker_name: 'Mona Samir', site_name: 'New Capital Zone B', arrival_time: at(7, 0), timestamp: at(12, 0), hours: 4.5, arrival_verdict: 'late', arrival_minutes: 12, role: 'moallem', status_code: 'approved', status: 'Approved' }
];

const OPEN = [
    { worker_id: 'w3', name: 'Amr Kamal', site_name: 'Downtown Tower A', clock_in_time: at(8, 0), role: 'worker', late_flag: 0 }
];

// A shift that began yesterday evening and is still open: the overnight case, which the
// timeline has to draw on the left of one axis rather than at 22:00 on the wrong side of noon.
const OVERNIGHT = [
    { worker_id: 'w9', name: 'Salma Nabil', site_name: 'Salmiya Block 4', clock_in_time: at(22, 0, -1), role: 'worker', late_flag: 0 }
];

let closed = CLOSED;
let sessions = OPEN;
let timelineFail = false;
let timelineReads = 0;

function responder(url) {
    if (url.indexOf('/admin/reports/shifts') >= 0) {
        timelineReads += 1;
        if (timelineFail) return { status: 500, body: { detail: 'Database is locked.' } };
        return { status: 200, body: { rows: closed, totals: {}, categories: [] } };
    }
    if (url.indexOf('/admin/active_sessions') >= 0) return { status: 200, body: sessions };
    if (url.indexOf('/admin/live_ops/count') >= 0) return { status: 200, body: {} };
    if (url.indexOf('/admin/shift_rules') >= 0) {
        return { status: 200, body: { regular_hours: 8.0, overtime_notify_hours: 8.1, break_minutes: 30.0, break_after_hours: 4.0, auto_close_at_regular: 1 } };
    }
    if (url.indexOf('/admin/sites') >= 0) {
        return { status: 200, body: [{ site_name: 'Downtown Tower A' }, { site_name: 'New Capital Zone B' }, { site_name: 'Salmiya Block 4' }] };
    }
    return { status: 200, body: {} };
}

function bootBoard(options) {
    const opts = options || {};
    closed = opts.closed || CLOSED;
    sessions = opts.sessions || OPEN;
    timelineFail = !!opts.timelineFail;
    timelineReads = 0;
    const env = boot();
    env.setResponder(responder);
    env.evaluate("State.saveUser({ id: '5000', name: 'Seed Head Admin', role: 'head_admin', token: 'tok-5000' })");
    return env;
}

// The pane's own region, read back the way the app left it: ``getElementById`` hands back
// the same memoized node the app wrote to, so this is the board or the timeline as painted.
function board(env) {
    return env.evaluate("document.getElementById('liveOpsBoard').innerHTML");
}

function pane(env) {
    return env.evaluate("document.getElementById('adminContent').innerHTML");
}

// The switch, driven the way the toolbar drives it: one click, delegated.
function open(env, view) {
    return env.evaluate(
        "UI_MODULES.onLiveOpsClick({ target: { closest: (sel) => sel === '[data-live-ops-view]'" +
        " ? { dataset: { liveOpsView: '" + view + "' } } : null } })"
    );
}

// One step of the scrubber, through the delegated input handler.
function scrub(env, value) {
    return env.evaluate(
        "UI_MODULES.onLiveOpsChange({ target: { getAttribute: (name) => (name === 'data-live-ops-scrub' ? '' : null)," +
        " value: '" + value + "' } })"
    );
}

const results = {};

// 1. the board alone: what it reads, and what a plain render never pays for
//
// Read off the whole pane, not off ``#liveOpsBoard``: the first render writes the region as
// part of the pane's markup string, and only a repaint (the switch, the poll, a scrub) ever
// writes that element directly.
{
    const env = bootBoard();
    await env.evaluate("UI.renderAdminTab('Live Ops')");
    const markup = pane(env);
    results.board = {
        asked_for: env.requests.map((r) => r.url.replace(/^https?:\/\/[^/]*\/api\/v1/, '')).sort(),
        timeline_reads: timelineReads,
        view: env.evaluate("UI_MODULES.liveOpsView()"),
        on_the_board: markup.indexOf('data-live-ops-timeline') >= 0,
        rows: (markup.match(/data-session=/g) || []).length,
        site_strip: (markup.match(/data-live-ops-occupancy/g) || []).length,
        switch_offered: markup.indexOf('data-live-ops-view="timeline"') >= 0,
        board_half_pressed: markup.indexOf('data-live-ops-view="board" aria-pressed="true"') >= 0
    };
}

// 2. the switch: one read of the day, and the timeline painted in the board's own region
{
    const env = bootBoard();
    await env.evaluate("UI.renderAdminTab('Live Ops')");
    const before = timelineReads;
    await open(env, 'timeline');
    const markup = board(env);
    const asked = env.requests.map((r) => r.url.replace(/^https?:\/\/[^/]*\/api\/v1/, ''));
    results.timeline = {
        reads_opening: timelineReads - before,
        view: env.evaluate("UI_MODULES.liveOpsView()"),
        title: markup.indexOf('data-live-ops-timeline') >= 0,
        curve: markup.indexOf('data-live-ops-curve') >= 0,
        curve_points: (markup.match(/,\d+(\.\d+)?(?= )/g) || []).length,
        lanes: (markup.match(/data-lane="/g) || []).length,
        open_lanes: (markup.match(/data-open="true"/g) || []).length,
        closed_ends: (markup.match(/data-end="\d+"/g) || []).length,
        scrub: markup.indexOf('data-live-ops-scrub') >= 0,
        scrub_aria: markup.indexOf('aria-valuetext') >= 0,
        axis: (markup.match(/class="ops-tick"/g) || []).length,
        note: env.evaluate("document.getElementById('liveOpsFilterNote').textContent"),
        site_strip: (markup.match(/data-live-ops-occupancy/g) || []).length,
        list_tools_hidden: env.evaluate("document.getElementById('liveOpsBoardTools').hidden"),
        the_day_was_asked_for: asked.filter((url) => url.indexOf('/admin/reports/shifts?start=' + today() + '&end=' + today()) >= 0).length,
        lane_names: (markup.match(/data-lane="([^"]+)"/g) || [])
    };
}

// 3. the arithmetic, at fixed minutes, with no DOM in the way
{
    const env = bootBoard();
    await env.evaluate("UI.renderAdminTab('Live Ops')");
    await open(env, 'timeline');
    const entries = env.evaluate("JSON.stringify(UI_MODULES.liveOpsTimelineEntries(UI_MODULES._liveOps))");
    const lane = (id) => env.evaluate(
        "JSON.stringify(UI_MODULES.liveOpsTimelineEntries(UI_MODULES._liveOps).filter((e) => e.worker_id === '" + id + "')[0])"
    );
    const count = (minute) => env.evaluate(
        "UI_MODULES.liveOpsTimelineCount(UI_MODULES.liveOpsTimelineEntries(UI_MODULES._liveOps), " + minute + ")"
    );
    const window = (now) => env.evaluate(
        "JSON.stringify(UI_MODULES.liveOpsTimelineWindow(UI_MODULES.liveOpsTimelineEntries(UI_MODULES._liveOps), " + now + "))"
    );
    const series = (now) => env.evaluate(
        "JSON.stringify(UI_MODULES.liveOpsTimelineSeries(UI_MODULES.liveOpsTimelineEntries(UI_MODULES._liveOps), UI_MODULES.liveOpsTimelineWindow(UI_MODULES.liveOpsTimelineEntries(UI_MODULES._liveOps), " + now + ")))"
    );
    results.math = {
        entries: JSON.parse(entries),
        w1: JSON.parse(lane('w1')),
        w3: JSON.parse(lane('w3')),
        // Walls of the day, on the three lanes below: 06:00-14:00 (w1), 07:00-12:00 (w2) and
        // 08:00 until now (w3). So the count steps 0 -> 1 -> 2 -> 3 -> 2 -> 1 as the morning
        // passes, and the departure minute is where a boundary bug would show.
        at_0559: count(359),
        at_0600: count(360),
        at_0700: count(420),
        at_0800: count(480),
        at_1200: count(720),
        at_1300: count(780),
        at_1400: count(840),
        at_2200: count(1320),
        window: JSON.parse(window(900)),
        window_early: JSON.parse(window(60)),
        series_tail: JSON.parse(series(900)).slice(-1)[0],
        series_peak: Math.max.apply(null, JSON.parse(series(900)).map((s) => s.count))
    };
}

// 4. the overnight arrival: one axis, so yesterday evening is a negative minute
{
    const env = bootBoard({ sessions: OVERNIGHT, closed: [] });
    await env.evaluate("UI.renderAdminTab('Live Ops')");
    await open(env, 'timeline');
    results.overnight = {
        minute: env.evaluate("Math.round(UI_MODULES.liveOpsTimelineEntries(UI_MODULES._liveOps)[0].start)"),
        window: JSON.parse(env.evaluate("JSON.stringify(UI_MODULES.liveOpsTimelineWindow(UI_MODULES.liveOpsTimelineEntries(UI_MODULES._liveOps), 600))")),
        lanes: (board(env).match(/data-lane="/g) || []).length,
        // A negative start must not produce a negative width: the lane is clamped to the
        // left edge of the window rather than drawn off the chart.
        negative_widths: (board(env).match(/width:-/g) || []).length
    };
}

// 5. scrubbing: the headline moves, the lanes are re-sorted, and the input is never rebuilt
{
    const env = bootBoard();
    await env.evaluate("UI.renderAdminTab('Live Ops')");
    await open(env, 'timeline');
    // At rest everything is read off the region's markup, because that is where the first
    // paint put it: only a scrub (or a poll's repaint) writes these elements directly.
    const markup = board(env);
    const atRest = {
        headline: (markup.match(/id="liveOpsTimelineAt"[^>]*>([^<]*)</) || [])[1],
        now_offered: /id="liveOpsNow"[^>]*hidden/.test(markup),
        scrub_min: (markup.match(/id="liveOpsScrub"[^>]*min="(\d+)"/) || [])[1],
        scrub_max: (markup.match(/id="liveOpsScrub"[^>]*max="(\d+)"/) || [])[1],
        scrub_value: (markup.match(/id="liveOpsScrub"[^>]*value="(\d+)"/) || [])[1],
        scrub_step: (markup.match(/id="liveOpsScrub"[^>]*step="(\d+)"/) || [])[1]
    };
    scrub(env, '390');
    const scrubbed = {
        headline: env.evaluate("document.getElementById('liveOpsTimelineAt').textContent"),
        // Now a property, because scrubbing is what puts it there.
        now_hidden: env.evaluate("document.getElementById('liveOpsNow').hidden"),
        body: env.evaluate("document.getElementById('liveOpsTimelineBody').innerHTML"),
        moment: env.evaluate("UI_MODULES._liveOpsMoment")
    };
    // The Reset-to-now button, delegated like everything else in the pane.
    env.evaluate("UI_MODULES.onLiveOpsClick({ target: { closest: (sel) => (sel === '[data-live-ops-now]' ? { dataset: {} } : null) } })");
    results.scrub = {
        at_rest: atRest,
        headline: scrubbed.headline,
        now_hidden_while_scrubbing: scrubbed.now_hidden,
        moment: scrubbed.moment,
        present: (scrubbed.body.match(/is-present/g) || []).length,
        absent: (scrubbed.body.match(/is-absent/g) || []).length,
        first_lane: (scrubbed.body.match(/data-lane="([^"]+)"/) || [])[1],
        back_to_now: env.evaluate("UI_MODULES._liveOpsMoment"),
        headline_after_reset: env.evaluate("document.getElementById('liveOpsTimelineAt').textContent")
    };
}

// 6. a day that has not started, and a read that failed
{
    const quiet = bootBoard({ sessions: [], closed: [] });
    await quiet.evaluate("UI.renderAdminTab('Live Ops')");
    await open(quiet, 'timeline');
    const markup = board(quiet);
    results.empty = {
        empty: markup.indexOf('data-empty="timeline"') >= 0,
        back: markup.indexOf('data-live-ops-view="board"') >= 0
    };
}
{
    const broken = bootBoard({ timelineFail: true });
    await broken.evaluate("UI.renderAdminTab('Live Ops')");
    await open(broken, 'timeline');
    const markup = board(broken);
    results.failed = {
        said: markup.indexOf('data-empty="timeline-failed"') >= 0,
        retry_is_the_timelines: markup.indexOf('data-live-ops-timeline-retry') >= 0,
        left_the_view: broken.evaluate("UI_MODULES.liveOpsView()")
    };
    // Its own retry: the read is asked for again, and the view stays where the reader was.
    timelineFail = false;
    await broken.evaluate("UI_MODULES.retryLiveOpsTimeline()");
    results.failed.after_retry = board(broken).indexOf('data-live-ops-timeline') >= 0;
    results.failed.view_after_retry = broken.evaluate("UI_MODULES.liveOpsView()");
}

// 7. leaving the tab puts the board back, and forgets the day
{
    const env = bootBoard();
    await env.evaluate("UI.renderAdminTab('Live Ops')");
    await open(env, 'timeline');
    scrub(env, '390');
    env.evaluate("UI_MODULES.stopLiveOps()");
    results.reset = {
        view: env.evaluate("UI_MODULES.liveOpsView()"),
        moment: env.evaluate("UI_MODULES._liveOpsMoment"),
        day: env.evaluate("UI_MODULES._liveOpsTimeline"),
        site: env.evaluate("UI_MODULES._liveOpsTimelineSite")
    };
}

// 8. one gate's day: the picker narrows the chart, its window, its count and its note
{
    const env = bootBoard();
    await env.evaluate("UI.renderAdminTab('Live Ops')");
    await open(env, 'timeline');
    // The picker as the timeline's header paints it, the same read the board's own picker
    // suites make: options, counts and the selected one, from the shipped function.
    const picker = env.evaluate("UI_MODULES.liveOpsTimelineSiteSelectHtml(UI_MODULES._liveOps)");
    // Choose a gate the way the picker does: one delegated ``change``.
    const choose = (value) => env.evaluate(
        "UI_MODULES.onLiveOpsChange({ target: { getAttribute: (name) => (name === 'data-live-ops-timeline-site' ? '' : null)," +
        " value: '" + value + "' } })"
    );
    const countAt = (minute) => env.evaluate(
        "UI_MODULES.liveOpsTimelineCount(UI_MODULES.liveOpsTimelineEntries(UI_MODULES._liveOps), " + minute + ")"
    );
    const windowFrom = (now) => env.evaluate(
        "UI_MODULES.liveOpsTimelineWindow(UI_MODULES.liveOpsTimelineEntries(UI_MODULES._liveOps), " + now + ").from"
    );

    // The whole deployment first, so the narrowing has a number to be narrower than.
    const all = { lanes: (board(env).match(/data-lane="/g) || []).length, at_0800: countAt(480) };

    // Downtown Tower A: two of today's three shifts - one closed, one still open. The moment
    // is chosen first, because a gate change must not throw away where the operator is.
    scrub(env, '390');
    choose('Downtown Tower A');
    const downtownMarkup = board(env);
    const downtown = {
        state: env.evaluate("UI_MODULES._liveOpsTimelineSite"),
        lanes: (downtownMarkup.match(/data-lane="/g) || []).length,
        lane_names: downtownMarkup.match(/data-lane="[^"]+"/g) || [],
        at_0800: countAt(480),
        note: env.evaluate("document.getElementById('liveOpsFilterNote').textContent"),
        moment_kept: env.evaluate("UI_MODULES._liveOpsMoment"),
        option_selected: downtownMarkup.indexOf('value="Downtown Tower A" selected') >= 0
    };

    // New Capital Zone B: one shift, and a window anchored to that gate's own first arrival
    // (07:00 here) rather than to the deployment's (06:00).
    choose('New Capital Zone B');
    const newCapital = {
        lanes: (board(env).match(/data-lane="/g) || []).length,
        window_from: windowFrom(900)
    };

    // Salmiya Block 4: a gate the registry knows and today did not visit.
    choose('Salmiya Block 4');
    const salmiyaMarkup = board(env);
    const absent = {
        empty: salmiyaMarkup.indexOf('data-empty="timeline-site"') >= 0,
        title: (salmiyaMarkup.match(/ops-empty-title">([^<]*)</) || [])[1],
        picker: salmiyaMarkup.indexOf('data-live-ops-timeline-site') >= 0,
        clear: salmiyaMarkup.indexOf('data-live-ops-timeline-all') >= 0
    };

    // ...and the way back to the whole deployment, delegated like every other control in the pane.
    env.evaluate("UI_MODULES.onLiveOpsClick({ target: { closest: (sel) => (sel === '[data-live-ops-timeline-all]' ? { dataset: {} } : null) } })");

    results.scope = {
        picker: picker,
        all: all,
        downtown: downtown,
        new_capital: newCapital,
        absent: absent,
        cleared: {
            state: env.evaluate("UI_MODULES._liveOpsTimelineSite"),
            lanes: (board(env).match(/data-lane="/g) || []).length
        }
    };
}
"""


@pytest.fixture(scope="module")
def results() -> dict:
    """The whole scenario, run once: one VM, seven boards, one JSON answer."""
    return frontend_vm.run(HARNESS)


# ---------------------------------------------------------------------------
# the board is not made to pay for the day
# ---------------------------------------------------------------------------
def test_the_board_still_does_not_read_the_days_history(results):
    """The timeline's read is deferred until the timeline is asked for.

    ``/admin/reports/shifts`` over a day is a timesheet: every shift closed today, with the
    joins that grade each arrival. A board whose whole job is to draw the current moment must
    not fetch it - so this asserts both halves: the paths the board does ask for, and the one
    it does not.
    """
    board = results["board"]
    for path in ("/admin/active_sessions", "/admin/live_ops/count", "/admin/sites", "/admin/shift_rules"):
        assert path in board["asked_for"], f"the board is missing {path}: {board['asked_for']}"
    assert not [url for url in board["asked_for"] if url.startswith("/admin/reports/shifts")], (
        f"a plain board render read the day's timesheet: {board['asked_for']}"
    )
    assert board["timeline_reads"] == 0, "the board fetched today's closed shifts to draw the current moment"
    assert board["view"] == "board", "the board opens on the list"
    assert board["on_the_board"] is False
    assert board["rows"] == 1, "the open shift is on the board, where it belongs"
    # The switch is offered from the first paint - the timeline is a reading of the board, not
    # a hidden mode - and the list half is the one pressed.
    assert board["switch_offered"] is True
    assert board["board_half_pressed"] is True


def test_opening_the_timeline_reads_today_once_and_paints_it_where_the_board_was(results):
    """One read of the day, and the same region draws the other reading."""
    timeline = results["timeline"]
    assert timeline["reads_opening"] == 1, "opening the timeline reads the day exactly once"
    assert timeline["the_day_was_asked_for"] == 1, "and asks for today, not some other day"
    assert timeline["view"] == "timeline"
    assert timeline["title"], "the timeline is in the document, named"
    assert timeline["curve"], "the shape of the day is drawn"
    assert timeline["scrub"], "and the scrubber is in it"
    assert timeline["scrub_aria"], "the scrubber states the moment it is standing on, in words"
    assert timeline["axis"] >= 2, "the axis is a ruler, not a single mark"
    assert timeline["list_tools_hidden"] is True, (
        "a search box that narrows a list nobody is looking at is a control that looks broken"
    )
    # The three tiles stay - they are the figures an operator came for - but the strip under
    # them is a breakdown of *those* figures, and over a chart standing at 04:30 a "now"
    # breakdown would be read as the answer for 04:30.
    assert results["board"]["site_strip"] >= 1, "the strip belongs to the board's own reading"
    assert timeline["site_strip"] == 0, "the timeline has no use for a figure about now"


def test_every_shift_that_touched_today_gets_a_lane(results):
    """Two closed and one open: three lanes, and the open one is drawn as open."""
    timeline = results["timeline"]
    assert timeline["lanes"] == 3, f"one lane per shift that touched today: {timeline['lane_names']}"
    assert timeline["open_lanes"] == 1, "the open shift is marked as still running"
    assert timeline["closed_ends"] == 2, "the two finished shifts carry a clock-out"
    assert timeline["note"] == "1 still on site · 2 finished today", timeline["note"]


# ---------------------------------------------------------------------------
# the headcount at a minute
# ---------------------------------------------------------------------------
def test_the_lanes_carry_the_arrival_and_the_departure_as_minutes(results):
    """06:00 -> 14:00 is minute 360 -> 840, whatever the wall clock says when this runs."""
    math = results["math"]
    assert math["w1"]["start"] == 360 and math["w1"]["end"] == 840, math["w1"]
    assert math["w1"]["open"] is False
    assert math["w3"]["start"] == 480, "the open shift's own clock-in is its start"
    assert math["w3"]["end"] is None, "and it has not ended: null is not zero"
    assert math["w3"]["open"] is True
    assert len(math["entries"]) == 3


def test_the_headcount_steps_up_and_down_across_the_day(results):
    """The whole point of the view, asserted at the walls rather than off the drawing.

    ``start`` is inclusive and ``end`` exclusive, so a lane is *on site at* its own arrival
    minute and already gone on its own departure minute - the two minutes a boundary bug hides
    in. The three lanes here are 06:00-14:00, 07:00-12:00 and 08:00-until-now.
    """
    math = results["math"]
    assert math["at_0559"] == 0, "nobody is here before the first arrival"
    assert math["at_0600"] == 1, "the arrival counts from its own minute"
    assert math["at_0700"] == 2, "two on site once the second shift starts"
    assert math["at_0800"] == 3, "and three at the peak, while all of them overlap"
    assert math["at_1200"] == 2, "one left at noon: the departure minute is already gone"
    assert math["at_1300"] == 2, "the two still open carry the afternoon between them"
    assert math["at_1400"] == 1, "and the second departure leaves the open shift alone"
    assert math["at_2200"] == 1, (
        "the open lane never ends - which is why its bar reaches the right edge of the chart"
    )


def test_the_window_starts_an_hour_before_the_first_arrival_and_ends_at_now(results):
    """At 06:00 a window from midnight would spend three quarters of its width on nothing."""
    math = results["math"]
    assert math["window"]["from"] == 300, "an hour of lead-in before the first bar"
    assert math["window"]["to"] == 900, "and the axis ends where the reader is standing"
    assert math["series_tail"]["minute"] == 900, "the curve is sampled right up to the end"
    assert math["series_peak"] == 3, "the shape peaks at the hour all three overlapped"
    # A window asked for before there is any history is still a window, never an empty span.
    assert math["window_early"]["from"] == 300 and math["window_early"]["to"] >= 300


def test_an_overnight_arrival_is_drawn_on_the_left_of_one_axis(results):
    """A shift that began yesterday evening is a negative minute, clamped - not 22:00 backward."""
    overnight = results["overnight"]
    assert overnight["minute"] == -120, "22:00 yesterday is two hours before today's midnight"
    assert overnight["window"]["from"] == 0, "the axis never starts before midnight"
    assert overnight["lanes"] == 1, "and the lane is still drawn"
    assert overnight["negative_widths"] == 0, "a clamped bar cannot come out inverted"


# ---------------------------------------------------------------------------
# scrubbing
# ---------------------------------------------------------------------------
def test_the_scrubber_moves_the_moment_and_never_rebuilds_itself(results):
    """The headline and the lanes follow the scrubber; the input does not.

    A range input rewritten under a thumb mid-drag loses the drag, so only the timeline's
    body is replaced - which is why the count and the present/absent classes are read off the
    body element rather than off the whole region.
    """
    scrub = results["scrub"]
    assert scrub["at_rest"]["now_offered"] is True, "at rest the board is at now: nothing to reset"
    assert "Now" in scrub["at_rest"]["headline"], scrub["at_rest"]["headline"]
    # The scrubber is a bounded range over the window the board chose, stepping in minutes -
    # the axis and the slider cannot disagree about where the day starts and ends.
    assert scrub["at_rest"]["scrub_step"] == "5", scrub["at_rest"]
    assert int(scrub["at_rest"]["scrub_min"]) >= 0, scrub["at_rest"]
    assert int(scrub["at_rest"]["scrub_max"]) <= 1440, scrub["at_rest"]
    assert int(scrub["at_rest"]["scrub_value"]) == int(scrub["at_rest"]["scrub_max"]), (
        "at rest the thumb stands on now, which is where the axis ends"
    )
    assert scrub["headline"] == "At 06:30 · 1 on site", scrub["headline"]
    assert scrub["moment"] == 390, "the moment is kept as a value a suite can read"
    assert scrub["now_hidden_while_scrubbing"] is False, "scrubbing offers the way back to now"
    assert scrub["present"] == 1 and scrub["absent"] == 2, (scrub["present"], scrub["absent"])
    assert scrub["first_lane"] == "w1", "whoever was on site at the moment sorts first"
    assert scrub["back_to_now"] is None, "the reset clears the moment rather than pinning it"
    assert "Now" in scrub["headline_after_reset"], scrub["headline_after_reset"]


# ---------------------------------------------------------------------------
# one gate's day
# ---------------------------------------------------------------------------
def test_the_timeline_offers_every_gate_and_the_whole_deployment(results):
    """The picker is the timeline's own: the board's list-only toolbar is hidden up here.

    Its options come from the site registry as well as the day, so an operator can ask "what
    happened at this gate" before anything has happened there - which is exactly the Salmiya
    case below, and would be impossible if the options were only the sites that are busy.
    """
    picker = results["scope"]["picker"]
    assert "data-live-ops-timeline-site" in picker, "the picker is the delegated control"
    for name in ("All sites", "Downtown Tower A", "New Capital Zone B", "Salmiya Block 4"):
        assert name in picker, f"the picker does not offer {name}: {picker}"


def test_choosing_a_gate_narrows_the_lanes_and_the_headcount_to_it(results):
    """Three shifts today across two gates; one gate holds two of them.

    The count is the assertion that matters: at 08:00 the deployment had three people on site,
    and Downtown Tower A had two of them - so a chart that still says three is a chart that
    ignored the picker. The lanes are checked by id for the same reason the count is checked at
    a minute: names and order are presentation, the set is the answer.
    """
    scope = results["scope"]
    assert scope["all"]["lanes"] == 3, "the unfiltered timeline draws every shift"
    assert scope["all"]["at_0800"] == 3
    assert scope["downtown"]["state"] == "Downtown Tower A"
    assert scope["downtown"]["lanes"] == 2, scope["downtown"]["lane_names"]
    assert 'data-lane="w1"' in scope["downtown"]["lane_names"]
    assert 'data-lane="w3"' in scope["downtown"]["lane_names"]
    assert scope["downtown"]["at_0800"] == 2, "the chart is still counting the other gate"
    assert scope["downtown"]["option_selected"] is True, "the picker does not show the choice"


def test_a_gate_is_a_window_and_a_note_of_its_own(results):
    """The axis starts an hour before *that gate's* first arrival, and the note names it.

    New Capital Zone B's only shift starts at 07:00, so its window opens at 06:00 while the
    deployment's opens at 05:00 (Downtown's first arrival is 06:00) - the same anchoring rule,
    applied to the subset. The note is where a reader who scrolled past the picker learns they
    are looking at one gate and not the company.
    """
    scope = results["scope"]
    assert scope["new_capital"]["lanes"] == 1
    assert scope["new_capital"]["window_from"] == 360, "the window did not follow the gate"
    assert scope["downtown"]["note"] == "Downtown Tower A · 1 still on site · 1 finished today", (
        scope["downtown"]["note"]
    )


def test_changing_the_gate_keeps_the_moment_the_operator_scrubbed_to(results):
    """An operator who asked about 06:30 and then chose a gate is still asking about 06:30."""
    assert results["scope"]["downtown"]["moment_kept"] == 390


def test_a_gate_with_nothing_today_is_a_named_emptiness_not_the_whole_deployment(results):
    """"Nothing to draw" over one gate must not read as "nothing happened anywhere"."""
    absent = results["scope"]["absent"]
    assert absent["empty"] is True, "the empty state is the gate's, not the day's"
    assert "Salmiya Block 4" in absent["title"], absent["title"]
    assert absent["picker"] is True, "the picker stays, so another gate is one tap away"
    assert absent["clear"] is True, "and every gate is one tap away too"


def test_clearing_the_gate_puts_the_whole_deployment_back(results):
    scope = results["scope"]
    assert scope["cleared"]["state"] == "", "the picker did not clear"
    assert scope["cleared"]["lanes"] == 3, "the chart did not widen back to every gate"


# ---------------------------------------------------------------------------
# the two ends of the day
# ---------------------------------------------------------------------------
def test_a_day_that_has_not_started_says_so_and_offers_the_way_back(results):
    """An empty timeline is a named emptiness with an exit, not a blank chart."""
    assert results["empty"]["empty"] is True
    assert results["empty"]["back"] is True, "the empty state can put the reader back on the list"


def test_a_day_that_could_not_be_read_keeps_the_reader_where_they_were(results):
    """The failure is the timeline's, and so is the retry: re-rendering the tab would leave."""
    failed = results["failed"]
    assert failed["said"] is True, "the timeline says it could not read the day"
    assert failed["retry_is_the_timelines"] is True
    assert failed["left_the_view"] == "timeline", "a failed read does not throw the reader back"
    assert failed["after_retry"] is True, "and its own retry reads the day and draws it"
    assert failed["view_after_retry"] == "timeline"


def test_leaving_the_tab_puts_the_board_back_and_forgets_the_day(results):
    """The view is per visit, like the fold and the pause - and the day is re-read next time."""
    reset = results["reset"]
    assert reset["view"] == "board", "returning to the tab opens on the moment it is for"
    assert reset["moment"] is None, "and at now, not wherever the scrubber was left"
    assert reset["day"] is None, "today's shifts are not carried across visits"
    assert reset["site"] == "", "nor is the gate the last visit narrowed to"


# ---------------------------------------------------------------------------
# the strings
# ---------------------------------------------------------------------------
#: Every string the timeline asks for, named here rather than scraped out of the module: a
#: key passed as a variable (the switch's own two labels, the headline's two sentences) would
#: not be found by a regex over the call sites, and a list that quietly stops covering them is
#: the failure this test exists to catch.
TIMELINE_KEYS = (
    "liveOpsViewLabel", "liveOpsViewBoard", "liveOpsViewTimeline",
    "liveOpsTimelineTitle", "liveOpsTimelineNowLine", "liveOpsTimelineAtLine",
    "liveOpsTimelineNow", "liveOpsTimelineScrubLabel", "liveOpsTimelineStillHere",
    "liveOpsTimelinePresent", "liveOpsTimelineAbsent", "liveOpsTimelineLoading",
    "liveOpsTimelineEmptyTitle", "liveOpsTimelineEmpty", "liveOpsTimelineBack",
    "liveOpsTimelineError", "liveOpsTimelineNote",
    # The scope picker and its narrow empty state: the board's own site keys are reused for
    # the label and the "every gate" option, so they are part of this contract too.
    "liveOpsSiteFilter", "liveOpsAllSites",
    "liveOpsTimelineSiteEmptyTitle", "liveOpsTimelineSiteEmpty", "liveOpsTimelineSiteNote",
)


def test_every_timeline_string_is_asked_for_and_defined_in_every_language(results):
    """A key the board asks for that a table has not got renders as the key itself.

    Read out of the source rather than the rendered page: the languages a visitor has not
    chosen are never loaded, so the only way to hold all four accountable is to look the words
    up in each table - in both directions, so neither a string nobody uses nor a string nobody
    translated can survive.
    """
    for key in TIMELINE_KEYS:
        assert f"'{key}'" in MODULE, f"the board asks for {key}, which nothing in the module names"
    for code, table in TABLES.items():
        missing = [key for key in TIMELINE_KEYS if f'"{key}":' not in table]
        assert not missing, f"{code} is missing {missing}"
    # The count placeholder is part of the sentence in every language, or one of them prints a
    # number that is never shown.
    for key in ("liveOpsTimelineNowLine", "liveOpsTimelineAtLine"):
        for code, table in TABLES.items():
            line = re.search(rf'"{key}":\s*"([^"]*)"', table)
            assert line and "{count}" in line.group(1), f"{code}:{key} lost its count placeholder"
