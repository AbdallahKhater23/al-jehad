"""The console's front door: one counted read, one view at a time, and what happens when one fails.

WHY THIS EXISTS
---------------
The dashboard is the screen every console role now lands on, so what can only break *in the
browser* is exactly what this file exercises:

1. **It is the landing screen, and it is first in the tab list** - ``renderAdminTab`` falls back
   to the first tab a reader is offered, so "first" is the whole mechanism;
2. **one read on paint.** Not four: the whole reason this endpoint exists is that the board next
   door downloads four full payloads on a poll loop to count what is in them, and a dashboard
   built the same way would pay the cost it was built to remove. The requests are asserted, not
   the intention;
3. **it does not poll.** The stamp is drawn and the screen says so in words, because an operator
   who assumes these figures are live is an operator acting on a figure from five minutes ago;
   the strip beside it then ages honestly - each figure carries the read time it shares with the
   one read, and past the window the strip is marked stale rather than printing an old number as
   though it were current - and the tick that does that repaints the age without fetching anything;
4. **a panel the server could not read is ``null``, and is drawn as such** - never as a zero
   standing in for it, which reads as good news on the one screen somebody checks to decide
   whether anything is wrong;
5. **the alert queue is drawn only where it can be read.** The server answers ``null`` for it to
   anybody but the root tier, and the tab is ``rootOnly``: both halves are checked, and neither
   drawn row would be a control that only ever answers "not you";
6. **every number's link goes to the tab that owns that queue**, and the numbers themselves are
   the server's - nothing on this screen is derived in the browser. The ``now`` panel is the
   case that matters most, because it is the panel that *borrows*: ``on_shift`` is the Live Ops
   board's own figure, the crossing is the Approvals queue's, and ``offline_waiting`` has no tab
   to link to at all - a button there would be a control into a screen that does not list it;
7. the refusal count is drawn as what it is: a rate to watch, with the sentence that says so,
   rather than a queue an operator is meant to work down;
8. text the server wrote is text, not markup: a site called ``<img ...>`` must not become an
   image in the middle of the places panel *or* of the by-site split;
9. the tab's own chrome is translated in all four languages (the parity suite counts the keys;
   what is asserted here is that the *screen* asks the table for them rather than hard-coding);
10. the period panel summarizes *a window*, so the window travels with the figures: the control
   says which one is in effect and asks the server for another when it is tapped, and each of the
   two linkages opens that person's own rows over exactly that window -
   in the tab that owns attendance rows, which is the plan's "a link into the context rather than a
   leaderboard".
11. **the screen does not stack, it switches.** Five cards down a page puts the answer below the
   fold, so the shell is a vital strip that never leaves and one view at a time; switching is a
   redraw from the snapshot already in hand, which is why a switch must make *no* request and must
   leave the stamp where it was - the alternative is a request per tap for an answer this page
   already holds. The tabs are a real ``tablist``, so the arrow keys have to walk them.12. **the day strip makes a number answerable.** "132 present days" does not say which days came
    apart, so the window is drawn day by day against its own busiest day - and the late count is a
    numeral on the day it happened rather than a colour, because colour alone is not a fact.
13. **the two figures that are a watch rather than a headcount** are drawn with the window they
    were counted over, taken from the payload rather than from a constant in the console; and the
    period card answers "is this window ready to be paid" in words, with the queue that clears it.
14. **the window can hand itself over as a file.** The one control on this screen that leaves the
    app, so it is the one place the suite has to read what was actually downloaded: the URL (this
    window), the token (the route is ``admin_only``), and the bytes (the server's answer, not
    something assembled here).


Node is optional; without it these skip rather than fail.
"""

from __future__ import annotations

import pytest

import frontend_vm

pytestmark = pytest.mark.skipif(frontend_vm.NODE is None, reason="node is not installed")

