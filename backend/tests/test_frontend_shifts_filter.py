"""The admin shifts timesheet, exercised for real.

WHY THIS EXISTS
---------------
The Shifts tab used to render the newest page of raw ``/admin/logs`` and call it
shifts. That was wrong in a way no backend test can see: no period (so "this
month's" totals are really "the last 500 clock events"), no approval state, and a
500-row cap a busy month runs past. It was then a per-worker monthly total - which
was still a payroll report, with the money hidden - and it is now what it claims to
be: **a timesheet**, one row per shift.

The server answers ``/admin/reports/shifts?start=&end=`` with those rows; this suite
runs the real frontend files in a Node VM with a stubbed DOM and asserts the
behaviour the screen depends on:

1. the tab opens on the current month and asks the server for exactly that window
   (the period is derived server-side, never assumed on the client);
2. changing the two dates changes the request *and* the figures shown;
3. ``end < start`` is refused locally, with a message, and without a request;
3b. the one-click presets fetch a real window - whole calendar months, a week that
   starts on the site's Sunday, and never an end date in the future;
3c. the shown period is mirrored into ``#shifts=start..end``, and a shared link is
   honoured at boot and on ``hashchange`` (period *and* tab), while a fragment that
   is not a usable period is ignored - and a fragment still using the pre-rename
   ``#payroll=`` keeps working, because those links are already out in the world;
4. one row is one shift, with its own date, site and hours, and a shift waiting for
   an administrator is marked as such and kept out of ``approved_hours``;
4b. the columns are the administrator's to rearrange - the default order is
   date, employee, id, site, hours, awaiting approval, open notes - the choice is
   remembered in ``localStorage``, and a stored order that is stale or junk cannot
   take the table down;
5. the CSV button writes exactly the rows on screen - same set, same order as the
   table, filtered or not - in the four columns ``Employee,id,site,hours`` and in
   that order whatever the screen's own column order has been changed to.

The search box has its own contract (name / worker id / site / day): a day is an
ordinary filter here, because a timesheet row *has* a date; see the ``3g`` block.

The tab is named **Shifts** and shows hours and approval state only. The report it
reads carries no ``hourly_rate`` and no ``gross_estimate`` any more, and one test
here asserts that no money reaches the screen either way: a tab that calls itself
shifts must not quietly be a payment screen.

Node is optional; without it these skip rather than fail.
"""

from __future__ import annotations

import pytest

import frontend_vm

pytestmark = pytest.mark.skipif(frontend_vm.NODE is None, reason="node is not installed")

