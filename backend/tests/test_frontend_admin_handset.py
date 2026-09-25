"""An administrator who works a site as well: the clock, and their own hours.

WHAT THIS GUARDS
----------------
``/attendance/verify`` and the ``/worker/me/*`` reads have always taken any authenticated
role, so a normal administrator could already clock in and read their own totals. The
console never offered the screens. ``renderApp`` sent exactly one role to the handset:

    if (State.user.role === 'worker') return this.renderWorkerPortal();

and everybody else - ``admin`` included - to the console, where there is no punch card, no
camera, no geofence check and no history. The capability existed and was unreachable, which
for the person holding the phone is the same as it not existing.

What can only be checked here, and not in a backend test:

1. the console offers the way in, and only to the roles that have one - an administrator
   does, a worker already lives on the handset, and a head administrator is not offered a
   punch card, because that role owns the deployment rather than a rota;
2. taking it lands on the *real* handset - the same punch card a worker gets, reading the
   worker's own self-scoped endpoints - and not on a second, lighter screen;
3. there is a way back, and a worker never sees one, because a worker has no console;
4. the history screen leads with the reader's own timesheet - totals, and the per-site
   breakdown that answers "how much did I work *where*" - and the download is built from
   the rows on screen, so the file cannot disagree with the figures beside it;
5. a site name is still escaped on the way into that summary, because it is a value off
   the wire like every other one;
6. the *shape* of both files - which columns the CSV and the PDF carry - is the worker's
   own, chosen here, and kept on the account rather than in the browser, so the next month
   and the next phone draw the same file. The vocabulary is compared against
   ``reports.REPORT_COLUMNS`` below, in order both ways: a column the server will store and
   this module cannot fill would be a column of empty cells in somebody's evidence.

Node is optional; without it these skip rather than fail.
"""

from __future__ import annotations

import pytest
import reports

import frontend_vm

pytestmark = pytest.mark.skipif(frontend_vm.NODE is None, reason="node is not installed")

