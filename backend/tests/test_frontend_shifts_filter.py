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
   date, employee, role, id, site, arrival, hours, awaiting approval, open notes -
   the choice is remembered in ``localStorage``, and a stored order that is stale or
   junk cannot take the table down;
5. the CSV button writes exactly the rows on screen - same set, same order as the
   table, filtered or not - in the four columns ``Employee,id,site,hours`` and in
   that order whatever the screen's own column order has been changed to;
5b. the same button, with the format set to PDF, prints the same report through the
   browser's print dialog - the rows on screen, the columns the administrator
   arranged, the period in the file name the dialog offers - and puts the console
   back when the dialog closes. Nothing is sent to the server to make a PDF, and no
   PDF library is involved: the dialog writes the file.

The search box has its own contract (name / worker id / site / day): a day is an
ordinary filter here, because a timesheet row *has* a date; see the ``3g`` block.

6. the tab an administrator taps while the console's own opening screen is still
   fetching is the tab they are left looking at (see the ``9`` block): screens are
   painted by their requests, and the one that answered last used to decide.

The tab is named **Shifts** and shows hours and approval state only. The report it
reads carries no ``hourly_rate`` and no ``gross_estimate`` any more, and one test
here asserts that no money reaches the screen either way: a tab that calls itself
shifts must not quietly be a payment screen.