# The scenarios this suite drives: the fake API answers and the runs that fill ``results``.
# The VM, the stub DOM, the recorded fetch and the exit that prints the results come from
# ``frontend_vm``: the files under test need one environment, not one per suite, and two
# copies of "what the browser gives these files" is how one of them stops being true.
HARNESS = r"""
// --- the server's answer, faked ------------------------------------------

// One shift, with everything the timesheet row can show. The dates are all inside the
// default month-to-date window the tab opens on, which is what makes "did this come from
// the request's range?" answerable for the searches below.
function shift(overrides) {
    return Object.assign({
        log_id: 900,
        date: '2026-08-07',
        timestamp: '2026-08-07 16:02:11',
        worker_id: '600',
        worker_name: 'Seed Lead',
        role: 'moallem',
        site_name: 'Downtown Tower A',
        hours: 8,
        recorded_hours: 8.5,
        approved_hours: null,
        break_hours: 0.5,
        status_code: 'approved',
        status: 'Approved by Admin',
        awaiting_approval: false,
        open_notes: 0
    }, overrides || {});
}

// The totals are derived here rather than typed twice, so a fixture cannot describe rows
// whose header disagrees with them. The Python assertions still use literal numbers.
function totalsOf(rows) {
    const sum = (key) => rows.reduce((total, row) => total + (Number(row[key]) || 0), 0);
    return {
        shifts: rows.length,
        workers: new Set(rows.map((row) => row.worker_id)).size,
        hours: sum('hours'),
        approved_hours: rows.reduce((total, row) => total + (row.awaiting_approval ? 0 : Number(row.hours) || 0), 0),
        awaiting_approval_hours: rows.reduce((total, row) => total + (row.awaiting_approval ? Number(row.hours) || 0 : 0), 0),
        awaiting_approval: rows.filter((row) => row.awaiting_approval).length,
        break_hours: sum('break_hours'),
        workers_with_open_notes: new Set(
            rows.filter((row) => Number(row.open_notes) > 0).map((row) => row.worker_id)
        ).size
    };
}

// Three shifts, three workers, two sites, one of them waiting for an administrator and two
// of the workers with something open in the notes inbox. A one-row fixture cannot tell a
// working search - or a working total - apart from a broken one.
const DEFAULT_ROWS = [
    shift({
        log_id: 901, date: '2026-08-07', worker_id: '600', worker_name: 'Seed Lead',
        site_name: 'Downtown Tower A', hours: 8, recorded_hours: 8.5, open_notes: 1
    }),
    shift({
        log_id: 902, date: '2026-08-06', timestamp: '2026-08-06 15:04:00', worker_id: '601',
        worker_name: 'Ana Torres', role: 'worker', site_name: 'Harbour Depot',
        hours: 4, recorded_hours: 4.5, break_hours: 0.5
    }),
    shift({
        log_id: 903, date: '2026-08-05', timestamp: '2026-08-05 16:30:00', worker_id: '602',
        worker_name: 'Bilal Khan', role: 'worker', site_name: 'Harbour Depot',
        hours: 3, recorded_hours: 5, break_hours: 0.5,
        status_code: 'pending_review', status: 'pending_review', awaiting_approval: true, open_notes: 2
    })
];

function report(start, end, rows) {
    return {
        period: { start: start, end: end },
        filters: { site: null, worker_id: null },
        fields: [
            'log_id', 'date', 'timestamp', 'worker_id', 'worker_name', 'role', 'site_name',
            'hours', 'recorded_hours', 'approved_hours', 'break_hours', 'status_code', 'status',
            'awaiting_approval', 'open_notes'
        ],
        note: 'A timesheet: one row per shift. Only hours an administrator has approved count.',
        rows: rows,
        totals: totalsOf(rows)
    };
}

// Windows chosen so "did this come from the request's range?" is answerable: the default
// (month-to-date) window holds 15 h, August holds 40 h, the searched-for day holds 8 h,
// and any other window is empty.
function shiftsResponder(url) {
    if (!url.includes('/admin/reports/shifts')) return { status: 200, body: {} };
    const params = new URLSearchParams(url.split('?')[1] || '');
    const start = params.get('start');
    const end = params.get('end');
    if (start > end) return { status: 400, body: { detail: 'end must not be before start.' } };
    if (start === '2000-01-01') return { status: 200, body: report(start, end, []) };
    if (start === '2001-01-01') {
        return {
            status: 200,
            body: report(start, end, [shift({
                log_id: 910, worker_name: '<img src=x onerror=alert(1)>Mallory',
                site_name: '<script>alert(2)</script> Depot', hours: 50, recorded_hours: 50
            })])
        };
    }
    if (start === '2002-01-01') {
        return {
            status: 200,
            body: report(start, end, [shift({
                log_id: 911, worker_id: '603', worker_name: 'Noor Haddad', site_name: null,
                hours: 6, recorded_hours: 6, break_hours: 0
            })])
        };
    }
    if (start === '2026-08-01' && end === '2026-08-31') {
        return {
            status: 200,
            body: report(start, end, [shift({
                log_id: 920, date: '2026-08-31', hours: 40, recorded_hours: 41, break_hours: 1
            })])
        };
    }
    if (start === '2026-08-07' && end === '2026-08-07') {
        return {
            status: 200,
            body: report(start, end, [shift({
                log_id: 901, date: '2026-08-07', hours: 8, recorded_hours: 8.5, break_hours: 0.5
            })])
        };
    }
    return { status: 200, body: report(start, end, DEFAULT_ROWS) };
}

// --- reading the rendered page -------------------------------------------

function expectedDefaultRange() {
    const now = new Date();
    const pad = (v) => String(v).padStart(2, '0');
    const iso = (d) => d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
    return { start: iso(new Date(now.getFullYear(), now.getMonth(), 1)), end: iso(now) };
}

// The intended meaning of the three presets, written out independently of the
// module: a whole calendar month, but never a future end date.
function expectedPresets() {
    const now = new Date();
    const pad = (v) => String(v).padStart(2, '0');
    const iso = (d) => d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
    return {
        thisMonth: {
            start: iso(new Date(now.getFullYear(), now.getMonth(), 1)),
            end: iso(now)
        },
        lastMonth: {
            start: iso(new Date(now.getFullYear(), now.getMonth() - 1, 1)),
            end: iso(new Date(now.getFullYear(), now.getMonth(), 0))
        },
        thisWeek: {
            start: iso(new Date(now.getFullYear(), now.getMonth(), now.getDate() - now.getDay())),
            end: iso(now)
        }
    };
}

function inputValue(html, id) {
    const match = new RegExp('id="' + id + '"[^>]*value="([^"]*)"').exec(html);
    return match ? match[1] : null;
}

// The shifts table alone, so a second table in the tab cannot be read as this one.
function shiftsTable(html) {
    const start = html.indexOf('data-shifts-table="true"');
    if (start < 0) return '';
    return html.slice(start, html.indexOf('</table>', start));
}

// The header, as the reader sees it: the labels, in the order they are painted. Read off
// the table's own ``th``/``td`` rather than off a padding class: the cells carry no class
// of their own now, and their padding comes from ``.ui-table`` - a class the suite would
// be pinning a framework's name for rather than the cell it is about.
function headerLabels(html) {
    return (shiftsTable(html).match(/<th[^>]*>[\s\S]*?<\/th>/g) || [])
        .map((cell) => cell.replace(/<[^>]*>/g, '').trim());
}

// Every row's cells, as text, in the order they are painted: what a reader would read
// off the table, line by line.
function allRows(html) {
    return (shiftsTable(html).match(/<tr data-shift="[^"]*"[\s\S]*?<\/tr>/g) || [])
        .map((row) => (row.match(/<td[^>]*>[\s\S]*?<\/td>/g) || [])
            .map((cell) => cell.replace(/<[^>]*>/g, '').trim()));
}

// Each total card carries data-total / data-value, so the figures are readable without
// hard-coding Tailwind classes into the assertions.
function cardValues(html) {
    const value = (key) => {
        const idx = html.indexOf('data-total="' + key + '"');
        if (idx < 0) return null;
        const match = /data-value="([^"]*)"/.exec(html.slice(idx, idx + 200));
        return match ? match[1] : null;
    };
    const order = /data-column-editor data-order="([^"]*)"/.exec(html);
    return {
        hours: value('hours'),
        approved_hours: value('approved_hours'),
        awaiting_approval_hours: value('awaiting_approval_hours'),
        awaiting_approval: value('awaiting_approval'),
        break_hours: value('break_hours'),
        shifts: value('shifts'),
        workers: value('workers'),
        money_visible: ['Gross estimate', 'Rate/hour', 'hourly_rate', 'gross_estimate'].some(
            (needle) => html.indexOf(needle) >= 0
        ),
        has_table: html.indexOf('<table') >= 0,
        has_empty_note: html.indexOf('No shifts in this period.') >= 0,
        has_filter: html.indexOf('id="shiftsFilter"') >= 0,
        has_search_box: html.indexOf('id="shiftsQuery"') >= 0,
        has_filter_note: html.indexOf('data-filter-note') >= 0,
        has_no_matches: html.indexOf('data-no-matches') >= 0,
        has_day_chip: html.indexOf('data-show-day') >= 0,
        has_columns_panel: html.indexOf('id="shiftsColumns"') >= 0,
        has_timesheet_note: html.indexOf('One row per shift.') >= 0,
        column_order: order ? order[1].split(',') : null,
        headers: headerLabels(html),
        // One per shift: a count that goes up with the number of shifts, not with the number
        // of people - which is what makes it a timesheet rather than a roster.
        shift_rows: (html.match(/data-shift=/g) || []).length,
        awaiting_rows: (html.match(/data-awaiting="true"/g) || []).length,
        // Built from a code point, not a literal: the harness writes to stdout as UTF-8
        // and a literal dash would be a different character by the time Python reads it.
        em_dash: String.fromCharCode(8212),
        empty_site_cell: html.indexOf('>' + String.fromCharCode(8212) + '<') >= 0,
        rows: allRows(html),
        first_row: allRows(html)[0] || null,
        site_names: (html.match(/Downtown Tower A|Harbour Depot/g) || []),
        input_start: inputValue(html, 'shiftsStart'),
        input_end: inputValue(html, 'shiftsEnd'),
        input_query: inputValue(html, 'shiftsQuery')
    };
}

function lastQuery(env) {
    const urls = env.requests.filter((r) => r.url.includes('/admin/reports/shifts')).map((r) => r.url);
    if (urls.length === 0) return null;
    const params = new URLSearchParams(urls[urls.length - 1].split('?')[1] || '');
    return { start: params.get('start'), end: params.get('end') };
}

function toasts(env) {
    return env.evaluate("(document.getElementById('toastRoot').__children || []).map((el) => el.textContent)");
}

function adminEnv(options) {
    const env = boot(options);
    env.setResponder(shiftsResponder);
    env.evaluate("State.saveUser({ id: '1', name: 'Admin', role: 'admin', token: 'tok-admin' })");
    return env;
}

const DEFAULT_COLUMNS = 'date,employee,id,site,hours,awaiting,notes';
// The labels those columns are painted with, in English (the harness boots in the default
// language). Written out here so a renamed translation cannot pass unnoticed.
const DEFAULT_HEADERS = ['Date', 'Employee', 'User ID', 'Site', 'Hours', 'Awaiting approval', 'Open notes'];

// The scenarios. ``results`` is printed by the epilogue in ``frontend_vm``.
const results = {};

    // 1. the tab opens on the current month and asks the server for that window
    {
        const env = adminEnv();
        await env.evaluate("UI.renderAdminTab('Shifts')");
        const html = env.evaluate("document.getElementById('adminContent').innerHTML");
        results.initial = Object.assign(cardValues(html), {
            expected: expectedDefaultRange(),
            query: lastQuery(env),
            urls: env.replacedUrls,
            read_raw_logs: env.requests.some((r) => r.url.includes('/admin/logs')),
            columns: env.evaluate("UI_MODULES.shiftsColumns().join(',')")
        });
    }

    // 2. moving the two dates must move the request and the figures
    {
        const env = adminEnv();
        await env.evaluate("UI.renderAdminTab('Shifts')");
        await env.evaluate(`(async () => {
            document.getElementById('shiftsStart').value = '2026-08-01';
            document.getElementById('shiftsEnd').value = '2026-08-31';
            await UI_MODULES.applyShiftsFilter();
        })()`);
        const html = env.evaluate("document.getElementById('adminContent').innerHTML");
        results.changed = Object.assign(cardValues(html), {
            query: lastQuery(env),
            state: env.evaluate('State.shiftsRange'),
            requests: env.requests.filter((r) => r.url.includes('/admin/reports/shifts')).length
        });
    }

    // 3. an unusable period is refused while the admin is still looking at the dates
    {
        const env = adminEnv();
        const shown = "State.shiftsRange.start + '..' + State.shiftsRange.end";
        await env.evaluate("UI.renderAdminTab('Shifts')");
        await env.evaluate("UI_MODULES.setShiftsRange('2026-08-01', '2026-08-31')");
        const before = env.evaluate(shown);
        const requestsBefore = env.requests.length;
        const acceptedBlank = await env.evaluate("UI_MODULES.setShiftsRange('', '2026-09-01')");
        const accepted = await env.evaluate("UI_MODULES.setShiftsRange('2026-09-10', '2026-09-01')");
        results.invalid = {
            accepted: accepted,
            accepted_blank: acceptedBlank,
            state_before: before,
            state_after: env.evaluate(shown),
            report_requests_after: env.requests.slice(requestsBefore).filter((r) => r.url.includes('/admin/reports/shifts')).length,
            toasts: toasts(env)
        };
    }

    // 3b. one tap on a preset must fetch that preset's real window
    {
        const env = adminEnv();
        await env.evaluate("UI.renderAdminTab('Shifts')");
        const listed = env.evaluate("UI_MODULES.shiftsPresets().map((p) => [p.key, p.range.start, p.range.end, p.active])");
        const htmlBefore = env.evaluate("document.getElementById('adminContent').innerHTML");
        await env.evaluate("UI_MODULES.applyShiftsPreset('lastMonth')");
        const htmlAfter = env.evaluate("document.getElementById('adminContent').innerHTML");
        const requests = env.requests.filter((r) => r.url.includes('/admin/reports/shifts'));
        const params = new URLSearchParams(requests[requests.length - 1].url.split('?')[1] || '');
        results.presets = {
            listed: listed,
            expected: expectedPresets(),
            buttons_before: (htmlBefore.match(/data-preset="([a-zA-Z]+)" data-active="([a-z]+)"/g) || []),
            buttons_after: (htmlAfter.match(/data-preset="([a-zA-Z]+)" data-active="([a-z]+)"/g) || []),
            applied: { start: params.get('start'), end: params.get('end') },
            state: env.evaluate("State.shiftsRange.start + '..' + State.shiftsRange.end"),
            input_start: inputValue(htmlAfter, 'shiftsStart'),
            input_end: inputValue(htmlAfter, 'shiftsEnd'),
            urls: env.replacedUrls
        };
    }

    // 3c. a shared link has to open on the shared period and search, and on the shifts tab
    {
        const env = adminEnv({ hash: '#shifts=2026-08-01..2026-08-31&q=tower' });
        await env.evaluate('UI.init()');
        // Boot paints the console without awaiting the shifts fetch - the first paint
        // must never wait on the network - so let that promise chain settle.
        await env.evaluate('new Promise((resolve) => setTimeout(resolve, 0))');
        const html = env.evaluate("document.getElementById('adminContent').innerHTML");
        results.shared_link = Object.assign(cardValues(html), {
            tab: env.evaluate('State.adminTab'),
            state: env.evaluate("State.shiftsRange ? State.shiftsRange.start + '..' + State.shiftsRange.end : null"),
            query: lastQuery(env)
        });
    }

    // 3d. a fragment that is not a usable period must not take the page over
    {
        const junk = adminEnv({ hash: '#shifts=banana' });
        await junk.evaluate('UI.init()');
        const backwards = adminEnv({ hash: '#shifts=2026-09-10..2026-09-01' });
        await backwards.evaluate('UI.init()');
        results.junk_link = {
            junk_state: junk.evaluate("State.shiftsRange ? 'set' : 'unset'"),
            junk_tab: junk.evaluate('State.adminTab'),
            junk_report_requests: junk.requests.filter((r) => r.url.includes('/admin/reports/shifts')).length,
            backwards_state: backwards.evaluate("State.shiftsRange ? 'set' : 'unset'"),
            backwards_tab: backwards.evaluate('State.adminTab')
        };
    }

    // 3d2. a link shared before the rename must still open on its period
    {
        const legacy = adminEnv({ hash: '#payroll=2026-08-01..2026-08-31' });
        await legacy.evaluate('UI.init()');
        await legacy.evaluate('new Promise((resolve) => setTimeout(resolve, 0))');
        results.legacy_link = Object.assign(
            cardValues(legacy.evaluate("document.getElementById('adminContent').innerHTML")),
            {
                state: legacy.evaluate("State.shiftsRange ? State.shiftsRange.start + '..' + State.shiftsRange.end : null"),
                tab: legacy.evaluate('State.adminTab'),
                query: lastQuery(legacy)
            }
        );
    }

    // 3e. a link pasted into this tab applies without a reload
    {
        const env = adminEnv();
        await env.evaluate('UI.init()');
        const beforeTab = env.evaluate('State.adminTab');
        env.fireHashChange('#shifts=2026-08-01..2026-08-31');
        const adopted = env.evaluate("State.shiftsRange ? State.shiftsRange.start + '..' + State.shiftsRange.end : null");
        await env.evaluate('UI.applyShiftsLink()');
        const html = env.evaluate("document.getElementById('adminContent').innerHTML");
        results.hash_change = Object.assign(cardValues(html), {
            before_tab: beforeTab,
            adopted: adopted,
            tab: env.evaluate('State.adminTab'),
            query: lastQuery(env)
        });
    }

    // 3f. the copy button hands over the period on screen
    {
        const env = adminEnv();
        await env.evaluate("UI.renderAdminTab('Shifts')");
        await env.evaluate("UI_MODULES.setShiftsRange('2026-08-01', '2026-08-31')");
        await env.evaluate("UI.renderAdminTab('Shifts')");
        const html = env.evaluate("document.getElementById('adminContent').innerHTML");
        await env.evaluate('UI_MODULES.copyShiftsLink()');
        results.copy_link = {
            has_button: html.indexOf('data-copy-link') >= 0,
            copied: env.copiedUrls,
            toasts: toasts(env)
        };
    }

    // 3g. the search box: name, id, site, several terms - and a day
    {
        const env = adminEnv();
        await env.evaluate("UI.renderAdminTab('Shifts')");
        const requestsBefore = env.requests.length;
        const search = async (query) => {
            await env.evaluate(`(async () => {
                document.getElementById('shiftsQuery').value = ${JSON.stringify(query)};
                await UI_MODULES.applyShiftsSearch();
            })()`);
            const html = env.evaluate("document.getElementById('adminContent').innerHTML");
            return Object.assign(cardValues(html), {
                query: env.evaluate('State.shiftsQuery'),
                url: env.replacedUrls[env.replacedUrls.length - 1]
            });
        };
        const byName = await search('torres');
        const byId = await search('600');
        const bySite = await search('harbour');
        // Two terms, both of which one shift has and the other does not: per-shift rows
        // carry one site each, so the term pair has to be name + site.
        const andTerms = await search('harbour khan');
        // A day is a filter like any other: a row carries its own date, so typing one
        // narrows the list instead of standing there refusing to.
        const byDate = await search('2026-08-07');
        const afterSearches = env.requests.length;
        await env.evaluate("UI_MODULES.applyShiftsDay('2026-08-07')");
        const afterDay = env.requests.length;
        const dayApplied = Object.assign(
            cardValues(env.evaluate("document.getElementById('adminContent').innerHTML")),
            {
                state: env.evaluate("State.shiftsRange.start + '..' + State.shiftsRange.end"),
                query: env.evaluate('State.shiftsQuery'),
                request: lastQuery(env)
            }
        );
        const noMatch = await search('zzz');
        results.search = {
            expected: expectedDefaultRange(),
            by_name: byName,
            by_id: byId,
            by_site: bySite,
            and_terms: andTerms,
            by_date: byDate,
            no_match: noMatch,
            day_applied: dayApplied,
            search_requests_added: afterSearches - requestsBefore,
            day_requests_added: afterDay - afterSearches
        };
    }

    // 4a. the columns the tab opens with, in the order it was asked for
    {
        const env = adminEnv();
        await env.evaluate("UI.renderAdminTab('Shifts')");
        const html = env.evaluate("document.getElementById('adminContent').innerHTML");
        results.columns_default = Object.assign(cardValues(html), {
            columns: env.evaluate("UI_MODULES.shiftsColumns().join(',')"),
            stored: env.evaluate("localStorage.getItem('shiftsColumns')")
        });
    }

    // 4b. moving a column to the front: the header, the panel and every row follow it
    {
        const env = adminEnv();
        await env.evaluate("UI.renderAdminTab('Shifts')");
        const steps = env.evaluate(`(function () {
            const seen = [];
            for (let i = 0; i < 5; i += 1) {
                UI_MODULES.moveShiftsColumn('awaiting', -1);
                seen.push(UI_MODULES.shiftsColumns().join(','));
            }
            return seen;
        })()`);
        const html = env.evaluate("document.getElementById('adminContent').innerHTML");
        results.columns_moved = Object.assign(cardValues(html), {
            steps: steps,
            stored: env.evaluate("localStorage.getItem('shiftsColumns')"),
            // The whole tab is repainted, so the figures have to survive the reorder. (The
            // period comes from the module, not ``State``: a first render is on the default
            // window, which is chosen by ``shiftsRange()`` and never written to State.)
            state: env.evaluate("UI_MODULES.shiftsRange().start + '..' + UI_MODULES.shiftsRange().end")
        });
    }

    // 4c. the choice outlives the page: a re-render (and a language switch) keeps it
    {
        const env = adminEnv();
        await env.evaluate("UI.renderAdminTab('Shifts')");
        // Two steps towards the front of the table, which is where that tab is read from.
        await env.evaluate("UI_MODULES.moveShiftsColumn('notes', -1)");
        await env.evaluate("UI_MODULES.moveShiftsColumn('notes', -1)");
        const before = env.evaluate("UI_MODULES.shiftsColumns().join(',')");
        await env.evaluate("UI.renderAdminTab('Shifts')");
        const htmlAfter = env.evaluate("document.getElementById('adminContent').innerHTML");
        results.columns_persist = Object.assign(cardValues(htmlAfter), {
            before: before,
            stored: env.evaluate("localStorage.getItem('shiftsColumns')")
        });
    }

    // 4d. a stored order that is stale or junk must not take the table down
    {
        const stale = adminEnv();
        stale.evaluate(`localStorage.setItem('shiftsColumns', JSON.stringify(['notes', 'banana', 'notes', 'date']))`);
        await stale.evaluate("UI.renderAdminTab('Shifts')");
        const staleHtml = stale.evaluate("document.getElementById('adminContent').innerHTML");

        const junk = adminEnv();
        junk.evaluate("localStorage.setItem('shiftsColumns', '{not json')");
        await junk.evaluate("UI.renderAdminTab('Shifts')");
        const junkHtml = junk.evaluate("document.getElementById('adminContent').innerHTML");

        const wrongType = adminEnv();
        wrongType.evaluate("localStorage.setItem('shiftsColumns', '\"date\"')");
        await wrongType.evaluate("UI.renderAdminTab('Shifts')");

        results.columns_repair = {
            stale: Object.assign(cardValues(staleHtml), {
                order: stale.evaluate("UI_MODULES.shiftsColumns().join(',')")
            }),
            junk: Object.assign(cardValues(junkHtml), {
                order: junk.evaluate("UI_MODULES.shiftsColumns().join(',')")
            }),
            wrong_type: wrongType.evaluate("UI_MODULES.shiftsColumns().join(',')")
        };
    }

    // 4e. the ends of the list are ends: nothing moves off the edge, and reset forgets
    {
        const env = adminEnv();
        await env.evaluate("UI.renderAdminTab('Shifts')");
        const atStart = env.evaluate("UI_MODULES.moveShiftsColumn('date', -1) === undefined ? 'no-move' : 'moved'");
        const html = env.evaluate("document.getElementById('adminContent').innerHTML");
        // Each chip disables the button that would take it off the end of the list.
        const chip = (key) => {
            const match = new RegExp('data-column="' + key + '"[\\s\\S]*?</span>').exec(html);
            return match ? match[0] : '';
        };
        const edges = {
            first_cannot_go_earlier: /<button[^>]*data-move-earlier[^>]*disabled/.test(chip('date')),
            last_cannot_go_later: /<button[^>]*data-move-later[^>]*disabled/.test(chip('notes'))
        };
        await env.evaluate("UI_MODULES.moveShiftsColumn('notes', -1)");
        const beforeReset = env.evaluate("UI_MODULES.shiftsColumns().join(',')");
        await env.evaluate("UI_MODULES.resetShiftsColumns()");
        const afterHtml = env.evaluate("document.getElementById('adminContent').innerHTML");
        results.columns_reset = Object.assign(cardValues(afterHtml), {
            at_start: atStart,
            edges: edges,
            before_reset: beforeReset,
            stored_after_reset: env.evaluate("localStorage.getItem('shiftsColumns')")
        });
    }

    // 5. the CSV button writes exactly the rows on screen
    {
        const env = adminEnv();
        await env.evaluate("UI.renderAdminTab('Shifts')");
        const requestsBefore = env.requests.length;
        env.evaluate('UI_MODULES.downloadShiftsCsv()');
        const csv = await env.lastBlobText();
        const anchor = env.lastAnchor();
        results.download = {
            csv: csv,
            filename: anchor ? anchor.download : null,
            clicked: anchor ? anchor.__clicked === true : null,
            requests_added: env.requests.length - requestsBefore,
            expected_range: expectedDefaultRange()
        };

        // The same button, with a search on: the file has to hold the rows on screen.
        await env.evaluate(`(async () => {
            document.getElementById('shiftsQuery').value = 'harbour';
            await UI_MODULES.applyShiftsSearch();
        })()`);
        env.evaluate('UI_MODULES.downloadShiftsCsv()');
        results.download.filtered = {
            csv: await env.lastBlobText(),
            filename: env.lastAnchor().download,
            requests_added: env.requests.length - requestsBefore
        };

        // A search that matches nobody matches nobody in the file either.
        await env.evaluate(`(async () => {
            document.getElementById('shiftsQuery').value = 'zzz';
            await UI_MODULES.applyShiftsSearch();
        })()`);
        env.evaluate('UI_MODULES.downloadShiftsCsv()');
        results.download.no_match = { csv: await env.lastBlobText() };

        // With no report for the period on screen there is no honest file to write.
        const stale = adminEnv();
        await stale.evaluate("UI.renderAdminTab('Shifts')");
        stale.evaluate('UI_MODULES._shiftsReport = null');
        const blobsBefore = stale.blobs.length;
        stale.evaluate('UI_MODULES.downloadShiftsCsv()');
        results.download.nothing_to_export = {
            blobs_added: stale.blobs.length - blobsBefore,
            toasts: toasts(stale)
        };
    }

    // 5b. the file's columns are the file's, whatever the screen has been rearranged to
    {
        const env = adminEnv();
        await env.evaluate("UI.renderAdminTab('Shifts')");
        // All the way to the front, so "the screen really was rearranged" cannot be
        // satisfied by a table that was never repainted.
        await env.evaluate(`(function () {
            for (let i = 0; i < 6; i += 1) UI_MODULES.moveShiftsColumn('notes', -1);
        })()`);
        env.evaluate('UI_MODULES.downloadShiftsCsv()');
        results.download.reordered = {
            csv: await env.lastBlobText(),
            screen_headers: headerLabels(env.evaluate("document.getElementById('adminContent').innerHTML"))
        };
    }

    // 6. a period with no shifts says so instead of showing an empty table
    {
        const env = adminEnv();
        await env.evaluate("UI_MODULES.setShiftsRange('2000-01-01', '2000-01-31')");
        await env.evaluate("UI.renderAdminTab('Shifts')");
        results.empty = cardValues(env.evaluate("document.getElementById('adminContent').innerHTML"));
    }

    // 7. the phone layout gets one card per shift, listing the same columns in the same order
    {
        const env = adminEnv();
        env.evaluate("localStorage.setItem('layoutOverride', 'mobile')");
        await env.evaluate("UI.renderAdminTab('Shifts')");
        const html = env.evaluate("document.getElementById('adminContent').innerHTML");
        results.mobile = Object.assign(cardValues(html), {
            labels: (html.match(/<dt[^>]*\bdata-shift-label\b[^>]*>[\s\S]*?<\/dt>/g) || [])
                .map((cell) => cell.replace(/<[^>]*>/g, '').trim())
        });
    }

    // 7b. a shift with no site on file still gets a row, and the row says so
    {
        const env = adminEnv();
        await env.evaluate("UI_MODULES.setShiftsRange('2002-01-01', '2002-01-31')");
        await env.evaluate("UI.renderAdminTab('Shifts')");
        const html = env.evaluate("document.getElementById('adminContent').innerHTML");
        results.sites = { no_site: cardValues(html) };
    }

    // 8. a worker name or a site must not become markup
    {
        const env = adminEnv();
        await env.evaluate("UI_MODULES.setShiftsRange('2001-01-01', '2001-01-31')");
        await env.evaluate("UI.renderAdminTab('Shifts')");
        const html = env.evaluate("document.getElementById('adminContent').innerHTML");
        results.escaped = {
            raw_tag: html.indexOf('<img src=x') >= 0,
            escaped_tag: html.indexOf('&lt;img src=x') >= 0,
            name_present: html.indexOf('Mallory') >= 0,
            raw_site_tag: html.indexOf('<script>alert(2)</script>') >= 0,
            escaped_site_tag: html.indexOf('&lt;script&gt;alert(2)&lt;/script&gt; Depot') >= 0,
            hours: cardValues(html).hours
        };
    }

"""


