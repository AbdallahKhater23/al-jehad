"""The Admin tab's Data & retention panel: what is on disk, and what the sweep would take.

WHY THIS EXISTS
---------------
Retention is the one part of this application whose correct behaviour is invisible: nothing
breaks when it stops, so the screen an operator reads has to be the evidence. The API's three
existing read-only answers - the policy, the last run, the residue a query cannot see - share one
blind spot: none of them says how much is actually *there*, or whether any of it is growing. That
is what this panel adds, together with the single control that asks the server what the next
sweep would erase.

What is pinned here, in the order the panel reads:

1. the figures are the server's, not arithmetic done in the browser, and the copy directory is
   never folded into the live total - a backup holds a second copy of the same faces, so one
   total would overstate what the deployment is holding by the size of its own safety net;
2. every store the API names is drawn with its path, count, size and span of ages, and a store
   that is empty says so rather than showing a blank;
3. the policy table prints days, and a knob set to keep forever says so in words - ``0`` on
   screen reads as "delete immediately" and a blank reads as "not configured";
4. the check is a *report*: the plan quotes what the cutoffs selected (``matched``) and not the
   report's own ``deleted_total``, which is zero by construction in a dry run - and the panel
   says out loud that nothing was removed, because a list of things about to be deleted without
   that sentence reads as a log of things that were;
5. a refusal is drawn *in the panel*. On this screen silence and "there is nothing to delete"
   look identical, and that is the most reassuring possible lie;
6. a failed read of this panel does not take the rest of the tab with it, and does not raise a
   toast about somebody else's rules;
7. nothing the server sends is interpolated as markup - the paths and the failure text are both
   attacker-adjacent (a directory an operator typed, an exception's message).

Node is optional; without it these skip rather than fail.
"""

from __future__ import annotations

import html as html_module

import pytest

import frontend_vm

pytestmark = pytest.mark.skipif(frontend_vm.NODE is None, reason="node is not installed")

#: The stylesheet, read as text for the one claim the rendered markup cannot make: how the two
#: tables are arranged is a CSS decision, and the panel carries no width logic of its own.
STYLE = (frontend_vm.FRONTEND / "style.css").read_text(encoding="utf-8", errors="ignore")

