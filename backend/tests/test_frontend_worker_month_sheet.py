"""One worker's month on paper, from the console.

WHY THIS EXISTS
---------------
The Shifts tab prints the *period*: every shift, every worker, for the dates on screen. That
is the right sheet for a period and the wrong one for a person, and "print what this worker
did this month" is what an administrator is actually asked for - usually while looking at one
of that worker's shifts. The row already knows whose shift it is, so that sheet is one tap
from the shift that raised the question.

Three things make it a different sheet rather than a filter over the period one, and each of
them is a way to get it wrong:

1. **The subject is the shift's worker**, sent as ``worker_id``, so the rows come back from
   the server for one person. Filtering on the client would print whatever this screen
   happened to be holding - narrowed by a search, or short of a month.
2. **The period is that person's calendar month**, not the dates on screen: the tab may be
   showing a week, a day, or a shared link's odd range, and the sheet is still the month. It
   is clipped to today while the month is running, like the presets are - a timesheet ending
   in the future reads as a month that lost its last shifts.
3. **The identity columns come off the table and into the header**: a one-worker sheet that
   repeated the employee, the role and the id down every row would be twenty lines of the
   same three facts, and the line above the table says them once.

What is asserted here, end to end through the real frontend files: the request (which month,
whose rows), the sheet (whose, which month, whose rows and no one else's, which columns, what
it totals), the name the print dialog offers, the two refusals - a month nobody worked, and a
request that failed - and the page coming back once the dialog closes.

The *click* that starts all of it is bound when the tab paints, which is the one thing no VM
can show: the stub DOM has no HTML parser, so the delegated click is driven directly here and
by a real browser in ``test_signed_in_sessions_in_a_browser``.

Node is optional; without it these skip rather than fail.
"""

from __future__ import annotations

import pytest

import frontend_vm

pytestmark = pytest.mark.skipif(frontend_vm.NODE is None, reason="node is not installed")