@pytest.fixture(scope="module")
def results() -> dict:
    return frontend_vm.run(HARNESS)


def test_the_tab_opens_on_the_current_month_and_asks_for_exactly_that_window(results):
    """The period has to be the one on screen, or the figures belong to other dates."""
    initial = results["initial"]
    assert initial["has_filter"] is True, "the admin needs a period picker, not a fixed window"
    assert initial["query"] == initial["expected"]
    assert initial["input_start"] == initial["expected"]["start"]
    assert initial["input_end"] == initial["expected"]["end"], "the picker must show what was fetched"


def test_every_row_is_a_shift_and_the_totals_come_from_the_report(results):
    """`/admin/logs` is a capped, per-event page: no period, no totals, no approval state."""
    initial = results["initial"]
    assert initial["read_raw_logs"] is False
    assert initial["shift_rows"] == 3, "one row per shift - three shifts, three rows"
    assert initial["has_table"] is True
    assert initial["hours"] == "15", "8 + 4 + 3 h of shifts"
    assert initial["approved_hours"] == "12", "8 + 4 h signed off"
    assert initial["awaiting_approval_hours"] == "3", "the third shift is still waiting"
    assert initial["awaiting_approval"] == "1", "one shift is waiting, not one worker"
    assert initial["break_hours"] == "1.5"
    assert initial["shifts"] == "3"
    assert initial["workers"] == "3"


