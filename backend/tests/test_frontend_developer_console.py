"""The root tier's own console: runtime flags, alerts, diagnostics and the audit stream.

WHY THIS SUITE EXISTS
---------------------
Four developer surfaces existed only as endpoints (``/developer/runtime``,
``/developer/alerts``, ``/developer/diagnostics/*``, ``/developer/audit``) - reachable, for the
person who runs the deployment, only with curl. The console gives them a screen. What no backend
test can see is the *client*: that the tab is offered to the root tier and refused to an
administrator, that opening it reads the four routes, that its controls are bound by ``data-``
hooks rather than inline ``onclick`` (the document CSP's per-file allowance may only fall), and
that a summary written by the server is escaped like any other server text.

Node is optional; without it these skip rather than fail.
"""

from __future__ import annotations

import re

import pytest

import frontend_vm
from harness import PROJECT_ROOT

pytestmark = pytest.mark.skipif(frontend_vm.NODE is None, reason="node is not installed")

HARNESS = r"""
// --- the server's answers, faked ----------------------------------------

const HOSTILE = '<img src=x onerror=alert(1)>';

const RUNTIME = {
    version: 7,
    values: { maintenance_mode: false, log_level: 'INFO', auth_anomaly_alerts: true },
    defaults: { maintenance_mode: false, log_level: 'INFO', auth_anomaly_alerts: true },
    documentation: {
        maintenance_mode: 'Refuse authenticated writes while the database is worked on.',
        log_level: 'The root logger level, applied on the next read.',
        auth_anomaly_alerts: 'Whether repeated credential failures are raised.'
    }
};

const ALERTS = [
    {
        id: 9, kind: 'db_pool_saturation', severity: 'critical',
        summary: 'Connection waits past what this deployment is sized for.',
        detail_json: '{}', trace_id: 'tr-9', dedupe_key: 'k:9',
        created_at: '2026-09-26 22:48:00', read_at: null, read_by: null
    },
    {
        id: 8, kind: 'developer_account_seeded', severity: 'warning',
        summary: 'root account 309010401073 created',
        detail_json: '{}', trace_id: null, dedupe_key: 'k:8',
        created_at: '2026-09-26 22:40:00', read_at: '2026-09-26 22:41:00', read_by: '309010401073'
    },
    {
        id: 7, kind: 'slow_query', severity: 'info',
        summary: HOSTILE,
        detail_json: '{}', trace_id: null, dedupe_key: 'k:7',
        created_at: '2026-09-25 05:00:00', read_at: null, read_by: null
    }
];

const POOL = {
    statements: 4021, slow_statements: 3, lock_timeouts: 0.0, max_lock_wait_seconds: 0.012,
    unread_alerts: 2, journal_mode: 'wal', busy_timeout_ms: 5000, page_count: 828,
    page_size: 4096, write_model: 'single-writer (SQLite, WAL)', saturated: false
};

const SLOW = {
    threshold_ms: 250,
    recent: [{ operation: 'select', duration_ms: 312.5, trace_id: 'tr-1' }],
    explainable: ['roster', 'audit_recent', 'open_notes'],
    note: 'Statement text is deliberately not recorded.'
};

const AUDIT = {
    events: [
        { id: 5, created_at: '2026-09-26 22:48:00', actor_id: '309010401073',
          actor_role: 'developer', action: 'login', entity: 'users', entity_id: '309010401073',
          ip: '10.0.0.1', user_agent: 'curl' }
    ],
    alerts: [], security_actions: ['login']
};

const requests = [];

function responders(url, init) {
    requests.push({ url: url.split('/api/v1')[1] || url, method: (init && init.method) || 'GET' });
    if (/\/developer\/alerts\/\d+\/read$/.test(url)) return { status: 200, body: { id: 9, already_read: false } };
    if (url.indexOf('/developer/diagnostics/caches/flush') >= 0) return { status: 200, body: { flushed: [{ cache: 'runtime_config' }] } };
    if (url.indexOf('/developer/diagnostics/query-plan/') >= 0) {
        const name = url.split('/query-plan/')[1];
        return { status: 200, body: { name: name, plan: ['SCAN users USING INDEX'] } };
    }
    if (url.indexOf('/developer/diagnostics/pool') >= 0) return { status: 200, body: POOL };
    if (url.indexOf('/developer/diagnostics/slow-queries') >= 0) return { status: 200, body: SLOW };
    if (url.indexOf('/developer/runtime') >= 0) return { status: 200, body: RUNTIME };
    if (url.indexOf('/developer/alerts') >= 0) {
        return { status: 200, body: { alerts: ALERTS, unread: 2, kinds: { slow_query: 'One statement was slow.' } } };
    }
    if (url.indexOf('/developer/audit') >= 0) return { status: 200, body: AUDIT };
    return { status: 200, body: [] };
}

function consoleEnv(who) {
    requests.length = 0;
    const env = boot();
    env.setResponder(responders);
    env.evaluate('State.saveUser(' + JSON.stringify(who) + ')');
    return env;
}

const DEVELOPER = { id: '309010401073', name: 'Developer', role: 'developer', token: 'tok-dev' };
const HEAD_ADMIN = { id: '5000', name: 'Head Admin', role: 'head_admin', token: 'tok-admin' };

function markup(env) { return env.evaluate("document.getElementById('adminContent').innerHTML"); }
function text(env) { return markup(env).replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim(); }

const results = {};

// 1. who is offered the console
{
    const developer = consoleEnv(DEVELOPER);
    const admin = consoleEnv(HEAD_ADMIN);
    const nav = (env) => env.evaluate(
        "adminNavGroups().reduce((all, group) => all.concat(group.tabs.map((tab) => tab.id)), [])"
    );
    results.audience = {
        developer_nav: nav(developer),
        developer_visible: developer.evaluate("adminVisibleTabs().map((tab) => tab.id)"),
        admin_nav: nav(admin),
        admin_visible: admin.evaluate("adminVisibleTabs().map((tab) => tab.id)"),
        inventory: admin.evaluate("ADMIN_TABS.map((tab) => tab.id)")
    };
    // Asked for by id anyway, an administrator lands on the first tab it *is* offered.
    await admin.evaluate("UI.renderAdminTab('Developer')");
    results.audience.admin_landed = admin.evaluate("State.adminTab");
    results.audience.admin_asked = requests.filter((r) => r.url.indexOf('/developer/') >= 0).length;
}

// 2. opening it reads the four surfaces and paints them
{
    const env = consoleEnv(DEVELOPER);
    await env.evaluate("UI.renderAdminTab('Developer')");
    const page = markup(env);
    results.render = {
        page_flag: page.indexOf('data-developer-console="true"') >= 0,
        sections: ['runtime', 'alerts', 'diagnostics', 'audit'].filter(
            (name) => page.indexOf('data-dev-section="' + name + '"') >= 0
        ),
        runtime_rows: ['maintenance_mode', 'log_level', 'auth_anomaly_alerts'].filter(
            (key) => page.indexOf('data-runtime="' + key + '"') >= 0
        ),
        alert_cards: (page.match(/data-dev-alert="\d+"/g) || []).length,
        unread_chip: page.indexOf('data-dev-alert-read="9"') >= 0,
        read_chip_on_8: /data-dev-alert="8"[\s\S]*?data-dev-alert-read-chip/.test(page),
        audit_rows: (page.match(/<tr>[\s\S]*?<\/tr>/g) || []).length,
        pool_field: page.indexOf('journal_mode') >= 0 && page.indexOf('wal') >= 0,
        slow_row: text(env).indexOf('312.5') >= 0,
        explain_buttons: ['roster', 'audit_recent', 'open_notes'].filter(
            (name) => page.indexOf('data-dev-explain="' + name + '"') >= 0
        ).length,
        flush_button: page.indexOf('data-dev-flush="true"') >= 0,
        endpoints: ['/developer/runtime', '/developer/alerts?limit=50',
                    '/developer/diagnostics/pool', '/developer/diagnostics/slow-queries?limit=20',
                    '/developer/audit?limit=100'].filter(
            (path) => requests.some((r) => r.url === path)
        ),
        // Every action is a data- hook, never an inline handler. A count over the *markup*
        // would be unreliable - the escaped hostile summary legitimately contains the
        // characters " onerror=" - so the source-level guard below is the real check.
        hooks: ['data-runtime-save', 'data-dev-alert-read', 'data-dev-explain',
                'data-dev-flush', 'data-dev-audit-reload'].filter(
            (hook) => page.indexOf(hook) >= 0
        )
    };
}

// 3. the controls act through the module's own methods (the delegated binding)
{
    const env = consoleEnv(DEVELOPER);
    await env.evaluate("UI.renderAdminTab('Developer')");
    requests.length = 0;
    await env.evaluate("UI_MODULES.markDevAlertRead('9')");
    await env.evaluate("UI_MODULES.flushDevCaches()");
    await env.evaluate("UI_MODULES.explainDevQuery('roster')");
    results.actions = {
        mark_read: requests.filter((r) => r.url === '/developer/alerts/9/read' && r.method === 'POST').length,
        flush: requests.filter((r) => r.url === '/developer/diagnostics/caches/flush' && r.method === 'POST').length,
        explain: requests.filter((r) => r.url === '/developer/diagnostics/query-plan/roster').length
    };
}

// 4. a summary the server wrote is escaped, never markup
{
    const env = consoleEnv(DEVELOPER);
    await env.evaluate("UI.renderAdminTab('Developer')");
    const page = markup(env);
    results.escape = {
        raw_tag_present: page.indexOf('<img src=x') >= 0,
        escaped_present: page.indexOf('&lt;img src=x') >= 0
    };
}

// 5. the tab and its key resolve in the reader's language
{
    const env = consoleEnv(DEVELOPER);
    results.i18n = {
        tab_label: env.evaluate("I18n.__('developerConsole')"),
        is_not_the_key: env.evaluate("I18n.__('developerConsole')") !== 'developerConsole',
        role_label: env.evaluate("I18n.__('roleDeveloper')"),
        runtime_heading: env.evaluate("I18n.__('devRuntime')")
    };
}
"""