#: The scenarios: a fake API that behaves like the report endpoint, and the runs that fill
#: ``results``. The VM, the stub DOM, the recorded fetch and the modelled print dialog come
#: from ``frontend_vm`` - one environment for every suite, not one per suite.
HARNESS = r"""
// --- the server's answer, faked ------------------------------------------

function iso(when) {
    const pad = (v) => String(v).padStart(2, '0');
    return when.getFullYear() + '-' + pad(when.getMonth() + 1) + '-' + pad(when.getDate());
}

const TODAY = iso(new Date());

// One shift, with everything a timesheet row can show.
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
        open_notes: 0,
        arrival_time: '2026-08-07 04:20:00',
        arrival_verdict: 'on_time',
        arrival_minutes: 0
    }, overrides || {});
}

// Derived rather than typed twice, so a fixture cannot describe rows whose totals disagree
// with them. The Python assertions still use literal numbers.
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
        late_arrivals: rows.filter((row) => row.arrival_verdict === 'late').length
    };
}

// Two of one worker's days in August - the second still waiting for a decision - one day of
// another worker's, one of a worker whose account is gone, one dated today for the
// current-month case, and one whose *id* is a quote-and-attribute payload: the id travels
// into an attribute of the row's own control and into the request's query string.
const SEED = [
    shift({
        log_id: 901, date: '2026-08-07', worker_id: '600', worker_name: 'Seed Lead',
        role: 'moallem', site_name: 'Downtown Tower A', hours: 8, recorded_hours: 8.5,
        break_hours: 0.5
    }),
    shift({
        log_id: 902, date: '2026-08-12', worker_id: '600', worker_name: 'Seed Lead',
        role: 'moallem', site_name: 'Harbour Depot', hours: 7.5, recorded_hours: 9.5,
        break_hours: 0, status_code: 'pending_review', status: 'pending_review',
        awaiting_approval: true
    }),
    shift({
        log_id: 903, date: '2026-08-06', worker_id: '601', worker_name: 'Ana Torres',
        role: 'worker', site_name: 'Harbour Depot', hours: 4, recorded_hours: 4.5,
        break_hours: 0.5
    }),
    shift({
        log_id: 904, date: '2026-08-20', worker_id: '603', worker_name: 'Noor Haddad',
        role: null, site_name: null, hours: 6, recorded_hours: 6, break_hours: 0
    }),
    shift({
        log_id: 905, date: TODAY, worker_id: '700', worker_name: 'Today Worker',
        role: 'worker', site_name: 'Downtown Tower A', hours: 5, recorded_hours: 5,
        break_hours: 0
    }),
    shift({
        log_id: 906, date: '2026-04-04', worker_id: '600" onmouseover="alert(1)',
        worker_name: 'Mallory', role: 'worker', site_name: 'Depot', hours: 9,
        recorded_hours: 9, break_hours: 0
    })
];

function report(start, end, rows, workerId) {
    return {
        period: { start: start, end: end },
        filters: { site: null, worker_id: workerId || null },
        rows: rows,
        totals: totalsOf(rows)
    };
}

// The endpoint as this screen sees it: the window's rows, and - when the request names a
// worker - only that worker's. A frontend that forgot the parameter would get everybody
// back and print them, which is what the assertions below catch. ``end`` is the window's
// last day here, the same value the console sends.
function shiftsResponder(url) {
    if (!url.includes('/admin/reports/shifts')) return { status: 200, body: {} };
    const params = new URLSearchParams(url.split('?')[1] || '');
    const start = params.get('start');
    const end = params.get('end');
    const worker = params.get('worker_id');
    if (worker === 'boom') return { status: 500, body: { detail: 'the report is down' } };
    const rows = SEED.filter((row) =>
        row.date >= start && row.date <= end && (!worker || row.worker_id === worker));
    return { status: 200, body: report(start, end, rows, worker) };
}

// --- reading the rendered page -------------------------------------------

function text(html) {
    return String(html || '').replace(/<[^>]*>/g, '').trim();
}

function attribs(html, name) {
    return (String(html || '').match(new RegExp('data-' + name + '="([^"]*)"', 'g')) || [])
        .map((pair) => pair.slice(pair.indexOf('"') + 1, -1));
}

function sheetOf(printed) {
    return (printed[printed.length - 1] || {}).sheet || '';
}

function sheetTitle(sheet) {
    const match = /<p class="print-sheet-title">([\s\S]*?)<\/p>/.exec(sheet);
    return match ? text(match[1]) : null;
}

function metaLines(sheet) {
    return (sheet.match(/<p class="print-sheet-meta">[\s\S]*?<\/p>/g) || []).map(text);
}

function sheetHeaders(sheet) {
    return (sheet.match(/<th>[\s\S]*?<\/th>/g) || []).map(text);
}

function sheetRows(sheet) {
    const body = (/<tbody>([\s\S]*?)<\/tbody>/.exec(sheet) || ['', ''])[1];
    return (body.match(/<tr>[\s\S]*?<\/tr>/g) || []).map((row) =>
        (row.match(/<td>[\s\S]*?<\/td>/g) || []).map(text));
}

function sheetTotals(sheet) {
    const match = /<p class="print-sheet-totals">([\s\S]*?)<\/p>/.exec(sheet);
    return match ? text(match[1]) : null;
}

function toasts(env) {
    return env.evaluate("(document.getElementById('toastRoot').__children || []).map((el) => el.textContent)");
}

function monthRequest(env, from) {
    const urls = env.requests.slice(from).map((r) => r.url)
        .filter((url) => url.includes('/admin/reports/shifts'));
    if (urls.length === 0) return null;
    const params = new URLSearchParams(urls[urls.length - 1].split('?')[1] || '');
    return {
        count: urls.length,
        start: params.get('start'),
        end: params.get('end'),
        worker: params.get('worker_id'),
        raw: urls[urls.length - 1]
    };
}

function adminEnv() {
    const env = boot({});
    env.setResponder(shiftsResponder);
    env.evaluate("State.saveUser({ id: '1000', name: 'Admin', role: 'admin', token: 'tok-admin' })");
    return env;
}

// The scenarios. ``results`` is printed by the epilogue in ``frontend_vm``.
const results = {};

    // 1. one worker's August, from a tab that is showing a single day of it
    {
        const env = adminEnv();
        await env.evaluate("UI_MODULES.setShiftsRange('2026-08-07', '2026-08-07')");
        await env.evaluate("UI.renderAdminTab('Shifts')");
        const screen = env.evaluate("document.getElementById('adminContent').innerHTML");
        const before = env.requests.length;
        await env.evaluate("UI_MODULES.printWorkerMonth('600')");
        const printed = env.printed[env.printed.length - 1] || {};
        const sheet = sheetOf(env.printed);
        results.month = {
            request: monthRequest(env, before),
            printed: env.printed.length,
            title: printed.title || null,
            printing: printed.printing === true,
            sheet: sheet,
            sheet_title: sheetTitle(sheet),
            meta: metaLines(sheet),
            headers: sheetHeaders(sheet),
            rows: sheetRows(sheet),
            totals: sheetTotals(sheet),
            workers_on_screen: attribs(screen, 'worker').length,
            screen_shift_rows: (screen.match(/data-shift=/g) || []).length,
            screen_print_buttons: (screen.match(/data-print-worker=/g) || []).length,
            files_written: env.blobs.length
        };
        env.fireWindowEvent('afterprint');
        results.month.after = {
            title: env.evaluate('document.title'),
            printing: env.evaluate("document.body.classList.contains('is-printing-report')"),
            sheets_left: env.evaluate(
                "document.body.__children.filter((c) => c.className === 'print-sheet').length")
        };
    }

    // 2. the current month is clipped to today, like the presets: a sheet may not describe
    //    work that has not happened yet
    {
        const env = adminEnv();
        await env.evaluate("UI.renderAdminTab('Shifts')");
        const before = env.requests.length;
        await env.evaluate("UI_MODULES.printWorkerMonth('700')");
        const sheet = sheetOf(env.printed);
        results.current_month = {
            request: monthRequest(env, before),
            today: TODAY,
            sheet: sheet,
            meta: metaLines(sheet),
            rows: sheetRows(sheet),
            printed: env.printed.length
        };
        env.fireWindowEvent('afterprint');
    }

    // 3. the month is a calendar month, read off the period on screen rather than the clock
    {
        const env = adminEnv();
        results.month_rule = {
            inside_a_month: env.evaluate("UI_MODULES.shiftsWorkerMonth({ start: '2026-08-07', end: '2026-08-07' })"),
            whole_past_month: env.evaluate("UI_MODULES.shiftsWorkerMonth({ start: '2026-02-14', end: '2026-02-20' })"),
            this_month: env.evaluate("UI_MODULES.shiftsWorkerMonth({ start: '" + TODAY + "', end: '" + TODAY + "' })"),
            future_month: env.evaluate("UI_MODULES.shiftsWorkerMonth({ start: '2030-05-05', end: '2030-05-05' })"),
            junk: env.evaluate("UI_MODULES.shiftsWorkerMonth({ start: 'not-a-date', end: 'nor-this' })")
        };
    }

    // 4. a month nobody worked: refused in words, and without a sheet
    {
        const env = adminEnv();
        await env.evaluate("UI.renderAdminTab('Shifts')");
        const before = { printed: env.printed.length, requests: env.requests.length };
        await env.evaluate("UI_MODULES.printWorkerMonth('999')");
        results.nobody = {
            request: monthRequest(env, before.requests),
            printed: env.printed.length - before.printed,
            toasts: toasts(env),
            printing: env.evaluate("document.body.classList.contains('is-printing-report')"),
            sheets_left: env.evaluate(
                "document.body.__children.filter((c) => c.className === 'print-sheet').length")
        };
    }

    // 5. a request that failed: nothing printed, and nothing left muted behind it
    {
        const env = adminEnv();
        await env.evaluate("UI.renderAdminTab('Shifts')");
        const before = env.printed.length;
        await env.evaluate("UI_MODULES.printWorkerMonth('boom')");
        results.failed = {
            printed: env.printed.length - before,
            toasts: toasts(env),
            printing: env.evaluate("document.body.classList.contains('is-printing-report')"),
            sheets_left: env.evaluate(
                "document.body.__children.filter((c) => c.className === 'print-sheet').length")
        };
    }

    // 6. the delegated click: a control on a row is what starts this, and a click anywhere
    //    else in the tab does nothing
    {
    const env = adminEnv();
    await env.evaluate("UI_MODULES.setShiftsRange('2026-08-01', '2026-08-31')");
    await env.evaluate("UI.renderAdminTab('Shifts')");
    const before = { printed: env.printed.length, requests: env.requests.length };
    await env.evaluate("UI_MODULES.onShiftsClick({ target: { closest: () => null } })");
        results.click = {
            nothing_printed: env.printed.length === before.printed,
            no_request: env.requests.length === before.requests
        };
        await env.evaluate(
            "UI_MODULES.onShiftsClick({ target: { closest: () => ({ dataset: { printWorker: '600' } }) } })"
        );
        results.click.printed = env.printed.length - before.printed;
        results.click.worker = (monthRequest(env, before.requests) || {}).worker;
        env.fireWindowEvent('afterprint');
    }

    // 7. an account that is gone keeps its name, and the header says so without a gap
    {
        const env = adminEnv();
        await env.evaluate("UI_MODULES.setShiftsRange('2026-08-01', '2026-08-31')");
        await env.evaluate("UI.renderAdminTab('Shifts')");
        await env.evaluate("UI_MODULES.printWorkerMonth('603')");
        const sheet = sheetOf(env.printed);
        results.gone = {
            meta: metaLines(sheet),
            sheet: sheet,
            rows: sheetRows(sheet)
        };
        env.fireWindowEvent('afterprint');
    }

    // 8. a worker id that is a quote and an attribute: it goes into the row's own control
    //    and into the query string, and neither is a place a value becomes markup
    {
        const env = adminEnv();
        await env.evaluate("UI_MODULES.setShiftsRange('2026-04-01', '2026-04-30')");
        await env.evaluate("UI.renderAdminTab('Shifts')");
        const screen = env.evaluate("document.getElementById('adminContent').innerHTML");
        const before = env.requests.length;
        await env.evaluate("UI_MODULES.printWorkerMonth('600\" onmouseover=\"alert(1)')");
        const sheet = sheetOf(env.printed);
        results.escaped = {
            attribute: attribs(screen, 'print-worker'),
            raw_in_page: screen.indexOf('onmouseover="alert(1)"') >= 0,
            request: monthRequest(env, before),
            sheet: sheet,
            raw_in_sheet: sheet.indexOf('onmouseover="alert(1)"') >= 0
        };
        env.fireWindowEvent('afterprint');
    }

    // 9. the sheet is in the reader's language, like the rest of the page
    {
        const env = adminEnv();
        const english = {
            title: env.evaluate("I18n.__('shiftsWorkerSheet')"),
            labels: env.evaluate("UI_MODULES.workerMonthColumns().map((key) => UI_MODULES.shiftsColumnLabel(key))")
        };
        env.evaluate("I18n.lang = 'ar'");
        await env.evaluate("UI_MODULES.setShiftsRange('2026-08-01', '2026-08-31')");
        await env.evaluate("UI.renderAdminTab('Shifts')");
        await env.evaluate("UI_MODULES.printWorkerMonth('600')");
        const sheet = sheetOf(env.printed);
        results.translated = {
            english: english,
            title: sheetTitle(sheet),
            expected_title: env.evaluate("I18n.__('shiftsWorkerSheet')"),
            headers: sheetHeaders(sheet),
            expected_headers: env.evaluate(
                "UI_MODULES.workerMonthColumns().map((key) => UI_MODULES.shiftsColumnLabel(key))"),
            meta: metaLines(sheet),
            role_word: env.evaluate("I18n.__('roleMoallem')")
        };
        env.evaluate("I18n.lang = 'en'");
        env.fireWindowEvent('afterprint');
    }

    // 10. the phone layout carries the same control on every card
    {
        const env = adminEnv();
        await env.evaluate("UI_MODULES.setShiftsRange('2026-08-01', '2026-08-31')");
        await env.evaluate("UI.renderAdminTab('Shifts')");
        const table = env.evaluate("document.getElementById('adminContent').innerHTML");
        env.evaluate("localStorage.setItem('layoutOverride', 'mobile')");
        await env.evaluate("UI.renderAdminTab('Shifts')");
        const cards = env.evaluate("document.getElementById('adminContent').innerHTML");
        results.mobile = {
            table_buttons: (table.match(/data-print-worker=/g) || []).length,
            card_buttons: (cards.match(/data-print-worker=/g) || []).length,
            cards: (cards.match(/data-worker=/g) || []).length
        };
    }

    // 11. the builder is total: a report with no rows says so instead of printing a titled
    //     table with nothing in it (the button refuses this case before it can happen, and
    //     a sheet that rendered it as an empty table would be a month of no work)
    {
        const env = adminEnv();
        const sheet = env.evaluate(`UI_MODULES.workerMonthSheetHtml(
            { rows: [], totals: {}, period: { start: '2026-08-01', end: '2026-08-31' } }, '600')`);
        results.empty_builder = {
            sheet: sheet,
            has_empty_note: sheet.indexOf('print-sheet-empty') >= 0,
            has_table: sheet.indexOf('print-sheet-table') >= 0,
            sentence: env.evaluate("I18n.__('shiftsEmpty')")
        };
    }

    // 12. the name the print dialog offers: who, then which month
    {
        const env = adminEnv();
        results.names = {
            month: env.evaluate(
                "UI_MODULES.workerMonthExportName({ start: '2026-08-01', end: '2026-08-31' }, 'Seed Lead')"),
            awkward: env.evaluate(
                "UI_MODULES.workerMonthExportName({ start: '2026-08-01', end: '2026-08-31' }, \"O'Brien / Lead\")"),
            period: env.evaluate("UI_MODULES.shiftsExportName({ start: '2026-08-01', end: '2026-08-31' }, 'harbour', '')")
        };
    }

"""