def test_the_shifts_tab_is_hours_and_approval_and_never_money(results):
    """This tab used to carry a rate and a gross estimate. It does not, and must not."""
    initial = results["initial"]
    assert initial["money_visible"] is False, "this tab records hours; it does not price them"
    assert initial["has_timesheet_note"] is True, "an admin must be told what a row is"


def test_a_shift_waiting_for_an_administrator_is_marked_and_not_counted_as_approved(results):
    """The 8.1 h gate only means anything if unapproved hours stay out of the signed-off figure."""
    initial = results["initial"]
    assert initial["awaiting_rows"] == 1, "the one pending shift is marked in the markup"
    assert initial["approved_hours"] != initial["hours"], "waiting hours are not counted hours"
    # The third fixture row is the pending one (3 h of the period's 15) and its awaiting cell
    # says so, where an approved row names the decision that was made instead.
    assert initial["first_row"][5] == "Approved by Admin", initial["first_row"]
    assert initial["rows"][2][5] == "Awaiting approval", initial["rows"][2]


def test_the_default_column_order_is_the_one_the_tab_was_asked_for(results):
    default = results["columns_default"]
    assert default["columns"] == "date,employee,id,site,hours,awaiting,notes"
    assert default["column_order"] == ["date", "employee", "id", "site", "hours", "awaiting", "notes"]
    assert default["headers"] == ["Date", "Employee", "User ID", "Site", "Hours", "Awaiting approval", "Open notes"]
    assert default["has_columns_panel"] is True, "the admin needs a way to change it"
    assert default["stored"] is None, "a default order is not a choice anybody made"