HARNESS = r"""
// --- the server's answers, faked ----------------------------------------

const ADMIN = { id: '1000', name: 'Site Admin', role: 'admin' };
const WORKER = { id: '1', name: 'Seed Worker', role: 'worker' };
const HEAD = { id: '5000', name: 'Head Admin', role: 'head_admin' };
const MOALLEM = { id: '600', name: 'Seed Lead Worker', role: 'moallem' };

// One approved shift and one still waiting on somebody: the split the whole overtime gate
// exists for, and the split this screen must not blur.
const REPORT = {
    worker_id: '1000',
    worker_name: 'Site Admin',
    period: { start: '2026-09-01', end: '2026-09-19' },
    rows: [
        {
            log_id: 2, date: '2026-09-12', site_name: 'New Capital Zone B',
            hours: 9.5, recorded_hours: 10.0, approved_hours: null, awaiting_approval: true,
            status: 'Pending Overtime Approval',
            arrival_time: '07:12', arrival_verdict: 'late', arrival_minutes: 12,
            break_hours: 0.5
        },
        {
            log_id: 1, date: '2026-09-10', site_name: 'Downtown Tower A',
            hours: 8.0, recorded_hours: 8.0, approved_hours: 8.0, awaiting_approval: false,
            status: 'Approved by Admin',
            arrival_time: '04:55', arrival_verdict: 'on_time', arrival_minutes: 0,
            break_hours: 0.0
        }
    ],
    by_site: [
        {
            site_name: 'New Capital Zone B', shifts: 1, hours: 9.5,
            approved_hours: 0.0, awaiting_approval_hours: 9.5
        },
        {
            site_name: 'Downtown Tower A', shifts: 1, hours: 8.0,
            approved_hours: 8.0, awaiting_approval_hours: 0.0
        }
    ],
    totals: {
        shifts: 2, sites: 2, hours: 17.5, approved_hours: 8.0, awaiting_approval_hours: 9.5,
        awaiting_approval: 1, break_hours: 0.5, late_arrivals: 0, overtime_hours: 1.5
    },
    //: The columns this account's own two files carry, as the server sends them: the shape
    //: travels *with* the rows it describes, because both files are built from this payload.
    columns: ['date', 'site', 'arrival', 'break', 'hours', 'approved', 'status']
};

const LOG_ROWS = [
    { id: 2, action: 'Clock Out', timestamp: '2026-09-12 18:00:00', site: 'New Capital Zone B', hours: 9.5, status: 'Pending Overtime Approval' },
    { id: 1, action: 'Clock In', timestamp: '2026-09-12 07:00:00', site: 'New Capital Zone B', hours: 0, status: 'Approved by Admin' }
];

//: Swapped by the hostile-value scenario; the refresh rebuilds the summary from it.
let reportBody = REPORT;

//: What the column endpoint answers, and what it was asked. ``savedColumns`` overrides the
//: echo, which is how "the server dropped a column this build cannot fill" is driven at the
//: client; the real normalisation is the backend suite's business, not this one's.
const columnSaves = [];
let savedColumns = null;
let columnsFail = false;

//: A report of this scenario's own, with the columns the server has stored for the account.
//:
//: Every environment is handed the object the responder returns, and a page that saves a
//: column choice patches that object with the answer (the screen keeps the server's list,
//: not the one it sent) - so one scenario's save would otherwise rewrite the next
//: scenario's account. The rows are read-only, so a shallow copy is enough.
function freshReport(columns) {
    return Object.assign({}, REPORT, columns ? { columns: columns } : {});
}

const HOSTILE = '<img src=x onerror=alert(1)>';

function hostileReport() {
    return {
        worker_id: '1000',
        worker_name: 'Site Admin',
        period: { start: '2026-09-01', end: '2026-09-19' },
        rows: [],
        by_site: [{ site_name: HOSTILE, shifts: 1, hours: 1.0, approved_hours: 1.0, awaiting_approval_hours: 0.0 }],
        totals: { shifts: 1, sites: 1, hours: 1.0, approved_hours: 1.0, awaiting_approval_hours: 0.0, awaiting_approval: 0, break_hours: 0, late_arrivals: 0, overtime_hours: 0 }
    };
}

function responders(url, init) {
    // Before the report itself, because the path is a report path with a tail.
    if (url.includes('/worker/me/report/columns')) {
        const body = JSON.parse((init && init.body) || '{}');
        columnSaves.push(body);
        if (columnsFail) {
            return { status: 400, body: { detail: 'A timesheet needs at least one column.' } };
        }
        return {
            status: 200,
            body: { status: 'success', columns: savedColumns || body.columns }
        };
    }
    if (url.includes('/worker/me/report')) return { status: 200, body: reportBody };
    if (url.includes('/worker/me/logs')) return { status: 200, body: LOG_ROWS };
    if (url.includes('/worker/me/stats')) {
        return {
            status: 200,
            body: {
                worker_id: '1000', total_hours: 0, overtime_notify_hours: 8.1,
                active_session: null, flagged_for_review: false
            }
        };
    }
    if (url.includes('/worker/notes')) return { status: 200, body: { notes: [], open: 0 } };
    // The console's own board, so an administrator's first paint is not an error screen.
    if (url.includes('/developer/notifications')) return { status: 200, body: { unread: 0, notifications: [] } };
    if (url.includes('/admin/')) return { status: 200, body: [] };
    return { status: 200, body: [] };
}

async function bootAs(user) {
    const env = boot();
    env.setResponder(responders);
    env.evaluate('State.saveUser(' + JSON.stringify({ ...user, token: 'tok-test' }) + ')');
    await env.evaluate('UI.renderApp()');
    return env;
}

const screen = (env) => env.evaluate("document.getElementById('app').className");

const results = {};

// 1. who is offered the clock, and who is not
{
    const admin = await bootAs(ADMIN);
    const worker = await bootAs(WORKER);
    const head = await bootAs(HEAD);
    results.offered = {
        admin: admin.evaluate('UI.canOpenHandset()'),
        worker: worker.evaluate('UI.canOpenHandset()'),
        head: head.evaluate('UI.canOpenHandset()'),
        // A data hook, not an inline handler: the CSP's inline-attribute allowance is
        // pinned per file and may not rise, so the two new controls are bound by
        // delegation. ``test_frontend_xss.py`` is what enforces that.
        admin_button: admin.evaluate('UI.handsetButtonHtml()').indexOf('data-open-handset') >= 0,
        head_button: head.evaluate('UI.handsetButtonHtml()'),
        worker_button: worker.evaluate('UI.handsetButtonHtml()')
    };
}

// 2. the console shows the button on screen, in the reader's own language
{
    const env = await bootAs(ADMIN);
    const markup = env.evaluate("document.getElementById('app').innerHTML");
    results.console = {
        screen: screen(env),
        has_button: markup.indexOf('data-open-handset') >= 0,
        // The label is a translation, not a hardcoded word: the console ships in three
        // languages and an English "Check In" on an Arabic screen is a bug with a keyboard.
        label: env.evaluate("I18n.__('clockIn')"),
        labelled: markup.indexOf(env.evaluate("UI.escapeHtml(I18n.__('clockIn'))")) >= 0
    };
}

// 3. taking the way in lands on the real handset, with a way back
{
    const env = await bootAs(ADMIN);
    const before = screen(env);
    await env.evaluate('UI.openHandset()');
    const handsetMarkup = env.evaluate("document.getElementById('app').innerHTML");
    results.open = {
        before,
        after: screen(env),
        // The worker's own panel is what rendered, and it reads the worker's own endpoints -
        // which is why an administrator needs no second implementation of the clock.
        asked_for: env.requests.map((r) => r.url.replace(/^https?:\/\/[^/]*\/api\/v1/, '')).sort(),
        has_clock_card: handsetMarkup.indexOf('workerDashboard') >= 0,
        // Not mislabelled as a worker: the header says which role is holding the phone.
        shown_role: handsetMarkup.indexOf(env.evaluate("I18n.__('roleAdmin')")) >= 0,
        back_button: env.evaluate('UI.closeHandsetButtonHtml()').indexOf('data-close-handset') >= 0
    };

    // ... and back again
    await env.evaluate('UI.closeHandset()');
    results.open.returned = screen(env);
}

// 4. a worker is never offered the console, and never promised one
{
    const env = await bootAs(WORKER);
    env.evaluate('UI.openHandset()');
    results.worker = {
        screen: screen(env),
        back_button: env.evaluate('UI.closeHandsetButtonHtml()'),
        handset_mode: env.evaluate('State.handsetMode')
    };
}

// 5. a head administrator is not put on the clock
{
    const env = await bootAs(HEAD);
    await env.evaluate('UI.openHandset()');
    results.head = {
        screen: screen(env),
        handset_mode: env.evaluate('State.handsetMode')
    };
}

// 6. my own timesheet, with the totals and the per-site split
{
    const env = await bootAs(ADMIN);
    await env.evaluate("WORKER_MODULES.renderHistory(document.getElementById('historyTable'))");
    const markup = env.evaluate("document.getElementById('historyTable').innerHTML");
    const escaped = (key) => env.evaluate('UI.escapeHtml(I18n.__(' + JSON.stringify(key) + '))');
    results.history = {
        asked_for: env.requests.map((r) => r.url.replace(/^https?:\/\/[^/]*\/api\/v1/, '')),
        heading: markup.indexOf(escaped('myHours')) >= 0,
        // 17.50 total, 8.00 signed off, 9.50 still waiting: three figures, all shown, and
        // the unapproved one never folded into the approved one.
        totals: ['17.50', '8.00', '9.50'].every((figure) => markup.indexOf(figure) >= 0),
        awaiting_named: markup.indexOf(escaped('myHoursAwaiting')) >= 0,
        // "Where": both sites, by name.
        sites: ['Downtown Tower A', 'New Capital Zone B'].every((name) => markup.indexOf(name) >= 0),
        // The timeline the History tab has always shown is still there. Its action is a
        // translation, not the wire value: "Clock Out" is what the server stores, and the
        // row is read by a person.
        timeline: markup.indexOf(escaped('clockOut')) >= 0,
        has_download: markup.indexOf('data-download-hours') >= 0
    };

    // 7. the download is the rows on screen, and cannot disagree with them
    await env.evaluate('WORKER_MODULES.downloadMyHoursCsv()');
    results.download = {
        text: await env.lastBlobText(),
        filename: env.lastAnchor().download
    };

    // 7b. the PDF half: the browser's print dialog, and the same rows on the paper
    const beforePrint = { files: env.blobs.length, requests: env.requests.length };
    env.evaluate('WORKER_MODULES.printMyHours()');
    const printed = env.printed[env.printed.length - 1] || {};
    results.print = {
        printed: env.printed.length,
        title: printed.title || null,
        printing: printed.printing === true,
        sheet: printed.sheet || '',
        files_written: env.blobs.length - beforePrint.files,
        requests_added: env.requests.length - beforePrint.requests,
        period: REPORT.period
    };
    // The dialog closes.
    env.fireWindowEvent('afterprint');
    results.print.after = {
        title: env.evaluate('document.title'),
        printing: env.evaluate("document.body.classList.contains('is-printing-report')"),
        sheets_left: env.evaluate(
            "document.body.__children.filter((c) => c.className === 'print-sheet').length")
    };

    // 7c. a period with nothing in it prints nothing: a blank sheet is not a report
    const empty = await bootAs(WORKER);
    empty.evaluate('WORKER_MODULES._myHoursReport = { period: {}, totals: {}, rows: [] }');
    empty.evaluate('WORKER_MODULES.printMyHours()');
    empty.evaluate('WORKER_MODULES.downloadMyHoursCsv()');
    results.print.nothing = {
        printed: empty.printed.length,
        files: empty.blobs.length
    };

    // 7e. a lead worker reads their own hours like a worker, and never the console
    const lead = await bootAs(MOALLEM);
    await lead.evaluate('UI.openHandset()');
    await lead.evaluate("WORKER_MODULES.renderHistory(document.getElementById('historyTable'))");
    results.lead = {
        screen: screen(lead),
        // The console's content host, which every ``/admin/*`` request that fills it would
        // answer 403 to for this role: it must not be on the screen they were sent to. Read
        // off the markup rather than the DOM - this harness creates an element for any id
        // it is asked for, so ``getElementById`` can never answer "no such thing".
        console_markup: lead.evaluate("document.getElementById('app').innerHTML")
            .indexOf('adminContent') >= 0,
        back_button: lead.evaluate('UI.closeHandsetButtonHtml()'),
        role_shown: lead.evaluate("document.getElementById('app').innerHTML")
            .indexOf(lead.evaluate("UI.escapeHtml(I18n.__('roleMoallem'))")) >= 0,
        has_both_files: lead.evaluate("document.getElementById('historyTable').innerHTML")
            .indexOf('data-download-hours="pdf"') >= 0
    };

    // 7f. an administrator's own month, printed from the screen they reach by stepping onto
    // the handset. Every print above is a worker's; this is the one that says the role the
    // clock was added for reads and prints its *own* report rather than a roster.
    const admin = await bootAs(ADMIN);
    await admin.evaluate('UI.openHandset()');
    await admin.evaluate("WORKER_MODULES.renderHistory(document.getElementById('historyTable'))");
    const beforeAdminPrint = { files: admin.blobs.length, requests: admin.requests.length };
    admin.evaluate('WORKER_MODULES.printMyHours()');
    const adminPrinted = admin.printed[admin.printed.length - 1] || {};
    results.adminPrint = {
        screen: screen(admin),
        offered: admin.evaluate("document.getElementById('historyTable').innerHTML")
            .indexOf('data-download-hours="pdf"') >= 0,
        printed: admin.printed.length,
        title: adminPrinted.title || null,
        printing: adminPrinted.printing === true,
        sheet: adminPrinted.sheet || '',
        files_written: admin.blobs.length - beforeAdminPrint.files,
        requests_added: admin.requests.length - beforeAdminPrint.requests,
        // Self-scoped: the request names no worker at all, so there is no id on it to point
        // at somebody else's month and the sheet is whoever the token belongs to.
        report_urls: admin.requests
            .map((r) => r.url.replace(/^https?:\/\/[^/]*\/api\/v1/, ''))
            .filter((url) => url.indexOf('/worker/me/report') >= 0)
    };
    admin.fireWindowEvent('afterprint');
    results.adminPrint.after = {
        printing: admin.evaluate("document.body.classList.contains('is-printing-report')"),
        sheets_left: admin.evaluate(
            "document.body.__children.filter((c) => c.className === 'print-sheet').length")
    };

    // 7d. the month picker: the payroll window, and the period it asks the server for
    const picker = await bootAs(WORKER);
    await picker.evaluate("WORKER_MODULES.renderHistory(document.getElementById('historyTable'))");
    const pickerMarkup = picker.evaluate("document.getElementById('historyTable').innerHTML");
    results.picker = {
        markup_has_picker: pickerMarkup.indexOf('id="myHoursMonth"') >= 0,
        markup_has_both_files: pickerMarkup.indexOf('data-download-hours="csv"') >= 0
            && pickerMarkup.indexOf('data-download-hours="pdf"') >= 0,
        // The window and its names. Read from the methods rather than the DOM: this harness
        // has no HTML parser, so the markup is a string and the select does not exist as an
        // element to interrogate - the same reason the other suites assert on markup text.
        options: picker.evaluate("WORKER_MODULES.myHoursMonthOptions('2026-09')"),
        // The period the control produces, including the two ends of a short February.
        query_for_september: picker.evaluate(
            "(function () { WORKER_MODULES._myHoursMonth = '2026-09'; return WORKER_MODULES.myHoursRangeQuery(); })()"
        ),
        query_for_february: picker.evaluate(
            "(function () { WORKER_MODULES._myHoursMonth = '2028-02'; return WORKER_MODULES.myHoursRangeQuery(); })()"
        ),
        // Junk in the field is the server's own default rather than a request it would 400.
        query_for_nonsense: picker.evaluate(
            "(function () { WORKER_MODULES._myHoursMonth = 'march'; return WORKER_MODULES.myHoursRangeQuery(); })()"
        )
    };
}

// 8. a site name is a value off the wire, and is escaped on the way in
{
    reportBody = hostileReport();
    const env = await bootAs(ADMIN);
    await env.evaluate("WORKER_MODULES.renderHistory(document.getElementById('historyTable'))");
    const markup = env.evaluate("document.getElementById('historyTable').innerHTML");
    results.hostile = {
        raw_tag: markup.indexOf('<img src=x') >= 0,
        escaped: markup.indexOf(env.evaluate('UI.escapeHtml(' + JSON.stringify(HOSTILE) + ')')) >= 0
    };
}

// 10. the columns my own two files carry, and the vocabulary the server shares
{
    // The value scenarios above left in place (the hostile one, most recently): this block
    // is about the account's choice, so it starts from the report as the server sends it.
    reportBody = freshReport();
    const env = await bootAs(WORKER);
    await env.evaluate("WORKER_MODULES.renderHistory(document.getElementById('historyTable'))");
    const markup = env.evaluate("document.getElementById('historyTable').innerHTML");
    const chosen = "WORKER_MODULES.myHoursColumns(WORKER_MODULES._myHoursReport).join(',')";
    results.chooser = {
        // The vocabulary both files are written from, in the order they are written. The
        // test below compares it against the backend's own list, in order and both ways: a
        // column the server will store and this file cannot fill is a column of empty cells
        // in somebody's evidence.
        vocabulary: env.evaluate('WORKER_MODULES.myHoursColumnDefs().map((def) => def.id)'),
        csv_labels: env.evaluate('WORKER_MODULES.myHoursColumnDefs().map((def) => def.csv)'),
        sheet_labels: env.evaluate(
            'WORKER_MODULES.myHoursColumnDefs().map((def) => I18n.__(def.key))'),
        defaults: env.evaluate('WORKER_MODULES.myHoursDefaultColumns()'),
        // What the account has stored arrives *with* the report, so the two files this
        // screen builds cannot be drawn from a column list that arrives after its rows.
        chosen: env.evaluate(chosen),
        markup_has_chooser: markup.indexOf('data-report-columns') >= 0,
        markup_has_save: markup.indexOf('data-save-columns') >= 0,
        boxes: (markup.match(/data-report-column=/g) || []).length,
        ticked: (markup.match(/data-report-column="[a-z]+" checked/g) || []).length,
        // Bound by delegation like every other control on this card, because the CSP's
        // inline allowance is pinned per file and may fall but never rise.
        inline_handlers: (markup.match(/on(click|change)=/g) || []).length
    };

    // 10b. a tick changes the very next file, before anything is saved: the promise of this
    // screen is that the download cannot disagree with the rows it was taken from
    reportBody = freshReport();
    const narrowed = await bootAs(WORKER);
    await narrowed.evaluate("WORKER_MODULES.renderHistory(document.getElementById('historyTable'))");
    narrowed.evaluate("WORKER_MODULES.toggleMyHoursColumn('break', false)");
    narrowed.evaluate("WORKER_MODULES.toggleMyHoursColumn('recorded', true)");
    await narrowed.evaluate('WORKER_MODULES.downloadMyHoursCsv()');
    const narrowedText = await narrowed.lastBlobText();
    narrowed.evaluate('WORKER_MODULES.printMyHours()');
    const narrowedSheet = (narrowed.printed[narrowed.printed.length - 1] || {}).sheet || '';
    narrowed.fireWindowEvent('afterprint');
    const narrowedLines = narrowedText.split('\r\n');
    results.narrowed = {
        header: narrowedLines[0],
        first_row: narrowedLines[1],
        sheet: narrowedSheet,
        // The sheet is the translated half of the same choice: the column that was ticked is
        // headed in the reader's language, and what was unticked is gone from both files.
        sheet_header: narrowedSheet.indexOf(
            env.evaluate("UI.escapeHtml(I18n.__('myHoursColRecorded'))")) >= 0,
        sheet_break_gone: narrowedSheet.indexOf(
            env.evaluate("UI.escapeHtml(I18n.__('myHoursColBreak'))")) < 0,
        csv_break_gone: narrowedText.indexOf('Break hours') < 0
    };

    // 10c. the last column cannot be unticked: a file with no columns is not a report, and a
    // checkbox that springs back unexplained is worse than one that says why
    reportBody = freshReport();
    const emptied = await bootAs(WORKER);
    await emptied.evaluate("WORKER_MODULES.renderHistory(document.getElementById('historyTable'))");
    // Everything but one, then the last one as well. Read off the module's own default list
    // rather than written out here, so this scenario follows the vocabulary.
    emptied.evaluate(`(function () {
        WORKER_MODULES.myHoursDefaultColumns().slice(1).forEach(
            (id) => WORKER_MODULES.toggleMyHoursColumn(id, false));
        return null;
    })()`);
    const downTo = emptied.evaluate(chosen);
    emptied.evaluate("WORKER_MODULES.toggleMyHoursColumn('date', false)");
    results.floor = {
        down_to: downTo,
        kept: emptied.evaluate(chosen),
        toasts: emptied.evaluate(
            "document.getElementById('toastRoot').__children.map((el) => el.textContent)"),
        posted: columnSaves.length
    };

    // 10d. saving: what is posted, what is kept, and no repaint under the finger
    reportBody = freshReport();
    const saver = await bootAs(WORKER);
    await saver.evaluate("WORKER_MODULES.renderHistory(document.getElementById('historyTable'))");
    saver.evaluate("WORKER_MODULES.toggleMyHoursColumn('status', false)");
    const beforeSave = saver.requests.length;
    columnSaves.length = 0;
    await saver.evaluate('WORKER_MODULES.saveMyHoursColumns()');
    results.save = {
        posted: columnSaves.map((call) => call.columns),
        urls: saver.requests
            .map((r) => r.url.split('/api/v1')[1])
            .filter((url) => url.indexOf('columns') >= 0),
        methods: saver.requests
            .filter((r) => r.url.indexOf('columns') >= 0).map((r) => r.method),
        stored: saver.evaluate("WORKER_MODULES._myHoursReport.columns.join(',')"),
        draft_cleared: saver.evaluate('WORKER_MODULES._myHoursColumnDraft === null'),
        toast: saver.evaluate(
            "document.getElementById('toastRoot').__children.map((el) => el.textContent)"),
        // One request for the save and nothing else: a repaint would close the panel under
        // the finger that just pressed the button, and the boxes already say what the
        // answer says.
        requests_added: saver.requests.length - beforeSave
    };

    // 10e. the server's answer is what is kept, not the list that was sent
    reportBody = freshReport();
    const dropped = await bootAs(WORKER);
    await dropped.evaluate("WORKER_MODULES.renderHistory(document.getElementById('historyTable'))");
    dropped.evaluate("WORKER_MODULES.toggleMyHoursColumn('recorded', true)");
    savedColumns = ['date', 'hours'];
    await dropped.evaluate('WORKER_MODULES.saveMyHoursColumns()');
    savedColumns = null;
    results.answer = {
        chosen: dropped.evaluate(chosen),
        stored: dropped.evaluate("WORKER_MODULES._myHoursReport.columns.join(',')")
    };

    // 10f. a refusal: the choice stays in force for this session, and the screen says why
    reportBody = freshReport();
    const refused = await bootAs(WORKER);
    await refused.evaluate("WORKER_MODULES.renderHistory(document.getElementById('historyTable'))");
    refused.evaluate("WORKER_MODULES.toggleMyHoursColumn('site', false)");
    columnsFail = true;
    await refused.evaluate('WORKER_MODULES.saveMyHoursColumns()');
    columnsFail = false;
    results.refusedSave = {
        still_in_force: refused.evaluate(chosen),
        draft_kept: refused.evaluate('Array.isArray(WORKER_MODULES._myHoursColumnDraft)'),
        toast: refused.evaluate(
            "document.getElementById('toastRoot').__children.map((el) => el.textContent)")
    };

    // 10g. a stored choice reaches a screen that has never seen it, because it is on the
    // account rather than in the browser that made it
    reportBody = freshReport(['date', 'site', 'recorded']);
    const stored = await bootAs(ADMIN);
    await stored.evaluate('UI.openHandset()');
    await stored.evaluate("WORKER_MODULES.renderHistory(document.getElementById('historyTable'))");
    results.storedChoice = {
        chosen: stored.evaluate(chosen),
        screen: screen(stored)
    };
    reportBody = REPORT;
}

// 9. every string this feature added exists in all three tables
{
    const env = await bootAs(ADMIN);
    const KEYS = [
        'handsetHint', 'backToConsole', 'myHours', 'myHoursDownload', 'myHoursWorked',
        'myHoursApproved', 'myHoursAwaiting', 'myHoursOvertime', 'myHoursSites',
        'myHoursWhere', 'myHoursNothingToExport', 'myHoursExportPdf', 'myHoursColBreak',
        'myHoursMonth', 'myHoursColRecorded', 'myHoursColumns', 'myHoursColumnsHint',
        'myHoursColumnsSave', 'myHoursColumnsSaved', 'myHoursColumnsEmpty'
    ];
    // Evaluated inside the page's own scope: ``TRANSLATIONS`` is the app's global, not
    // this harness script's, and a suite that defined its own copy would be checking the
    // wrong table.
    results.translations = env.evaluate(`(function () {
        const KEYS = ${JSON.stringify(KEYS)};
        return {
            present_in_english: KEYS.filter((key) => !(key in TRANSLATIONS.en)),
            missing: Object.keys(TRANSLATIONS).map((lang) => [
                lang,
                KEYS.filter((key) => !(key in TRANSLATIONS[lang])).join(',')
            ])
        };
    })()`);
}
"""