@pytest.fixture(scope="module")
def results() -> dict:
    return frontend_vm.run(HARNESS)


def test_the_request_is_one_worker_and_that_worker_s_whole_month(results):
    """The two parameters that make this sheet what it is: whose rows, and which month.

    The tab is showing one day of August and the request is August - so the sheet is not the
    period on screen - and it names one worker, so the server answers with that worker's rows
    rather than the whole period's.
    """
    request = results["month"]["request"]
    assert request is not None, "the row action made no request at all"
    assert request["count"] == 1, f"one request per press, not {request['count']}"
    assert request["worker"] == "600", f"the subject of the request is {request['worker']!r}"
    assert request["start"] == "2026-08-01", "the sheet starts at the month's first day"
    assert request["end"] == "2026-08-31", "and ends at its last - not at the period on screen"
    assert results["month"]["screen_shift_rows"] == 1, (
        "the scenario is only about a month if the tab was showing something narrower"
    )


def test_the_sheet_is_that_worker_s_month_and_nobody_else_s(results):
    """One person's days, and the other three workers in the same month are not on it."""
    month = results["month"]
    sheet = month["sheet"]
    assert month["printed"] == 1, "the dialog has to open"
    assert month["printing"] is True, "the console must be out of the paper while it prints"
    assert month["sheet_title"] == "Worker timesheet", month["sheet_title"]
    for day in ("2026-08-07", "2026-08-12"):
        assert day in sheet, f"{day} is one of that worker's shifts and is not on the sheet"
    for other in ("Ana Torres", "Noor Haddad", "Today Worker"):
        assert other not in sheet, f"{other}'s shift was printed on somebody else's sheet"
    assert month["files_written"] == 0, "the page writes no PDF - the dialog does"
    assert len(month["rows"]) == 2, month["rows"]