HARNESS = r"""
// --- the server's answers, faked ----------------------------------------

// A site name and a category name are both free text somebody typed, and they are the only
// *lists* of server values this page draws - so one of them is markup on purpose.
const HOSTILE = '<img src=x onerror=alert(1)>';

const DAY = { start: '2026-09-28', end: '2026-09-29', timezone: 'Asia/Kuwait', today: '2026-09-28' };

function panel(overrides) {
    return Object.assign({
        status: 'success',
        as_of: '2026-09-28 14:03:11',
        // The server's own freshness windows, sent with the read. Deliberately present and
        // deliberately *not* the console's fallback pair, so a strip that aged from a constant
        // instead of the payload fails here rather than in somebody's trust of a number.
        freshness: { aging_seconds: 90, stale_seconds: 300 },
        day: DAY,
        people: {
            accounts: 41, active: 38, pending_approval: 2, deactivated: 3,
            by_role: { worker: 30, moallem: 4, off_office: 2, admin: 1, head_admin: 1 },
            enrolled: 36, no_face: 5, no_password: 0, new_this_week: 4, never_clocked_in: 2,
            // The two watch figures, and the windows they were counted over - deliberately *not*
            // the console's fallbacks (30 and 7), so a label that printed a constant instead of
            // the server's own window fails here rather than in somebody's headcount review.
            onboarding: 3, dormant: 6, dormant_days: 45, onboarding_days: 10
        },
        // The moment: the board's own figure, and the three ways a punch can be waiting. The
        // by-site list carries the hostile name too - it is the second *list* of server text
        // this page draws.
        now: {
            on_shift: 12,
            by_site: [{ site_name: 'Depot', workers: 4 }, { site_name: HOSTILE, workers: 1 }],
            overtime_open: 1, offline_waiting: 3, refused_24h: 2
        },
        places: {
            sites: 3, categories: 2,
            by_category: [{ category: 'Depot', sites: 2 }, { category: HOSTILE, sites: 1 }],
            no_category: 0, overriding_window: 1,
            unmanned_today: ['Site C', HOSTILE]
        },
        waiting: { reviews: 4, registrations: 2, notes: 1, alerts: null, oldest_seconds: 8100 },
        // The period: a window rather than a moment, so every figure arrives with the dates it
        // describes - and the two linkages the plan asks for, each one tap from that person's own
        // rows. The second linkage's *name* is markup, because this is the third place the page
        // turns server text into a row (see the escaping test).
        period: {
            days: 7,
            preset: 'days',
            start: '2026-09-22',
            end: '2026-09-28',
            workers: 31,
            expected_days: 5,
            present_days: 132,
            late_arrivals: 4,
            average_attendance_rate: 0.85,
            approved_hours: 488.5,
            overtime_hours: 6.0,
            awaiting_approval_hours: 12.0,
            // The count behind those hours: "12 h waiting" does not say whether that is one
            // shift or four, and four decisions is a different afternoon's work.
            awaiting_approval_shifts: 3,
            // The shape of the window, as the server counted it: one entry per day, and these
            // seven add up to ``present_days`` above - so the fixture cannot describe a week the
            // strip and the figure beside it disagree about.
            by_day: [
                { day: '2026-09-22', present: 20, late: 0 },
                { day: '2026-09-23', present: 24, late: 1 },
                { day: '2026-09-24', present: 18, late: 0 },
                { day: '2026-09-25', present: 26, late: 2 },
                { day: '2026-09-26', present: 12, late: 1 },
                { day: '2026-09-27', present: 0, late: 0 },
                { day: '2026-09-28', present: 32, late: 0 }
            ],
            quietest: [{ worker_id: '43', worker_name: 'Row 43', present_days: 1, rate: 0.2, late: 2 }],
            most_late: [{ worker_id: '12', worker_name: HOSTILE, present_days: 4, rate: 0.8, late: 3 }]
        }
    }, overrides || {});
}

//: What the root tier's own read looks like: the same payload with the alert queue filled in.
const ROOT_PANEL = panel();

let reads = 0;              // how many times the dashboard was asked for this scenario
let answered = null;        // the body to answer with; null means "the shared one"
let failure = null;         // {status, detail} to answer the next read with
let exportUrl = null;       // the export the window's download actually asked for

function responders(url, init) {
    // The export route, which answers a *file* rather than a payload: the two are told apart by
    // what the console does with the answer, and this is the only place either file exists. The
    // Excel half answers with a marker no CSV row could contain - a PK header - because that is the
    // difference between the console's two savers, and the whole reason it has two.
    if (url.indexOf('/admin/reports/export') >= 0) {
        exportUrl = String(url);
        if (url.indexOf('format=xlsx') >= 0) {
            return {
                status: 200,
                body: 'PK\x00xlsx\x00',
                contentType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
            };
        }
        return { status: 200, body: 'Employee,id,site,hours\r\nRow 1,1,Depot,8\r\n', contentType: 'text/csv' };
    }
    if (url.indexOf('/admin/dashboard') >= 0) {
        reads += 1;
        if (failure) {
            const refused = failure;
            failure = null;
            return { status: refused.status, body: { detail: refused.detail } };
        }
        // A second read carries a *later* stamp, so "the refresh re-read" is asserted on the
        // thing a reader can see rather than on a request count alone.
        const body = answered || panel();
        if (reads > 1) body.as_of = '2026-09-28 14:06:02';
        return { status: 200, body: body };
    }
    return { status: 200, body: {} };
}

// --- reading the rendered page -----------------------------------------

function textOf(markup) {
    return markup.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();
}

/** One fact off a panel, by the field name the server sent. */
function fact(markup, name) {
    const found = new RegExp('data-dashboard-fact="' + name + '">([^<]*)<').exec(markup);
    return found ? found[1] : null;
}

function panelOf(markup, name) {
    const found = new RegExp('<section class="ui-card dashboard-panel" data-dashboard-panel="' + name + '"[\\s\\S]*?</section>').exec(markup);
    return found ? found[0] : '';
}

function render(env) {
    return env.evaluate("document.getElementById('adminContent').innerHTML");
}

const HEAD_ADMIN = { id: '5000', name: 'Head Admin', role: 'head_admin', token: 'tok-admin' };
const DEVELOPER = { id: '309010401073', name: 'Developer', role: 'developer', token: 'tok-dev' };

function consoleEnv(who) {
    reads = 0;
    answered = null;
    failure = null;
    const env = boot();
    env.setResponder(responders);
    env.evaluate("State.saveUser(" + JSON.stringify(who || HEAD_ADMIN) + ")");
    return env;
}

const results = {};

/**
 * The dashboard as every view draws it: one paint, then a switch per view.
 *
 * The screen shows one view at a time, so a suite that asks about three of them has to go and
 * get them. Switching makes *no* request - that is what it means to redraw from the snapshot -
 * so every string in the map describes the same read, which is what makes an assertion about one
 * view and another view's figure a statement about one deployment rather than about two reads.
 * ``reads`` is the count of requests the paint itself made: the switch must have added none.
 */
async function eachView(who, overrides) {
    const env = consoleEnv(who);
    if (overrides) answered = overrides;
    await env.evaluate("UI.renderAdminTab('Dashboard')");
    const views = { waiting: render(env), env: env, reads: reads };
    for (const id of ['now', 'people', 'places', 'period']) {
        env.evaluate("UI_MODULES.dashboardSetMetric('" + id + "')");
        views[id] = render(env);
    }
    views.reads_after_switching = reads;
    // Every view as one string, for the questions that are about "anywhere on this screen".
    views.all = ['waiting', 'now', 'people', 'places', 'period'].map((id) => views[id]).join('\n');
    return views;
}

// 1. the tab is first on the rail, and it is where an administrator lands
{
    const env = consoleEnv();
    results.rail = env.evaluate(`
        [ADMIN_TABS[0].id, ADMIN_TABS[0].key, ADMIN_TABS[0].hint, ADMIN_TABS[0].group, ADMIN_TABS[0].icon,
         adminVisibleTabs()[0].id,
         typeof ADMIN_ICONS.dashboard === 'string' && ADMIN_ICONS.dashboard.indexOf('<svg') === 0,
         State.adminTab === ADMIN_TABS[0].id]
    `);
    await env.evaluate("UI.paintAdminConsole()");
    results.landing = {
        tab: env.evaluate("State.adminTab"),
        // Landing is not enough on its own: the screen has to be the one that painted.
        page: render(env).indexOf('data-dashboard="true"') >= 0,
        reads: reads
    };

    // ...and the root tier lands on the same screen, not on its own tools.
    const root = consoleEnv(DEVELOPER);
    await root.evaluate("UI.paintAdminConsole()");
    results.root_landing = { tab: root.evaluate("State.adminTab"), reads: reads };
}

// 2. one read, then the three panels - and the numbers are the server's
{
    const env = consoleEnv();
    await env.evaluate("UI.renderAdminTab('Dashboard')");
    const markup = render(env);
    results.paint = {
        flags: markup.indexOf('data-dashboard="true"') >= 0,
        // One view is drawn at a time now, so this is a single name: the tab that says it is
        // selected and the panel in the DOM have to be the same view, or the screen is lying.
        panel: (/data-dashboard-panel="([^"]*)"/.exec(markup) || [])[1] ?? null,
        panels: (markup.match(/data-dashboard-panel="/g) || []).length,
        // The tab that says it is selected comes *before* the hook that names the view, which is
        // why this reads them in that order: the attribute order is the markup's, not the test's.
        selected: (/<button[^>]*aria-selected="true"[^>]*data-dashboard-metric="([^"]*)"/.exec(markup) || [])[1] ?? null,
        switch_tabs: (markup.match(/data-dashboard-metric="/g) || []).length,
        // The strip that never switches away: five figures, each a link into the tab that owns it.
        vitals: {
            tiles: (markup.match(/data-dashboard-vital="/g) || []).length,
            facts: ['reviews', 'registrations', 'on_shift', 'present_days', 'awaiting_approval_hours']
                .map((id) => (new RegExp('data-dashboard-vital-fact="' + id + '">([^<]*)<').exec(markup) || [])[1] ?? null),
            linked: (markup.match(/data-dashboard-vital="[^"]*"[^>]*data-dashboard-go="/g) || []).length,
            present_note: (/data-dashboard-vital="present_days"[\s\S]*?dashboard-vital-note">([^<]*)</.exec(markup) || [])[1] ?? null,
            warned: markup.indexOf('class="dashboard-vital is-warn" data-dashboard-vital="awaiting_approval_hours"') >= 0,
            // Where a tile goes is no longer a line of grey text under it, so the one thing that
            // has to survive the shortening is the tile *saying* it: the button's accessible name.
            // Read off each tile's own opening tag, because the queue rows below carry one too.
            spoken: (markup.match(/<button[^>]*data-dashboard-vital="[^"]*"[^>]*>/g) || [])
                .map((tag) => (/aria-label="([^"]*)"/.exec(tag) || [])[1] ?? '')
                .filter((label) => /Shifts to review|On shift now/.test(label))
        },
        requests: env.requests.map((r) => r.url),
        waiting: {
            reviews: fact(markup, 'reviews'),
            registrations: fact(markup, 'registrations'),
            notes: fact(markup, 'notes'),
            oldest: (/data-dashboard-oldest="([^"]*)"/.exec(markup) || [])[1],
            queues: (markup.match(/data-dashboard-queue="/g) || []).length
        },
        // The other four views are switched to in the scenarios below - one redraw each, and not
        // one extra request - because a view that is not on top is not in the DOM to be read.
        stamp: (/data-dashboard-asof="true">([\s\S]*?)<\/p>/.exec(markup) || [])[1],
        text: textOf(markup),
        links: (markup.match(/data-dashboard-go="([^"]*)"/g) || []).map((m) => /"([^"]*)"/.exec(m.slice(18))[1])
    };
}

// 2a. freshness: one read's age, and that an old read says so rather than printing a number
{
    const env = consoleEnv();
    await env.evaluate("UI.renderAdminTab('Dashboard')");
    const freshMarkup = render(env);
    const readsAtPaint = reads;
    // Age the *read*, not the figures: this is the clock moving over one snapshot, so the redraw
    // must rebuild the markup from the payload already in hand and cost no request at all.
    const aged = await env.evaluate(`(() => {
        UI_MODULES._dashboardReadAt = Date.now() - 400000;
        UI_MODULES.dashboardSetMetric('now');
        return document.getElementById('adminContent').innerHTML;
    })()`);
    // The chip is repainted in place from the clock, which is the tick's whole job.
    const chip = await env.evaluate(`(() => {
        UI_MODULES.paintDashboardFreshness();
        const node = document.getElementById('dashboardFreshness');
        return { text: node.textContent, className: node.className };
    })()`);
    // The boundaries are the server's, not this module's. Hold the read's age still (400 s) and
    // move only the window on the wire: a longer aging window must say "fresh" where the
    // console's own constant would already say "stale", a wider stale window must make the
    // middle word reachable, and a payload that omits the block - or sends a zero or a negative
    // - must fall back to the console's constants rather than reading every figure as stale.
    const windowsMoved = await env.evaluate(`(() => {
        UI_MODULES._dashboardReadAt = Date.now() - 400000;
        const verdict = (freshness) => UI_MODULES.dashboardFresh(
            freshness === undefined ? {} : { freshness: freshness }
        );
        return {
            from_long_aging: verdict({ aging_seconds: 600, stale_seconds: 900 }),
            from_wide_stale: verdict({ aging_seconds: 300, stale_seconds: 900 }),
            from_omitted: verdict(undefined),
            from_junk: verdict({ aging_seconds: 0, stale_seconds: -5 })
        };
    })()`);
    const chipOf = (markup) => {
        const found = /<span class="ops-fresh dashboard-fresh is-([a-z]+)" id="dashboardFreshness"[^>]*>([^<]*)</.exec(markup);
        return found ? [found[1], found[2]] : null;
    };
    const tileTitleOf = (markup) => (/data-dashboard-vital="reviews"[^>]*title="([^"]*)"/.exec(markup) || [])[1] ?? null;
    const tileAriaOf = (markup) => (/<button[^>]*data-dashboard-vital="reviews"[^>]*aria-label="([^"]*)"/.exec(markup) || [])[1] ?? null;
    results.freshness = {
        fresh_chip: chipOf(freshMarkup),
        fresh_read_at: (/data-dashboard-read-at="([^"]*)"/.exec(freshMarkup) || [])[1] ?? null,
        fresh_title: tileTitleOf(freshMarkup),
        fresh_aria: tileAriaOf(freshMarkup),
        reads: reads - readsAtPaint,
        aged_chip: chipOf(aged),
        aged_tiles: (aged.match(/class="dashboard-vital [^"]*is-stale"/g) || []).length,
        aged_tile_read_at: tileTitleOf(aged),
        aged_tile_aria: tileAriaOf(aged),
        painted_chip: chip,
        window_moved: windowsMoved
    };
}

// 2b. the views that are not in the first frame, each drawn in full when it is chosen
{
    const views = await eachView();
    results.views = {
        // One request for all five: the switch redrew a snapshot, it did not go and fetch one.
        reads: views.reads_after_switching,
        now: {
            panel: (/data-dashboard-panel="([^"]*)"/.exec(views.now) || [])[1] ?? null,
            on_shift: fact(views.now, 'on_shift'),
            overtime_open: fact(views.now, 'overtime_open'),
            offline_waiting: fact(views.now, 'offline_waiting'),
            refused_24h: fact(views.now, 'refused_24h'),
            rows: (views.now.match(/data-dashboard-now="/g) || []).length,
            // The queues view is not in the DOM at all now, so its hook cannot be borrowed here:
            // this is the count that proves the now view was drawn by its own renderer.
            waiting_rows: (views.now.match(/data-dashboard-queue="/g) || []).length,
            sites: (views.now.match(/data-dashboard-now-site="/g) || []).length,
            depot: (/data-dashboard-now-site="Depot">([^<]*)</.exec(views.now) || [])[1],
            nobody: views.now.indexOf('data-dashboard-nobody="true"') >= 0,
            refused_note: views.now.indexOf('data-dashboard-refused-note="true"') >= 0
        },
        people: {
            panel: (/data-dashboard-panel="([^"]*)"/.exec(views.people) || [])[1] ?? null,
            accounts: fact(views.people, 'accounts'),
            active: fact(views.people, 'active'),
            pending_approval: fact(views.people, 'pending_approval'),
            deactivated: fact(views.people, 'deactivated'),
            enrolled: fact(views.people, 'enrolled'),
            no_face: fact(views.people, 'no_face'),
            no_password: fact(views.people, 'no_password'),
            new_this_week: fact(views.people, 'new_this_week'),
            never_clocked_in: fact(views.people, 'never_clocked_in'),
            workers: (/data-dashboard-role="worker">([^<]*)</.exec(views.people) || [])[1],
            roles: (views.people.match(/data-dashboard-role="/g) || []).length,
            // The two watch figures, each in the queue shape (a value to act on) rather than in
            // the facts grid (a headcount) - and each label carrying the window the *server*
            // counted, which is the fixture's 45 and 10 rather than the console's own defaults.
            watch: (views.people.match(/data-dashboard-watch="/g) || []).length,
            watch_list: views.people.indexOf('data-dashboard-watch-list="true"') >= 0,
            dormant: fact(views.people, 'dormant'),
            onboarding: fact(views.people, 'onboarding'),
            dormant_label: (/data-dashboard-watch="dormant"[\s\S]*?dashboard-queue-label">([^<]*)</
                .exec(views.people) || [])[1] ?? null,
            onboarding_label: (/data-dashboard-watch="onboarding"[\s\S]*?dashboard-queue-label">([^<]*)</
                .exec(views.people) || [])[1] ?? null
        },
        places: {
            panel: (/data-dashboard-panel="([^"]*)"/.exec(views.places) || [])[1] ?? null,
            sites: fact(views.places, 'sites'),
            categories: fact(views.places, 'categories'),
            no_category: fact(views.places, 'no_category'),
            overriding_window: fact(views.places, 'overriding_window'),
            depot: (/data-dashboard-category="Depot">([^<]*)</.exec(views.places) || [])[1],
            unmanned: (views.places.match(/data-dashboard-unmanned-site="/g) || []).length,
            all_manned: views.places.indexOf('data-dashboard-all-manned="true"') >= 0
        },
        // The strip is drawn on every one of them, which is the whole reason it is not a view.
        vitals_on_every_view: ['waiting', 'now', 'people', 'places', 'period']
            .filter((id) => views[id].indexOf('data-dashboard-vitals="true"') < 0).length
    };
}

// 2c. the period view: a window rather than a moment, with the window drawn on it
{
    const views = await eachView();
    const markup = views.period;
    results.period = {
        panel: (/data-dashboard-panel="([^"]*)"/.exec(markup) || [])[1] ?? null,
        workers: fact(markup, 'workers'),
        present_days: fact(markup, 'present_days'),
        expected_days: fact(markup, 'expected_days'),
        late_arrivals: fact(markup, 'late_arrivals'),
        rate: fact(markup, 'average_attendance_rate'),
        approved_hours: fact(markup, 'approved_hours'),
        overtime_hours: fact(markup, 'overtime_hours'),
        awaiting_hours: fact(markup, 'awaiting_approval_hours'),
        window: (/data-dashboard-period-window="true">([^<]*)</.exec(markup) || [])[1] ?? null,
        // The preset that says it is in effect, and whether the other one says so too: this pair
        // of attributes is the whole control, and the reader has to be able to tell which window
        // the figures under it belong to.
        active: (/<button[^>]*data-dashboard-preset="([^"]*)"[^>]*aria-pressed="true"/.exec(markup) || [])[1] ?? null,
        pressed: (markup.match(/aria-pressed="true"/g) || []).length,
        presets: (markup.match(/data-dashboard-preset="/g) || []).length,
        // Payroll readiness: the count beside the hours, and the sentence that says what the
        // count costs a reader who is about to run a pay cycle.
        payroll_shifts: fact(markup, 'awaiting_approval_shifts'),
        payroll_state: (/data-dashboard-payroll="([^"]*)"/.exec(markup) || [])[1] ?? null,
        payroll_text: (/data-dashboard-payroll="[^"]*"[\s\S]*?<span>([^<]*)</.exec(markup) || [])[1] ?? null,
        payroll_link: (/data-dashboard-payroll="[^"]*"[\s\S]*?data-dashboard-go="([^"]*)"/
            .exec(markup) || [])[1] ?? null,
        // The artifacts: one button per format, in the order the card draws them, each naming
        // what it hands over rather than the word "export".
        export_formats: (markup.match(/data-dashboard-export="([^"]*)"/g) || [])
            .map((entry) => entry.replace('data-dashboard-export="', '').replace('"', '')),
        quietest: (/data-dashboard-extreme="quietest"[\s\S]*?<\/div>/.exec(markup) || [])[0],
        most_late: (/data-dashboard-extreme="most_late"[\s\S]*?<\/div>/.exec(markup) || [])[0],
        rows: (markup.match(/data-dashboard-extreme-row="/g) || []).length,
        worker_links: (markup.match(/data-dashboard-worker="/g) || []).length,
        // the day strip: one column per day of the window, its scale said in words, and both
        // figures on every column so the picture is not the only carrier of the meaning.
        days: {
            columns: (markup.match(/data-dashboard-day="/g) || []).length,
            caption: (/data-dashboard-days-caption="true">([^<]*)</.exec(markup) || [])[1] ?? null,
            aria: (/data-dashboard-day="2026-09-25"[^>]*aria-label="([^"]*)"/.exec(markup) || [])[1] ?? null,
            late_marks: (markup.match(/data-dashboard-day-late="/g) || []).length,
            late_days: (markup.match(/data-dashboard-day-late="[^"]*"/g) || [])
                .map((m) => m.replace('data-dashboard-day-late="', '').replace('"', '')),
            // The tallest bar is the busiest day, drawn at 100% of the strip, and a quiet day is
            // smaller on the same scale.
            tallest: (/data-dashboard-day="2026-09-28"[\s\S]*?class="dashboard-day-bar" style="height: (\d+)%"/
                .exec(markup) || [])[1] ?? null,
            shortest: (/data-dashboard-day="2026-09-26"[\s\S]*?class="dashboard-day-bar" style="height: (\d+)%"/
                .exec(markup) || [])[1] ?? null,
            // Nobody came in: no bar at all, and the column is still there.
            empty: (markup.match(/class="dashboard-day-bar is-empty"/g) || []).length,
            empty_day: markup.indexOf('data-dashboard-day="2026-09-27"') >= 0,
            present_flags: (markup.match(/data-dashboard-day-present="0"/g) || []).length
        }
    };
}

// 3. every link goes to the tab that owns that queue
{
    const views = await eachView();
    // The figures that are a *queue* link from the view that lists them; the two that are the
    // whole point of a view ("open the roster", "see the full list") are links from anywhere on
    // the screen, so they are asked of all five.
    // A row is a ``<button>`` where there is a queue to open and a plain ``<div>`` where there is
    // not, so the read stops at whichever tag actually closes it - otherwise a row with no owner
    // would inherit the next row's link and read as one that has one.
    const linkFor = (markup, field) => {
        const block = new RegExp('data-dashboard-queue="' + field + '"[\\s\\S]*?</(?:button|div)>').exec(markup);
        return block ? (/data-dashboard-go="([^"]*)"/.exec(block[0]) || [])[1] : null;
    };
    // The now view's rows carry their own hook, and the same question: which tab owns this
    // figure? ``offline_waiting`` has no owner - the punch queue is triaged on a route no screen
    // in this console lists - so it must come back empty rather than pointing at a tab that does
    // not show the rows.
    const nowLinkFor = (field) => {
        const block = new RegExp('data-dashboard-now="' + field + '"[\\s\\S]*?</(?:button|div)>').exec(views.now);
        // ``?? null`` rather than an undefined falling out of the array: a property the JSON
        // bridge drops is a *missing* key on this side, which reads as "not asserted" in a test
        // instead of as "no link", and those are different claims.
        return block ? ((/data-dashboard-go="([^"]*)"/.exec(block[0]) || [])[1] ?? null) : null;
    };
    results.owners = {
        reviews: linkFor(views.waiting, 'reviews'),
        registrations: linkFor(views.waiting, 'registrations'),
        notes: linkFor(views.waiting, 'notes'),
        alerts_row: views.waiting.indexOf('data-dashboard-queue="alerts"') >= 0,
        credentials: (/data-dashboard-go="Credentials"/).test(views.all),
        sites: (/data-dashboard-go="Sites"/).test(views.all),
        // The row itself is the button rather than a button sitting inside it, and the destination
        // it opens is in the row's accessible name - the same shortening the vital tiles got.
        row: (views.waiting.match(/<button[^>]*data-dashboard-queue="[^"]*"[^>]*>/g) || []).length,
        row_spoken: (/<button[^>]*data-dashboard-queue="reviews"[^>]*aria-label="([^"]*)"/.exec(views.waiting) || [])[1] ?? null,
        // ...and no queue row carries a smaller button inside it any more - which is what the
        // "Open Approvals" row under every figure was.
        inner_buttons: (views.waiting.match(/ui-btn-sm/g) || []).length,
        // ...and switching is not a read: five views, one request.
        reads: views.reads_after_switching
    };
    results.now_owners = {
        on_shift: nowLinkFor('on_shift'),
        overtime_open: nowLinkFor('overtime_open'),
        offline_waiting: nowLinkFor('offline_waiting'),
        // The root tier's tab, and this is an administrator's session: no link at all.
        refused_24h: nowLinkFor('refused_24h')
    };
}

// 4. the root tier's alert row is drawn for the root tier and for nobody else
{
    const admin = await eachView();
    results.alerts_admin = {
        row: admin.waiting.indexOf('data-dashboard-queue="alerts"') >= 0,
        label: textOf(admin.all).indexOf('Unacknowledged alerts') >= 0
    };

    const root = await eachView(DEVELOPER,
        panel({ waiting: { reviews: 4, registrations: 2, notes: 1, alerts: 3, oldest_seconds: 8100 } }));
    results.alerts_root = {
        row: root.waiting.indexOf('data-dashboard-queue="alerts"') >= 0,
        count: fact(root.waiting, 'alerts'),
        link: (/data-dashboard-queue="alerts"[\s\S]*?data-dashboard-go="([^"]*)"/.exec(root.waiting) || [])[1],
        label: textOf(root.all).indexOf('Unacknowledged alerts') >= 0
    };
    // The refusals are the root tier's to read, and this is the tier that can: the row gets the
    // link the administrator's copy of the same view does not have.
    results.refused_root_link = (/data-dashboard-now="refused_24h"[\s\S]*?data-dashboard-go="([^"]*)"/
        .exec(root.now) || [])[1] ?? null;
}

// 5. null is not zero: one panel that could not be read, and the other two still painted
{
    const env = consoleEnv();
    answered = panel({ waiting: null });
    await env.evaluate("UI.renderAdminTab('Dashboard')");
    const markup = render(env);
    const failed = panelOf(markup, 'waiting');
    results.unreadable = {
        flag: failed.indexOf('data-dashboard-unreadable="waiting"') >= 0,
        no_zero: fact(failed, 'reviews') === null && failed.indexOf('data-dashboard-oldest') < 0,
        // The sentence is the server's own reason for the panel being blank, and it is drawn
        // rather than left as an empty card.
        says_so: textOf(failed).indexOf('could not be read') >= 0,
        // The three figures this panel could not answer are an em dash on the strip that
        // always draws, never a zero - on this screen a zero is good news.
        dash: (/data-dashboard-vital-fact="reviews">([^<]*)</.exec(markup) || [])[1] ?? null,
        dash_class: markup.indexOf('class="dashboard-vital is-unknown" data-dashboard-vital="reviews"') >= 0,
        // ...and the figures it *could* answer are untouched, because they came from panels that
        // were readable. Only one view is in the DOM at a time, so the strip is what proves the
        // failure did not travel.
        on_shift_still_there: (/data-dashboard-vital-fact="on_shift">([^<]*)</.exec(markup) || [])[1] === '12',
        page_flag: markup.indexOf('data-dashboard="true"') >= 0
    };
    // The other views are reachable and intact: switching to a readable one draws it.
    env.evaluate("UI_MODULES.dashboardSetMetric('people')");
    results.unreadable.people_after_switch = fact(render(env), 'accounts') === '41';
    env.evaluate("UI_MODULES.dashboardSetMetric('waiting')");

    // The read itself failing is the other case: the shell is drawn anyway, so the refresh
    // control is on the screen that says the read failed.
    const refused = consoleEnv();
    failure = { status: 500, detail: 'database is locked' };
    await refused.evaluate("UI.renderAdminTab('Dashboard')");
    const refusedMarkup = render(refused);
    results.read_failed = {
        error: refusedMarkup.indexOf('ui-error') >= 0,
        reason: refusedMarkup.indexOf('database is locked') >= 0,
        refresh: refusedMarkup.indexOf('data-dashboard-refresh="true"') >= 0,
        // The shell is drawn, so the one view it offers says it could not be read rather than
        // being absent - an empty frame is not a failure notice.
        panels: (refusedMarkup.match(/data-dashboard-panel="/g) || []).length,
        unreadable_panels: (refusedMarkup.match(/data-dashboard-unreadable="/g) || []).length,
        tabs: (refusedMarkup.match(/data-dashboard-metric="/g) || []).length,
        vitals: refusedMarkup.indexOf('data-dashboard-vitals="true"') >= 0,
        // A failed read leaves *no* figure on the strip, not even as a zero.
        dashes: (refusedMarkup.match(/data-dashboard-vital-fact="[^"]*">—</g) || []).length
    };
}

// 6. an empty deployment reads zeroes, and says nothing is waiting
{
    const views = await eachView(undefined, panel({
        people: {
            accounts: 2, active: 2, pending_approval: 0, deactivated: 0,
            by_role: { head_admin: 1, admin: 1 },
            enrolled: 0, no_face: 2, no_password: 0, new_this_week: 0, never_clocked_in: 2
        },
        places: {
            sites: 2, categories: 2, by_category: [{ category: 'Depot', sites: 0 }, { category: 'Yard', sites: 0 }],
            no_category: 2, overriding_window: 0, unmanned_today: []
        },
        now: { on_shift: 0, by_site: [], overtime_open: 0, offline_waiting: 0, refused_24h: 0 },
        waiting: { reviews: 0, registrations: 0, notes: 0, alerts: null, oldest_seconds: null }
    }));
    results.empty = {
        zeroes: [fact(views.waiting, 'reviews'), fact(views.waiting, 'notes'), fact(views.people, 'no_password')],
        now_zeroes: [fact(views.now, 'on_shift'), fact(views.now, 'offline_waiting'), fact(views.now, 'refused_24h')],
        nobody: views.now.indexOf('data-dashboard-nobody="true"') >= 0,
        oldest: (/data-dashboard-oldest="([^"]*)"/.exec(views.waiting) || [])[1],
        nothing_waiting: textOf(views.waiting).indexOf('Nothing is waiting.') >= 0,
        all_manned: views.places.indexOf('data-dashboard-all-manned="true"') >= 0,
        unmanned_badges: (views.places.match(/data-dashboard-unmanned-site="/g) || []).length,
        // A category with nothing in it is a real answer, and it is drawn as one.
        zero_category: /data-dashboard-category="Depot">0</.test(views.places)
    };
}

// 7. server text stays text
{
    const views = await eachView();
    // Every view at once: a name is free text somebody typed, and the view that draws it is not
    // the one that happens to be on top.
    const markup = views.all;
    results.escaping = {
        raw_absent: markup.indexOf('<img src=x') < 0,
        escaped_present: markup.indexOf('&lt;img src=x onerror=alert(1)&gt;') >= 0,
        // The escaped name is *visible*: a view that dropped the list would be safe and empty.
        readable_tail: views.places.indexOf('Site C') >= 0,
        category_readable: views.places.indexOf('Depot') >= 0,
        // The same name in the other view that draws a list of sites, and in the very hook
        // that names it on the element.
        onsite_row: views.now.indexOf('data-dashboard-now-site="&lt;img src=x onerror=alert(1)&gt;"') >= 0,
        onsite_count: (/data-dashboard-now-site="&lt;img[^"]*">([^<]*)</.exec(views.now) || [])[1],
        // The third place server text becomes a row here: a worker's own name, in a linkage.
        extreme_row: views.period.indexOf('data-dashboard-extreme-row="12"') >= 0,
        extreme_escaped: /data-dashboard-extreme-row="12"[\s\S]*?&lt;img src=x onerror=alert\(1\)&gt;[\s\S]*?<\/div>/.test(views.period),
        extreme_raw_absent: !/data-dashboard-extreme-row="12"[\s\S]*?<img src=x[\s\S]*?<\/div>/.test(views.period)
    };
}

// 8. the controls are bound rather than inlined, and a tap reaches the right screen
{
    const env = consoleEnv();
    // A container the binder can walk: the stub DOM's elements have no ``querySelectorAll``, so
    // ``bindDashboardControls`` is handed one that does - which is the seam under test, plus the
    // tab each hook carries. (An inline ``onclick`` would make this untestable *and* cost a CSP
    // allowance.)
    results.binding = await env.evaluate(`(async () => {
        const selectors = [];
        const clicked = [];
        const node = (attribute, value) => ({
            getAttribute: (name) => (name === attribute ? String(value) : null),
            addEventListener: (type, handler) => {
                // Only the navigation hooks are collected: the refresh control is bound by the
                // same pass and firing it here would read the tab a second time.
                if (type === 'click' && attribute === 'data-dashboard-go') clicked.push({ value: value, handler: handler });
            }
        });
        const container = {
            querySelectorAll: (selector) => {
                selectors.push(selector);
                if (selector === '[data-dashboard-go]') return [node('data-dashboard-go', 'Approvals'), node('data-dashboard-go', 'Notes')];
                if (selector === '[data-dashboard-refresh]') return [node('data-dashboard-refresh', 'true')];
                return [];
            }
        };
        UI_MODULES.bindDashboardControls(container);
        await clicked[0].handler();
        return { selectors: selectors.slice(), bound: clicked.map((entry) => entry.value) };
    })()`);
    results.binding.tab_after_click = env.evaluate("State.adminTab");
    results.binding.approvals_read = env.requests.some((r) => r.url.indexOf('/admin/overtime/crossings') >= 0
        || r.url.indexOf('/reports/pending') >= 0);

    // The refresh control is the one control this screen has: it re-reads, and the stamp moves.
    const refreshed = consoleEnv();
    await refreshed.evaluate("UI.renderAdminTab('Dashboard')");
    const firstReads = reads;
    await refreshed.evaluate(`(async () => {
        const button = {
            getAttribute: () => 'true',
            addEventListener: (type, handler) => { if (type === 'click') button.handler = handler; }
        };
        UI_MODULES.bindDashboardControls({ querySelectorAll: (selector) => (selector === '[data-dashboard-refresh]' ? [button] : []) });
        await button.handler();
    })()`);
    const refreshedMarkup = render(refreshed);
    results.refresh = {
        reads: reads - firstReads,
        tab: refreshed.evaluate("State.adminTab"),
        stamp: (/data-dashboard-asof="true">([\s\S]*?)<\/p>/.exec(refreshedMarkup) || [])[1],
        first_stamp: (/data-dashboard-asof="true">([\s\S]*?)<\/p>/.exec(render(refreshed)) || [])[1]
    };
}

// 9. the screen asks the table for its words rather than carrying them
{
    const env = consoleEnv();
    results.wording = env.evaluate(`[
        I18n.__('dashboard'), I18n.__('hintDashboard'), I18n.__('dashboardWaitingTitle'),
        I18n.__('dashboardOldest').replace('{waiting}', '2h 15m'),
        I18n.__('dashboardOpenTab').replace('{tab}', 'Approvals'),
        I18n.__('dashboardNowTitle'), I18n.__('dashboardOnShift'),
        I18n.__('dashboardPendingApproval'),
        I18n.__('dashboardPeriodTitle'),
        I18n.__('dashboardPeriodPresetDays').replace('{days}', '7'),
        I18n.__('dashboardPeriodWindow').replace('{start}', '2026-09-22').replace('{end}', '2026-09-28'),
        I18n.__('dashboardPeriodOpenWorker').replace('{tab}', 'Shifts'),
        // The shell's own words: the five short tab labels and the day strip's two sentences.
        UI_MODULES.dashboardViews().map((view) => I18n.__(view.label)).join('/'),
        I18n.__('dashboardPeriodDays').replace('{days}', '7').replace('{busiest}', '18'),
        I18n.__('dashboardPeriodDayAria').replace('{day}', '2026-09-25').replace('{present}', '26').replace('{late}', '2'),
        I18n.__('dashboardVitalOfExpected').replace('{expected}', '5'),
        // Phase 4's own vocabulary: the two watch figures, payroll readiness in both states, the
        // export control and the one sentence it can fail with.
        I18n.__('dashboardWatch'),
        I18n.__('dashboardDormant').replace('{days}', '45'),
        I18n.__('dashboardOnboarding').replace('{days}', '10'),
        I18n.__('dashboardPeriodAwaitingShifts'),
        I18n.__('dashboardPayrollWaiting').replace('{shifts}', '3').replace('{hours}', '12'),
        I18n.__('dashboardPayrollReady').replace('{hours}', '488.5'),
        I18n.__('dashboardPayrollLink').replace('{tab}', 'Approvals'),
        // The period's three artifacts, and the sheet's own words - the block the export control
        // turned into when it stopped being one button.
        I18n.__('dashboardPeriodExportCsv'),
        I18n.__('dashboardPeriodExportExcel'),
        I18n.__('dashboardPeriodPrint'),
        I18n.__('dashboardPeriodExportFailed'),
        I18n.__('dashboardPeriodPrintFailed'),
        I18n.__('dashboardPeriodPrintTitle'),
        I18n.__('dashboardPeriodPrintFigure'),
        I18n.__('dashboardPeriodPrintValue'),
        I18n.__('dashboardPeriodPrintEachDay'),
        I18n.__('dashboardPeriodPrintDay').replace('{present}', '26').replace('{late}', '2'),
        I18n.__('dashboardPeriodPrintNote')
    ]`);
    // ``I18n.__`` reads ``I18n.lang``, and all four tables are loaded in this environment, so
    // the same key is asked of each table by name - which is the whole question: does the
    // screen's vocabulary exist four times, or once with three fallbacks?
    results.other_languages = env.evaluate(`(() => {
        const was = I18n.lang;
        const read = (lang, key) => { I18n.lang = lang; const text = I18n.__(key); I18n.lang = was; return text; };
        return ['ar', 'hi', 'ur', 'en'].map((lang) => read(lang, 'dashboard') + '|' + read(lang, 'dashboardWaitingTitle')
            + '|' + read(lang, 'dashboardNowTitle') + '|' + read(lang, 'dashboardPeriodTitle'));
    })()`);
}

// 10. the period's window control: a tap asks the server for that window, and nothing else moves
{
    const env = consoleEnv();
    await env.evaluate("UI.renderAdminTab('Dashboard')");
    const before = env.requests.map((r) => r.url);
    await env.evaluate(`(async () => {
        const button = {
            getAttribute: () => 'month',
            addEventListener: (type, handler) => { if (type === 'click') button.handler = handler; }
        };
        UI_MODULES.bindDashboardControls({ querySelectorAll: (selector) => (selector === '[data-dashboard-preset]' ? [button] : []) });
        await button.handler();
    })()`);
    results.preset = {
        before: before,
        after: env.requests.map((r) => r.url),
        days: env.evaluate("State.dashboardDays"),
        tab: env.evaluate("State.adminTab")
    };
}

// 11. a linkage opens *that person* over *that window*, in the tab that owns attendance rows
{
    const env = consoleEnv();
    await env.evaluate("UI.renderAdminTab('Dashboard')");
    results.worker_link = await env.evaluate(`(async () => {
        const button = {
            getAttribute: (name) => ({
                'data-dashboard-worker': '43',
                'data-dashboard-start': '2026-09-22',
                'data-dashboard-end': '2026-09-28'
            }[name] || null),
            addEventListener: (type, handler) => { if (type === 'click') button.handler = handler; }
        };
        UI_MODULES.bindDashboardControls({ querySelectorAll: (selector) => (selector === '[data-dashboard-worker]' ? [button] : []) });
        // The stub DOM is not the subject here and a paint may well not survive it: what has to
        // be true either way is the state the tap wrote - the person, the window, and the tab.
        try { await button.handler(); } catch (err) { /* see above */ }
        return {
            range: State.shiftsRange || null,
            query: State.shiftsQuery,
            category: State.shiftsCategory,
            tab: State.adminTab,
            hash: String(window.location.hash || '')
        };
    })()`);
}

// 12. a window nobody worked, and a period view that could not be read at all
{
    const quiet = panel();
    quiet.period.present_days = 0;
    quiet.period.late_arrivals = 0;
    quiet.period.quietest = [];
    quiet.period.most_late = [];
    // A window nobody worked and nobody is holding hours in: the *ready* state of the payroll
    // sentence, which is the one state that must not offer a queue to go and work.
    quiet.period.awaiting_approval_hours = 0;
    quiet.period.awaiting_approval_shifts = 0;
    // The strip has to agree with the figure beside it, so an empty week is seven empty columns
    // rather than the fixture's seven days of work.
    quiet.period.by_day = quiet.period.by_day.map((entry) => ({ day: entry.day, present: 0, late: 0 }));
    const views = await eachView(undefined, quiet);
    results.period_empty = {
        zeroes: [fact(views.period, 'present_days'), fact(views.period, 'late_arrivals')],
        none: (views.period.match(/data-dashboard-extreme-none="/g) || []).length,
        rows: (views.period.match(/data-dashboard-extreme-row="/g) || []).length,
        says_so: textOf(views.period).indexOf('Nobody was present') >= 0,
        // The window is still described: an empty summary is still a summary *of* something.
        window: (/data-dashboard-period-window="true">([^<]*)</.exec(views.period) || [])[1] ?? null,
        // ...and the strip is a window with no work in it rather than no window: one column per
        // day, every bar empty, and not one late numeral.
        strip_columns: (views.period.match(/data-dashboard-day="/g) || []).length,
        strip_empty: (views.period.match(/class="dashboard-day-bar is-empty"/g) || []).length,
        strip_late: (views.period.match(/data-dashboard-day-late="/g) || []).length,
        // Nothing is unsigned, so the sentence says so and points at no queue - a *ready* window
        // with a button into the approvals queue would send somebody to work an empty list. The
        // link is looked for *after* the sentence: the vital strip above it carries a tile into
        // the same tab, and a whole-markup search would find that one instead.
        payroll: (/data-dashboard-payroll="([^"]*)"/.exec(views.period) || [])[1] ?? null,
        payroll_link: (/data-dashboard-payroll="[^"]*"[\s\S]*?data-dashboard-go="([^"]*)"/
            .exec(views.period) || [])[1] ?? null
    };

    const broken = await eachView(undefined, panel({ period: null }));
    const panelMarkup = panelOf(broken.period, 'period');
    results.period_unreadable = {
        flag: panelMarkup.indexOf('data-dashboard-unreadable="period"') >= 0,
        no_zero: fact(panelMarkup, 'approved_hours') === null && fact(panelMarkup, 'present_days') === null,
        // No window control over a view with no window to draw: a preset there would re-ask for
        // a period the server has already said it cannot count.
        presets: (panelMarkup.match(/data-dashboard-preset="/g) || []).length,
        strip: panelMarkup.indexOf('data-dashboard-days="true"') >= 0,
        // The view under the unreadable one is reached by a switch, which makes no request at
        // all - so a window the server could not count cannot block the rest of the screen.
        other_views: fact(broken.now, 'on_shift') === '12' && fact(broken.people, 'accounts') === '41'
    };
}

// 12b. the window's three artifacts: two files and a sheet, each asked for by its own button
{
    const env = consoleEnv();
    await env.evaluate("UI.renderAdminTab('Dashboard')");
    const readsBefore = env.requests.length;
    // The formats are tapped through the same binder the markup uses, one at a time, so what is
    // asserted is what a reader's tap does rather than what a handler called directly would do.
    const tap = async (format) => {
        exportUrl = null;
        await env.evaluate(`(async () => {
            const button = {
                getAttribute: () => ${JSON.stringify(format)},
                addEventListener: (type, handler) => { if (type === 'click') button.handler = handler; }
            };
            UI_MODULES.bindDashboardControls({ querySelectorAll: (selector) => (selector === '[data-dashboard-export]' ? [button] : []) });
            await button.handler();
        })()`);
        return {
            url: exportUrl,
            name: env.lastAnchor() ? env.lastAnchor().download : null,
            file: await env.lastBlobText()
        };
    };

    const csv = await tap('csv');
    const xlsx = await tap('xlsx');
    const asked = env.requests.filter((r) => r.url.indexOf('/reports/export') >= 0);
    // The sheet needs no server at all: it is the card's own content, printed by the browser.
    const sheetCountBefore = env.printed.length;
    const print = await tap('print');
    const printed = env.printed[env.printed.length - 1] || {};

    results.export = {
        csv: csv,
        xlsx: xlsx,
        // The route is ``admin_only``, so a download without this header is a 401 saved as a file.
        auth: asked.length ? (asked[0].headers || {}).Authorization : null,
        formats: asked.map((r) => (/format=([a-z]+)/.exec(r.url) || [])[1] || null),
        // The sheet: the dialog's file name, the fact that the page was pulled out of the paper,
        // and the content itself - what the helper drew from the card's own figure list.
        print: {
            asked_nothing: print.url === null,
            prints: env.printed.length - sheetCountBefore,
            title: printed.title || null,
            printing: printed.printing === true,
            sheet: printed.sheet || ''
        },
        // The export is a request, but not a *read* of the dashboard: the snapshot is untouched
        // and the stamp on it does not move. The sheet makes no request at all.
        reads: env.requests.length - readsBefore
    };
}

// 13. switching a view: a redraw from the snapshot, no request, and the arrow keys walk it
{
    const env = consoleEnv();
    await env.evaluate("UI.renderAdminTab('Dashboard')");
    const reads = env.requests.length;
    // Inside ``evaluate`` the only names in scope are the page's own - ``UI_MODULES``,
    // ``State``, the document. So the markup is read off the element the tab painted into,
    // which is the same string ``render`` above reads from the outside.
    results.switch = env.evaluate(`(() => {
        const host = document.getElementById('adminContent');
        const markup = () => host.innerHTML;
        const stamp = () => (/data-dashboard-asof="true">([^<]*)</.exec(markup()) || [])[1] || null;
        const before_stamp = stamp();
        UI_MODULES.dashboardSetMetric('period');
        const after = markup();
        // A view id the dashboard does not know changes nothing at all.
        const junk = UI_MODULES.dashboardSetMetric('not-a-view') === undefined;
        return {
            metric: State.dashboardMetric,
            panel: (/data-dashboard-panel="([^"]*)"/.exec(after) || [])[1] || null,
            panels: (after.match(/data-dashboard-panel="/g) || []).length,
            tabs: (after.match(/data-dashboard-metric="/g) || []).length,
            selected_period: /aria-selected="true"[^>]*data-dashboard-metric="period"/.test(after),
            selected_count: (after.match(/aria-selected="true"/g) || []).length,
            in_tab_order: (after.match(/tabindex="0"/g) || []).length,
            // The strip is the constant: it draws on every view, which is the whole reason it is
            // not one of them - and the period's own day strip came with this view.
            vitals: after.indexOf('data-dashboard-vitals="true"') >= 0,
            strip: after.indexOf('data-dashboard-days="true"') >= 0,
            before_stamp: before_stamp,
            after_stamp: stamp(),
            junk: junk && State.dashboardMetric === 'period'
        };
    })()`);
    results.switch.reads = env.requests.length - reads;
    // Coming back to the tab re-reads it (a visit costs one read) and lands on the view that was
    // left on - which is the whole reason the choice is in ``State`` rather than in the tab.
    const visits = env.requests.length;
    await env.evaluate("UI.renderAdminTab('Dashboard')");
    const back = render(env);
    results.switch.remembered = {
        panel: (/data-dashboard-panel="([^"]*)"/.exec(back) || [])[1] ?? null,
        metric: env.evaluate("State.dashboardMetric"),
        reads: env.requests.length - visits
    };
    // The arrow keys, which a ``tablist`` promises: the arrows walk and wrap, Home and End jump,
    // and a key that is not this control's is left to the page.
    results.switch.keys = env.evaluate(`(() => {
        const handled = [];
        const press = (key) => {
            UI_MODULES.dashboardMetricKey({ key: key, preventDefault: () => handled.push(key) });
            return State.dashboardMetric;
        };
        return {
            order: [press('ArrowLeft'), press('Home'), press('ArrowRight'), press('End'),
                    press('ArrowRight')],
            handled: handled,
            ignored: [press('Escape'), press('PageDown'), press('Tab')],
            final: State.dashboardMetric
        };
    })()`);
    results.switch.keys.reads = env.requests.length - reads - results.switch.reads - results.switch.remembered.reads;
}
"""