@pytest.fixture(scope="module")
def results():
    return frontend_vm.run(HARNESS)


def test_only_a_normal_admin_is_offered_the_clock(results):
    """The console button exists for an ``admin`` and for nobody else."""
    offered = results["offered"]
    assert offered["admin"] is True, "an administrator who works a site has nowhere to clock in"
    assert offered["admin_button"] is True, "and the console must actually offer it"
    assert offered["worker"] is False, "a worker is already on the handset"
    assert offered["worker_button"] == "", "a worker has no console to leave from"
    assert offered["head"] is False, "the deployment-owning role is not a rota"
    assert offered["head_button"] == "", "so it gets no punch card"


def test_the_console_shows_the_way_in(results):
    console = results["console"]
    assert console["screen"] == "admin", "an administrator lands on the console, as before"
    assert console["has_button"] is True
    assert console["labelled"] is True, "the button must be translated, not hardcoded English"
    assert console["label"], "there is a translation to show"


def test_opening_it_lands_on_the_real_handset(results):
    opened = results["open"]
    assert opened["before"] == "admin"
    assert opened["after"] == "hand-app", "the administrator reaches the punch screen itself"
    assert opened["has_clock_card"] is True, "not a summary of one"
    assert any("/worker/me/stats" in url for url in opened["asked_for"]), opened["asked_for"]
    assert opened["shown_role"] is True, "the header says which role is holding the phone"
    assert opened["back_button"] is True, "there is a way back to the console"
    assert opened["returned"] == "admin", "and taking it returns to the console"