def test_the_sheet_says_whose_it_is_once_and_the_identity_is_not_a_column(results):
    """Whose month it is belongs in the header, not repeated down every row.

    The employee, the role and the id are columns of the *tab* - three of its nine - and on a
    sheet about one person they would carry the same three facts on every line. They are said
    once, above the table, and the columns that remain keep the administrator's own order.
    """
    month = results["month"]
    assert month["meta"][0] == "Seed Lead · Moallem · id 600", month["meta"]
    assert month["sheet"].count("Seed Lead") == 1, "the name is on the sheet once"
    assert month["headers"] == [
        "Date", "Site", "Arrival", "Hours", "Awaiting approval", "Open notes"
    ], "the tab's own columns, without the three that say whose rows these are"
    assert "2026-08-01" in month["meta"][1] and "2026-08-31" in month["meta"][1], (
        f"the period line has to name the month that was requested: {month['meta'][1]!r}"
    )
    assert "data-print-worker" not in month["sheet"], "a sheet must carry no controls"


def test_the_sheet_totals_that_worker_s_month_and_keeps_the_waiting_hours_out(results):
    """The figures are the timesheet's own, read the way this app reads every timesheet.

    Two days, 15.5 h on the clock, 8 h signed off and 7.5 h still waiting for a decision: the
    sheet has to show both figures, or a manager reads the period as settled.
    """
    totals = results["month"]["totals"]
    assert "Total: 15.5 h" in totals, totals
    assert "Approved hours: 8 h" in totals, totals
    assert "Hours awaiting approval: 7.5 h" in totals, totals
    assert "2 Shifts worked" in totals, totals
    assert "0.5 h Unpaid break" in totals, totals
    # The fifth column of the sheet's own six: Date, Site, Arrival, Hours, Awaiting, Notes.
    awaiting = [row for row in results["month"]["rows"] if row[4] == "Awaiting approval"]
    assert len(awaiting) == 1, f"the one undecided shift has to say so: {results['month']['rows']}"