HARNESS = r"""
// --- the server's answers, faked ----------------------------------------

const RULES = {
    clock_in_window_start: '04:00', clock_in_window_end: '06:30', regular_hours: 8.0,
    overtime_notify_hours: 8.1, site_timezone: 'Africa/Cairo', break_minutes: 30,
    break_after_hours: 4, auto_close_at_regular: 1,
    day_end: {
        regular_hours: 8.0, notify_hours: 8.1, close_at_paid_hours: null, close_defers: true,
        alert_reachable: true, day_ended_by: 'overtime_review', detail: 'the close stands down'
    }
};

const ADMIN = {
    id: '5000', name: 'Head Admin', role: 'head_admin', phone: '', email: 'head@example.test',
    status: 'active', face_enrolled: true, enrolled_at: null, password_set: true,
    password_changed_at: null, sessions_revoked: 0
};

// The report's shape is the contract: eight stores, of which one is a *copy* of the data rather
// than data, one is a single file with a journal beside it, and one nests. A panel tested against
// a flat list of faces would not notice itself dropping any of the three.
const STATUS = {
    policy: {
        punch_photo_days: 30,
        punch_frame_days: 30,
        biometric_days: 90,
        audit_days: 365,
        notification_days: 30,
        punch_queue_days: 60,
        anchor_days: 0,
        max_items_per_sweep: 5000
    },
    dry_run: false,
    scheduler: { enabled: true, running: true, interval_seconds: 21600, initial_delay_seconds: 300 },
    last_run: {
        id: 7, started_at: '2026-09-29 20:00:00', finished_at: '2026-09-29 20:00:04',
        actor: 'system', deleted_total: 412, bytes_wiped: 3250000, failures_total: 0, digest: 'abc'
    },
    residue: {
        biometric_files_for_deactivated_accounts: 2,
        orphaned_biometric_files: 1,
        biometric_staging_files: 0,
        orphaned_punch_photos: 0,
        listed: ['9f.json']
    },
    storage: {
        as_of: '2026-09-29 20:15:00',
        stores: [
            {
                key: 'worker_photos', kind: 'live', path: 'C:/srv/worker_photos', recursive: false,
                files: 12, bytes: 2400000, oldest: '2026-08-30 09:00:00',
                newest: '2026-09-28 17:00:00', truncated: false
            },
            {
                key: 'local_references', kind: 'live', path: 'C:/srv/local_references', recursive: false,
                files: 12, bytes: 120000, oldest: '2026-08-30 09:00:00',
                newest: '2026-09-28 17:00:00', truncated: false
            },
            {
                key: 'punch_frames', kind: 'live', path: 'C:/srv/punch_frames', recursive: false,
                files: 430, bytes: 2150000, oldest: '2026-06-01 06:00:00',
                newest: '2026-09-29 06:00:00', truncated: false
            },
            {
                key: 'quick_link_photos', kind: 'live', path: 'C:/srv/quick_link_photos', recursive: false,
                files: 41, bytes: 2050000, oldest: '2026-06-02 06:00:00',
                newest: '2026-09-29 06:10:00', truncated: false
            },
            {
                // A store nothing has ever written to: no span, and the panel has to say so.
                key: 'registration_photos', kind: 'live', path: 'C:/srv/registration_photos', recursive: false,
                files: 0, bytes: 0, oldest: null, newest: null, truncated: false
            },
            {
                key: 'calibration_corpus', kind: 'live', path: 'C:/srv/corpus', recursive: true,
                files: 14, bytes: 460000, oldest: '2026-07-01 09:00:00',
                newest: '2026-09-20 09:00:00', truncated: false
            },
            {
                key: 'backups', kind: 'copy', path: 'C:/srv/backups', recursive: true,
                files: 6, bytes: 1048576, oldest: '2026-09-01 02:00:00',
                newest: '2026-09-29 02:00:00', truncated: false
            },
            {
                key: 'database', kind: 'live', path: 'C:/srv/times.db', recursive: false,
                files: 3, bytes: 65536, oldest: '2026-08-01 00:00:00',
                newest: '2026-09-29 20:00:04', truncated: false
            }
        ],
        files_total: 518,
        live_bytes: 7340032,
        copy_bytes: 1048576,
        truncated: false
    },
    attendance_logs: 'never deleted automatically: the hours a person worked are pay records.'
};

function blank() {
    return {
        matched: 0, deleted: 0, bytes: 0, references: 0, deferred: 0, skipped: null,
        digest: null, listed: [], failures: []
    };
}

// A dry run of a deployment with a month of faces past their windows. ``deleted_total`` is zero
// because a check deletes nothing - which is exactly the number the panel must not quote.
const PLAN = {
    dry_run: true,
    started_at: '2026-09-29 20:20:00',
    finished_at: '2026-09-29 20:20:02',
    targets: {
        punch_photos: Object.assign(blank(), { matched: 12 }),
        punch_frames: Object.assign(blank(), { matched: 40, bytes: 307200 }),
        refused_punches: Object.assign(blank(), { matched: 0 }),
        biometric_files: Object.assign(blank(), { matched: 3, bytes: 4500 }),
        audit_log: Object.assign(blank(), { matched: 0 }),
        notifications: Object.assign(blank(), { matched: 0 }),
        worker_notifications: Object.assign(blank(), { matched: 0 }),
        punch_queue: Object.assign(blank(), { matched: 0 }),
        attendance_logs: Object.assign(blank(), {
            skipped: 'attendance_logs is never deleted automatically (hours worked are pay records)'
        })
    },
    deleted_total: 0,
    bytes_total: 311700,
    failures_total: 0
};

// Nothing past its window: the normal case, and the one where a plan table full of zeroes would
// train the reader to stop reading.
const EMPTY_PLAN = (function () {
    const targets = {};
    Object.keys(PLAN.targets).forEach((name) => { targets[name] = blank(); });
    return {
        dry_run: true, started_at: '2026-09-29 20:20:00', finished_at: '2026-09-29 20:20:01',
        targets: targets, deleted_total: 0, bytes_total: 0, failures_total: 0
    };
})();

function deepCopy(value) { return JSON.parse(JSON.stringify(value)); }

let statusReply = STATUS;
let statusFails = false;
let planReply = PLAN;
let planStatus = 200;
const planCalls = [];

function resetPlan() {
    planReply = deepCopy(PLAN);
    planStatus = 200;
    planCalls.length = 0;
}

function responders(url, init) {
    // The dry run first: its path contains the status route's, and reading them in the wrong
    // order would have the panel's *check* answered by the status payload.
    if (url.indexOf('/admin/retention/dry-run') >= 0) {
        planCalls.push({
            url: String(url),
            method: (init && init.method) || 'GET',
            headers: (init && init.headers) || {}
        });
        if (planStatus !== 200) {
            return { status: planStatus, body: { detail: 'Retention is switched off for this deployment.' } };
        }
        return { status: 200, body: deepCopy(planReply) };
    }
    if (url.indexOf('/admin/retention') >= 0) {
        if (statusFails) return { status: 503, body: { detail: 'Database is locked.' } };
        return { status: 200, body: deepCopy(statusReply) };
    }
    if (url.indexOf('/admin/shift_rules') >= 0) return { status: 200, body: RULES };
    return { status: 200, body: {} };
}

// --- the two elements the check touches ---------------------------------
//
// The stub DOM answers ``querySelector`` with null, which the binder copes with - but then there
// would be no way to *press* the control the paint wired. Two elements are stood up inside the
// page so a case can tap what the panel bound and read the region it drew into; every other
// selector still answers null, exactly as the stub does. They live in ``window`` because that is
// the only thing the harness and the page share: a stub built out here would be an object from
// the wrong realm, and the paint's assignment to it would land somewhere nothing can read.
const CHECK_STUBS = `
    window.__check = { disabled: false, textContent: '', onclick: null };
    window.__plan = { innerHTML: '' };
    document.querySelector = (selector) => {
        if (selector === '[data-retention-check]') return window.__check;
        if (selector === '[data-retention-plan]') return window.__plan;
        return null;
    };
`;

function pressCheck(env) {
    return env.evaluate('window.__check.onclick()');
}

function planMarkup(env) {
    return env.evaluate('window.__plan.innerHTML');
}

function checkState(env) {
    return env.evaluate(
        "({ disabled: window.__check.disabled, label: window.__check.textContent," +
        " wired: typeof window.__check.onclick === 'function' })"
    );
}

// --- reading what was rendered ------------------------------------------

function textOf(cell) {
    return String(cell || '').replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();
}

// The four figures, by the name the panel publishes rather than by position: the residue is a
// badge where it is not nought, so a parser that counted figures would read the row wrong the
// moment there is something to report.
function factsOf(markup) {
    const out = {};
    const pattern = /data-retention-fact="([^"]*)"[^>]*>([\s\S]*?)<\/span>/g;
    let match = pattern.exec(markup);
    while (match) {
        out[match[1]] = textOf(match[2]);
        match = pattern.exec(markup);
    }
    return out;
}

function residueOf(markup) {
    const match = /<span[^>]*data-retention-residue="([^"]*)"[^>]*>([\s\S]*?)<\/span>/.exec(markup);
    if (!match) return null;
    return {
        state: match[1],
        // The title is searched across the whole tag rather than after the attribute: on the
        // failing read it is the only place the server's reason exists on the page.
        title: (/title="([^"]*)"/.exec(match[0]) || [])[1] || null,
        value: textOf(match[2])
    };
}

// Just the panel, for the assertions about what the page does with hostile text: the Admin tab
// also draws a company lockup, and a logo is an ``<img>`` nobody is testing here.
function panelOf(markup) {
    return (/<section[^>]*data-retention-panel="true"[\s\S]*?<\/section>/.exec(markup) || [])[0] || '';
}

function storeRowHtml(markup, key) {
    const row = new RegExp('<tr data-retention-store="' + key + '"[\\s\\S]*?<\\/tr>').exec(markup);
    return row ? row[0] : '';
}

function noteOf(markup, attribute) {
    const match = new RegExp('data-' + attribute + '="true"[^>]*>([\\s\\S]*?)<\\/p>').exec(markup);
    return match ? textOf(match[1]) : null;
}

function rowsUnder(markup, table) {
    const found = new RegExp('<table[^>]*data-retention-' + table + '="true"[\\s\\S]*?<\\/table>').exec(markup);
    return found ? found[0] : '';
}

function storeRows(markup) {
    const rows = rowsUnder(markup, 'stores').match(/<tr data-retention-store="[^"]*"[\s\S]*?<\/tr>/g) || [];
    return rows.map((row) => ({
        key: (/data-retention-store="([^"]*)"/.exec(row) || [])[1],
        kind: (/data-retention-kind="([^"]*)"/.exec(row) || [])[1],
        path: (/title="([^"]*)"/.exec(row) || [])[1] || null,
        copy_badge: row.indexOf('ui-badge') >= 0,
        cells: (row.match(/<td[^>]*>[\s\S]*?<\/td>/g) || []).map(textOf)
    }));
}

function policyRows(markup) {
    const rows = rowsUnder(markup, 'policy').match(/<tr data-retention-window="[^"]*"[\s\S]*?<\/tr>/g) || [];
    return rows.map((row) => ({
        field: (/data-retention-window="([^"]*)"/.exec(row) || [])[1],
        cells: (row.match(/<td[^>]*>[\s\S]*?<\/td>/g) || []).map(textOf)
    }));
}

function planRows(markup) {
    const rows = markup.match(/<tr data-retention-target="[^"]*"[\s\S]*?<\/tr>/g) || [];
    return rows.map((row) => ({
        target: (/data-retention-target="([^"]*)"/.exec(row) || [])[1],
        failed: row.indexOf('data-retention-target-failed=') >= 0,
        error: (/title="([^"]*)"/.exec(row) || [])[1] || null,
        cells: (row.match(/<td[^>]*>[\s\S]*?<\/td>/g) || []).map(textOf)
    }));
}

function toasts(env) {
    return env.evaluate("(document.getElementById('toastRoot').__children || []).map((el) => el.textContent)");
}

function render(env) {
    return env.evaluate("document.getElementById('adminContent').innerHTML");
}

function adminEnv() {
    resetPlan();
    statusReply = deepCopy(STATUS);
    statusFails = false;
    const env = boot();
    env.evaluate(CHECK_STUBS);
    env.setResponder(responders);
    env.evaluate('State.saveUser(' + JSON.stringify({
        id: ADMIN.id, name: ADMIN.name, role: ADMIN.role, token: 'tok-5000'
    }) + ')');
    return env;
}

const results = {};

// 1. the panel as painted: four figures, the last run, the stores, the windows
{
    const env = adminEnv();
    await env.evaluate("UI.renderAdminTab('Admin')");
    const markup = render(env);
    results.panel = {
        present: markup.indexOf('data-retention-panel') >= 0,
        title: textOf((/id="retentionTitle"[^>]*>([\s\S]*?)<\/h3>/.exec(markup) || [])[1] || ''),
        hint: textOf((/id="retentionTitle"[\s\S]*?<p class="ui-section-note"[^>]*>([\s\S]*?)<\/p>/.exec(markup) || [])[1] || ''),
        facts: factsOf(markup),
        residue: residueOf(markup),
        run: noteOf(markup, 'retention-run'),
        pay: noteOf(markup, 'retention-pay'),
        copies: noteOf(markup, 'retention-copies'),
        stores: storeRows(markup),
        policy: policyRows(markup),
        check_label: textOf((/data-retention-check="true"[^>]*>([\s\S]*?)<\/button>/.exec(markup) || [])[1] || ''),
        // The two tables are one wrapper's business: they sit side by side where the container
        // is wide and stack where it is not, and *that* is the decision worth pinning - the
        // panel is the same markup at every width.
        layout: (function () {
            const split = /<div class="retention-split">([\s\S]*?)<\/div>\s*<div data-retention-plan=/.exec(markup);
            const inside = split ? split[1] : '';
            return {
                wrapped: inside.indexOf('data-retention-stores') >= 0 && inside.indexOf('data-retention-policy') >= 0,
                stores_before_windows: inside.indexOf('data-retention-stores') < inside.indexOf('data-retention-policy'),
                plan_below_both: markup.indexOf('data-retention-plan="true"') > markup.indexOf('data-retention-policy')
            };
        })(),
        inline_handler: /onclick=/.test(markup),
        wired: checkState(env).wired,
        plan_region_empty: planMarkup(env) === '',
        retention_reads: env.requests.filter((r) => r.url.indexOf('/admin/retention') >= 0).length,
        rules_still_there: markup.indexOf('data-rules-panel') >= 0,
        // This tab drew a create-an-administrator form once; it lives on Credentials now, and
        // adding a fourth section must not quietly bring back a second way to make an account.
        create_form: markup.indexOf('id="addAdminForm"') >= 0
    };
}

// 2. a listing that stopped at its cap says so rather than rounding the answer down
{
    const env = adminEnv();
    statusReply = deepCopy(STATUS);
    statusReply.storage.truncated = true;
    statusReply.storage.stores[0].truncated = true;
    await env.evaluate("UI.renderAdminTab('Admin')");
    const markup = render(env);
    results.truncated = {
        notice: markup.indexOf('data-retention-truncated') >= 0,
        words: noteOf(markup, 'retention-truncated'),
        // The count it cut short is still printed: the note qualifies the figure, it does not
        // replace it with nothing.
        files_fact: factsOf(markup).files
    };
}

// 3. residue that cannot be counted is not reported as zero
{
    const env = adminEnv();
    statusReply = deepCopy(STATUS);
    statusReply.residue = { error: 'OperationalError: unable to open database file' };
    await env.evaluate("UI.renderAdminTab('Admin')");
    results.residue_error = {
        residue: residueOf(render(env)),
        facts: factsOf(render(env))
    };
}

// 4. a scheduler that is not running, and a sweep with failures behind it
{
    const env = adminEnv();
    statusReply = deepCopy(STATUS);
    statusReply.scheduler.enabled = false;
    statusReply.last_run.failures_total = 2;
    await env.evaluate("UI.renderAdminTab('Admin')");
    const markup = render(env);
    results.scheduler_off = {
        sweep_fact: factsOf(markup).sweep,
        run: noteOf(markup, 'retention-run')
    };
}

// 4b. and a deployment where no sweep has ever completed
{
    const env = adminEnv();
    statusReply = deepCopy(STATUS);
    statusReply.last_run = null;
    await env.evaluate("UI.renderAdminTab('Admin')");
    results.never_run = noteOf(render(env), 'retention-run');
}

// 5. the check: a POST that quotes what the cutoffs selected, and says nothing was removed
{
    const env = adminEnv();
    await env.evaluate("UI.renderAdminTab('Admin')");
    await pressCheck(env);
    const plan = planMarkup(env);
    results.plan = {
        calls: planCalls.length,
        path: planCalls[0].url.replace(/^.*\/api\/v1/, ''),
        method: planCalls[0].method,
        authorized: planCalls[0].headers['Authorization'] || null,
        rows: planRows(plan),
        sentence: textOf((/data-retention-plan-total="true">([\s\S]*?)<\/p>/.exec(plan) || [])[1] || ''),
        untouched: noteOf(plan, 'retention-untouched'),
        none_line: plan.indexOf('data-retention-plan-none') >= 0,
        button_disabled: checkState(env).disabled,
        button_label: checkState(env).label,
        // The plan is drawn where the button is, so the panel has not navigated away from the
        // figures it qualifies.
        panel_still_there: render(env).indexOf('data-retention-stores') >= 0,
        post_reads: env.requests.filter((r) => r.url.indexOf('/admin/retention/dry-run') >= 0).length
    };
}

// 6. nothing past its window: the panel says so, and does not print a table of zeroes
{
    const env = adminEnv();
    planReply = deepCopy(EMPTY_PLAN);
    await env.evaluate("UI.renderAdminTab('Admin')");
    await pressCheck(env);
    const plan = planMarkup(env);
    results.plan_empty = {
        none_line: noteOf(plan, 'retention-plan-none'),
        untouched: plan.indexOf('data-retention-untouched') >= 0,
        rows: planRows(plan).length,
        has_table: plan.indexOf('data-retention-plan-table') >= 0
    };
}

// 7. a target that could not run is named, with the server's own words beside it
{
    const env = adminEnv();
    planReply = deepCopy(PLAN);
    planReply.targets.audit_log = Object.assign(blank(), { error: 'OperationalError: database is locked' });
    await env.evaluate("UI.renderAdminTab('Admin')");
    await pressCheck(env);
    results.plan_target_failed = planRows(planMarkup(env));
}

// 8. a refused check is drawn in the panel, and the button comes back
{
    const env = adminEnv();
    await env.evaluate("UI.renderAdminTab('Admin')");
    planStatus = 503;
    await pressCheck(env);
    const plan = planMarkup(env);
    results.plan_refused = {
        flagged: plan.indexOf('data-retention-plan-failed') >= 0,
        words: textOf((/data-retention-plan-failed="true"[^>]*>([\s\S]*?)<\/p>/.exec(plan) || [])[1] || ''),
        rows: planRows(plan).length,
        button_disabled: checkState(env).disabled,
        button_label: checkState(env).label,
        // The figures are still on screen: a check that could not run says nothing about disk.
        figures_kept: render(env).indexOf('data-retention-stores') >= 0
    };
}

// 9. a status read that fails is the panel's own problem, not the tab's
{
    const env = adminEnv();
    statusFails = true;
    await env.evaluate("UI.renderAdminTab('Admin')");
    const markup = render(env);
    results.status_failed = {
        flagged: markup.indexOf('data-retention-unavailable') >= 0,
        words: noteOf(markup, 'retention-unavailable'),
        panel_present: markup.indexOf('data-retention-panel') >= 0,
        stores_drawn: markup.indexOf('data-retention-stores') >= 0,
        rules_still_there: markup.indexOf('data-rules-panel') >= 0,
        toasts: toasts(env)
    };
}

// 10. a hostile directory path and a hostile exception are text, not markup
{
    const env = adminEnv();
    statusReply = deepCopy(STATUS);
    statusReply.storage.stores[0].path = 'C:/srv/"><img src=x onerror=alert(1)>';
    await env.evaluate("UI.renderAdminTab('Admin')");
    const markup = render(env);
    const panel = panelOf(markup);
    planReply = deepCopy(PLAN);
    planReply.targets.audit_log = Object.assign(blank(), { matched: 1, error: '<b>boom</b>' });
    await pressCheck(env);
    const plan = planMarkup(env);
    results.injection = {
        panel_has_img: panel.indexOf('<img') >= 0,
        panel_has_script: panel.indexOf('<script') >= 0,
        store_row_has_img: storeRowHtml(panel, 'worker_photos').indexOf('<img') >= 0,
        store_path: storeRows(panel)[0].path,
        plan_has_bold: plan.indexOf('<b>') >= 0,
        plan_error: (planRows(plan).filter((row) => row.target === 'audit_log')[0] || {}).error
    };
}
"""