def test_a_worker_is_not_promised_a_console(results):
    worker = results["worker"]
    assert worker["screen"] == "hand-app", "a worker still lands on the handset"
    assert worker["back_button"] == "", "with no console to go back to"
    assert worker["handset_mode"] is False, "and cannot switch itself onto a console screen"


def test_a_head_administrator_is_not_put_on_the_clock(results):
    """The role the request explicitly excluded stays off the punch screen."""
    head = results["head"]
    assert head["screen"] == "admin"
    assert head["handset_mode"] is False, "openHandset() must refuse a role without one"


def test_my_hours_leads_with_the_totals_and_where_they_were_worked(results):
    history = results["history"]
    assert any("/worker/me/report" in url for url in history["asked_for"]), history["asked_for"]
    assert history["heading"] is True
    assert history["totals"] is True, "hours, approved and awaiting must all be on screen"
    assert history["awaiting_named"] is True, "and the unapproved figure must be named"
    assert history["sites"] is True, "how much I worked *where* is the point of the screen"
    assert history["timeline"] is True, "the punch list is still there"
    assert history["has_download"] is True


def test_the_download_is_the_report_on_screen(results):
    """A file built from the rows it was read from cannot contradict them.

    "How much did I work" is the figure, and "what did I do" is the rest of the row: which
    day, which site, what time the shift started, whether that counted as late, and how
    much of the day was the unpaid break. A file that carried only the total would answer
    half the question the worker opened it with.
    """
    download = results["download"]
    text = download["text"]
    assert text, "the button has to write a file"
    lines = text.splitlines()
    assert lines[0] == "Date,Site,Arrival,Break hours,Hours,Approved hours,Status"
    assert len(lines) == 3, f"two shifts, two rows: {lines}"
    # The unapproved overtime shift: the whole day, no approved figure, named as waiting.
    assert lines[1] == "2026-09-12,New Capital Zone B,07:12 late 12 min,0.50,9.50,,Awaiting approval", lines[1]
    assert lines[2] == "2026-09-10,Downtown Tower A,04:55 on time,0.00,8.00,8.00,Approved by Admin", lines[2]
    assert download["filename"] == "my_hours_2026-09-01_2026-09-19.csv", download["filename"]