Node is optional; without it these skip rather than fail.
"""

from __future__ import annotations

import re

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
        // The site's category, as the server resolves it onto every timesheet row. Two of the
        // three fixture sites are warehouses and one is a depot, so a category filter - and the
        // column that now shows it - has more than one value to get wrong.
        site_category: 'Warehouse',
        hours: 8,
        recorded_hours: 8.5,
        approved_hours: null,
        break_hours: 0.5,
        status_code: 'approved',
        status: 'Approved by Admin',
        awaiting_approval: false,
        open_notes: 0,
        // Who the worker answered to on this shift, as the server joins it onto the row. The
        // default is nobody - a moallem's own row has no moallem over it, and the timesheet has
        // to say that rather than leave the cell blank - and the worker rows below name one.
        moallem_id: null,
        moallem_name: null,
        // Where the shift's clock-in fell against the site's window. The server sends the
        // verdict and the minutes; the wording is the console's.
        arrival_time: '2026-08-07 04:20:00',
        arrival_verdict: 'on_time',
        arrival_minutes: 0
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
        ).size,
        late_arrivals: rows.filter((row) => row.arrival_verdict === 'late').length
    };
}

// Three shifts, three workers, two sites, one of them waiting for an administrator and two
// of the workers with something open in the notes inbox. One arrived late by 42 minutes, so
// "who was late, and by how much" is a question with a wrong answer as well as a right one.
// A one-row fixture cannot tell a working search - or a working total - apart from a broken
// one.
const DEFAULT_ROWS = [
    shift({
        log_id: 901, date: '2026-08-07', worker_id: '600', worker_name: 'Seed Lead',
        site_name: 'Downtown Tower A', hours: 8, recorded_hours: 8.5, open_notes: 1
    }),
    shift({
        log_id: 902, date: '2026-08-06', timestamp: '2026-08-06 15:04:00', worker_id: '601',
        worker_name: 'Ana Torres', role: 'worker', site_name: 'Harbour Depot', site_category: 'Depot',
        // Their moallem is somebody who has no shift in this fixture, so "the search excluded
        // the moallem's own row" and "the moallem's name is in the moallem column" stay two
        // different assertions.
        moallem_id: '700', moallem_name: 'Ustad Karim',
        hours: 4, recorded_hours: 4.5, break_hours: 0.5,
        arrival_time: '2026-08-06 05:10:00'
    }),
    shift({
        log_id: 903, date: '2026-08-05', timestamp: '2026-08-05 16:30:00', worker_id: '602',
        worker_name: 'Bilal Khan', role: 'worker', site_name: 'Harbour Depot', site_category: 'Depot',
        moallem_id: '700', moallem_name: 'Ustad Karim',
        hours: 3, recorded_hours: 5, break_hours: 0.5,
        status_code: 'pending_review', status: 'pending_review', awaiting_approval: true, open_notes: 2,
        arrival_time: '2026-08-05 07:12:00', arrival_verdict: 'late', arrival_minutes: 42
    })
];

function report(start, end, rows) {
    return {
        period: { start: start, end: end },
        filters: { site: null, worker_id: null },
        fields: [
            'log_id', 'date', 'timestamp', 'worker_id', 'worker_name', 'role', 'site_name',
            'site_category', 'arrival_time', 'arrival_verdict', 'arrival_minutes',
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
                site_category: null, hours: 6, recorded_hours: 6, break_hours: 0
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
    if (start === '2004-01-12' && end === '2004-01-12') {
        // The day the strip's own column points at: one shift, four hours, arrived an hour late.
        return {
            status: 200,
            body: report(start, end, [shift({
                log_id: 942, worker_id: '612', worker_name: 'Strip Three', date: '2004-01-12',
                timestamp: '2004-01-12 16:00:00', hours: 4, recorded_hours: 4.5,
                arrival_time: '2004-01-12 07:30:00', arrival_verdict: 'late', arrival_minutes: 60
            })])
        };
    }
    if (start === '2004-01-01') {
        // A window the strip can be read off: two shifts on one day (one of them waiting for a
        // decision), one on another day that arrived an hour late, and 28 days nobody worked.
        return {
            status: 200,
            body: report(start, end, [
                shift({
                    log_id: 940, worker_id: '610', worker_name: 'Strip One', date: '2004-01-05',
                    timestamp: '2004-01-05 16:00:00', hours: 8, recorded_hours: 8.5,
                    arrival_time: '2004-01-05 04:10:00'
                }),
                shift({
                    log_id: 941, worker_id: '611', worker_name: 'Strip Two', date: '2004-01-05',
                    timestamp: '2004-01-05 17:00:00', hours: 3, recorded_hours: 3.5,
                    status_code: 'pending_review', status: 'pending_review', awaiting_approval: true,
                    arrival_time: '2004-01-05 04:10:00'
                }),
                shift({
                    log_id: 942, worker_id: '612', worker_name: 'Strip Three', date: '2004-01-12',
                    timestamp: '2004-01-12 16:00:00', hours: 4, recorded_hours: 4.5,
                    arrival_time: '2004-01-12 07:30:00', arrival_verdict: 'late', arrival_minutes: 60
                })
            ])
        };
    }
    if (start === '2005-01-01') {
        // A whole year: 365 days of window, one shift in the middle of it. The strip has to
        // become twelve columns rather than 365.
        return {
            status: 200,
            body: report(start, end, [shift({
                log_id: 950, date: '2005-06-10', timestamp: '2005-06-10 16:00:00', hours: 7,
                recorded_hours: 7.5, arrival_time: '2005-06-10 04:10:00'
            })])
        };
    }
    if (start === '2006-01-01') {
        // A month that fills more than one page: 120 shifts, which is what "Show more" and the
        // "Showing the first n of m" line exist for.
        const many = [];
        for (let i = 0; i < 120; i += 1) {
            many.push(shift({
                log_id: 1000 + i, worker_id: String(700 + (i % 7)), worker_name: `Row ${i}`,
                date: `2006-01-${String((i % 28) + 1).padStart(2, '0')}`,
                timestamp: '2006-01-01 16:00:00', hours: 8, recorded_hours: 8.5,
                arrival_time: '2006-01-01 04:10:00'
            }));
        }
        return { status: 200, body: report(start, end, many) };
    }
    if (start === '2003-01-01') {
        // Production-shaped ids. This deployment's roster is ids like `1`, `2` and `4`, and
        // every date on a sheet carries those digits - so a period whose ids are digits of
        // its own dates is the only fixture on which "search by id" can be told apart from
        // "search by whatever the date happens to contain".
        return {
            status: 200,
            body: report(start, end, [
                shift({
                    log_id: 930, worker_id: '4', worker_name: 'Ahmed', date: '2003-01-04',
                    timestamp: '2003-01-04 16:02:11', arrival_time: '2003-01-04 04:20:00'
                }),
                shift({
                    log_id: 931, worker_id: '2', worker_name: 'abood', date: '2003-01-02',
                    timestamp: '2003-01-02 15:04:00', arrival_time: '2003-01-02 05:10:00'
                })
            ])
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
        late_arrivals: value('late_arrivals'),
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
        has_days: html.indexOf('data-shifts-days="true"') >= 0,
        has_columns_panel: html.indexOf('id="shiftsColumns"') >= 0,
        has_timesheet_note: html.indexOf('One row per shift.') >= 0,
        column_order: order ? order[1].split(',') : null,
        headers: headerLabels(html),
        // The row action: the one cell on a row that is not a column. One per shift, and
        // never on paper - a printable sheet has no controls on it.
        print_buttons: (html.match(/data-print-worker=/g) || []).length,
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

// The coverage strip, read as numbers rather than as heights: which unit it settled on, how
// many columns that is, and what each column's data says. The bars are a share of the busiest
// day in the window, so a bar is only meaningful beside the others - which is why the counts
// are read off the buckets as well.
function coverage(html) {
    const unit = /data-shifts-coverage="([a-z]+)"/.exec(html);
    const bars = (html.match(/class="shifts-day-bar" style="height:(\d+)%"/g) || [])
        .map((found) => Number(/height:(\d+)%/.exec(found)[1]));
    const undecided = (html.match(/class="shifts-day-undecided" style="height:(\d+)%"/g) || [])
        .map((found) => Number(/height:(\d+)%/.exec(found)[1]));
    return {
        unit: unit ? unit[1] : null,
        has_strip: html.indexOf('data-shifts-days="true"') >= 0,
        has_hint: html.indexOf('shifts-coverage-hint') >= 0,
        has_legend: html.indexOf('shifts-legend-key is-counted') >= 0
            && html.indexOf('shifts-legend-key is-undecided') >= 0,
        count: (html.match(/<li class="shifts-day/g) || []).length,
        buttons: (html.match(/class="shifts-day-btn"/g) || []).length,
        days: (html.match(/data-shift-day="([0-9-]+)"/g) || []).map((found) => found.slice(16, -1)),
        bars: bars,
        undecided: undecided,
        late: (html.match(/class="shifts-day-late">(\d+)</g) || []).map((found) => Number(/>\d+/.exec(found)[0].slice(1))),
        selected: (html.match(/class="shifts-day is-selected"/g) || []).length,
        caption: (/data-shifts-days="true" aria-label="([^"]*)"/.exec(html) || [])[1] || null,
        // One label per column: what a screen reader is handed instead of a bar. Anchored on
        // the button so the strip's own caption cannot be counted as one of its columns.
        labels: (html.match(/class="shifts-day-btn"[\s\S]{0,240}?aria-label="[^"]*h counted, [^"]*h awaiting a decision/g) || []).length
    };
}

// One element's text, with its markup taken off - for reading a sentence out of the page.
function textOf(html, marker) {
    const at = html.indexOf(marker);
    if (at < 0) return null;
    return html.slice(at, html.indexOf('</p>', at)).replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();
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

const DEFAULT_COLUMNS = 'date,employee,role,moallem,id,site,category,arrival,hours,awaiting,notes';
// The labels those columns are painted with, in English (the harness boots in the default
// language). Written out here so a renamed translation cannot pass unnoticed. The moallem
// column sits beside the role because it answers the same kind of question the role does -
// whose row this is - and it is painted with the role's own word (``roleMoallem``).
const DEFAULT_HEADERS = ['Date', 'Employee', 'Role', 'Moallem', 'User ID', 'Site', 'Category', 'Arrival', 'Hours', 'Awaiting approval', 'Open notes'];

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
        // The arrival is searchable like any other column: "late" is the question the
        // column answers, and a second term is how far off - "late 42" is the one shift that
        // far outside its window.
        const byLate = await search('late');
        const lateBy = await search('late 42');
        const byOnTime = await search('on time');
        // The role is searchable too - "whose shifts are these" is asked by typing the
        // column, and the column is on screen in the reader's words.
        const byRole = await search('moallem');
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
            by_late: byLate,
            late_by: lateBy,
            by_on_time: byOnTime,
            by_role: byRole,
            no_match: noMatch,
            day_applied: dayApplied,
            search_requests_added: afterSearches - requestsBefore,
            day_requests_added: afterDay - afterSearches
        };
    }

    // 3h. a number in the box is somebody's id, not a digit of the day they worked
    {
        const env = adminEnv();
        await env.evaluate("UI_MODULES.setShiftsRange('2003-01-01', '2003-01-31')");
        await env.evaluate("UI.renderAdminTab('Shifts')");
        const read = () => cardValues(env.evaluate("document.getElementById('adminContent').innerHTML"));
        const search = async (query) => {
            await env.evaluate(`(async () => {
                document.getElementById('shiftsQuery').value = ${JSON.stringify(query)};
                await UI_MODULES.applyShiftsSearch();
            })()`);
            return read();
        };
        const all = read();
        results.worker_ids = {
            all: all,
            by_id: await search('4'),
            by_other_id: await search('2'),
            // A number that names nobody. Every row of the period carries it in its date,
            // which is exactly what used to make a search for an id answer with the period.
            by_year: await search('2003'),
            // ...and a day is still a day: it is typed with its dashes, like the chip offers.
            by_day: await search('2003-01-04')
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
            // All the way to the front, whatever the table is made of: one press, one swap.
            for (let i = 0; i < UI_MODULES.shiftsColumns().length - 1; i += 1) {
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

    // 4f. a shift whose clock-in is not on file: unknown, not punctual
    {
        const env = adminEnv();
        await env.evaluate("UI.renderAdminTab('Shifts')");
        // A force-clock-out, or a shift closed with no arrival recorded: the row is real,
        // the arrival is not. The server sends null for all three fields.
        env.evaluate(`(function () {
            const cached = UI_MODULES._shiftsReport;
            cached.report.rows = [Object.assign({}, cached.report.rows[0], {
                worker_id: '603', worker_name: 'Noor Haddad',
                arrival_time: null, arrival_verdict: null, arrival_minutes: null
            })];
            cached.report.totals = UI_MODULES.sumShiftsRows(cached.report.rows);
            UI_MODULES.repaintShiftsFromCache();
            return true;
        })()`);
        const html = env.evaluate("document.getElementById('adminContent').innerHTML");
        await env.evaluate(`(async () => {
            document.getElementById('shiftsQuery').value = 'on time';
            await UI_MODULES.applyShiftsSearch();
        })()`);
        const searched = env.evaluate("document.getElementById('adminContent').innerHTML");
        const firstRow = allRows(html)[0] || [];
        results.no_arrival = Object.assign(cardValues(html), {
            cell: firstRow.length > 7 ? firstRow[7] : null,
            search_no_match: cardValues(searched).has_no_matches
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
            for (let i = 0; i < UI_MODULES.shiftsColumns().length; i += 1) UI_MODULES.moveShiftsColumn('notes', -1);
        })()`);
        env.evaluate('UI_MODULES.downloadShiftsCsv()');
        results.download.reordered = {
            csv: await env.lastBlobText(),
            screen_headers: headerLabels(env.evaluate("document.getElementById('adminContent').innerHTML"))
        };
    }

    // 5c. the PDF choice prints the same report, through the browser's own dialog
    {
        const env = adminEnv();
        await env.evaluate("UI.renderAdminTab('Shifts')");
        const before = { requests: env.requests.length, blobs: env.blobs.length };
        env.evaluate("document.getElementById('shiftsExportFormat').value = 'pdf'");
        env.evaluate('UI_MODULES.downloadShiftsReport()');
        const printed = env.printed[env.printed.length - 1] || {};
        const html = env.evaluate("document.getElementById('adminContent').innerHTML");
        results.print = {
            printed: env.printed.length,
            title: printed.title || null,
            printing: printed.printing === true,
            sheet: printed.sheet || '',
            screen_hours: cardValues(html).hours,
            screen_headers: headerLabels(html),
            files_written: env.blobs.length - before.blobs,
            requests_added: env.requests.length - before.requests,
            expected_range: expectedDefaultRange()
        };
        // The dialog closes.
        env.fireWindowEvent('afterprint');
        results.print.after = {
            title: env.evaluate('document.title'),
            printing: env.evaluate("document.body.classList.contains('is-printing-report')"),
            sheets_left: env.evaluate(
                "document.body.__children.filter((c) => c.className === 'print-sheet').length")
        };

        // The same button, with a search on: the sheet has to hold the rows on screen.
        await env.evaluate(`(async () => {
            document.getElementById('shiftsQuery').value = 'harbour';
            await UI_MODULES.applyShiftsSearch();
        })()`);
        env.evaluate('UI_MODULES.downloadShiftsReport()');
        const narrowed = env.printed[env.printed.length - 1] || {};
        results.print.filtered = { sheet: narrowed.sheet || '', title: narrowed.title || null };
        env.fireWindowEvent('afterprint');

        // Nothing on screen for the period: no sheet may be printed, and no request to
        // invent one.
        const stale = adminEnv();
        await stale.evaluate("UI.renderAdminTab('Shifts')");
        stale.evaluate('UI_MODULES._shiftsReport = null');
        stale.evaluate("document.getElementById('shiftsExportFormat').value = 'pdf'");
        stale.evaluate('UI_MODULES.downloadShiftsReport()');
        results.print.nothing_to_export = { printed: stale.printed.length, toasts: toasts(stale) };

        // A period that *is* on screen and has nothing in it: there is a report to print,
        // so the dialog opens - and what it prints is the sentence, not an empty table. The
        // two are different states, and a sheet that rendered the second as the first would
        // hand somebody a blank page under a heading.
        const quiet = adminEnv();
        await quiet.evaluate("UI_MODULES.setShiftsRange('2000-01-01', '2000-01-31')");
        await quiet.evaluate("UI.renderAdminTab('Shifts')");
        quiet.evaluate("document.getElementById('shiftsExportFormat').value = 'pdf'");
        quiet.evaluate('UI_MODULES.downloadShiftsReport()');
        const quietSheet = (quiet.printed[quiet.printed.length - 1] || {}).sheet || '';
        results.print.quiet = {
            printed: quiet.printed.length,
            sheet: quietSheet,
            has_empty_note: quietSheet.indexOf('print-sheet-empty') >= 0,
            has_table: quietSheet.indexOf('print-sheet-table') >= 0
        };

        // No choice made - and a value this build does not know - is still the spreadsheet:
        // the download exists longest, and a print dialog nobody asked for is the worse
        // failure of the two.
        const plain = adminEnv();
        await plain.evaluate("UI.renderAdminTab('Shifts')");
        plain.evaluate('UI_MODULES.downloadShiftsReport()');
        plain.evaluate("document.getElementById('shiftsExportFormat').value = 'xlsx'");
        plain.evaluate('UI_MODULES.downloadShiftsReport()');
        const anchor = plain.lastAnchor();
        results.print.default_format = {
            printed: plain.printed.length,
            files: plain.blobs.length,
            filename: anchor ? anchor.download : null
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

    // 9. the tab tapped while the screen in front of it is still fetching is the tab the admin
    //    is left looking at
    //
    // The console opens on a tab of its own, and that first read is the slow one - a session
    // starting on site, over a phone. An admin who wants the timesheet taps Shifts *inside*
    // it. Which of the two screens was left on the page used to follow from which answer came
    // back last: the board landed after the tab that was asked for and painted over it, so the
    // tap looked like it had done nothing, and the figures underneath the date boxes were the
    // front door's. So the board's read is held open here and answered only after the tap - the
    // arrival order that used to lose - and what is on the page at the end is the assertion.
    {
        const env = adminEnv();
        // The hold is installed inside the page's own scope: ``API`` and ``window`` belong to
        // the files under test, not to this harness, and the answer is kept on ``window`` so
        // the release is a second call into the same scope rather than a value crossing out of
        // it. ``API.request`` is bound on the way out because it is a method that reads the
        // base URL off its own object - called off the object it throws instead of asking, and
        // this scenario would paint two error screens rather than the two screens it is about.
        env.evaluate([
            'window.__heldReads = [];',
            'window.__realRequest = API.request.bind(API);',
            'API.request = (path, options) => {',
            "    if (String(path).indexOf('/admin/dashboard') >= 0) {",
            '        return new Promise((resolve, reject) => {',
            '            window.__heldReads.push(() => window.__realRequest(path, options).then(resolve, reject));',
            '        });',
            '    }',
            '    return window.__realRequest(path, options);',
            '};'
        ].join(''));

        // The console's own frame, opening on its own tab: not awaited, because its read is
        // being held.
        const opening = env.evaluate('UI.paintAdminConsole()');
        // A real turn of the event loop before the tap: the opening screen paints from a
        // promise callback, so until the loaded page has had a turn of its own its read has
        // not gone out and the tap below would arrive *before* the screen it interrupts. The
        // scenario would then be a test of nothing.
        await env.evaluate('new Promise((resolve) => setTimeout(resolve, 0))');
        const tapped = env.evaluate("UI.renderAdminTab('Shifts')");
        const wasOut = env.evaluate('window.__heldReads.length');
        // Another turn of the event loop, so the tab that was tapped has had every chance to
        // serve its own read and paint while the board is still out. Whichever screen is up
        // after this is the one that would be standing if the board never answered at all.
        await env.evaluate('new Promise((resolve) => setTimeout(resolve, 0))');
        const tappedWhileOut = env.evaluate("document.getElementById('adminContent').innerHTML")
            .indexOf('id="shiftsFilter"') >= 0;
        // Only now does the board answer - the arrival order that used to leave the front door
        // on the page.
        env.evaluate('window.__heldReads.forEach((release) => release())');
        await tapped;
        await opening;
        env.evaluate('API.request = window.__realRequest');

        const html = env.evaluate("document.getElementById('adminContent').innerHTML");
        results.order = {
            // The premise, reported rather than assumed: a scenario that never had the board's
            // read in flight would pass without testing anything.
            was_out: wasOut,
            // The screen the tap had put up on its own, before the board answered. Reported and
            // not asserted: how long a tab waits is the console's to choose, but which screen
            // is still there at the end is not.
            tapped_while_out: tappedWhileOut,
            tab: env.evaluate('State.adminTab'),
            asked: env.requests.map((r) => r.url.replace(/^https?:\/\/[^/]*\/api\/v1/, '')),
            shifts_after_both: html.indexOf('id="shiftsFilter"') >= 0,
            dashboard_after_both: html.indexOf('data-dashboard="true"') >= 0,
            rows_after_both: (html.match(/data-shift=/g) || []).length,
            hours: cardValues(html).hours
        };
    }

    // 10. the coverage strip: the period read day by day, the undecided hours drawn apart
    {
        const env = adminEnv();
        await env.evaluate("UI_MODULES.setShiftsRange('2004-01-01', '2004-01-31')");
        await env.evaluate("UI.renderAdminTab('Shifts')");
        const html = env.evaluate("document.getElementById('adminContent').innerHTML");
        results.coverage = Object.assign(coverage(html), cardValues(html), {
            // The same rows as buckets: the arithmetic behind the bars, so a strip drawing the
            // wrong heights fails here rather than in a screenshot.
            buckets: env.evaluate(`(function () {
                const covered = UI_MODULES.shiftsCoverageBuckets(UI_MODULES._shiftsReport.report);
                return covered.buckets.map((b) => [b.key, b.hours, b.awaiting, b.late, b.tick]);
            })()`)
        });

        // A year is twelve columns, not 365.
        const year = adminEnv();
        await year.evaluate("UI_MODULES.setShiftsRange('2005-01-01', '2005-12-31')");
        await year.evaluate("UI.renderAdminTab('Shifts')");
        results.coverage_year = coverage(year.evaluate("document.getElementById('adminContent').innerHTML"));

        // A period with nothing in it draws no strip: the panel below already says so.
        const quiet = adminEnv();
        await quiet.evaluate("UI_MODULES.setShiftsRange('2000-01-01', '2000-01-31')");
        await quiet.evaluate("UI.renderAdminTab('Shifts')");
        results.coverage_empty = coverage(quiet.evaluate("document.getElementById('adminContent').innerHTML"));

        // One tap on a column narrows the whole tab to that day - and takes the search with it.
        await env.evaluate(`(async () => {
            document.getElementById('shiftsQuery').value = 'strip';
            await UI_MODULES.applyShiftsSearch();
        })()`);
        const selectedWhileFiltered = env.evaluate("document.getElementById('adminContent').innerHTML")
            .indexOf('class=\"shifts-day is-selected\"') >= 0;
        await env.evaluate("UI_MODULES.applyShiftsBucket('2004-01-12..2004-01-12')");
        results.coverage_tap = Object.assign(
            cardValues(env.evaluate("document.getElementById('adminContent').innerHTML")),
            {
                state: env.evaluate("State.shiftsRange.start + '..' + State.shiftsRange.end"),
                query: env.evaluate('State.shiftsQuery'),
                request: lastQuery(env),
                selected_while_filtered: selectedWhileFiltered
            }
        );
    }

    // 11. the two attention filters, which the amber figures above the table point at
    {
        const env = adminEnv();
        // A window whose two flagged shifts are *different* shifts: one is waiting for a
        // decision and arrived on time, the other was decided and arrived an hour late. On a
        // window where one shift is both, a chip that filtered by the wrong field would pass.
        await env.evaluate("UI_MODULES.setShiftsRange('2004-01-01', '2004-01-31')");
        await env.evaluate("UI.renderAdminTab('Shifts')");
        const html = env.evaluate("document.getElementById('adminContent').innerHTML");
        const read = () => cardValues(env.evaluate("document.getElementById('adminContent').innerHTML"));
        const chips = [];
        const chipRe = /data-attention="([a-z]+)"[^>]*aria-pressed="([a-z]+)"[^>]*>([^<]*)</g;
        let found = chipRe.exec(html);
        while (found !== null) {
            chips.push([found[1], found[2], found[3].trim()]);
            found = chipRe.exec(html);
        }
        await env.evaluate("UI_MODULES.setShiftsAttention('awaiting')");
        const awaiting = read();
        const note = textOf(env.evaluate("document.getElementById('adminContent').innerHTML"), 'data-filter-note');
        env.evaluate('UI_MODULES.downloadShiftsCsv()');
        const csv = await env.lastBlobText();
        await env.evaluate("UI_MODULES.setShiftsAttention('late')");
        const late = read();
        // The period on screen is not the period the rows are dated in for this fixture, so the
        // chip that is on stays on screen even once nothing matches it: a filter nobody can see
        // is a filter nobody can switch off.
        await env.evaluate("UI_MODULES.setShiftsAttention('late')");
        const cleared = read();
        // Read where it is, not at the end of the scenario: the filter moves on from here.
        const clearedState = env.evaluate('State.shiftsAttention');
        // Two different filters do not cancel each other: the search, the category and the
        // attention chip all narrow the same rows.
        await env.evaluate("UI_MODULES.setShiftsAttention('awaiting')");
        const both = read();
        await env.evaluate("UI_MODULES.setShiftsCategory('Warehouse')");
        const narrowed = read();
        await env.evaluate("UI_MODULES.setShiftsCategory('')");

        const decided = adminEnv();
        await decided.evaluate("UI_MODULES.setShiftsRange('2002-01-01', '2002-01-31')");
        await decided.evaluate("UI.renderAdminTab('Shifts')");
        const decidedHtml = decided.evaluate("document.getElementById('adminContent').innerHTML");

        results.attention = {
            chips: chips,
            awaiting: awaiting,
            note: note,
            csv: csv,
            late: late,
            cleared: Object.assign(cleared, { state: clearedState }),
            both: both,
            narrowed: narrowed,
            quiet_chips: (decidedHtml.match(/data-attention=/g) || []).length
        };
    }

    // 12. sorting a column - and the third press, which puts the period's own order back
    {
        const env = adminEnv();
        await env.evaluate("UI.renderAdminTab('Shifts')");
        const read = () => {
            const html = env.evaluate("document.getElementById('adminContent').innerHTML");
            return Object.assign(cardValues(html), {
                sort: env.evaluate('JSON.stringify(UI_MODULES.shiftsSort())'),
                sorted_headers: (html.match(/<th[^>]*aria-sort="[a-z]+"[^>]*>/g) || []),
                headers: headerLabels(html)
            });
        };
        const initial = read();
        await env.evaluate("UI_MODULES.sortShiftsBy('hours')");
        const byHours = read();
        await env.evaluate("UI_MODULES.sortShiftsBy('hours')");
        const byHoursAsc = read();
        await env.evaluate("UI_MODULES.sortShiftsBy('hours')");
        const restored = read();
        await env.evaluate("UI_MODULES.sortShiftsBy('employee')");
        const byName = read();
        // The file follows the screen: a sorted table exports in the order it is being read in.
        env.evaluate('UI_MODULES.downloadShiftsCsv()');
        const csv = await env.lastBlobText();
        results.sorting = {
            initial: initial,
            by_hours: byHours,
            by_hours_asc: byHoursAsc,
            restored: restored,
            by_name: byName,
            csv: csv,
            // A sort is how one reader is holding the page, not a setting: nothing is written
            // to storage for it, unlike the column order.
            stored: env.evaluate("localStorage.getItem('shiftsSort')")
        };
    }

    // 13. a month that fills more than one page is painted one page at a time
    {
        const env = adminEnv();
        await env.evaluate("UI_MODULES.setShiftsRange('2006-01-01', '2006-01-31')");
        await env.evaluate("UI.renderAdminTab('Shifts')");
        const read = () => {
            const html = env.evaluate("document.getElementById('adminContent').innerHTML");
            return Object.assign(cardValues(html), {
                count_line: (/data-shifts-count="(\d+)"/.exec(html) || [])[1] || null,
                more: html.indexOf('data-shifts-more') >= 0,
                limit: env.evaluate('State.shiftsLimit')
            });
        };
        const first = read();
        await env.evaluate('UI_MODULES.showMoreShifts()');
        const second = read();
        await env.evaluate('UI_MODULES.showMoreShifts()');
        const third = read();
        // The file and the sheet are the whole period whatever has been painted: a download of
        // what somebody happened to have scrolled to would be the worse lie of the two.
        env.evaluate('UI_MODULES.downloadShiftsCsv()');
        const csv = await env.lastBlobText();
        env.evaluate("document.getElementById('shiftsExportFormat').value = 'pdf'");
        env.evaluate('UI_MODULES.downloadShiftsReport()');
        const sheet = (env.printed[env.printed.length - 1] || {}).sheet || '';
        env.fireWindowEvent('afterprint');
        // A new search is a new list: the painted count goes back to one page of it.
        await env.evaluate(`(async () => {
            document.getElementById('shiftsQuery').value = 'Row';
            await UI_MODULES.applyShiftsSearch();
        })()`);
        results.paging = {
            first: first,
            second: second,
            third: third,
            csv_rows: csv.replace(/\r\n$/, '').split('\r\n').length - 1,
            sheet_rows: (sheet.match(/<tr>/g) || []).length,
            searched: read()
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


def test_every_shift_offers_its_worker_s_month_and_no_sheet_carries_a_control(results):
    """The row action is on every row, on both layouts, and never on paper.

    One tap per shift, because the shift is what an administrator is looking at when the
    question "what did this person work this month?" comes up. The printed sheet is the
    other half of the rule: a control inside a printed table is an instruction to somebody
    holding a sheet of paper.
    """
    assert results["initial"]["print_buttons"] == 3, "one action per shift row"
    assert results["mobile"]["print_buttons"] == 3, "and the phone cards carry it too"
    assert "data-print-worker" not in results["print"]["sheet"], (
        "a printable sheet must not carry the row's print button"
    )


def test_a_shift_waiting_for_an_administrator_is_marked_and_not_counted_as_approved(results):
    """The 8.1 h gate only means anything if unapproved hours stay out of the signed-off figure."""
    initial = results["initial"]
    assert initial["awaiting_rows"] == 1, "the one pending shift is marked in the markup"
    assert initial["approved_hours"] != initial["hours"], "waiting hours are not counted hours"
    # The third fixture row is the pending one (3 h of the period's 15) and its awaiting cell
    # says so, where an approved row names the decision that was made instead.
    assert initial["first_row"][9] == "Approved by Admin", initial["first_row"]
    assert initial["rows"][2][9] == "Awaiting approval", initial["rows"][2]


def test_the_default_column_order_is_the_one_the_tab_was_asked_for(results):
    default = results["columns_default"]
    assert default["columns"] == "date,employee,role,moallem,id,site,category,arrival,hours,awaiting,notes"
    assert default["column_order"] == [
        "date", "employee", "role", "moallem", "id", "site", "category", "arrival", "hours",
        "awaiting", "notes"
    ]
    assert default["headers"] == [
        "Date", "Employee", "Role", "Moallem", "User ID", "Site", "Category", "Arrival", "Hours",
        "Awaiting approval", "Open notes", PRINT_ACTION_HEADER
    ], "the data columns, then the row action - which is not one of them"
    assert default["has_columns_panel"] is True, "the admin needs a way to change it"
    assert default["stored"] is None, "a default order is not a choice anybody made"


def test_the_first_row_carries_the_column_values_in_that_order(results):
    """Date, name, role, id, site, arrival, hours, approval - the row lines up with its header."""
    cells = results["columns_default"]["first_row"]
    # Twelve cells: the eleven data columns and the action, which carries no text of its own -
    # its label is its ``aria-label``, and what is inside it is an icon.
    assert cells is not None and len(cells) == 12, cells
    assert cells[11] == "", cells
    assert cells[0] == "2026-08-07"
    assert cells[1] == "Seed Lead"
    # The role, in the reader's words: the wire says "moallem", the table says "Moallem".
    assert cells[2] == "Moallem"
    # Their own row has no moallem over it - a moallem answers to nobody - and the cell says so
    # rather than going blank, which is what an administrator reads as "nobody is assigned".
    assert cells[3] == "Unassigned", cells
    assert cells[4] == "600"
    assert cells[5] == "Downtown Tower A"
    # The site's category, resolved by the server on this row: the value the chip filter
    # above the table selects by, and now a column of its own.
    assert cells[6] == "Warehouse"
    assert cells[7] == "On time"
    assert cells[8] == "8"
    assert cells[9] == "Approved by Admin"
    assert cells[10] == "1", "this worker has one note open"
    # ...and a shift that *was* worked under somebody names them: the column is not a permanent
    # "Unassigned" the test could not tell from a broken read.
    assert results["columns_default"]["rows"][1][3] == "Ustad Karim"


def test_moving_a_column_moves_it_in_the_header_the_panel_and_every_row(results):
    moved = results["columns_moved"]
    # One step per button press, all the way to the front of the table. One step swaps a
    # column with its neighbour and nothing else - no reshuffle, no jump.
    assert len(moved["steps"]) == 10
    assert moved["steps"][0] == "date,employee,role,moallem,id,site,category,arrival,awaiting,hours,notes"
    assert moved["steps"][1] == "date,employee,role,moallem,id,site,category,awaiting,arrival,hours,notes"
    assert moved["steps"][-1] == "awaiting,date,employee,role,moallem,id,site,category,arrival,hours,notes"
    assert moved["column_order"][0] == "awaiting"
    assert moved["headers"][0] == "Awaiting approval"
    # The cells follow the header: the newest shift is signed off, the one below it is not.
    assert moved["first_row"][0] == "Approved by Admin", moved["first_row"]
    assert moved["rows"][2][0] == "Awaiting approval", moved["rows"][2]
    assert moved["first_row"][1] == "2026-08-07"
    assert moved["first_row"][2] == "Seed Lead"
    assert sorted(moved["column_order"]) == sorted([
        "date", "employee", "role", "moallem", "id", "site", "category", "arrival", "hours",
        "awaiting", "notes"
    ]), "reordering must never lose or add a column"
    assert moved["hours"] == "15", "and the figures survive the repaint"


def test_the_chosen_order_is_remembered_in_the_browser(results):
    moved = results["columns_moved"]
    assert moved["stored"] == (
        '["awaiting","date","employee","role","moallem","id","site","category","arrival","hours","notes"]'
    ), "the order has to outlive the repaint that follows the click"
    persist = results["columns_persist"]
    assert persist["before"] == "date,employee,role,moallem,id,site,category,arrival,notes,hours,awaiting"
    assert persist["column_order"] == persist["before"].split(","), "a re-render keeps it"
    assert persist["headers"][8] == "Open notes", "and the header follows the stored order"


def test_a_stale_or_junk_stored_order_cannot_break_the_table(results):
    """A stored order is data from an older version of the file, and is treated as such."""
    repair = results["columns_repair"]
    # Unknown keys are dropped, duplicates collapse, and a column the stored order forgets is
    # appended - so a release that adds a column shows it instead of hiding it for ever.
    assert repair["stale"]["order"] == "notes,date,employee,role,moallem,id,site,category,arrival,hours,awaiting"
    assert repair["stale"]["headers"][0] == "Open notes"
    assert repair["stale"]["has_table"] is True
    assert repair["junk"]["order"] == "date,employee,role,moallem,id,site,category,arrival,hours,awaiting,notes", (
        "junk under the key falls back to the default order, it does not empty the table"
    )
    assert repair["junk"]["headers"] == [
        "Date", "Employee", "Role", "Moallem", "User ID", "Site", "Category", "Arrival", "Hours",
        "Awaiting approval", "Open notes", PRINT_ACTION_HEADER
    ]
    assert repair["wrong_type"] == "date,employee,role,moallem,id,site,category,arrival,hours,awaiting,notes", (
        "a stored value that is not a list is not an order"
    )


def test_the_ends_of_the_column_list_are_ends_and_reset_puts_the_default_back(results):
    reset = results["columns_reset"]
    assert reset["at_start"] == "no-move", "the first column cannot move further left"
    assert reset["edges"]["first_cannot_go_earlier"] is True, "and the button says so"
    assert reset["edges"]["last_cannot_go_later"] is True
    assert reset["before_reset"] == "date,employee,role,moallem,id,site,category,arrival,hours,notes,awaiting", (
        "one step left puts Open notes beside the hours it explains"
    )
    assert reset["column_order"] == [
        "date", "employee", "role", "moallem", "id", "site", "category", "arrival", "hours",
        "awaiting", "notes"
    ]
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


#: The console's landing tab, named once here so a link that does *not* apply can be asserted to
#: leave the reader where they were without this file carrying the screen's name twice. It is the
#: Dashboard since it became the console's front door (see ``test_frontend_dashboard.py``).
LANDING_TAB = "Dashboard"


def test_a_fragment_that_is_not_a_period_is_ignored(results):
    junk = results["junk_link"]
    assert junk["junk_state"] == "unset", "junk in the fragment must not become the period"
    assert junk["junk_tab"] == LANDING_TAB
    assert junk["junk_report_requests"] == 0
    assert junk["backwards_state"] == "unset", "a backwards range is not a period"
    assert junk["backwards_tab"] == LANDING_TAB


def test_a_link_pasted_into_the_open_tab_applies_without_a_reload(results):
    change = results["hash_change"]
    assert change["before_tab"] == LANDING_TAB
    assert change["adopted"] == "2026-08-01..2026-08-31"
    assert change["tab"] == "Shifts"
    assert change["query"] == {"start": "2026-08-01", "end": "2026-08-31"}
    assert change["hours"] == "40"


def test_the_search_box_finds_a_role_in_the_words_the_column_shows(results):
    """A column nobody can filter by is a column somebody has to scan by eye."""
    search = results["search"]
    # The one moallem in the fixture, and only their shift: the role is in the haystack as
    # the code that arrives and as the label the table paints, which is what makes
    # "whose shifts are these" a question this box answers.
    assert search["by_role"]["shift_rows"] == 1
    assert search["by_role"]["hours"] == "8"
    assert search["by_role"]["query"] == "moallem"


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


def test_a_number_in_the_search_box_is_a_worker_id_and_not_a_digit_of_the_date(results):
    """This deployment's ids are `1`, `2`, `4` - every one of them a digit of every date.

    So a substring match over the whole row cannot answer "whose shifts are these": `4`
    found worker 4's row *and* every row worked on the 4th, and a fragment of an id that
    names nobody found the whole period. The row's own fields are the haystack for a
    number; the date is searched by typing it, dashes and all.
    """
    ids = results["worker_ids"]
    assert ids["all"]["shift_rows"] == 2
    assert ids["by_id"]["shift_rows"] == 1, "'4' is one worker, not everyone who worked on the 4th"
    assert ids["by_id"]["first_row"][4] == "4", "and the row is that worker's"
    assert ids["by_other_id"]["shift_rows"] == 1, "'2' is the other one"
    assert ids["by_other_id"]["first_row"][4] == "2"
    assert ids["by_year"]["shift_rows"] == 0, (
        "a number that names nobody is a search with no matches, not the whole period"
    )
    assert ids["by_year"]["has_no_matches"] is True
    # The day search is untouched by the rule: a date is not a bare number.
    assert ids["by_day"]["shift_rows"] == 1
    assert ids["by_day"]["first_row"][0] == "2003-01-04"


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

#: The header of the row action - the screen's own last cell, and no part of the stored
#: column order, because a control is not a column. One word, and deliberately without an
#: apostrophe: the suite reads headers off the markup with the tags stripped and does not
#: decode entities, so a label with one in it would be compared as ``&#39;``.
PRINT_ACTION_HEADER = "Print"


# ---------------------------------------------------------------------------
# 4f. the arrival: on time, or late by how much - and searchable
# ---------------------------------------------------------------------------
def test_the_timesheet_says_who_arrived_late_and_by_how_much(results):
    """One row's arrival is the shift's own clock-in against the window its site applies."""
    initial = results["initial"]
    assert initial["late_arrivals"] == "1", "one of the three shifts walked in late"
    # Column order: date, employee, role, moallem, id, site, category, arrival, hours, awaiting, notes.
    assert initial["rows"][0][7] == "On time", initial["rows"][0]
    assert initial["rows"][2][7] == "Late 42 min", initial["rows"][2]


def test_a_search_for_late_arrivals_finds_them_and_their_minutes(results):
    """The question an administrator actually asks: who was late, and by how much."""
    search = results["search"]
    assert search["by_late"]["shift_rows"] == 1, "one shift arrived late"
    assert search["by_late"]["late_arrivals"] == "1", "and the figure above it says so"
    assert search["late_by"]["shift_rows"] == 1, "a second term is how far off: 'late 42'"
    assert search["late_by"]["hours"] == "3", "the narrowed view totals only that shift"
    assert search["by_on_time"]["shift_rows"] == 2, "the two that were inside the window"
    assert search["by_on_time"]["late_arrivals"] == "0", "nothing late is left in the list"
    assert search["by_late"]["has_no_matches"] is False


def test_a_shift_with_no_arrival_on_file_is_not_called_punctual(results):
    """The unknown state matters: 'no clock-in' is not 'on time'."""
    missing = results["no_arrival"]
    assert missing["cell"] == "No clock-in", missing["cell"]
    assert missing["late_arrivals"] == "0"
    assert missing["search_no_match"] is True, (
        "and it is not found by a search for the punctual ones either"
    )


def test_the_printed_sheet_carries_the_arrival_but_the_csv_does_not(results):
    """The screen gained a column; the file did not, and paper follows the screen.

    The CSV is the four fixed columns on purpose: two months of sheets have to line up
    column for column, and a column that appears in one month's file and not the other's is
    a spreadsheet nobody can compare. The printed sheet is the table on screen, which is
    what the person holding it has been reading.
    """
    download = results["download"]
    header, rows = csv_rows(download["csv"])
    assert header == CSV_HEADER, "still who, their id, where and how long"
    assert len(header.split(",")) == 4
    assert len(rows) == 3, "the file is the whole period, arrival column or not"
    assert "late" not in download["csv"].lower()
    assert "on time" not in download["csv"].lower()
    # ...and the column really is on screen, so this is about the file rather than about a
    # column that was never added.
    assert "Arrival" in results["columns_default"]["headers"]
    assert "Arrival" in results["print"]["screen_headers"]
    assert "Late 42 min" in results["print"]["sheet"], "paper keeps the arrival column"


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


# ---------------------------------------------------------------------------
# 5b. the PDF choice: the same report, printed by the browser
# ---------------------------------------------------------------------------
def test_the_report_is_downloaded_as_a_spreadsheet_or_printed_as_a_pdf(results):
    """One button, two files, and the page's own rows either way."""
    printed = results["print"]
    assert printed["printed"] == 1, "the PDF choice has to reach the browser's print dialog"
    assert printed["printing"] is True, (
        "the console has to be out of the paper before the dialog opens"
    )
    assert printed["files_written"] == 0, "the page writes no PDF - the dialog does"
    assert printed["requests_added"] == 0, "the sheet is built from the rows already in hand"