@pytest.fixture(scope="module")
def results() -> dict:
    return frontend_vm.run(HARNESS)


def test_the_dashboard_is_first_on_the_rail_and_is_where_a_console_lands(results):
    """Tab *order* is the landing mechanism: no new routing was needed, and none was added."""
    tab = results["rail"]
    assert tab[:5] == ["Dashboard", "dashboard", "hintDashboard", "navGroupOperations", "dashboard"], tab
    assert tab[5] == "Dashboard", "the first visible tab is not the dashboard"
    assert tab[6] is True, "the tab has no icon"
    assert tab[7] is True, (
        "the tab a fresh session opens on is not ADMIN_TABS[0]: the console would land on one "
        "screen and fall back to another"
    )

    assert results["landing"] == {"tab": "Dashboard", "page": True, "reads": 1}, results["landing"]
    assert results["root_landing"]["tab"] == "Dashboard", (
        "the root tier landed somewhere else: " + str(results["root_landing"])
    )


def test_opening_it_is_one_counted_read_and_not_a_download(results):
    """The whole decision, asserted on the requests the paint made.

    One request, to the aggregate endpoint. Not ``/admin/users``, not ``/admin/sites``, not
    ``/admin/active_sessions`` - a dashboard built from those would pay the cost it exists to
    remove, and its price would grow with the roster rather than with the number of panels.
    """
    paint = results["paint"]
    assert paint["flags"] is True
    assert len(paint["requests"]) == 1, paint["requests"]
    assert "/admin/dashboard" in paint["requests"][0], paint["requests"]
    # One view, not five: the five panels are five *tabs* now, and the one in the DOM is the one
    # the switcher says is selected. The tab that claims it and the panel that is drawn are the
    # same view - a strip that marked one tab while drawing another would be the screen lying.
    assert paint["panels"] == 1, paint["panels"]
    assert paint["panel"] == paint["selected"] == "waiting", paint["panel"]
    assert paint["switch_tabs"] == 5, paint["switch_tabs"]
    # The one request also carries the period's window - a day count, or the word ``month`` - and
    # it is the server's default until a reader chooses otherwise.
    assert "?days=7" in paint["requests"][0], paint["requests"]