def test_the_pdf_button_prints_the_same_rows_and_totals(results):
    """The paper half: the browser's dialog, the worker's own days, and the figures they
    add up to - then the page put back exactly as it was.

    The PDF is the print dialog's job here, as it is for the console: no PDF library is
    pinned and no Arabic-capable font ships with one, so what this pins is that the choice
    reaches the dialog with the report *on* the page, rather than writing a file itself.
    """
    printed = results["print"]
    assert printed["printed"] == 1, "the PDF button has to reach the browser's print dialog"
    assert printed["printing"] is True, "the page has to be out of the paper before it opens"
    assert printed["files_written"] == 0, "the page writes no PDF - the dialog does"
    assert printed["requests_added"] == 0, "the sheet is built from the rows already in hand"

    sheet = printed["sheet"]
    for expected in (
        "My hours",
        "Site Admin",
        "2026-09-12",
        "New Capital Zone B",
        "Late 12 min",
        "On time",
        "Break",
        "Worked 17.50 h",
        "Approved 8.00 h",
        "Awaiting approval 9.50 h",
        "Overtime 1.50 h",
        "Sites worked 2",
    ):
        assert expected in sheet, f"the sheet is missing {expected!r}: {sheet}"
    # The dialog names the file after the document title, and appends its own extension.
    assert printed["title"] == "my_hours_2026-09-01_2026-09-19", printed["title"]

    # And the page is handed back: no sheet left for the next print to read, no title
    # borrowed from the tab, no print styling left switched on.
    assert printed["after"]["title"] == "Al-Jehad - Site Attendance", printed["after"]
    assert printed["after"]["printing"] is False, printed["after"]
    assert printed["after"]["sheets_left"] == 0, printed["after"]