def test_the_printed_sheet_holds_the_rows_on_screen_and_the_period_in_its_name(results):
    """The sheet names its period, holds the three shifts, and totals what they add to.

    The name matters as much as the rows: "Save as PDF" offers the *document title*, so a
    sheet called "Al-Jehad - Site Attendance" is a folder of files nobody can tell apart.
    """
    printed = results["print"]
    expected = printed["expected_range"]
    assert printed["title"] == f"shifts_{expected['start']}_{expected['end']}", (
        "the period has to be in the name the dialog offers"
    )
    sheet = printed["sheet"]
    for name in ("Seed Lead", "Ana Torres", "Bilal Khan"):
        assert name in sheet, f"{name}'s shift is on screen but not on the sheet"
    assert "Downtown Tower A" in sheet and "Harbour Depot" in sheet
    assert sheet.count("<tr>") == 4, "one header row and the three shifts"
    assert printed["screen_hours"] in sheet, "the total printed is the total on screen"


def test_the_printed_sheet_keeps_the_columns_the_administrator_arranged(results):
    """Paper is read by whoever set the table up, so the sheet follows their order.

    The CSV deliberately does not - fixed columns so two months can be compared - and this
    is the difference between the two files, asserted rather than assumed.
    """
    printed = results["print"]
    headers = re.findall(r"<th>(.*?)</th>", printed["sheet"])
    assert printed["screen_headers"][-1] == PRINT_ACTION_HEADER, (
        "the row action is the screen's last column"
    )
    assert headers == printed["screen_headers"][:-1], (
        "the sheet's columns are the screen's, minus the row action - a button is not paper"
    )
    assert headers[0] == "Date", "the default order, not the file's fixed four"