def test_the_first_row_carries_the_column_values_in_that_order(results):
    """Date, name, id, site, hours, approval - the row has to line up with its header."""
    cells = results["columns_default"]["first_row"]
    assert cells is not None and len(cells) == 7, cells
    assert cells[0] == "2026-08-07"
    assert cells[1] == "Seed Lead"
    assert cells[2] == "600"
    assert cells[3] == "Downtown Tower A"
    assert cells[4] == "8"
    assert cells[5] == "Approved by Admin"
    assert cells[6] == "1", "this worker has one note open"


def test_moving_a_column_moves_it_in_the_header_the_panel_and_every_row(results):
    moved = results["columns_moved"]
    # Five steps to the left, one per button press, ending at the front of the table. One
    # step swaps a column with its neighbour and nothing else - no reshuffle, no jump.
    assert len(moved["steps"]) == 5
    assert moved["steps"][0] == "date,employee,id,site,awaiting,hours,notes"
    assert moved["steps"][1] == "date,employee,id,awaiting,site,hours,notes"
    assert moved["steps"][-1] == "awaiting,date,employee,id,site,hours,notes"
    assert moved["column_order"][0] == "awaiting"
    assert moved["headers"][0] == "Awaiting approval"
    # The cells follow the header: the newest shift is signed off, the one below it is not.
    assert moved["first_row"][0] == "Approved by Admin", moved["first_row"]
    assert moved["rows"][2][0] == "Awaiting approval", moved["rows"][2]
    assert moved["first_row"][1] == "2026-08-07"
    assert moved["first_row"][2] == "Seed Lead"
    assert sorted(moved["column_order"]) == sorted(["date", "employee", "id", "site", "hours", "awaiting", "notes"]), (
        "reordering must never lose or add a column"
    )
    assert moved["hours"] == "15", "and the figures survive the repaint"