def test_the_vital_strip_draws_the_five_figures_somebody_checks_first(results):
    """The one part of the screen that does not switch away, so the state is always in view.

    Five figures, each the server's own field and each a link into the tab that owns it, so a
    number that needs acting on is one tap from the queue that lists it.
    """
    vitals = results["paint"]["vitals"]
    assert vitals["tiles"] == 5, vitals
    assert vitals["facts"] == ["4", "2", "12", "132", "12"], vitals
    assert vitals["linked"] == 5, vitals
    # "132 of 5 expected" would be two unrelated figures side by side; the days the sites were
    # open is what the figure beside it is out of.
    assert vitals["present_note"] == "of 5 expected", vitals
    # The tone is a state: a queue with something in it is marked, and the hours nobody has
    # signed for yet are one of those queues.
    assert vitals["warned"] is True, vitals
    # ...and each tile still says where it goes, in the one place the shortening moved it to.
    assert vitals["spoken"] == [
        "4 — Shifts to review — Approvals",
        "12 — On shift now — Active Shifts"
    ], vitals


def test_every_count_is_drawn_from_the_payload(results):
    """Each figure in its own view, from the server's own field, with nothing derived here."""
    paint = results["paint"]
    assert paint["waiting"]["reviews"] == "4", paint["waiting"]
    assert paint["waiting"]["registrations"] == "2", paint["waiting"]
    assert paint["waiting"]["notes"] == "1", paint["waiting"]
    assert paint["waiting"]["queues"] == 3, paint["waiting"]