def test_the_printed_sheet_is_the_search_the_administrator_ran(results):
    filtered = results["print"]["filtered"]
    assert "Ana Torres" in filtered["sheet"] and "Bilal Khan" in filtered["sheet"]
    assert "Seed Lead" not in filtered["sheet"], "the shift the search excluded was printed"
    assert filtered["title"].endswith("_harbour"), "two prints of one period must not clash"


def test_closing_the_dialog_puts_the_console_back(results):
    """A print that left the page muted would leave the next one printing a stale sheet."""
    after = results["print"]["after"]
    assert after["printing"] is False, "the body still claims a report is being printed"
    assert after["title"] == "Al-Jehad - Site Attendance", "the tab's own title comes back"
    assert after["sheets_left"] == 0, (
        "a sheet left in the page is a second one printed behind the next report"
    )


def test_a_print_with_nothing_on_screen_prints_nothing(results):
    nothing = results["print"]["nothing_to_export"]
    assert nothing["printed"] == 0, "a failed request leaves nothing to print"
    assert nothing["toasts"] == ["There is nothing on screen to download for this period."]


def test_a_period_with_no_shifts_prints_the_sentence_not_an_empty_table(results):
    """Two states that must not look alike: no report, and a report with no rows.

    The frame around a sheet is shared with the worker's own timesheet, so this is where
    "a sheet for an empty period says so" is pinned: a print that produced a titled table
    with no rows would read as a month of no work rather than a month with nothing filed.
    """
    quiet = results["print"]["quiet"]
    assert quiet["printed"] == 1, "there *is* a report, so the dialog opens"
    assert quiet["has_empty_note"] is True
    assert quiet["has_table"] is False, "no table at all, rather than a table with no rows"
    assert "No shifts in this period." in quiet["sheet"]