def test_the_chosen_order_is_remembered_in_the_browser(results):
    moved = results["columns_moved"]
    assert moved["stored"] == '["awaiting","date","employee","id","site","hours","notes"]', (
        "the order has to outlive the repaint that follows the click"
    )
    persist = results["columns_persist"]
    assert persist["before"] == "date,employee,id,site,notes,hours,awaiting"
    assert persist["column_order"] == persist["before"].split(","), "a re-render keeps it"
    assert persist["headers"][4] == "Open notes", "and the header follows the stored order"


def test_a_stale_or_junk_stored_order_cannot_break_the_table(results):
    """A stored order is data from an older version of the file, and is treated as such."""
    repair = results["columns_repair"]
    # Unknown keys are dropped, duplicates collapse, and a column the stored order forgets is
    # appended - so a release that adds a column shows it instead of hiding it for ever.
    assert repair["stale"]["order"] == "notes,date,employee,id,site,hours,awaiting"
    assert repair["stale"]["headers"][0] == "Open notes"
    assert repair["stale"]["has_table"] is True
    assert repair["junk"]["order"] == "date,employee,id,site,hours,awaiting,notes", (
        "junk under the key falls back to the default order, it does not empty the table"
    )
    assert repair["junk"]["headers"] == ["Date", "Employee", "User ID", "Site", "Hours", "Awaiting approval", "Open notes"]
    assert repair["wrong_type"] == "date,employee,id,site,hours,awaiting,notes", (
        "a stored value that is not a list is not an order"
    )


def test_the_ends_of_the_column_list_are_ends_and_reset_puts_the_default_back(results):
    reset = results["columns_reset"]
    assert reset["at_start"] == "no-move", "the first column cannot move further left"
    assert reset["edges"]["first_cannot_go_earlier"] is True, "and the button says so"
    assert reset["edges"]["last_cannot_go_later"] is True
    assert reset["before_reset"] == "date,employee,id,site,hours,notes,awaiting", (
        "one step left puts Open notes beside the hours it explains"
    )
    assert reset["column_order"] == ["date", "employee", "id", "site", "hours", "awaiting", "notes"]
    assert reset["stored_after_reset"] is None, "reset forgets the choice, it does not store the default"


def test_moving_the_dates_changes_the_request_and_the_figures(results):
    changed = results["changed"]
    assert changed["query"] == {"start": "2026-08-01", "end": "2026-08-31"}
    assert changed["state"] == {"start": "2026-08-01", "end": "2026-08-31"}
    assert changed["input_start"] == "2026-08-01"
    assert changed["input_end"] == "2026-08-31"
    assert changed["hours"] == "40"
    assert changed["shift_rows"] == 1, "August's own shift, not the month-to-date ones"
    assert changed["query"] != results["initial"]["query"], "the same window would mean the filter did nothing"