def test_the_dialog_offers_a_name_that_says_who_and_which_month(results):
    """"Save as PDF" offers the document title, and a folder may hold two of these."""
    month = results["month"]
    assert month["title"] == "timesheet_seed-lead_2026-08-01_2026-08-31", month["title"]


def test_the_page_comes_back_when_the_dialog_closes(results):
    """A print that left the console muted would leave the next one printing a stale sheet."""
    after = results["month"]["after"]
    assert after["printing"] is False, "the body still claims a report is being printed"
    assert after["title"] == "Al-Jehad - Site Attendance", "the tab's own title comes back"
    assert after["sheets_left"] == 0, "the sheet is left in the page"


def test_the_current_month_is_clipped_to_today(results):
    """A sheet may not describe work that has not happened: the presets' own rule."""
    current = results["current_month"]
    request = current["request"]
    assert request is not None
    assert request["end"] == current["today"], (
        f"the month is still running, so it ends today - not on {request['end']}"
    )
    assert request["start"] == f"{current['today'][:7]}-01", request["start"]
    assert current["printed"] == 1, "the worker whose shift is today still gets a sheet"
    assert len(current["rows"]) == 1 and current["rows"][0][0] == current["today"]


def test_a_month_means_a_calendar_month_and_only_the_running_one_is_clipped(results):
    """Read off the period on screen rather than the clock, and bounded on both ends."""
    rule = results["month_rule"]
    assert rule["inside_a_month"] == {"start": "2026-08-01", "end": "2026-08-31"}, rule
    assert rule["whole_past_month"] == {"start": "2026-02-01", "end": "2026-02-28"}, (
        "February 2026 is not a leap month, and a past month keeps its last day"
    )
    assert rule["future_month"] == {"start": "2030-05-01", "end": "2030-05-31"}, (
        "a month that has not started is a month, not a clip to today"
    )
    assert rule["this_month"]["start"].endswith("-01"), rule
    assert rule["this_month"]["end"] == results["current_month"]["today"], rule
    assert rule["junk"] == {"start": "not-a-date", "end": "nor-this"}, (
        "a period that is not a month is left as it is rather than invented"
    )