def test_the_other_views_draw_the_same_server_fields_when_they_are_switched_to(results):
    """Three views that are not drawn until they are chosen - and are then drawn in full.

    The switcher is why this is a separate scenario: a view that only exists when it is selected
    is a view whose numbers are not in the first frame, so the suite has to go and get them.
    """
    people = results["views"]["people"]
    assert [
        people["accounts"], people["active"], people["pending_approval"], people["deactivated"],
        people["enrolled"], people["no_face"], people["no_password"], people["new_this_week"],
        people["never_clocked_in"],
    ] == ["41", "38", "2", "3", "36", "5", "0", "4", "2"], people
    # Every role the payload carried, under the role's own translated label.
    assert people["roles"] == 5, people
    assert people["workers"] == "30", people
    # The two watch figures are the server's own fields, in the queue shape - a value to act on
    # rather than a headcount - and each label names the window the *server* counted over.
    assert people["watch"] == 2 and people["watch_list"] is True, people
    assert (people["dormant"], people["onboarding"]) == ("6", "3"), people
    assert people["dormant_label"] == "Dormant — no punch in 45 days", people
    assert people["onboarding_label"] == "Joined in the last 10 days, never clocked in", people

    places = results["views"]["places"]
    assert [places["sites"], places["categories"], places["no_category"], places["overriding_window"]] == [
        "3", "2", "0", "1"
    ], places
    assert places["depot"] == "2", places
    # A *list*, not a count: the sites nobody has reached today, by name.
    assert places["unmanned"] == 2, places
    assert places["all_manned"] is False, places