def test_an_unusable_period_is_refused_without_a_request(results):
    """The server rejects both of these too, but the admin should hear it in the picker."""
    invalid = results["invalid"]
    assert invalid["state_before"] == "2026-08-01..2026-08-31"
    assert invalid["accepted_blank"] is False
    assert invalid["accepted"] is False
    assert invalid["state_after"] == invalid["state_before"], "a rejected range must not be remembered"
    assert invalid["report_requests_after"] == 0, "the server also rejects this; do not ask it"
    assert invalid["toasts"] == [
        "Pick both a start and an end date.",
        "The end date cannot be before the start date.",
    ]


def test_the_presets_describe_real_shifts_windows(results):
    presets = results["presets"]
    by_key = {entry[0]: {"start": entry[1], "end": entry[2], "active": entry[3]} for entry in presets["listed"]}
    assert sorted(by_key) == ["lastMonth", "thisMonth", "thisWeek"]
    for key, expected in presets["expected"].items():
        assert by_key[key]["start"] == expected["start"], key
        assert by_key[key]["end"] == expected["end"], key
        assert by_key[key]["start"] <= by_key[key]["end"], f"{key} must be a forward range"
    today = presets["expected"]["thisMonth"]["end"]
    assert by_key["lastMonth"]["end"] < today, "last month must be in the past"
    assert by_key["thisMonth"]["end"] == today, "a shifts period cannot end in the future"


def test_the_preset_in_effect_is_the_one_marked_active(results):
    presets = results["presets"]
    assert "data-preset=\"thisMonth\" data-active=\"true\"" in presets["buttons_before"]
    assert presets["buttons_before"] == [
        'data-preset="thisMonth" data-active="true"',
        'data-preset="lastMonth" data-active="false"',
        'data-preset="thisWeek" data-active="false"',
    ]
    assert "data-preset=\"lastMonth\" data-active=\"true\"" in presets["buttons_after"]
    assert 'data-active="true"' not in presets["buttons_after"][0], "only one preset can be in effect"


def test_tapping_a_preset_fetches_that_window(results):
    presets = results["presets"]
    expected = presets["expected"]["lastMonth"]
    assert presets["applied"] == expected, "the preset must be what the server is asked for"
    assert presets["state"] == f"{expected['start']}..{expected['end']}"
    assert presets["input_start"] == expected["start"], "the picker has to follow the preset"
    assert presets["input_end"] == expected["end"]


def test_the_shown_period_is_the_one_in_the_url(results):
    """Copying the address bar has to describe the figures on screen."""
    initial = results["initial"]
    expected = initial["expected"]
    assert initial["urls"] == [f"#shifts={expected['start']}..{expected['end']}"]


def test_the_url_follows_a_preset(results):
    presets = results["presets"]
    expected = presets["expected"]["lastMonth"]
    assert presets["urls"][-1] == f"#shifts={expected['start']}..{expected['end']}"


def test_a_shared_link_opens_on_the_shared_period_and_tab(results):
    """Landing on Live Ops would hide exactly the figures the link was sent to show."""
    shared = results["shared_link"]
    assert shared["state"] == "2026-08-01..2026-08-31"
    assert shared["tab"] == "Shifts"
    assert shared["query"] == {"start": "2026-08-01", "end": "2026-08-31"}
    assert shared["hours"] == "40"
    assert shared["input_start"] == "2026-08-01"
    assert shared["input_query"] == "tower", "the search comes from the link too"
    assert shared["has_filter_note"] is True


def test_a_link_shared_before_the_rename_still_opens(results):
    """`#payroll=` was already in circulation; renaming must not break a sent link."""
    legacy = results["legacy_link"]
    assert legacy["state"] == "2026-08-01..2026-08-31"
    assert legacy["tab"] == "Shifts"
    assert legacy["query"] == {"start": "2026-08-01", "end": "2026-08-31"}
    assert legacy["hours"] == "40"


def test_a_fragment_that_is_not_a_period_is_ignored(results):
    junk = results["junk_link"]
    assert junk["junk_state"] == "unset", "junk in the fragment must not become the period"
    assert junk["junk_tab"] == "Live Ops"
    assert junk["junk_report_requests"] == 0
    assert junk["backwards_state"] == "unset", "a backwards range is not a period"
    assert junk["backwards_tab"] == "Live Ops"


def test_a_link_pasted_into_the_open_tab_applies_without_a_reload(results):
    change = results["hash_change"]
    assert change["before_tab"] == "Live Ops"
    assert change["adopted"] == "2026-08-01..2026-08-31"
    assert change["tab"] == "Shifts"
    assert change["query"] == {"start": "2026-08-01", "end": "2026-08-31"}
    assert change["hours"] == "40"


def test_the_search_box_finds_a_name_a_worker_id_and_a_site(results):
    search = results["search"]
    assert search["by_name"]["shift_rows"] == 1, "'torres' is one shift"
    assert search["by_name"]["hours"] == "4", "Ana's 4 h shift, not the period's 15 h"
    assert search["by_id"]["shift_rows"] == 1, "'600' is the other worker's shift"
    assert search["by_id"]["hours"] == "8"
    assert search["by_site"]["shift_rows"] == 2, "Harbour Depot has two shifts"
    assert search["by_site"]["hours"] == "7", "4 h + 3 h"
    assert search["and_terms"]["shift_rows"] == 1, "'harbour khan' narrows to the one shift that is both"
    assert search["and_terms"]["hours"] == "3"


def test_a_day_typed_into_the_search_box_narrows_the_rows(results):
    """A timesheet row has a date, so a day is a filter - and it still offers the switch."""
    search = results["search"]
    assert search["by_date"]["shift_rows"] == 1
    assert search["by_date"]["first_row"][0] == "2026-08-07"
    assert search["by_date"]["hours"] == "8"
    assert search["by_date"]["has_day_chip"] is True, "and the one-tap period switch is still offered"
    assert search["by_name"]["has_day_chip"] is False, "a name is not a date"


def test_the_totals_of_a_filtered_view_cover_only_the_rows_shown(results):
    """Otherwise the numbers above a one-row list would silently be the period's."""
    by_site = results["search"]["by_site"]
    assert by_site["has_filter_note"] is True
    assert by_site["workers"] == "2"
    assert by_site["shifts"] == "2", "1 + 1 shift"
    assert by_site["approved_hours"] == "4", "Ana's 4 h; Bilal's 3 h are still waiting"
    assert by_site["awaiting_approval_hours"] == "3"
    assert by_site["money_visible"] is False
    assert by_site["input_query"] == "harbour", "the box keeps the search it applied"