def test_a_month_nobody_worked_is_refused_in_words(results):
    """A titled sheet with no rows reads as a lost timesheet, not as somebody who did not work."""
    nobody = results["nobody"]
    assert nobody["request"]["worker"] == "999", "the request still has to be made"
    assert nobody["printed"] == 0, "nothing may be printed for an empty month"
    assert nobody["toasts"] == [
        "There is nothing to print: that worker worked nothing in this month."
    ], nobody["toasts"]
    assert nobody["printing"] is False and nobody["sheets_left"] == 0


def test_a_failed_report_prints_nothing_and_leaves_the_console_as_it_found_it(results):
    """The one path where an error could arrive with the page already muted."""
    failed = results["failed"]
    assert failed["printed"] == 0
    assert failed["toasts"] == ["Error: the report is down"], failed["toasts"]
    assert failed["printing"] is False, "the body was left out of the page's own print rules"
    assert failed["sheets_left"] == 0


def test_the_click_that_starts_it_is_the_row_action_and_nothing_else(results):
    """Delegated, so the handler has to ignore a click that is not on a row's control."""
    click = results["click"]
    assert click["nothing_printed"] is True and click["no_request"] is True, (
        "a click on the tab's own furniture printed something"
    )
    assert click["printed"] == 1, "a click on a row action did not print"
    assert click["worker"] == "600", "and it printed the wrong worker"