def test_the_now_view_draws_the_boards_figure_and_the_three_kinds_of_waiting(results):
    """The view that borrows, drawn from the server's own fields and nothing else.

    Four figures and a split by site. Nothing here is computed in the browser - the by-site
    numbers are the server's own ``now.by_site`` rows, which is what makes the total and the
    split impossible to disagree about.
    """
    now_panel = results["views"]["now"]
    assert [
        now_panel["on_shift"], now_panel["overtime_open"], now_panel["offline_waiting"],
        now_panel["refused_24h"],
    ] == ["12", "1", "3", "2"], now_panel
    assert now_panel["rows"] == 4, now_panel
    # The queues view's own hook is untouched: these are two views, not one drawn twice.
    assert now_panel["waiting_rows"] == 0, now_panel
    assert now_panel["sites"] == 2 and now_panel["depot"] == "4", now_panel
    assert now_panel["nobody"] is False, now_panel
    # A refusal has no triage state anybody can clear, and the view says so rather than leaving
    # the figure to be read as a backlog.
    assert now_panel["refused_note"] is True, now_panel


def test_the_stamp_says_what_it_is_and_that_it_does_not_tick(results):
    """Not live, and why - written on the panel, or an operator acts on a stale figure."""
    paint = results["paint"]
    assert paint["stamp"] == "Counted at 2026-09-28 14:03:11. A snapshot, not a live board.", (
        paint["stamp"]
    )
    # The one panel that answers "has anything been sitting here too long", in words.
    assert paint["waiting"]["oldest"] == "8100", paint["waiting"]
    assert "waited" in paint["text"] or "waiting" in paint["text"], paint["text"]


def test_a_figure_says_when_it_was_read_and_an_old_read_is_marked_stale(results):
    """One read, one age: each figure carries its read time, and past the window it says so.

    The dashboard does not poll, so there is no second read to compare against: the only honest
    freshness is how long ago the one read landed, and every figure shares it. A figure printed
    as current after that window is exactly the false confidence this screen exists to prevent -
    so the read is stamped, and once it is old the strip says so *without fetching a thing*.
    """
    fresh = results["freshness"]
    # The read time is on the strip and in every tile: one read, so no figure has a time of its own.
    assert fresh["fresh_read_at"] == "2026-09-28 14:03:11", fresh
    assert fresh["fresh_title"] == "Read at 2026-09-28 14:03:11", fresh
    assert fresh["fresh_chip"] == ["fresh", "Read just now"], fresh
    assert "out of date" not in fresh["fresh_aria"].lower(), fresh
    # Aging the read is the clock moving over one snapshot: nothing may be re-read for it.
    assert fresh["reads"] == 0, (
        "aging the read cost a request: freshness is a property of the clock, not a refresh: "
        + str(fresh)
    )
    # Past the stale window, the figures are still shown - they are the last thing counted - but
    # marked, and their accessible names say old rather than letting a number read as current.
    assert fresh["aged_chip"] == ["stale", "Out of date. Refresh to re-count."], fresh
    assert fresh["aged_tiles"] == 5, fresh
    assert fresh["aged_tile_read_at"] == "Read at 2026-09-28 14:03:11", fresh
    assert "out of date" in fresh["aged_tile_aria"].lower(), fresh
    # And the chip repaints in place from the clock, which is the tick's whole job.
    assert fresh["painted_chip"]["className"].endswith("is-stale"), fresh
    assert fresh["painted_chip"]["text"] == fresh["aged_chip"][1], fresh
    # The boundaries are the server's, not this module's. With the read held at 400 s, a longer
    # aging window on the wire says "fresh", a wider stale window makes "aging" reachable, and a
    # payload that omits the block - or sends a zero or a negative - falls back to the console's
    # constants rather than reading every figure as stale. This is what stops the strip's
    # definition of "current" drifting from the deployment's.
    moved = fresh["window_moved"]
    assert moved["from_long_aging"] == "fresh", moved
    assert moved["from_wide_stale"] == "aging", moved
    assert moved["from_omitted"] == "stale", moved
    assert moved["from_junk"] == "stale", moved


def test_each_count_links_to_the_tab_that_owns_that_queue(results):
    """A count that cannot be worked from where it is read sends the reader looking."""
    owners = results["owners"]
    assert owners["reviews"] == "Approvals", owners
    assert owners["registrations"] == "Registrations", owners
    assert owners["notes"] == "Notes", owners
    assert owners["credentials"] is True, owners
    assert owners["sites"] is True, owners
    # The alert queue is the root tier's, and this is an administrator's session: no row.
    assert owners["alerts_row"] is False, owners
    # Three queues a person has to work, and each one *is* the tap into the tab that works it.
    assert owners["row"] == 3, owners
    assert owners["row_spoken"] == "4 Shifts to review — Approvals", owners
    assert owners["inner_buttons"] == 0, owners

    # The now panel's three rows, and the one that must not have an owner: the punch queue is
    # triaged at a route no tab in this console lists.
    now_owners = results["now_owners"]
    assert now_owners["on_shift"] == "Live Ops", now_owners
    assert now_owners["overtime_open"] == "Approvals", now_owners
    assert now_owners["offline_waiting"] is None, now_owners
    assert now_owners["refused_24h"] is None, (
        "an administrator was offered the root tier's own surface: " + str(now_owners)
    )
    assert results["refused_root_link"] == "Developer", results["refused_root_link"]