@pytest.fixture(scope="module")
def results() -> dict:
    return frontend_vm.run(HARNESS)


def test_the_panel_shows_the_four_figures_the_server_sent(results):
    """Bytes and files come from the report, and the two derived ones say what they mean."""
    panel = results["panel"]
    assert panel["present"] is True
    assert html_module.unescape(panel["title"]) == "Data & retention"
    assert panel["facts"]["live"] == "7.0 MB", "live_bytes, in the units an operator reads"
    assert panel["facts"]["files"] == "518"
    assert panel["facts"]["sweep"] == "every 6 h", "3600-second arithmetic, not a hardcoded six"
    assert panel["run"].startswith("2026-09-29 20:00:04")
    assert "412" in panel["run"] and "3.1 MB" in panel["run"]
    assert panel["hint"], "the panel has to say that nothing on it changes anything"
    assert panel["check_label"] == "Check what the next sweep would delete"


def test_the_left_behind_figure_is_a_warning_rather_than_a_measurement(results):
    """Three files the policy says should be gone: the one figure here that is a fault."""
    residue = results["panel"]["residue"]
    assert residue == {"state": "true", "title": None, "value": "3"}
    # Published under its own attribute rather than as a fourth plain figure: the value is a
    # *badge* here, and a parser (or a reader) that looked for the figure would find nothing at
    # all - which is the same shape as a fault, deliberately.
    assert results["panel"]["facts"].get("residue") is None