def test_the_untouched_format_field_still_writes_the_spreadsheet(results):
    """The safe failure is the file the button used to write, not a print dialog."""
    default = results["print"]["default_format"]
    assert default["printed"] == 0, "an unknown or missing format must never print"
    assert default["files"] == 2
    assert default["filename"].endswith(".csv")


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
    # The same columns, in the same order - a card is a row that had to fold.
    assert mobile["labels"][:11] == [
        "Date", "Employee", "Role", "Moallem", "User ID", "Site", "Category", "Arrival", "Hours",
        "Awaiting approval", "Open notes"
    ]
    assert mobile["site_names"] == ["Downtown Tower A", "Harbour Depot", "Harbour Depot"], (
        "a phone card names the same site the desktop row does"
    )


def test_a_shift_with_no_site_on_file_still_gets_a_row(results):
    """A missing site must not blank the row or the figures above it."""
    table = results["sites"]["no_site"]
    assert table["shift_rows"] == 1
    assert table["site_names"] == []
    assert table["first_row"][5] == table["em_dash"], "an em dash, not an empty cell"
    assert table["hours"] == "6"


def test_a_site_name_cannot_inject_markup(results):
    """Site names are admin-authored too, and they are rendered in every row."""
    escaped = results["escaped"]
    assert escaped["raw_site_tag"] is False
    assert escaped["escaped_site_tag"] is True