def test_a_period_with_nothing_in_it_offers_nothing_to_print(results):
    """A blank sheet with a title on it is not a report, and an empty file is not a lie -
    it is the header and no rows. Neither one is worth a print dialog."""
    nothing = results["print"]["nothing"]
    assert nothing["printed"] == 0, "there was nothing to print, so no dialog may open"
    assert nothing["files"] == 1, "the file is written; it is the paper that is skipped"


def test_an_administrator_prints_their_own_month_on_the_same_sheet(results):
    """The "or admin" half of the request: the same paper, with their own name on it.

    An administrator who works a site reads this screen by stepping onto the handset, and
    what is on it is their own report - not a roster, and not the console's Shifts tab
    filtered down to themselves. The sheet their PDF option prints is therefore the one the
    worker's option prints, from the same rows: same columns, same period, same figures.
    What this test adds is the audience - every print asserted above is a worker's - and
    the self-scoping, which is what makes it *their* month rather than anybody's.
    """
    printed = results["adminPrint"]
    assert printed["screen"] == "hand-app", "the administrator reads their own hours on the handset"
    assert printed["offered"] is True, "and is offered the PDF there like the worker is"
    assert printed["printed"] == 1, "the PDF button has to reach the browser's print dialog"
    assert printed["printing"] is True, "the page has to be out of the paper before it opens"
    assert printed["files_written"] == 0, "the page writes no PDF - the dialog does"
    assert printed["requests_added"] == 0, "the sheet is built from the rows already in hand"

    # Self-scoped: the token decides whose month this is, so the request carries no worker
    # id that could be pointed at somebody else.
    assert printed["report_urls"], printed["report_urls"]
    for url in printed["report_urls"]:
        assert url.startswith("/worker/me/report"), url
        assert "worker_id" not in url and "id=" not in url, url

    sheet = printed["sheet"]
    for expected in ("My hours", "Site Admin", "id 1000", "2026-09-12", "Downtown Tower A", "Worked 17.50 h"):
        assert expected in sheet, f"the sheet is missing {expected!r}: {sheet}"

    # The same document the worker's option produces - the period IS the file name, so the
    # two are the same download two people can each make.
    assert printed["title"] == results["print"]["title"], (
        f"{printed['title']!r} is not the sheet the worker prints"
    )

    # And the page is handed back, exactly as it is for a worker.
    assert printed["after"]["printing"] is False, printed["after"]
    assert printed["after"]["sheets_left"] == 0, printed["after"]