def test_the_two_tables_are_arranged_as_one_wrapper_not_two_blocks(results):
    """Both tables in one wrapper, stores first, with the plan below them where the button is."""
    layout = results["panel"]["layout"]
    assert layout["wrapped"] is True, "one wrapper decides the arrangement"
    assert layout["stores_before_windows"] is True, "what is there, then how long it is kept for"
    assert layout["plan_below_both"] is True


def test_the_arrangement_wraps_by_container_rather_than_by_viewport():
    """Flex with wrapping bases, and no viewport breakpoint - deliberately.

    The widths here belong to the container, not the window. The windows table is two short
    columns and the stores table needs its four; beside each other at 900px they fill it, and
    under a fixed breakpoint a pane narrower than the window it sits in would squeeze one of
    them to a third of a column instead of letting it drop to its own line - which is the case
    a media query gets wrong and nobody tests.
    """
    block = STYLE[STYLE.index(".retention-split {") : STYLE.index("/* ---------- Tables")]
    assert "display: flex" in block and "flex-wrap: wrap" in block
    assert ".retention-split > .ui-table-wrap:first-child" in block
    assert "@media" not in block, "a viewport breakpoint is the wrong measure for a container"


def test_every_store_is_drawn_with_its_path_size_and_span(results):
    """Eight stores, in the order the API lists them, and none of them invented here."""
    stores = results["panel"]["stores"]
    assert [store["key"] for store in stores] == [
        "worker_photos",
        "local_references",
        "punch_frames",
        "quick_link_photos",
        "registration_photos",
        "calibration_corpus",
        "backups",
        "database",
    ]
    frames = stores[2]
    assert frames["cells"] == ["Punch frames", "430", "2.1 MB", "2026-06-01 → 2026-09-29"]
    assert frames["path"] == "C:/srv/punch_frames", "the path rides on the row so it can be checked"
    # An empty store says so rather than showing a blank or a zero-length span.
    assert stores[4]["cells"][1:] == ["0", "0 B", "—"]
    # And the backup directory is labelled as the second copy it is - the label is the whole
    # reason the report measures it apart from the data it copies.
    assert stores[6]["kind"] == "copy" and stores[6]["copy_badge"] is True
    assert stores[0]["copy_badge"] is False