def test_the_tab_tapped_last_is_the_tab_left_on_screen(results):
    """One screen, one request, one tap - and the tap is the one that survives.

    A tab is not painted by the click, it is painted by whatever its own requests come back
    with, and the console opens on a tab of its own. On a slow connection an admin taps Shifts
    inside that first read, so the two answers can arrive in either order - and the answer that
    arrived last used to be the one on screen. The failure is not cosmetic: the rows an admin
    is about to approve belong to a screen they have already left, under the dates of the one
    they asked for.

    The scenario holds the board's own read open until after the tap, which is the arrival
    order that used to lose, and then asks what is on the page. It also reports how many reads
    were really held, so a run where nothing was in flight fails as a scenario that proved
    nothing rather than passing quietly.
    """
    order = results["order"]
    assert order["was_out"] == 1, (
        "the opening screen's read was not still in flight when the tab was tapped, so this "
        f"scenario never put the two answers in the order it exists to put them in\n{order}"
    )
    assert order["shifts_after_both"] is True, (
        "the board landed after the tab that was tapped and painted over it: the admin is "
        "looking at a screen they left"
    )
    assert order["dashboard_after_both"] is False, (
        "the front door is still in the page the admin asked to leave"
    )
    assert order["tab"] == "Shifts", (
        f"the tab the session thinks it is on and the screen do not agree: {order['tab']!r}"
    )
    # And it is the real tab, not an empty one that merely kept its period picker.
    assert order["rows_after_both"] == 3
    assert order["hours"] == "15"