@pytest.fixture(scope="module")
def results() -> dict:
    return frontend_vm.run(HARNESS)


def test_the_console_is_the_root_tiers_and_no_one_elses(results):
    audience = results["audience"]
    assert "Developer" in audience["developer_nav"], audience["developer_nav"]
    assert "Developer" in audience["developer_visible"]
    assert "Developer" not in audience["admin_nav"], (
        f"an administrator is offered the root tier's tools: {audience['admin_nav']}"
    )
    assert "Developer" not in audience["admin_visible"]
    assert "Developer" in audience["inventory"], (
        "the tab left the console's inventory: this is a change of audience, not a removal"
    )
    assert audience["admin_landed"] == "Live Ops", audience["admin_landed"]
    assert audience["admin_asked"] == 0, (
        "an administrator's console asked for a root-tier route it cannot read"
    )


def test_opening_it_reads_every_surface_and_paints_them(results):
    render = results["render"]
    assert render["page_flag"], "the panel did not mark itself"
    assert render["sections"] == ["runtime", "alerts", "diagnostics", "audit"], render["sections"]
    assert render["runtime_rows"] == ["maintenance_mode", "log_level", "auth_anomaly_alerts"]
    assert render["alert_cards"] == 3, render["alert_cards"]
    assert render["unread_chip"] and render["read_chip_on_8"], (
        "an unread alert must offer the action and a read one must not"
    )
    assert render["pool_field"], "the write-path counters were not drawn"
    assert render["slow_row"], "the recent slow statement is missing"
    assert render["explain_buttons"] == 3
    assert render["flush_button"]
    assert render["endpoints"] == [
        "/developer/runtime", "/developer/alerts?limit=50",
        "/developer/diagnostics/pool", "/developer/diagnostics/slow-queries?limit=20",
        "/developer/audit?limit=100",
    ], render["endpoints"]
    assert render["hooks"] == ["data-runtime-save", "data-dev-alert-read", "data-dev-explain",
                               "data-dev-flush", "data-dev-audit-reload"], (
        f"a control is missing its data- hook: {render['hooks']}"
    )