def test_a_lead_worker_reads_their_own_hours_and_never_the_console(results):
    """A ``moallem`` is a lead worker, not an operator, and the console is not their screen.

    ``admin_only`` names ``admin`` and ``head_admin``, so every request the console makes on
    behalf of a lead worker comes back 403: routing them to it left the role with a screen
    that could not load anything at all. The handset is the one they can use - they clock
    in, they read their own timesheet, and they take it away - and it says which role is
    holding the phone rather than calling a lead worker a worker.
    """
    lead = results["lead"]
    assert lead["screen"] == "hand-app", (
        f"a lead worker landed on {lead['screen']!r}, not the handset"
    )
    assert lead["console_markup"] is False, "the console is not a lead worker's screen"
    assert lead["back_button"] == "", "there is no console to go back to, so no button promising one"
    assert lead["role_shown"], "the header has to name the role the account actually has"
    assert lead["has_both_files"], "and My hours offers both files there too"


def test_the_month_picker_covers_the_payroll_window(results):
    """A timesheet is filed and paid by the month, and a month is what the control asks for.

    Twelve months, newest first, ending at the month the report is actually for - and the
    names come from the platform's calendar in the reader's language, so a worker who
    chose Urdu reads Urdu months without a table of forty-eight month names in this repo.
    """
    picker = results["picker"]
    assert picker["markup_has_picker"], "My hours has to offer the period, not only the file"
    assert picker["markup_has_both_files"], "and both files, csv and pdf"

    options = picker["options"]
    assert len(options) == 12, f"a year of months: {options}"
    assert options[0]["value"] == "2026-09" and options[0]["chosen"], options[0]
    assert options[-1]["value"] == "2025-10", options[-1]
    # Newest first and strictly descending, so the list reads the way a payslip archive does.
    values = [option["value"] for option in options]
    assert values == sorted(values, reverse=True) and len(set(values)) == 12, values
    # The name is a real month name, not the value echoed back.
    assert options[0]["label"] != options[0]["value"], options[0]
    assert "2026" in options[0]["label"], options[0]

    # The request the control produces, rather than only the one the default made.
    assert picker["query_for_september"] == "?start=2026-09-01&end=2026-09-30"
    assert picker["query_for_february"] == "?start=2028-02-01&end=2028-02-29", (
        "the last day of the month is the one date arithmetic worth pinning"
    )
    assert picker["query_for_nonsense"] == "", (
        "a value this build cannot read is the server's own default, not a request it 400s"
    )


def test_the_column_vocabulary_is_the_servers_own(results):
    """One vocabulary, two files: the server decides what may be stored, this decides what
    can be drawn - and the two lists have to be the same list, in the same order.

    Order matters as much as membership: the files are written in the vocabulary's order
    whatever order the boxes were ticked in, which is what makes two months comparable.
    """
    chooser = results["chooser"]
    assert chooser["vocabulary"] == list(reports.REPORT_COLUMNS), chooser["vocabulary"]
    assert chooser["defaults"] == list(reports.DEFAULT_REPORT_COLUMNS), chooser["defaults"]
    assert [
        column for column in chooser["vocabulary"] if column not in chooser["defaults"]
    ] == ["recorded"], "exactly one column is new, and it is not in anybody's default"
    # The CSV headers are the fixed English ones the console's own export uses, so two
    # downloads of the same month line up column for column whatever language the reader is
    # in; the sheet is the translated half of the same choice.
    assert chooser["csv_labels"] == [
        "Date", "Site", "Arrival", "Break hours", "Hours", "Recorded hours",
        "Approved hours", "Status"
    ]
    assert all(chooser["sheet_labels"]), chooser["sheet_labels"]
    assert not [label for label in chooser["sheet_labels"] if label.startswith("myHours")], (
        f"an untranslated key renders as its own name: {chooser['sheet_labels']}"
    )