def test_the_alert_queue_is_drawn_for_the_tier_that_can_read_it(results):
    """``null`` is "not yours", and the row is absent rather than showing a zero."""
    assert results["alerts_admin"] == {"row": False, "label": False}, results["alerts_admin"]
    root = results["alerts_root"]
    assert root["row"] is True, root
    assert root["count"] == "3", root
    assert root["link"] == "Alerts", root
    assert root["label"] is True, root


def test_a_view_that_could_not_be_read_is_null_and_not_zero(results):
    """The one output this page must never produce, and the sentence it produces instead."""
    unreadable = results["unreadable"]
    assert unreadable["flag"] is True, unreadable
    assert unreadable["no_zero"] is True, (
        "a failed view drew a zero for its queues: " + str(unreadable)
    )
    assert unreadable["says_so"] is True, unreadable
    # The strip is what proves the failure did not travel: the three figures this panel could not
    # answer are an em dash, and the ones that came from readable panels are untouched.
    assert unreadable["dash"] == "—", unreadable
    assert unreadable["dash_class"] is True, (
        "an unreadable figure was not marked as unknown - a zero there reads as good news: "
        + str(unreadable)
    )
    assert unreadable["on_shift_still_there"] is True, (
        "a view that answered null took a figure that did not: " + str(unreadable)
    )
    assert unreadable["people_after_switch"] is True, (
        "one unreadable view took the views beside it: " + str(unreadable)
    )
    assert unreadable["page_flag"] is True


def test_a_failed_read_still_leaves_the_refresh_control_on_the_screen(results):
    """The moment somebody wants a retry is the moment the read failed."""
    failed = results["read_failed"]
    assert failed["error"] is True, failed
    assert failed["reason"] is True, "the server's own sentence is not shown"
    assert failed["refresh"] is True, "there is no way to ask again"
    assert failed["panels"] == 1 and failed["unreadable_panels"] == 1, (
        "a shell drawn around a failed read should say the view it draws is unread: " + str(failed)
    )
    # The five tabs and the strip are still there, because a reader whose read failed needs to be
    # able to go somewhere else - and the strip must carry no figure at all, not a zero.
    assert failed["tabs"] == 5 and failed["vitals"] is True, failed
    assert failed["dashes"] == 5, (
        "a failed read drew a figure on the strip - which on this screen reads as good news: "
        + str(failed)
    )


def test_an_empty_deployment_reads_zeroes_rather_than_nulls(results):
    """A state, not a fault - the readiness precedent, on the first morning of a deployment."""
    empty = results["empty"]
    assert empty["zeroes"] == ["0", "0", "0"], empty
    assert empty["now_zeroes"] == ["0", "0", "0"], empty
    assert empty["nobody"] is True, (
        "an empty deployment must say nobody is on site, not draw an empty list of sites"
    )
    assert empty["oldest"] == "none", (
        "nothing waiting must not be given an age of zero, which would say something was just filed"
    )
    assert empty["nothing_waiting"] is True, empty
    # And the places panel is a list rather than a count: every site has been reached, so none
    # is named, and the screen says so.
    assert empty["all_manned"] is True, empty
    assert empty["unmanned_badges"] == 0, empty
    assert empty["zero_category"] is True, "a category with no sites in it is a real answer"


def test_a_hostile_site_name_stays_text(results):
    """A site is free text somebody typed, on a read path that does not get to assume."""
    escaping = results["escaping"]
    assert escaping["raw_absent"] is True, "the payload was interpolated into the DOM raw"
    assert escaping["escaped_present"] is True, (
        "the value is neither raw nor escaped - it was dropped, and a dropped site is a blank row"
    )
    assert escaping["readable_tail"] is True and escaping["category_readable"] is True, escaping
    # The by-site list is the second place server text becomes a list on this page, and the hook
    # that names the site carries the escaped value too - so the panel's own markup cannot become
    # the injection the text was escaped to prevent.
    assert escaping["onsite_row"] is True, escaping
    assert escaping["onsite_count"] == "1", escaping
    # And in the period panel's linkage, where the name is a *person's* rather than a site's - the
    # value is escaped in the row, in the hook that names the row, and nowhere raw.
    assert escaping["extreme_row"] is True, escaping
    assert escaping["extreme_escaped"] is True, (
        "the linkage's worker name is neither raw nor escaped: " + str(escaping)
    )
    assert escaping["extreme_raw_absent"] is True, escaping


def test_the_controls_are_bound_by_hook_and_reach_the_right_screen(results):
    """No inline ``onclick``: the CSP's attribute allowance may only fall."""
    binding = results["binding"]
    assert binding["selectors"] == [
        "[data-dashboard-go]", "[data-dashboard-refresh]", "[data-dashboard-metric]",
        "[data-dashboard-preset]", "[data-dashboard-export]", "[data-dashboard-worker]",
    ], binding["selectors"]
    assert binding["bound"] == ["Approvals", "Notes"], binding
    assert binding["tab_after_click"] == "Approvals", (
        "the tap did not open the tab the row carried: " + str(binding)
    )


def test_the_refresh_control_re_reads_and_moves_the_stamp(results):
    refresh = results["refresh"]
    assert refresh["reads"] == 1, refresh
    assert refresh["tab"] == "Dashboard", refresh
    assert "14:06:02" in refresh["stamp"], (
        "the refresh drew the cached payload rather than re-reading: " + str(refresh)
    )


def test_the_screens_words_come_from_the_tables(results):
    """The tab's chrome is translated; the alert's own sentence is the server's, as ever."""
    wording = results["wording"]
    assert wording[0] == "Dashboard", wording
    assert wording[1].startswith("What this deployment is"), wording
    assert wording[2] == "Waiting on a person", wording
    assert wording[3] == "The oldest of them has been waiting 2h 15m.", wording
    assert wording[4] == "Open Approvals", wording
    assert wording[5] == "Right now", wording
    assert wording[6] == "On shift now", wording
    # The quarantine has a name on this screen too, and it is not "Deactivated".
    assert wording[7] == "Waiting for approval", wording
    # The period's own words, including the two linkages' names for what they open.
    assert wording[8] == "The period", wording
    assert wording[9] == "Last 7 days", wording
    assert wording[10] == "2026-09-22 to 2026-09-28", wording
    assert wording[11] == "Open in Shifts", wording
    # The switcher's labels are the views' names shortened to fit a phone - and they are the
    # panel titles' own vocabulary rather than a second naming of the same five things.
    assert wording[12] == "Waiting/Now/People/Places/Period", wording
    # The strip's scale, said in words, and one column's own sentence: both are the text that
    # makes the picture readable when the picture is not.
    assert wording[13] == "Each of the 7 days in this window, against the busiest at 18.", wording
    assert wording[14] == "2026-09-25: 26 present, 2 late.", wording
    assert wording[15] == "of 5 expected", wording
    # The window on each watch label is a placeholder the *screen* fills, and the payroll sentence
    # is a template rather than five hard-coded strings: both are what keeps a translated table
    # from having to know arithmetic.
    assert wording[16:34] == [
        "Worth a look",
        "Dormant — no punch in 45 days",
        "Joined in the last 10 days, never clocked in",
        "Shifts awaiting approval",
        "3 shifts (12 h) in this window are not approved yet, so a pay run now would leave them out.",
        "Everything in this window is signed off: 488.5 h approved, nothing waiting on a decision.",
        "Sign them off in Approvals",
        # Three artifacts rather than one button: each says what it hands over.
        "Download CSV",
        "Download Excel",
        "Print sheet",
        "That window could not be downloaded.",
        "This window is not on the screen to print.",
        # ...and the sheet's own words, including the block the day strip becomes on paper.
        "Attendance summary",
        "Figure",
        "Value",
        "Each day",
        "26 present · 2 late",
        "Counted over this window only. Hours nobody has approved yet are not counted as approved.",
    ], wording

    other = results["other_languages"]
    assert len(other) == 4 and all(entry.count("|") == 3 for entry in other), other
    assert other[3].startswith("Dashboard|"), other
    # Four tables, four different words: a key that only exists in English would answer with
    # the English text in every language (``I18n.__`` falls back), which is exactly the state
    # the parity suite counts keys to prevent and this one shows on the rendered screen.
    assert len(set(other)) == 4, other


def test_the_period_view_draws_the_window_its_figures_belong_to(results):
    """A summary has to say what it is a summary *of*, and every figure in it is the server's."""
    period = results["period"]
    assert period["panel"] == "period", period
    assert [
        period["workers"], period["present_days"], period["expected_days"], period["late_arrivals"],
        period["rate"], period["approved_hours"], period["overtime_hours"], period["awaiting_hours"],
    ] == ["31", "132", "5", "4", "0.85", "488.5", "6", "12"], period
    assert period["window"] == "2026-09-22 to 2026-09-28", period
    # One window is in effect and only one says so: two identical-looking buttons would leave the
    # reader deciding which one produced the figures.
    assert period["presets"] == 2 and period["pressed"] == 1, period
    assert period["active"] == "days", period
    assert period["rows"] == 2 and period["worker_links"] == 2, period

    # Payroll readiness: the count behind the hours, the sentence that says what it costs, and the
    # queue that clears it. Three figures and one link, all the server's.
    assert period["payroll_shifts"] == "3", period
    assert period["payroll_state"] == "waiting", period
    assert period["payroll_text"] == (
        "3 shifts (12 h) in this window are not approved yet, so a pay run now would leave them out."
    ), period
    assert period["payroll_link"] == "Approvals", (
        "the un-ready window offers no queue to work: " + str(period)
    )
    # And the artifacts: the window can be handed over from the card that describes it, in each
    # of the three forms. The order is asserted because it is the markup's, not the test's.
    assert period["export_formats"] == ["csv", "xlsx", "print"], period


def test_the_periods_two_linkages_open_that_person_over_that_window(results):
    """The extremes are two named people, one tap from the context they came from.

    The plan refuses a leaderboard and asks for a route instead, so each row is asserted for the
    three things that make it a route: the person, the window, and the tab that holds their rows.
    """
    period = results["period"]
    quietest = period["quietest"]
    most_late = period["most_late"]

    # The quietest row is ranked by the rate, and the most-late one by the count: two panels over
    # one week, answering two different questions about the same people.
    assert 'data-dashboard-extreme-figure="43">0.2<' in quietest, quietest
    assert "Row 43" in quietest, quietest
    assert 'data-dashboard-extreme-figure="12">3<' in most_late, most_late

    assert 'data-dashboard-worker="43"' in quietest, quietest
    assert 'data-dashboard-worker="12"' in most_late, most_late
    assert 'data-dashboard-start="2026-09-22"' in quietest, quietest
    assert 'data-dashboard-end="2026-09-28"' in quietest, quietest

    link = results["worker_link"]
    assert link["tab"] == "Shifts", link
    assert link["range"] == {"start": "2026-09-22", "end": "2026-09-28"}, (
        "the linkage did not carry the window the figure came from: " + str(link)
    )
    assert link["query"] == "43", (
        "the linkage opened the tab without saying which person it is about: " + str(link)
    )
    # A category filter left over from an earlier visit would hide the very rows the link promises.
    assert link["category"] == "", link
    assert "shifts=2026-09-22..2026-09-28" in link["hash"], link