def test_a_worker_name_cannot_inject_markup_into_the_totals(results):
    escaped = results["escaped"]
    assert escaped["name_present"] is True
    assert escaped["raw_tag"] is False
    assert escaped["escaped_tag"] is True
    assert escaped["hours"] == "50"


# ---------------------------------------------------------------------------
# The strip, the attention chips, the sort, and the page
#
# Four things the tab grew because it could say what a period added up to and never
# *when* it was worked, never which rows needed somebody, could not be read in any
# order but the server's, and painted a payroll month as one string of 15 000 cells.
# Each is asserted against the report it was built from rather than against numbers
# typed here twice.
# ---------------------------------------------------------------------------
def test_the_strip_reads_the_period_one_column_per_day(results):
    """The same hours as the cards, arranged by when they were worked.

    The strip is the tab's own figures over a time axis, so it is checked against those
    figures: 15 h over three shifts, two of them on the 5th with one still waiting for a
    decision, and one on the 12th that arrived an hour late.
    """
    coverage = results["coverage"]
    assert coverage["has_strip"] is True, "a period with work in it needs a strip"
    assert coverage["unit"] == "day"
    assert coverage["count"] == 31, "January has 31 columns, worked or not"
    assert coverage["buttons"] == 31, "every column is a button: tapping one narrows the period"
    assert coverage["days"][0] == "2004-01-01" and coverage["days"][-1] == "2004-01-31"
    assert coverage["has_legend"] is True, "two series need a key, and not a colour-only one"
    assert coverage["has_hint"] is True, "a control nobody is told about is a control nobody uses"

    buckets = {entry[0]: entry for entry in coverage["buckets"]}
    assert buckets["2004-01-05"][1] == 11, buckets["2004-01-05"]
    assert buckets["2004-01-05"][2] == 3, "the pending shift's hours are counted apart"
    assert buckets["2004-01-05"][3] == 0
    assert buckets["2004-01-12"][1] == 4 and buckets["2004-01-12"][3] == 1
    assert buckets["2004-01-07"][1] == 0, "a day nobody worked is a column, not a gap"

    # The bars are a share of the busiest day in the window: the busiest is full height, and
    # the day beside it is a third of it rather than a second full bar. Only the two worked
    # days have a bar at all - which is how a reader sees that 29 days hold nothing.
    assert len(coverage["bars"]) == 2, coverage["bars"]
    assert coverage["bars"][0] == 100, "the 5th is the busiest day of this window"
    assert 30 <= coverage["bars"][1] <= 40, coverage["bars"][1]
    assert coverage["undecided"][0] == 27, "3 h of the 5th's 11 h are still waiting"
    assert coverage["undecided"][1] == 0, "nothing on the 12th is waiting"
    assert coverage["late"] == [1], "only the 12th had a late arrival"

    assert "31 days" in coverage["caption"]
    assert f"{coverage['hours']} h counted" in coverage["caption"]
    assert f"{coverage['awaiting_approval_hours']} h awaiting a decision" in coverage["caption"]
    assert coverage["labels"] == 31, "one labelled column each: the strip for a reader who hears it"