def test_the_chooser_opens_on_what_the_account_has_stored(results):
    """The boxes are the account's choice, not a default this screen invented.

    The choice arrives with the report - one request, the one that already carries the rows
    - so the file cannot be built from a column list that arrives after the rows it
    describes.
    """
    chooser = results["chooser"]
    assert chooser["markup_has_chooser"] is True, "My hours has to offer the shape of the file"
    assert chooser["markup_has_save"] is True
    assert chooser["boxes"] == len(reports.REPORT_COLUMNS), "one box per column, no more"
    assert chooser["ticked"] == len(chooser["defaults"]), "an account that has never chosen"
    assert chooser["chosen"] == ",".join(chooser["defaults"])
    assert chooser["inline_handlers"] == 0, "bound by delegation, like the rest of the card"


def test_unticking_a_column_changes_the_next_file_before_anything_is_saved(results):
    """The screen's whole promise, kept for the file's *shape* as well as its figures: the
    download is built from the rows on screen, and now from the columns on screen too.

    Save is what makes a choice outlive the page, not what makes it take effect.
    """
    narrowed = results["narrowed"]
    assert narrowed["header"] == "Date,Site,Arrival,Hours,Recorded hours,Approved hours,Status"
    assert narrowed["first_row"] == (
        "2026-09-12,New Capital Zone B,07:12 late 12 min,9.50,10.00,,Awaiting approval"
    ), narrowed["first_row"]
    assert narrowed["csv_break_gone"] is True, "what was unticked is gone from the file"
    # The sheet is the same choice, in the reader's own words.
    assert narrowed["sheet_header"] is True, narrowed["sheet"]
    assert narrowed["sheet_break_gone"] is True, narrowed["sheet"]
    assert "10.00 h" in narrowed["sheet"], "the new column has its value on the paper"


def test_the_last_column_cannot_be_unticked(results):
    """A file with no columns is not a report, so the one tick that cannot come off is
    refused - and said out loud, because a checkbox that springs back is a bug to the
    person who unticked it unless something tells them why."""
    floor = results["floor"]
    assert floor["down_to"] == "date", floor["down_to"]
    assert floor["kept"] == "date", "the column that cannot come off stays ticked"
    assert floor["toasts"] == ["Keep at least one column."], floor["toasts"]
    assert floor["posted"] == 0, "a refusal is not a save"


def test_saving_remembers_the_choice_on_the_account(results):
    """One request, the chosen list, and the answer kept instead of the request.

    No repaint: the boxes on screen already say what the answer says, and repainting would
    close the panel under the finger that just pressed the button.
    """
    save = results["save"]
    assert save["posted"] == [["date", "site", "arrival", "break", "hours", "approved"]], save
    assert save["urls"] == ["/worker/me/report/columns"], save["urls"]
    assert save["methods"] == ["POST"], "this app writes with POST, like every other route"
    assert save["stored"] == "date,site,arrival,break,hours,approved"
    assert save["draft_cleared"] is True, "the saved choice is the account's now"
    assert save["toast"] == ["Columns saved."], save["toast"]
    assert save["requests_added"] == 1, "the save, and nothing else"


def test_the_screen_keeps_the_servers_answer_not_the_list_it_sent(results):
    """The server drops what it cannot fill, and the screen has to stop offering it.

    A client one release ahead asks for a column this server does not know; keeping its own
    list would leave it drawing a column that will come back empty for ever.
    """
    answer = results["answer"]
    assert answer["stored"] == "date,hours", answer
    assert answer["chosen"] == "date,hours", "the answer is what the files will now carry"


def test_a_refused_save_leaves_the_choice_in_force(results):
    """Remembering it failed; using it did not. The worker keeps the file they just built
    for this session, and the toast is the server's own sentence about why."""
    refused = results["refusedSave"]
    assert refused["still_in_force"] == "date,arrival,break,hours,approved,status", refused
    assert refused["draft_kept"] is True, "the ticks are not thrown away by a failure"
    assert refused["toast"] == ["A timesheet needs at least one column."], refused["toast"]


def test_a_stored_choice_reaches_a_screen_that_never_made_it(results):
    """The reason this lives on the account: the file is handed in from wherever the worker
    is standing, so a phone that has never seen the choice draws the same document."""
    stored = results["storedChoice"]
    assert stored["screen"] == "hand-app"
    assert stored["chosen"] == "date,site,recorded", stored


def test_a_site_name_cannot_become_markup_in_the_summary(results):
    """The new screen renders a value the app does not choose, so it escapes it."""
    hostile = results["hostile"]
    assert hostile["escaped"] is True, "the site name must reach the DOM as text"
    assert hostile["raw_tag"] is False, "and never as a tag"


def test_every_new_string_exists_in_every_language(results):
    """A key present in English only renders as its own name on every phone that lacks it."""
    translations = results["translations"]
    assert translations["present_in_english"] == [], translations["present_in_english"]
    # The tables the session actually loaded, not a list written out here: a language added
    # to the app is covered by its file arriving, and this test never has to be edited - nor
    # can it pass while a table quietly stops being loaded.
    languages = [lang for lang, _ in translations["missing"]]
    assert languages == ["en", "ar", "hi", "ur"], languages
    assert all(missing == "" for _, missing in translations["missing"]), translations["missing"]