def test_the_day_strip_answers_which_days_and_not_only_how_many(results):
    """A total says a week was short-staffed; the strip says which day it was.

    One column per day of the window, scaled against the busiest day in *that* window rather than
    against the roster - and every column carries both figures as text, so the picture is a
    shortcut to the fact rather than the only place the fact exists.
    """
    period = results["period"]
    days = period["days"]
    assert days["columns"] == 7, days
    # The scale is stated in words: a bar chart whose axis is only implied cannot be read, and
    # the busiest day is the number the tallest bar means.
    assert days["caption"] == (
        "Each of the 7 days in this window, against the busiest at 32."
    ), days["caption"]
    assert "32" in days["caption"], days
    # The busiest day is the full height and a quiet one is shorter on the same scale: an
    # absolute scale would make a quiet deployment look like a dead one.
    assert days["tallest"] == "100" and days["shortest"] == "38", days
    # Nobody came in: the column is there, without a bar.
    assert days["empty"] == 1 and days["empty_day"] is True, days
    assert days["present_flags"] == 1, days
    # A late arrival is the day it happened and how many, in numerals - not a colour alone.
    assert days["late_marks"] == 3, days
    assert days["late_days"] == ["2026-09-23", "2026-09-25", "2026-09-26"], days
    assert days["aria"] == "2026-09-25: 26 present, 2 late.", days["aria"]


def test_the_views_are_switched_not_scrolled_and_switching_costs_no_read(results):
    """Five cards down a page hides the answer; one view at a time puts it at eye level.

    The switch redraws the snapshot already in hand, so it must make no request and must not
    touch the stamp - a request per tap for an answer the page already holds is the cost this
    whole screen exists to avoid. The tabs are a real ``tablist``, so the arrows have to walk them.
    """
    switch = results["switch"]
    assert switch["metric"] == "period", switch
    assert switch["panel"] == "period", switch
    # One panel in the DOM, five tabs offered, and exactly one of them selected and in the tab
    # order - the roving ``tabindex`` that makes a tab walk a tab walk.
    assert switch["panels"] == 1 and switch["tabs"] == 5, switch
    assert switch["selected_period"] is True, switch
    assert switch["selected_count"] == 1 and switch["in_tab_order"] == 1, switch
    # The strip and the shell are the constants: they draw on every view.
    assert switch["vitals"] is True and switch["strip"] is True, switch
    assert switch["before_stamp"] == switch["after_stamp"], (
        "switching a view moved the stamp: nothing was re-counted, so nothing may say it was: "
        + str(switch)
    )
    assert switch["reads"] == 0, (
        "switching a view asked the server for something it already had: " + str(switch["reads"])
    )
    # A view id the dashboard does not know changes nothing rather than blanking the screen.
    assert switch["junk"] is True, switch
    # Coming back to the tab re-reads it - a visit is one read - and lands on the view that was
    # left on, which is the reason the choice lives outside the tab.
    assert switch["remembered"] == {"panel": "period", "metric": "period", "reads": 1}, switch["remembered"]

    keys = switch["keys"]
    # The walk, from the period: left goes one back, Home jumps to the first, right walks on, End
    # jumps to the last, and the arrows wrap rather than stopping at an end.
    assert keys["order"] == ["places", "waiting", "now", "period", "waiting"], keys
    assert keys["handled"] == ["ArrowLeft", "Home", "ArrowRight", "End", "ArrowRight"], keys
    # A key that is not this control's is left to the page - the view in effect after Escape,
    # PageDown and Tab is the one before them, and none of the three was a movement. Swallowing
    # every keystroke to move a tab breaks the scroll keys on the view underneath.
    assert keys["ignored"] == ["waiting", "waiting", "waiting"], keys
    assert keys["reads"] == 0, (
        "the arrow keys re-read the tab per keypress: " + str(keys["reads"])
    )


def test_the_periods_window_is_what_the_control_changes(results):
    """One tap asks the server for that window - a re-read, not a filter over the old figures."""
    preset = results["preset"]
    assert preset["before"][0].endswith("/admin/dashboard?days=7"), preset["before"]
    assert preset["days"] == "month", preset
    assert preset["after"][-1].endswith("/admin/dashboard?days=month"), preset["after"]
    assert len(preset["after"]) == len(preset["before"]) + 1, preset["after"]
    assert preset["tab"] == "Dashboard", preset


def test_a_window_nobody_worked_and_a_window_that_could_not_be_read(results):
    """Empty is a state; unread is not - and the panel never draws one as the other."""
    empty = results["period_empty"]
    assert empty["zeroes"] == ["0", "0"], empty
    assert empty["rows"] == 0 and empty["none"] == 2, empty
    assert empty["says_so"] is True, empty
    assert empty["window"] == "2026-09-22 to 2026-09-28", (
        "an empty summary is still a summary of a window: " + str(empty)
    )
    # A quiet window that is also fully signed off says *that*, and offers nobody a queue.
    assert empty["payroll"] == "ready", empty
    assert empty["payroll_link"] is None, empty

    broken = results["period_unreadable"]
    assert broken["flag"] is True, broken
    assert broken["no_zero"] is True, (
        "a panel that could not be read drew a figure for this window: " + str(broken)
    )
    # No window control over a panel whose window could not be counted: tapping it would re-ask
    # for the thing the server has just said it cannot answer.
    assert broken["presets"] == 0, broken
    assert broken["strip"] is False, (
        "a day strip was drawn for a window the server could not count: " + str(broken)
    )
    assert broken["other_views"] is True, (
        "one unreadable view took the views beside it: " + str(broken)
    )


def test_the_window_hands_itself_over_as_the_files_the_api_writes(results):
    """The two downloads: a summary that cannot hand you the file sends the reader elsewhere.

    Three things make each of these an export rather than a download button: the *window* it asks
    for is the one on screen (a file named for one period and holding another gets forwarded as the
    period's record), the request carries this session's token (the route is ``admin_only``, so
    without it the browser saves a 401 as ``attendance_....csv``), and the bytes that land on disk
    are the server's answer rather than anything this screen assembled - which is why the fixture's
    CSV is a string no frontend code holds, and why the Excel half of this test is the one that
    proves ``saveBlob`` exists at all: an .xlsx round-tripped through a string does not survive.
    """
    exported = results["export"]
    # One request per format, and each one asks for its own - the two are not the same call with a
    # different file name.
    assert exported["formats"] == ["csv", "xlsx"], exported
    assert exported["auth"] == "Bearer tok-admin", exported

    assert "/admin/reports/export" in exported["csv"]["url"], exported["csv"]
    # The route's own parameters, and the window the period card was drawn from.
    assert "kind=attendance" in exported["csv"]["url"], exported["csv"]
    assert "start=2026-09-22" in exported["csv"]["url"], exported["csv"]
    assert "end=2026-09-28" in exported["csv"]["url"], exported["csv"]
    # The file's name is the route's own convention for the same window, so a file pulled from here
    # and one pulled from the API are visibly the same report - and the extension is the format.
    assert exported["csv"]["name"] == "attendance_20260922-20260928.csv", exported["csv"]
    assert exported["csv"]["file"] == "Employee,id,site,hours\r\nRow 1,1,Depot,8\r\n", exported["csv"]
    assert "format=xlsx" in exported["xlsx"]["url"], exported["xlsx"]
    assert exported["xlsx"]["name"] == "attendance_20260922-20260928.xlsx", exported["xlsx"]
    # The bytes are the server's: the marker the responder answered this URL with, which is not
    # something the CSV path could have produced - the two formats do not share a reader.
    assert exported["xlsx"]["file"] == "PK\x00xlsx\x00", exported["xlsx"]
    # A download is a request but not a *read*: the snapshot it came from is untouched.
    assert exported["reads"] == 2, exported


def test_the_window_prints_as_a_sheet_of_its_own_figures(results):
    """The third artifact: paper, drawn by the browser, from the card's own content.

    It is deliberately *not* the attendance report - that is the CSV and the spreadsheet, one row
    per worker, which the server builds and this card cannot: the card holds aggregates, and
    aggregates cannot be turned back into rows. So the sheet is the summary the reader is looking
    at, and the thing worth asserting is that it carries the same figures under the same words, in
    the frame the helper draws for every report in this app.
    """
    printed = results["export"]["print"]
    # No request: the sheet is built from the snapshot already in hand.
    assert printed["asked_nothing"] is True, printed
    assert printed["prints"] == 1, printed
    # The dialog names the file after the document title, and the title carries the window with no
    # extension - the dialog appends one, and "...csv.pdf" is what a title with one produces.
    assert printed["title"] == "attendance_20260922-20260928", printed
    # ...and the page is out of the way of the paper while the dialog is open.
    assert printed["printing"] is True, printed

    sheet = printed["sheet"]
    # The frame: whose document it is, what it is, and which window it covers - the period line is
    # the helper's, so both dates and the arrow between them are not this screen's opinion.
    assert "print-sheet-title" in sheet and "Attendance summary" in sheet, sheet
    assert "Period: 2026-09-22 \u2192 2026-09-28" in sheet, sheet
    # The stamp travels, because a printed summary is a snapshot and paper cannot refresh itself.
    assert "2026-09-28 14:03:11" in sheet, sheet
    # The figures, under the sheet's own two column headings.
    assert ">Figure<" in sheet and ">Value<" in sheet, sheet
    assert ">Days present<" in sheet and ">132<" in sheet, sheet
    assert ">Approved hours<" in sheet and ">488.5<" in sheet, sheet
    # The day strip as a block of rows - every day, both figures on it, because a height on a strip
    # is not a figure anybody can quote off paper.
    assert ">Each day<" in sheet, sheet
    assert ">2026-09-25<" in sheet and ">26 present · 2 late<" in sheet, sheet
    assert ">2026-09-27<" in sheet and ">0 present · 0 late<" in sheet, sheet
    # Both linkages, by name: the two people the card says are worth opening.
    assert ">Least present<" in sheet and ">Row 43<" in sheet, sheet
    assert ">Most late arrivals<" in sheet, sheet
    # What the window adds up to, and the note that says which figures count.
    assert "3 shifts (12 h) in this window are not approved yet" in sheet, sheet
    assert "Counted over this window only." in sheet, sheet
    # The company's own head, drawn by the helper from the settings row rather than by this screen.
    assert "print-sheet-brand" in sheet, sheet