def test_the_windows_are_the_policys_and_keeping_forever_says_so(results):
    """A knob at ``0`` means keep forever; printing the nought would read as delete now."""
    policy = {row["field"]: row["cells"] for row in results["panel"]["policy"]}
    assert list(policy) == [
        "punch_photo_days",
        "punch_frame_days",
        "biometric_days",
        "audit_days",
        "notification_days",
        "punch_queue_days",
        "anchor_days",
    ]
    assert policy["punch_photo_days"] == ["Link selfies", "30 days"]
    assert policy["audit_days"] == ["Audit stream", "365 days"], "the Developer console's own word for it"
    assert policy["anchor_days"] == ["Device keys", "kept forever"]


def test_the_panel_explains_what_it_is_not_allowed_to_delete(results):
    """The pay records, and why the copy directory is counted twice."""
    panel = results["panel"]
    assert "pay records" in panel["pay"]
    assert "second copy" in panel["copies"]
    assert panel["rules_still_there"] is True, "the retention panel joins the tab, it does not replace it"
    assert panel["create_form"] is False, "a fourth section must not smuggle back a second create form"


def test_the_panel_asks_once_and_binds_its_own_control(results):
    """One read of the report, no write, and no inline handler on the button."""
    panel = results["panel"]
    assert panel["retention_reads"] == 1, "one read of the status, nothing polled"
    assert panel["inline_handler"] is False, "the check binds a listener; the CSP budget may not grow"
    assert panel["wired"] is True, "the paint wired the button it drew"
    assert panel["plan_region_empty"] is True, "nothing is asked until somebody asks"