def test_each_control_acts_through_its_own_endpoint(results):
    actions = results["actions"]
    assert actions["mark_read"] == 1, "Mark read did not POST to /developer/alerts/{id}/read"
    assert actions["flush"] == 1, "Drop caches did not POST to /developer/diagnostics/caches/flush"
    assert actions["explain"] == 1, "Explain did not GET the named query's plan"


def test_a_summary_the_server_wrote_is_escaped(results):
    escape = results["escape"]
    assert not escape["raw_tag_present"], "a server summary reached the DOM as markup"
    assert escape["escaped_present"], "the escaped form of the summary is missing"


def test_the_tab_and_its_key_read_in_the_readers_language(results):
    i18n = results["i18n"]
    assert i18n["is_not_the_key"], "the tab label is the raw key"
    assert i18n["tab_label"] and i18n["role_label"] and i18n["runtime_heading"]


def test_the_console_binds_its_controls_without_inline_handlers():
    """The panel's controls are data- hooks, not ``onclick`` attributes.

    The document CSP still allows ``script-src-attr 'unsafe-inline'``, and its per-file budget
    (``test_frontend_xss``) may only fall - so this section may not spend any of it. The check
    reads the section's own *source*, not the rendered markup: an escaped summary can contain
    the characters of a handler (a hostile ``onerror=``), and a check over markup would read
    that as one.
    """
    source = (PROJECT_ROOT / "frontend" / "admin_modules.js").read_text(encoding="utf-8")
    start = source.index("Developer - the root tier's own tools")
    # The block ends where the Admin management panel's own state begins, the method
    # that followed the role-label helper the console was inserted beside.
    end = source.index("_shiftRules: null,", start)
    section = source[start:end]
    handlers = re.findall(r"\son[a-z]+\s*=", section)
    assert handlers == [], f"the Developer console hand-rolled an inline handler: {handlers}"
    assert "bindDeveloperControls" in section, "nothing binds the panel's controls"