def test_a_long_period_is_read_in_months_rather_than_in_days(results):
    """A year is twelve columns, not 365: the strip has to survive the window it is given."""
    year = results["coverage_year"]
    assert year["unit"] == "month"
    assert year["count"] == 12, "twelve months in a year"
    assert year["days"][0] == "2005-01-01" and year["days"][-1] == "2005-12-01"
    assert year["bars"] == [100], "one shift in June, and it is the busiest month by default"
    assert "12 months" in year["caption"]


def test_a_period_with_no_shifts_draws_no_strip(results):
    """The empty panel already says the period is empty; a row of zero columns says it twice."""
    empty = results["coverage_empty"]
    assert empty["has_strip"] is False
    assert empty["count"] == 0


def test_a_column_of_the_strip_narrows_the_period_to_that_day(results):
    """What happened on that Tuesday is asked by pointing at the Tuesday."""
    tap = results["coverage_tap"]
    assert tap["selected_while_filtered"] is False, "no column is the period until a day is taken"
    assert tap["state"] == "2004-01-12..2004-01-12"
    assert tap["request"] == {"start": "2004-01-12", "end": "2004-01-12"}, (
        "the strip must ask for the day it was tapped on"
    )
    assert tap["query"] == "", "the search was a request for a day, and the day has been taken"
    assert tap["shift_rows"] == 1 and tap["hours"] == "4"


def test_the_amber_figures_become_filters_carrying_the_count_they_will_show(results):
    """Two questions about a period, asked by tapping the figure that raises them.

    The window is chosen so the two answers are different shifts: the one waiting for a
    decision arrived on time, and the one that arrived an hour late has already been decided.
    """
    attention = results["attention"]
    assert [chip[0] for chip in attention["chips"]] == ["awaiting", "late"]
    assert attention["chips"][0][2] == "Awaiting approval (1)", attention["chips"]
    assert attention["chips"][1][2] == "Late arrivals (1)", attention["chips"]
    assert [chip[1] for chip in attention["chips"]] == ["false", "false"], (
        "neither filter is on until it is tapped"
    )

    # The count on the chip is the card's own figure: the same 3 h the period is waiting on.
    assert attention["awaiting"]["awaiting_approval"] == "1"
    assert attention["awaiting"]["shift_rows"] == 1
    assert attention["awaiting"]["hours"] == "3", "the pending shift's own three hours"
    assert attention["awaiting"]["approved_hours"] == "0"
    assert attention["awaiting"]["awaiting_approval_hours"] == "3"
    assert attention["awaiting"]["has_filter_note"] is True, (
        "the figures above a narrowed table have to say they cover fewer rows"
    )
    assert "Awaiting approval" in attention["note"], attention["note"]
    assert "1 / 3" in attention["note"], attention["note"]

    # The other chip selects the other shift - 4 h, decided, late.
    assert attention["late"]["shift_rows"] == 1
    assert attention["late"]["hours"] == "4"

    # Tapping the chip that is on turns it off again.
    assert attention["cleared"]["shift_rows"] == 3
    assert attention["cleared"]["state"] == ""
    assert attention["cleared"]["hours"] == "15"

    # The attention filter travels into the file, like every other narrowing on this tab.
    lines = attention["csv"].strip().split("\r\n")
    assert len(lines) == 2, lines
    assert lines[1] == "Strip Two,611,Downtown Tower A,3", lines

    # Search, category and attention narrow the same rows rather than cancelling out.
    assert attention["both"]["shift_rows"] == 1
    assert attention["narrowed"]["shift_rows"] == 1, (
        "the pending shift is at a warehouse site, so the category keeps it"
    )

    # A period whose rows are all decided and all on time offers no chip at all: a filter that
    # selects nothing is a dead end, and the card that would raise the question is a zero.
    assert attention["quiet_chips"] == 0


def test_a_column_can_be_sorted_and_the_third_press_puts_the_period_back(results):
    """A thousand rows cannot be read in any order but the one you need.

    Three states, not two: the order the server sent has to stay reachable, or an
    administrator who sorted the table once has lost the period's own order for the session.
    """
    sorting = results["sorting"]
    assert sorting["initial"]["sort"] == '{"key":"","direction":"asc"}', sorting["initial"]["sort"]
    assert sorting["initial"]["sorted_headers"] == [], "nothing is sorted until a header is pressed"
    assert sorting["initial"]["rows"][0][0] == "2026-08-07", "the server's own order, untouched"

    # Hours starts at its biggest: 8, 4, 3 - which here is also the order they arrived in.
    assert sorting["by_hours"]["sort"] == '{"key":"hours","direction":"desc"}'
    assert [row[8] for row in sorting["by_hours"]["rows"]] == ["8", "4", "3"]
    assert len(sorting["by_hours"]["sorted_headers"]) == 1, sorting["by_hours"]["sorted_headers"]
    assert 'aria-sort="descending"' in sorting["by_hours"]["sorted_headers"][0]

    # Pressed again: the same column, the other way, smallest shift first.
    assert sorting["by_hours_asc"]["sort"] == '{"key":"hours","direction":"asc"}'
    assert [row[8] for row in sorting["by_hours_asc"]["rows"]] == ["3", "4", "8"]
    assert sorting["by_hours_asc"]["rows"][0][1] == "Bilal Khan"
    assert 'aria-sort="ascending"' in sorting["by_hours_asc"]["sorted_headers"][0]

    # A third press is the way back, and the header stops claiming to sort anything.
    assert sorting["restored"]["sort"] == '{"key":"","direction":"asc"}'
    assert sorting["restored"]["sorted_headers"] == []
    assert sorting["restored"]["rows"] == sorting["initial"]["rows"]

    # A different column, sorted by what the cell shows rather than by the wire value.
    assert [row[1] for row in sorting["by_name"]["rows"]] == ["Ana Torres", "Bilal Khan", "Seed Lead"]

    # The header's *text* is still exactly the column's name: the arrow is drawn, not written,
    # because the printed sheet and the file read this same text.
    assert sorting["by_name"]["headers"] == sorting["initial"]["headers"]
    assert sorting["by_name"]["headers"][0] == "Date"

    # The file follows the screen: a table being read in a sorted order exports in that order.
    lines = sorting["csv"].strip().split("\r\n")
    assert lines[1] == "Ana Torres,601,Harbour Depot,4", lines

    # And none of this is a setting: a sort is how one reader is holding the page.
    assert sorting["stored"] is None


def test_a_month_that_fills_more_than_one_page_is_painted_one_page_at_a_time(results):
    """120 shifts are painted 50 at a time, and the file is not.

    The cap is on what is *drawn*: the download and the printed sheet are the rows the view
    holds, or a file would quietly become whatever somebody had scrolled to.
    """
    paging = results["paging"]
    assert paging["first"]["shift_rows"] == 50, "one page of the 120"
    assert paging["first"]["count_line"] == "120"
    assert paging["first"]["more"] is True
    assert paging["first"]["limit"] == 0, "nothing has been grown yet"
    assert paging["first"]["hours"] == "960", "the figures are the period's, not the page's"

    assert paging["second"]["shift_rows"] == 100
    assert paging["second"]["more"] is True

    assert paging["third"]["shift_rows"] == 120
    assert paging["third"]["more"] is False, "the whole period is painted: nothing left to offer"

    # The two files are the period, whatever has been scrolled to.
    assert paging["csv_rows"] == 120
    assert paging["sheet_rows"] == 121, "one header row and the 120 shifts"

    # A new search is a new list: the painted count goes back to one page of it.
    assert paging["searched"]["limit"] == 0
    assert paging["searched"]["shift_rows"] == 50
    assert paging["searched"]["count_line"] == "120", "the search matched every row of the period"