def test_a_listing_that_stopped_at_its_cap_says_so(results):
    truncated = results["truncated"]
    assert truncated["notice"] is True
    assert "more files than were counted" in truncated["words"]
    assert truncated["files_fact"] == "518", "the figure it qualifies is still shown"


def test_residue_that_could_not_be_counted_is_not_a_zero(results):
    """``retention.residue`` answers an unreadable database with an error, and so must the panel."""
    residue = results["residue_error"]["residue"]
    assert residue["state"] == "error"
    assert residue["value"] == "could not be checked"
    assert "unable to open database file" in residue["title"]


def test_a_switched_off_sweeper_and_a_failing_one_are_both_visible(results):
    off = results["scheduler_off"]
    assert off["sweep_fact"] == "switched off"
    assert "2 failed" in off["run"], "a sweep that half worked is not a sweep that worked"


def test_a_sweep_that_has_never_run_says_that_rather_than_showing_a_blank(results):
    assert results["never_run"] == "has not run yet"


def test_the_check_posts_and_quotes_what_the_cutoffs_selected(results):
    """The report's own totals are what a sweep *removed* - zero in a check, and not the answer."""
    plan = results["plan"]
    assert plan["calls"] == 1
    assert plan["path"].endswith("/admin/retention/dry-run")
    assert plan["method"] == "POST"
    assert plan["authorized"] == "Bearer tok-5000"
    assert plan["post_reads"] == 1
    assert [row["target"] for row in plan["rows"]] == ["punch_photos", "punch_frames", "biometric_files"]
    assert plan["rows"][0]["cells"] == ["Link selfies", "12", "—"]
    assert plan["rows"][1]["cells"] == ["Punch frames", "40", "300.0 KB"]
    assert "55 items" in plan["sentence"], "12 + 40 + 3, summed from ``matched``"
    assert "304.4 KB" in plan["sentence"], "and the space those files hold"
    assert "0 items" not in plan["sentence"], "``deleted_total`` is zero by construction in a dry run"
    assert plan["none_line"] is False
    assert plan["untouched"] == "A check, not a deletion: nothing was removed."
    assert plan["button_disabled"] is False and plan["button_label"] == "Check what the next sweep would delete"
    assert plan["panel_still_there"] is True