def test_a_worker_whose_account_is_gone_keeps_their_name_and_loses_the_gap(results):
    """The id is still the id. A header line with a double separator reads as broken data."""
    gone = results["gone"]
    assert gone["meta"][0] == "Noor Haddad · id 603", gone["meta"]
    assert "· ·" not in gone["meta"][0]
    assert len(gone["rows"]) == 1, "the shift of a deleted login is still a shift"
    assert gone["rows"][0][1] == "—", "and its missing site is a dash, not a blank"


def test_a_hostile_worker_id_is_an_attribute_value_and_a_query_parameter_only(results):
    """The id travels into the row's control and the request; neither is a place it is markup."""
    escaped = results["escaped"]
    assert escaped["attribute"] == ["600&quot; onmouseover=&quot;alert(1)"], escaped["attribute"]
    assert escaped["raw_in_page"] is False, "the id became markup on the table"
    assert escaped["request"]["worker"] == '600" onmouseover="alert(1)', (
        "and it is what was asked for, unescaped, on the wire"
    )
    assert escaped["raw_in_sheet"] is False, "the id became markup on the sheet"


def test_the_sheet_is_written_in_the_reader_s_language(results):
    """A sheet read in Arabic has to be Arabic - the worker's own sheet makes the same point."""
    translated = results["translated"]
    assert translated["title"] == translated["expected_title"], translated
    assert translated["title"] != translated["english"]["title"], "the sheet is still English"
    assert translated["headers"] == translated["expected_headers"], translated["headers"]
    assert translated["headers"] != translated["english"]["labels"]
    assert translated["role_word"] in translated["meta"][0], (
        f"the role is on the header line in the reader's words: {translated['meta']}"
    )


def test_the_phone_cards_carry_the_same_control_as_the_table(results):
    """The action is on the row on both layouts - one row, one way to print it."""
    mobile = results["mobile"]
    assert mobile["card_buttons"] == mobile["table_buttons"], mobile
    assert mobile["card_buttons"] == mobile["cards"], "one control per shift card"


def test_a_report_with_no_rows_says_so_instead_of_printing_an_empty_table(results):
    """The builder is total, even though the button refuses this case before it can happen."""
    built = results["empty_builder"]
    assert built["has_empty_note"] is True
    assert built["has_table"] is False, "a table with no rows is not a report"
    assert built["sentence"] in built["sheet"]


def test_the_file_name_of_one_worker_s_month_says_who_it_is_about(results):
    """Two of these in a folder must not be told apart by opening them."""
    names = results["names"]
    assert names["month"] == "timesheet_seed-lead_2026-08-01_2026-08-31", names
    assert names["awkward"] == "timesheet_o-brien-lead_2026-08-01_2026-08-31", (
        "a name with punctuation in it is still a file name"
    )
    assert names["period"] == "shifts_2026-08-01_2026-08-31_harbour", (
        "the period sheet's own name is unchanged by any of this"
    )