def test_searching_narrows_the_rows_without_moving_the_period(results):
    """A search is a lens on the fetched period: it must not re-ask for a different one."""
    search = results["search"]
    assert search["search_requests_added"] == 0, "the rows were already in hand"
    assert search["day_requests_added"] == 1, "but a new period is a new question - ask it"
    expected = search["expected"]
    assert search["by_name"]["query"] == "torres"
    assert search["by_name"]["url"] == f"#shifts={expected['start']}..{expected['end']}&q=torres"
    assert search["and_terms"]["url"].endswith("&q=harbour%20khan"), "a two-term search has to survive the URL"


def test_taking_the_day_switches_the_whole_period_to_it(results):
    day = results["search"]["day_applied"]
    assert day["state"] == "2026-08-07..2026-08-07"
    assert day["request"] == {"start": "2026-08-07", "end": "2026-08-07"}
    assert day["hours"] == "8", "the figures are now that day's"
    assert day["query"] == "", "the day was a request for a period, not a filter to keep"
    assert day["input_query"] == ""


def test_a_search_that_matches_nobody_says_so_without_showing_zeros(results):
    no_match = results["search"]["no_match"]
    assert no_match["has_no_matches"] is True
    assert no_match["hours"] is None, "a grid of zeros would read as 'this period held no work'"
    assert no_match["has_table"] is False
    assert no_match["has_filter_note"] is True, "the admin still needs the way back out"


def test_the_copy_button_hands_over_the_period_on_screen(results):
    copy_link = results["copy_link"]
    assert copy_link["has_button"] is True
    assert copy_link["copied"] == ["http://localhost:8000/#shifts=2026-08-01..2026-08-31"]
    assert copy_link["toasts"] == ["Copied to clipboard"]


def csv_rows(csv):
    """Data lines of a CSV (CRLF, with a final break like csv.writer)."""
    lines = csv.split("\r\n")
    assert lines[-1] == "", "the file must end with a line break"
    return lines[0], [line for line in lines[1:-1] if line]


CSV_HEADER = "Employee,id,site,hours"


def test_the_csv_holds_only_who_their_id_where_and_how_long(results):
    """Four columns, and the four the API's own export writes: a sheet with no money in it."""
    header, rows = csv_rows(results["download"]["csv"])
    assert header == CSV_HEADER
    assert len(rows) == 3, "the three shifts of the period"
    assert "hourly_rate" not in header and "gross_estimate" not in header


def test_the_csv_button_writes_every_row_of_the_period(results):
    download = results["download"]
    _, rows = csv_rows(download["csv"])
    assert rows[0] == "Seed Lead,600,Downtown Tower A,8"
    assert rows[1] == "Ana Torres,601,Harbour Depot,4"
    assert rows[2] == "Bilal Khan,602,Harbour Depot,3"
    expected = download["expected_range"]
    assert download["filename"] == f"shifts_{expected['start']}_{expected['end']}.csv"
    assert download["clicked"] is True
    assert download["requests_added"] == 0, "the rows were already in hand - no request needed"


def test_the_csv_button_writes_only_the_rows_a_search_shows(results):
    """The bug this guards: a search narrowed the table and the file ignored it."""
    filtered = results["download"]["filtered"]
    header, rows = csv_rows(filtered["csv"])
    assert header == CSV_HEADER
    assert rows == ["Ana Torres,601,Harbour Depot,4", "Bilal Khan,602,Harbour Depot,3"], (
        "the two Harbour Depot shifts"
    )
    assert "600," not in filtered["csv"], "the shift the search excluded must not be in the file"
    assert filtered["requests_added"] == 0, "still no request: the same rows, narrowed"
    assert filtered["filename"].endswith("_harbour.csv"), "two downloads of one period must not clash"


def test_a_csv_for_a_search_that_matches_nobody_holds_no_rows(results):
    header, rows = csv_rows(results["download"]["no_match"]["csv"])
    assert header == CSV_HEADER
    assert rows == [], "an empty table downloads an empty file, not the whole period"


def test_the_file_columns_do_not_follow_the_screen_order(results):
    """A sheet whose columns move from day to day cannot be compared with last month's."""
    reordered = results["download"]["reordered"]
    header, rows = csv_rows(reordered["csv"])
    assert reordered["screen_headers"][0] == "Open notes", "the screen really was rearranged"
    assert header == CSV_HEADER, "the file keeps its own order regardless"
    assert rows[0] == "Seed Lead,600,Downtown Tower A,8"


def test_nothing_is_downloaded_when_the_screen_holds_no_report(results):
    """A failed request leaves no figures to export, so no file may be written."""
    nothing = results["download"]["nothing_to_export"]
    assert nothing["blobs_added"] == 0
    assert nothing["toasts"] == ["There is nothing on screen to download for this period."]


def test_a_period_with_no_shifts_says_so(results):
    empty = results["empty"]
    assert empty["has_empty_note"] is True
    assert empty["has_table"] is False, "an empty table reads as 'everything is worth zero'"
    assert empty["shift_rows"] == 0


def test_the_phone_layout_shows_one_card_per_shift_with_the_same_columns(results):
    mobile = results["mobile"]
    assert mobile["has_filter"] is True, "the period picker has to fit on a phone too"
    assert mobile["has_search_box"] is True, "and so does the search box"
    assert mobile["shift_rows"] == 3
    assert mobile["has_table"] is False
    assert mobile["hours"] == "15"
    # The same seven columns, in the same order - a card is a row that had to fold.
    assert mobile["labels"][:7] == [
        "Date", "Employee", "User ID", "Site", "Hours", "Awaiting approval", "Open notes"
    ]
    assert mobile["site_names"] == ["Downtown Tower A", "Harbour Depot", "Harbour Depot"], (
        "a phone card names the same site the desktop row does"
    )


def test_a_shift_with_no_site_on_file_still_gets_a_row(results):
    """A missing site must not blank the row or the figures above it."""
    table = results["sites"]["no_site"]
    assert table["shift_rows"] == 1
    assert table["site_names"] == []
    assert table["first_row"][3] == table["em_dash"], "an em dash, not an empty cell"
    assert table["hours"] == "6"


def test_a_site_name_cannot_inject_markup(results):
    """Site names are admin-authored too, and they are rendered in every row."""
    escaped = results["escaped"]
    assert escaped["raw_site_tag"] is False
    assert escaped["escaped_site_tag"] is True


def test_a_worker_name_cannot_inject_markup_into_the_totals(results):
    escaped = results["escaped"]
    assert escaped["name_present"] is True
    assert escaped["raw_tag"] is False
    assert escaped["escaped_tag"] is True
    assert escaped["hours"] == "50"