def test_a_plan_with_nothing_in_it_says_nothing_is_past_its_window(results):
    empty = results["plan_empty"]
    assert empty["none_line"] == "Nothing is past its window."
    assert empty["rows"] == 0 and empty["has_table"] is False, "a table of zeroes is how a reader stops reading"
    assert empty["untouched"] is True


def test_a_target_that_could_not_be_checked_is_named_with_the_reason(results):
    rows = {row["target"]: row for row in results["plan_target_failed"]}
    assert rows["audit_log"]["failed"] is True
    assert "database is locked" in rows["audit_log"]["error"]
    assert rows["audit_log"]["cells"] == ["Audit stream", "0", "could not be checked"]
    assert rows["punch_frames"]["failed"] is False, "one failing target does not silence the others"


def test_a_refused_check_is_reported_in_the_panel_and_the_button_returns(results):
    refused = results["plan_refused"]
    assert refused["flagged"] is True
    assert refused["words"] == "The check could not be run."
    assert refused["rows"] == 0
    assert refused["button_disabled"] is False, "a dead-looking button is a button that gets tapped again"
    assert refused["button_label"] == "Check what the next sweep would delete"
    assert refused["figures_kept"] is True


def test_a_status_read_that_fails_does_not_take_the_rest_of_the_tab_with_it(results):
    failed = results["status_failed"]
    assert failed["flagged"] is True
    assert failed["words"] == "This panel could not be read. The rest of the tab is unaffected."
    assert failed["panel_present"] is True and failed["stores_drawn"] is False, "no half-panel"
    assert failed["rules_still_there"] is True
    assert failed["toasts"] == [], "the shift rules are not the thing that failed"


def test_a_path_or_an_error_from_the_server_is_text_rather_than_markup(results):
    injected = results["injection"]
    assert injected["panel_has_img"] is False and injected["panel_has_script"] is False
    assert injected["store_row_has_img"] is False, "a path is a tooltip's text, not a tag"
    assert injected["store_path"] == 'C:/srv/&quot;&gt;&lt;img src=x onerror=alert(1)&gt;', "escaped, and readable"
    assert injected["plan_has_bold"] is False
    assert injected["plan_error"] == "&lt;b&gt;boom&lt;/b&gt;"
