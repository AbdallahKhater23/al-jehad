"""The root tier's own console: runtime flags, alerts, diagnostics and the audit stream.

WHY THIS SUITE EXISTS
---------------------
Four developer surfaces existed only as endpoints (``/developer/runtime``,
``/developer/alerts``, ``/developer/diagnostics/*``, ``/developer/audit``) - reachable, for the
person who runs the deployment, only with curl. The console gives them a screen, and the
diagnostic domains follow it there: the models and their decision lines (``/developer/ml/*``),
the database and its journal (``/developer/db/*``), the child process that owns the models
(``/developer/engine/*``) and the offline protocol's own forensics (``/developer/offline/*``).
What no backend test can see is the *client*: that the tab is offered to the root tier and
refused to an administrator, that opening it reads every route, that its controls are bound by
``data-`` hooks rather than inline ``onclick`` (the document CSP's per-file allowance may only
fall), and that a summary written by the server is escaped like any other server text.

The domains add a second thing worth pinning here rather than at the endpoint: two of their
reads are *acts* - a full ``PRAGMA integrity_check`` and a manual WAL checkpoint - and both are
buttons rather than part of the paint. The suite holds that line by reading the endpoint list
this tab really asks for.

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

const SESSIONS = [
    { id: '309010401073', name: 'Developer', role: 'developer', status: 'active', token_version: 1,
      last_login_at: '2026-09-26 22:48:00', last_login_ip: '10.0.0.1', active_devices: 0 },
    { id: '5000', name: 'Head Admin', role: 'head_admin', status: 'active', token_version: 3,
      last_login_at: '2026-09-26 21:00:00', last_login_ip: '10.0.0.2', active_devices: 2 },
    { id: '1', name: 'Seed Worker', role: 'worker', status: 'active', token_version: 2,
      last_login_at: null, last_login_ip: null, active_devices: 1 }
];

// The backup directory, with the three kinds of thing that can be in one. The hostile name is
// deliberate: a directory listing is server text like any other, and one of these names is the
// place a `<` reaches this panel.
const BACKUPS = {
    directory: '/srv/attendance/backups',
    exists: true,
    prefix: 'manual_dev',
    snapshots: 1,
    databases: 1,
    unverified: 1,
    count: 3,
    total: 3,
    total_bytes: 5242880,
    items: [
        {
            name: 'manual_dev_20260927_120000', kind: 'snapshot', status: 'PASS', prefix: 'manual_dev',
            created_at: '2026-09-27 12:00:00', age_hours: 30.4, size_bytes: 4194304, files: 412,
            integrity_check: 'ok', db_sha256: 'ab12', verifiable: true, notes: []
        },
        {
            name: 'backup_manual_dev_20260926_090000.db', kind: 'database', status: null, prefix: null,
            created_at: '2026-09-26 09:00:00', age_hours: 54.5, size_bytes: 1048576, files: 1,
            integrity_check: null, db_sha256: null, verifiable: false,
            notes: ['a single-file copy of the database, with no manifest to check it against']
        },
        {
            name: HOSTILE, kind: 'unverified', status: null, prefix: null,
            created_at: '2026-09-25 01:02:03', age_hours: 62.0, size_bytes: 2048, files: null,
            integrity_check: null, db_sha256: null, verifiable: false,
            notes: ['no readable VERIFY.json']
        }
    ],
    newest: { name: 'manual_dev_20260927_120000', age_hours: 30.4 }
};

// --- the diagnostic domains ------------------------------------------------

const ML = {
    engine: {
        capacity: 4, queue_depth: 8, queued: 1, in_flight: 1, submitted: 40, completed: 39,
        failed: 0, refused: 1, last_duration_ms: 180.5, slowest_ms: 410.2, busy: false,
        last_error: null, name: 'face-engine', wait_seconds: 0.25
    },
    detector: {
        available: true, reason: null, detector: 'yunet', pipeline: 'yunet+facenet128',
        intended: { detector: 'yunet', pipeline: 'yunet+facenet128' },
        fallback: { detector: 'haar', pipeline: 'haar+facenet128' },
        model_present: true, model_fingerprint: 'ab12cd34',
        crop: { aligned_size: 112, min_subject_px: 96, native_input_size: 640, tiles: 1 }
    },
    liveness: {
        mode: 'advisory', configured_mode: 'enforce', override: 'advisory', available: true,
        input_size: 128, accept_threshold: 0.9, reject_threshold: 0.2,
        model_path: '/srv/liveness.onnx', model_fingerprint: 'ff00'
    },
    liveness_override: 'advisory',
    liveness_modes: ['off', 'advisory', 'enforce'],
    active_band: {
        model: 'facenet128', approve: 0.36, review: 0.42, derived_approve: 0.36,
        derived_review: 0.42, one_line: true, genuine_ceiling: 0.31, impostor_floor: 0.55,
        evidence: 'band-2026-08', basis: 'derived from genuine_ceiling x ratio'
    },
    active_band_error: null,
    bands: {
        facenet128: {
            model: 'facenet128', approve: 0.36, review: 0.42, derived_approve: 0.36,
            derived_review: 0.42, one_line: true, genuine_ceiling: 0.31, impostor_floor: 0.55,
            evidence: 'band-2026-08', basis: HOSTILE
        },
        facenet512: {
            model: 'facenet512', approve: 0.4, review: 0.35, derived_approve: 0.4,
            derived_review: 0.35, one_line: false, genuine_ceiling: 0.3, impostor_floor: 0.6,
            evidence: 'band-2026-09', basis: 'typed by hand during a migration'
        }
    }
};

// ``flip_ready`` is false and not null: the gate *was* evaluated and said no. The panel has to
// tell that apart from "not evaluated", which is the whole reason the field is three-valued.
const SHADOW = {
    configured: {
        log: '/srv/attendance/data/shadow_scores.db', log_present: true,
        shadow_model: '/srv/attendance/models/facenet512.onnx', shadow_model_present: true,
        contract: 'facenet512-v1'
    },
    started: true,
    gallery: {
        path: '/srv/attendance/assets/shadow_gallery.json', templates: 40, width: 512,
        model_id: 'facenet512', contract: 'facenet512-v1',
        band: { model: 'facenet512', approve: 0.4, review: 0.35, evidence: 'band-2026-09' },
        workers: 41
    },
    summary: {
        events: 4200, errors: 0, paired_samples: 4200, error_rate: 0.0,
        verdict_agreement: 0.9976, enforced_ms: 22.5, shadow_ms: 31.0
    },
    paired: {
        paired_samples: 4200, agreements: 4190, disagreements: 10,
        enforced_approvals: 4000, shadow_approvals: 3995
    },
    coverage: 0.9756,
    flip_ready: false,
    gate: { min_coverage: 0.98, min_paired_samples: 2000, max_error_rate: 0.005 },
    reason: 'coverage 0.9756 is under the 0.98 floor'
};

const DB = {
    counters: {
        connections_opened: 12, read_only_connections: 3, statements: 4021, slow_statements: 3,
        lock_waits: 1, lock_timeouts: 0, max_lock_wait_seconds: 0.012, busy_timeout_ms: 5000,
        slow_statement_threshold_ms: 250
    },
    scope: "statement counters are per process; the pragmas below are the shared database's",
    checkpoint_modes: ['PASSIVE', 'FULL', 'RESTART', 'TRUNCATE'],
    pragmas: {
        journal_mode: 'wal', wal_autocheckpoint: 1000, page_size: 4096, page_count: 828,
        freelist_count: 0, cache_size: -2000, synchronous: 1, foreign_keys: 0
    },
    wal: {
        busy: 0, pages_in_log: 41, frames_checkpointed: 41,
        bytes_in_log: 167936, report_is_a_passive_checkpoint: true
    }
};

const INTEGRITY = {
    schema: { ready: true, mode: 'detect', missing: [], extra: [], drifted: [] },
    schema_version: { expected: 34, current: 34, applied: [1, 2, 34], pending: [] },
    integrity: { ok: true, output: ['ok'], note: 'a full page-level check' }
};

const ENGINE = {
    enabled: true, started: true, name: 'face-worker', handler: 'face_worker.py', alive: true,
    pid: 4242, worker_pid: 4242, interpreter_pid: 4242, starts: 2, calls: 118, failures: 0,
    timeouts: 0, in_flight: 0, last_call_ms: 12.5, deadline_seconds: 30.0, last_error: null,
    ping_ms: 1.9, interpreter_python: '/srv/attendance/venv/bin/python', rss_pid: 4242,
    rss_bytes: 209715200
};

// In-process inference: the payload the flag is off produces. No separate process exists to
// restart, so the lever must not be drawn at all - the endpoint would answer 409.
const ENGINE_IN_PROCESS = {
    enabled: false, message: 'In-process inference active',
    engine: { capacity: 2, queue_depth: 4, queued: 0, in_flight: 0, busy: false }
};

const DEVICES = {
    window_hours: 72, since: '2026-09-24 05:00:00',
    devices: [
        {
            id: 3, device_id: 'dev-alpha', worker_id: '1', worker_name: 'Seed Worker',
            worker_role: 'worker', worker_status: 'active', key_epoch: 3,
            key_salt: { prefix: '012345', length: 16 }, created_at: '2026-09-20 09:00:00',
            last_seen_at: '2026-09-26 22:00:00', revoked_at: null,
            last_anchor_at: '2026-09-26 21:59:00'
        },
        {
            id: 2, device_id: HOSTILE, worker_id: null, worker_name: null, worker_role: null,
            worker_status: null, key_epoch: 1, key_salt: null, created_at: '2026-09-10 09:00:00',
            last_seen_at: null, revoked_at: '2026-09-12 10:00:00', last_anchor_at: null
        }
    ],
    anchors: { issued: 12, unconsumed: 2, consumed: 10 },
    note: 'key_salt is masked to its first six characters'
};

const TAMPER = {
    window_days: 7,
    tamper_codes: ['clock_tampered', 'invalid_monotonic_offset', 'bad_signature', 'replayed_nonce'],
    counts: { in_window: 2, tamper: 1, with_an_anchor: 1 },
    alerts: [
        {
            id: 7, client_punch_id: 'punch-tampered', device_id: 'dev-alpha', worker_id: '1',
            worker_name: 'Seed Worker', action: 'clock_in',
            client_timestamp: '2026-09-26 22:06:40', anchor_server_time: '2026-09-26 22:00:00',
            monotonic_offset_s: 90.0, client_offset_s: null, status: 'rejected',
            rejection_code: 'clock_tampered', flag_reason: HOSTILE, signature_version: 2,
            received_at: '2026-09-26 22:00:05', materialized_log_id: null, tamper: true,
            effective_time: '2026-09-26 22:01:30', skew_seconds: 310.0
        },
        {
            id: 6, client_punch_id: 'punch-flagged', device_id: 'dev-alpha', worker_id: '1',
            worker_name: 'Seed Worker', action: 'clock_out',
            client_timestamp: '2026-09-26 21:00:00', anchor_server_time: null,
            monotonic_offset_s: null, client_offset_s: null, status: 'rejected',
            rejection_code: 'low_confidence', flag_reason: null, signature_version: 2,
            received_at: '2026-09-26 21:00:01', materialized_log_id: null, tamper: false,
            effective_time: null, skew_seconds: null
        }
    ],
    note: 'a rejected offline punch was never materialised as attendance'
};

const requests = [];

// Whether the deployment under test runs the models in a child process at all. The engine
// endpoint's answer is the whole difference between a lever and a 409, so the suite drives both.
let inProcess = false;

function responders(url, init) {
    requests.push({ url: url.split('/api/v1')[1] || url, method: (init && init.method) || 'GET' });
    if (/\/developer\/db\/snapshot$/.test(url)) {
        return { status: 200, body: { status: 'success', name: 'manual_dev_20260927_130000', directory: '/srv/attendance/backups/manual_dev_20260927_130000', files: 413, size_bytes: 4200000, verified: 'PASS', include_assets: true } };
    }
    if (/\/developer\/db\/backups\/[^/]+\/verify$/.test(url)) {
        return { status: 200, body: { status: 'FAIL', name: 'manual_dev_20260927_120000', files_listed: 412, files_checked: 411, errors: ['hash mismatch: source/README.md'], warnings: [] } };
    }
    if (url.indexOf('/developer/db/backups') >= 0) return { status: 200, body: BACKUPS };
    if (/\/developer\/alerts\/\d+\/read$/.test(url)) return { status: 200, body: { id: 9, already_read: false } };
    if (url.indexOf('/developer/diagnostics/caches/flush') >= 0) return { status: 200, body: { flushed: [{ cache: 'runtime_config' }] } };
    if (url.indexOf('/developer/diagnostics/query-plan/') >= 0) {
        const name = url.split('/query-plan/')[1];
        return { status: 200, body: { name: name, plan: ['SCAN users USING INDEX'] } };
    }
    if (url.indexOf('/developer/diagnostics/pool') >= 0) return { status: 200, body: POOL };
    if (url.indexOf('/developer/diagnostics/slow-queries') >= 0) return { status: 200, body: SLOW };
    if (/\/developer\/ml\/liveness-mode$/.test(url)) {
        return { status: 200, body: {
            mode: 'advisory', previous: 'enforce', reason: 'the gate is refusing honest workers',
            version: 8, note: 'applies to every worker on its next read'
        } };
    }
    if (url.indexOf('/developer/ml/shadow-summary') >= 0) return { status: 200, body: SHADOW };
    if (url.indexOf('/developer/ml/diagnostics') >= 0) return { status: 200, body: ML };
    if (/\/developer\/db\/wal-checkpoint/.test(url)) {
        return { status: 200, body: {
            status: 'success', mode: 'PASSIVE', busy: 0, log_frames: 0, checkpointed_frames: 41,
            before_frames: 41, busy_before: 0, at: '2026-09-27 12:00:00'
        } };
    }
    if (url.indexOf('/developer/db/integrity') >= 0) return { status: 200, body: INTEGRITY };
    if (url.indexOf('/developer/db/stats') >= 0) return { status: 200, body: DB };
    if (/\/developer\/engine\/restart-worker$/.test(url)) {
        return { status: 200, body: {
            status: 'success', old_pid: 101, new_pid: 202, old_worker_pid: 101,
            new_worker_pid: 202, ping_error: null, restarted_at: '2026-09-27 12:00:00'
        } };
    }
    if (url.indexOf('/developer/engine/process-stats') >= 0) {
        return { status: 200, body: inProcess ? ENGINE_IN_PROCESS : ENGINE };
    }
    if (url.indexOf('/developer/offline/devices') >= 0) return { status: 200, body: DEVICES };
    if (url.indexOf('/developer/offline/tamper-alerts') >= 0) {
        // A worker id that matches nothing: the endpoint's own honest empty answer.
        return { status: 200, body: /worker_id=999999/.test(url)
            ? { ...TAMPER, counts: { in_window: 0, tamper: 0, with_an_anchor: 0 }, alerts: [] }
            : TAMPER };
    }
    if (url.indexOf('/developer/runtime') >= 0) return { status: 200, body: RUNTIME };
    if (url.indexOf('/developer/sessions') >= 0) return { status: 200, body: SESSIONS };
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
        sections: ['runtime', 'alerts', 'diagnostics', 'ml', 'database', 'engine', 'offline',
                   'backups', 'sessions', 'audit'].filter(
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
        // What the paint *reads*, and the two reads it deliberately does not make: the full
        // integrity check and the WAL checkpoint are buttons ("/developer/db/integrity" and
        // the checkpoint POST appear in no paint, which the actions section drives instead).
        endpoints: ['/developer/runtime', '/developer/alerts?limit=50',
                    '/developer/diagnostics/pool', '/developer/diagnostics/slow-queries?limit=20',
                    '/developer/audit?limit=100', '/developer/db/backups?limit=50',
                    '/developer/sessions', '/developer/ml/diagnostics',
                    '/developer/ml/shadow-summary', '/developer/db/stats',
                    '/developer/engine/process-stats', '/developer/offline/devices',
                    '/developer/offline/tamper-alerts'].filter(
            (path) => requests.some((r) => r.url === path)
        ),
        paint_acts: ['/developer/db/integrity', '/developer/db/wal-checkpoint',
                     '/developer/engine/restart-worker'].filter(
            (path) => requests.some((r) => r.url === path)
        ),
        // Every action is a data- hook, never an inline handler. A count over the *markup*
        // would be unreliable - the escaped hostile summary legitimately contains the
        // characters " onerror=" - so the source-level guard below is the real check.        // Every action is a data- hook, never an inline handler. A count over the *markup*
        // would be unreliable - the escaped hostile summary legitimately contains the
        // characters " onerror=" - so the source-level guard below is the real check.
        hooks: ['data-runtime-save', 'data-dev-session-revoke', 'data-dev-alert-read', 'data-dev-explain',
                'data-dev-flush', 'data-dev-backup-now', 'data-dev-backup-verify',
                'data-dev-audit-reload', 'data-dev-liveness-apply', 'data-dev-shadow-evaluate',
                'data-dev-checkpoint', 'data-dev-integrity', 'data-dev-engine-restart',
                'data-dev-tamper-filter'].filter(
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

// 6. the tab wears the developer glyph, and the force-in roster never offers the root tier
{
    const env = consoleEnv(DEVELOPER);
    await env.evaluate("UI.renderAdminTab('Developer')");
    const nav = env.evaluate(
        "adminVisibleTabs().filter((tab) => tab.id === 'Developer').map((tab) => tab.icon)[0]"
    );
    results.icon = {
        icon_key: nav,
        // The icon set carries the glyph, and it is not the Admin tab's sliders: two tabs
        // wearing one picture read as one tab twice.
        glyph_defined: env.evaluate("typeof ADMIN_ICONS.developer === 'string'").toString(),
        is_brackets: env.evaluate("ADMIN_ICONS.developer.indexOf('m8 7-5 5 5 5') >= 0").toString(),
        not_the_admin_icon: env.evaluate("ADMIN_ICONS.developer !== ADMIN_ICONS.admin").toString(),
        svg: env.evaluate("ADMIN_ICONS.developer.indexOf('<svg') === 0").toString(),
        aria_hidden: env.evaluate("ADMIN_ICONS.developer.indexOf('aria-hidden') >= 0").toString()
    };

    // The force-in roster: the developer must be filtered out like the head admin is.
    const users = [
        { id: '1', name: 'Seed Worker', role: 'worker', status: 'active' },
        { id: '1000', name: 'Seed Admin', role: 'admin', status: 'active' },
        { id: '5000', name: 'Head Admin', role: 'head_admin', status: 'active' },
        { id: '309010401073', name: 'Developer', role: 'developer', status: 'active' }
    ];
    const sites = [{ site_name: 'Downtown Tower A' }];
    const panel = env.evaluate(
        "UI.forceInPanelHtml([], " + JSON.stringify(users) + ", " + JSON.stringify(sites) + ")"
    );
    const offered = (panel.match(/<option value="([^"]*)"/g) || [])
        .map((attr) => /value="([^"]*)"/.exec(attr)[1]);
    results.force_in_roster = {
        offered: offered,
        developer_listed: offered.indexOf('309010401073') >= 0,
        head_admin_listed: offered.indexOf('5000') >= 0,
        worker_listed: offered.indexOf('1') >= 0,
        admin_listed: offered.indexOf('1000') >= 0
    };
}

// 7. the backups section: the button, the list, and the three kinds told apart
{
    const env = consoleEnv(DEVELOPER);
    await env.evaluate("UI.renderAdminTab('Developer')");
    const page = markup(env);
    const section = page.slice(
        page.indexOf('data-dev-section="backups"'),
        page.indexOf('data-dev-section="sessions"')
    );
    const rows = (section.match(/data-dev-backup="([^"]*)"/g) || [])
        .map((attr) => /data-dev-backup="([^"]*)"/.exec(attr)[1]);
    const kinds = (section.match(/data-dev-backup-kind="([^"]*)"/g) || [])
        .map((attr) => /data-dev-backup-kind="([^"]*)"/.exec(attr)[1]);
    results.backups = {
        section: section.length > 0,
        rows: rows.length,
        kinds: kinds,
        now_button: section.indexOf('data-dev-backup-now="true"') >= 0,
        assets_box: /data-dev-backup-assets="true" checked/.test(section),
        // The lever is offered on the snapshot and not on the file that has no manifest behind it.
        verify_button: section.indexOf('data-dev-backup-verify="manual_dev_20260927_120000"') >= 0,
        verify_on_the_copy: section.indexOf('data-dev-backup-verify="backup_manual_dev_20260926_090000.db"') >= 0,
        no_manifest_note: section.indexOf('no manifest') >= 0,
        // What each row claims: the snapshot's recorded verdict, and an em dash where a row has
        // no verdict to claim.
        pass_badge: /ui-badge is-ok"[^>]*>PASS/.test(section) || /is-ok"[^>]*>\s*PASS/.test(section),
        badged_rows: (section.match(/<span class="ui-badge/g) || []).length,
        verdict_dashes: (section.match(/\u2014/g) || []).length,
        // The three facts an operator reads first: where the directory is, what is in it, and
        // how old the newest one is.
        directory_attr: /data-dev-backups-dir="\/srv\/attendance\/backups"/.test(section),
        counts: section.indexOf('Snapshots: 1. Database copies: 1. No verdict: 1.') >= 0,
        total_bytes: section.indexOf('5.0 MB in total') >= 0,
        size_column: section.indexOf('4.0 MB') >= 0,
        stale_warning: section.indexOf('data-dev-backup-stale="true"') >= 0,
        stale_hours: section.indexOf('30 hours old') >= 0,
        report_line: section.indexOf('data-dev-backup-report="true"') >= 0,
        verify_hint: section.indexOf('re-reads every file') >= 0
    };

    // The two controls, driven the way the delegated binding drives them.
    requests.length = 0;
    await env.evaluate("UI_MODULES.takeDevSnapshot()");
    results.backup_actions = {
        posts: requests.filter((r) => r.url === '/developer/db/snapshot' && r.method === 'POST').length
    };
    requests.length = 0;
    await env.evaluate("UI_MODULES.verifyDevBackup('manual_dev_20260927_120000')");
    results.backup_verify = {
        posts: requests.filter((r) =>
            r.url === '/developer/db/backups/manual_dev_20260927_120000/verify' && r.method === 'POST'
        ).length
    };
}

// 8. the sessions section: one row per account, a revoke lever per row
{
    const env = consoleEnv(DEVELOPER);
    await env.evaluate("UI.renderAdminTab('Developer')");
    const page = markup(env);
    const section = page.slice(
        page.indexOf('data-dev-section="sessions"'),
        page.indexOf('data-dev-section="audit"')
    );
    const revokeIds = (section.match(/data-dev-session-revoke="([^"]*)"/g) || [])
        .map((attr) => /data-dev-session-revoke="([^"]*)"/.exec(attr)[1]);
    results.sessions = {
        section_present: section.indexOf('ui-note') >= 0,
        row_keys: (section.match(/data-dev-session="([^"]*)"/g) || []).length,
        revoke_ids: revokeIds,
        self_marked: section.indexOf('data-dev-session-self="true"') >= 0,
        worker_listed: section.indexOf('Seed Worker') >= 0,
        head_admin_listed: section.indexOf('Head Admin') >= 0,
        dash_for_never_signed_in: section.indexOf('\u2014') >= 0
    };

    // The action, driven the way the binding drives it.
    requests.length = 0;
    await env.evaluate("UI_MODULES.revokeDevSession('1')");
    results.session_revoke = {
        asked: requests.filter((r) => r.url === '/developer/sessions/1/revoke' && r.method === 'POST').length
    };
}

// 9. the diagnostic domains: what each of the four panels drew, from the payloads above
{
    const env = consoleEnv(DEVELOPER);
    await env.evaluate("UI.renderAdminTab('Developer')");
    const page = markup(env);
    const slice = (from, to) => {
        const start = page.indexOf('data-dev-section="' + from + '"');
        const end = page.indexOf('data-dev-section="' + to + '"');
        return start < 0 ? '' : page.slice(start, end < 0 ? page.length : end);
    };
    const ml = slice('ml', 'database');
    const database = slice('database', 'engine');
    const engine = slice('engine', 'offline');
    const offline = slice('offline', 'backups');
    const values = (block, selector) => {
        const found = new RegExp(selector + '">([\\s\\S]*?)</select>').exec(block);
        return found ? (found[1].match(/value="[^"]*"/g) || [])
            .map((attr) => /value="([^"]*)"/.exec(attr)[1]) : null;
    };
    results.domains = {
        sections: [ml, database, engine, offline].map((block) => block.length > 0),
        // The models: every band in the table, the live one marked, and the mode select drawn
        // from the vocabulary the endpoint published rather than from three words spelled out
        // in the frontend.
        bands: (ml.match(/data-dev-band="[^"]*"/g) || [])
            .map((attr) => /data-dev-band="([^"]*)"/.exec(attr)[1]),
        active_bands: (ml.match(/data-dev-band="[^"]*" data-dev-band-active="1"/g) || [])
            .map((attr) => /data-dev-band="([^"]*)"/.exec(attr)[1]),
        two_line_warning: ml.indexOf('review line is above its approve line') >= 0,
        liveness_options: values(ml, 'data-dev-liveness-mode="true'),
        liveness_selected: /<option value="advisory" selected>/.test(ml),
        liveness_reason_box: ml.indexOf('data-dev-liveness-reason="true"') >= 0,
        // ``flip_ready`` is false here, not null: the gate *was* evaluated and said no.
        gate: (/data-dev-shadow-gate="([^"]*)"/.exec(ml) || [null, null])[1],
        gate_words: ml.indexOf('Not ready') >= 0,
        gate_words_absent: ml.indexOf('Not evaluated') >= 0,
        shadow_facts: ['paired_samples', 'verdict_agreement'].filter((k) => ml.indexOf(k) >= 0),
        gallery_box: ml.indexOf('data-dev-shadow-gallery="true"') >= 0,
        detector_facts: ['pipeline', 'min_subject_px'].filter((k) => ml.indexOf(k) >= 0),
        liveness_facts: ['configured_mode', 'reject_threshold'].filter((k) => ml.indexOf(k) >= 0),
        // The database: the journal, the mode select drawn from the endpoint's own tuple, and the
        // full check present as a *button* with nowhere-but-the-report for its answer.
        checkpoint_options: values(database, 'data-dev-checkpoint-mode="true'),
        checkpoint_default: /<option value="TRUNCATE" selected>/.test(database),
        wal_facts: ['pages_in_log', 'frames_checkpointed', 'report_is_a_passive_checkpoint']
            .filter((k) => database.indexOf(k) >= 0),
        counters: ['statements', 'lock_timeouts'].filter((k) => database.indexOf(k) >= 0),
        scope_note: database.indexOf('per process') >= 0,
        pragmas: ['journal_mode', 'wal_autocheckpoint'].filter((k) => database.indexOf(k) >= 0),
        integrity_button: database.indexOf('data-dev-integrity="true"') >= 0,
        integrity_report: database.indexOf('data-dev-integrity-report="true"') >= 0,
        integrity_not_painted: database.indexOf('schema_version') >= 0,
        // The engine child: its own counters, the ping, and the one lever.
        engine_state: (/data-dev-engine-state="([^"]*)"/.exec(engine) || [null, null])[1],
        engine_flags: ['alive', 'ping_ms', 'in_flight'].filter((k) => engine.indexOf(k) >= 0),
        engine_restart: engine.indexOf('data-dev-engine-restart="true"') >= 0,
        engine_rss: engine.indexOf('200.0 MB') >= 0,
        // Offline: two ledgers, the anchor counts, the masked salt, and the filter.
        device_cards: (offline.match(/data-dev-device="[^"]*"/g) || []).length,
        device_revoked: (offline.match(/data-dev-device-revoked="1"/g) || []).length,
        salt_masked: offline.indexOf('012345\u2026 (16)') >= 0,
        salt_whole: offline.indexOf('0123456789abcdef') >= 0,
        anchors: offline.indexOf('12 issued, 10 consumed, 2 never used') >= 0,
        tamper_cards: (offline.match(/data-dev-tamper="[^"]*"/g) || []).length,
        tamper_flagged: (offline.match(/data-dev-tamper-flag="1"/g) || []).length,
        tamper_skew: offline.indexOf('skew 310s') >= 0,
        tamper_counts: offline.indexOf('2 in the window, 1 of them tampering, 1 carrying an anchor') >= 0,
        filter_box: offline.indexOf('data-dev-tamper-worker="true"') >= 0,
        // Server text is escaped in these panels too: a band's basis, a device id and a flag
        // reason all arrive from the payload.
        escaped: ml.indexOf('&lt;img src=x') >= 0 && offline.indexOf('&lt;img src=x') >= 0,
        raw_tag: ml.indexOf('<img src=x') >= 0 || offline.indexOf('<img src=x') >= 0
    };
}

// 9b. in-process inference: there is no child, so there is nothing to replace and no lever
{
    inProcess = true;
    const env = consoleEnv(DEVELOPER);
    await env.evaluate("UI.renderAdminTab('Developer')");
    const page = markup(env);
    const engine = page.slice(
        page.indexOf('data-dev-section="engine"'),
        page.indexOf('data-dev-section="offline"')
    );
    results.engine_in_process = {
        state: (/data-dev-engine-state="([^"]*)"/.exec(engine) || [null, null])[1],
        note: engine.indexOf('in-process') >= 0,
        server_message: engine.indexOf('data-dev-engine-message="true"') >= 0,
        restart: engine.indexOf('data-dev-engine-restart="true"') >= 0,
        queue_facts: (engine.match(/ui-fact-value/g) || []).length
    };
    inProcess = false;
}

// 10. the levers: what each one sends, and what the two deliberate reads answer with
{
    const env = consoleEnv(DEVELOPER);
    await env.evaluate("UI.renderAdminTab('Developer')");
    // The stub DOM answers ``querySelector`` with null, and these controls read the box they
    // were drawn beside. A small map stands in for the page while the levers are driven.
    await env.evaluate(`window.__fields = {
        '[data-dev-liveness-mode]': { value: 'advisory' },
        '[data-dev-liveness-reason]': { value: 'the gate is refusing honest workers' },
        '[data-dev-shadow-gallery]': { value: '/srv/attendance/assets/shadow_gallery.json' },
        '[data-dev-checkpoint-mode]': { value: 'PASSIVE' },
        '[data-dev-tamper-worker]': { value: '1' },
        '[data-dev-integrity]': { disabled: false, textContent: '' },
        '[data-dev-integrity-report]': { textContent: '' },
        '[data-dev-shadow-report]': { textContent: '' },
        '[data-dev-tamper-block]': { innerHTML: '' }
    };
    document.querySelector = (selector) => window.__fields[selector] || null;`);
    requests.length = 0;
    await env.evaluate('UI_MODULES.moveDevLivenessMode()');
    const liveness = requests.filter((r) => r.url === '/developer/ml/liveness-mode');
    // The request *body*, which the harness's own recorder does not keep: ``env.requests`` is the
    // VM's fetch stub, and it is the one that sees the payload the app actually sent.
    const livenessSent = env.requests.filter(
        (r) => r.url.indexOf('/developer/ml/liveness-mode') >= 0
    );
    requests.length = 0;
    await env.evaluate('UI_MODULES.checkpointDevWal()');
    const checkpoint = requests.filter((r) => r.url.indexOf('/developer/db/wal-checkpoint') === 0);
    requests.length = 0;
    await env.evaluate('UI_MODULES.runDevIntegrityCheck()');
    const integrity = requests.filter((r) => r.url === '/developer/db/integrity');
    requests.length = 0;
    await env.evaluate('UI_MODULES.evaluateDevShadowGate()');
    const shadow = requests.filter((r) => r.url.indexOf('/developer/ml/shadow-summary') === 0);
    requests.length = 0;
    await env.evaluate('UI_MODULES.restartDevEngine()');
    const restart = requests.filter((r) => r.url === '/developer/engine/restart-worker');
    requests.length = 0;
    env.evaluate("window.__fields['[data-dev-tamper-worker]'].value = '999999'");
    await env.evaluate('UI_MODULES.filterDevTamperAlerts()');
    const filtered = requests.filter((r) => r.url.indexOf('/developer/offline/tamper-alerts') === 0);
    const narrowedBlock = env.evaluate(
        "document.querySelector('[data-dev-tamper-block]').innerHTML"
    );
    const typed = env.evaluate("window.__fields['[data-dev-tamper-worker]'].value");
    // And clearing the box is the other thing the control is for: the whole window again.
    requests.length = 0;
    env.evaluate("window.__fields['[data-dev-tamper-worker]'].value = ''");
    await env.evaluate('UI_MODULES.filterDevTamperAlerts()');
    const cleared = requests.filter((r) => r.url.indexOf('/developer/offline/tamper-alerts') === 0);
    const clearedBlock = env.evaluate(
        "document.querySelector('[data-dev-tamper-block]').innerHTML"
    );
    results.levers = {
        liveness_method: liveness[0] && liveness[0].method,
        liveness_body: livenessSent[0] && livenessSent[0].body
            ? JSON.parse(livenessSent[0].body) : null,
        liveness_header: livenessSent[0] && livenessSent[0].headers
            && livenessSent[0].headers['Content-Type'],
        checkpoint_url: checkpoint[0] && checkpoint[0].url,
        checkpoint_method: checkpoint[0] && checkpoint[0].method,
        integrity_url: integrity[0] && integrity[0].url,
        integrity_method: integrity[0] && integrity[0].method,
        integrity_report: env.evaluate(
            "document.querySelector('[data-dev-integrity-report]').textContent"
        ),
        shadow_url: shadow[0] && shadow[0].url,
        shadow_report: env.evaluate(
            "document.querySelector('[data-dev-shadow-report]').textContent"
        ),
        restart_url: restart[0] && restart[0].url,
        restart_method: restart[0] && restart[0].method,
        filtered_url: filtered[0] && filtered[0].url,
        // The box keeps what was typed: the question and the narrowed list are one answer.
        filtered_box: typed,
        filtered_rows: (narrowedBlock.match(/data-dev-tamper="/g) || []).length,
        filtered_empty: narrowedBlock.indexOf('Nothing in this window') >= 0,
        cleared_url: cleared[0] && cleared[0].url,
        cleared_rows: (clearedBlock.match(/data-dev-tamper="/g) || []).length
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
    # The first tab this reader is offered, which is the Dashboard since it became the
    # console's front door. What is pinned is that an unoffered tab id falls back to a tab
    # the session *does* have, not which tab happens to be first.
    assert audience["admin_landed"] == "Dashboard", audience["admin_landed"]
    assert audience["admin_asked"] == 0, (
        "an administrator's console asked for a root-tier route it cannot read"
    )


def test_opening_it_reads_every_surface_and_paints_them(results):
    render = results["render"]
    assert render["page_flag"], "the panel did not mark itself"
    assert render["sections"] == [
        "runtime", "alerts", "diagnostics", "ml", "database", "engine", "offline",
        "backups", "sessions", "audit"
    ], render["sections"]
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
        "/developer/audit?limit=100", "/developer/db/backups?limit=50",
        "/developer/sessions", "/developer/ml/diagnostics",
        "/developer/ml/shadow-summary", "/developer/db/stats",
        "/developer/engine/process-stats", "/developer/offline/devices",
        "/developer/offline/tamper-alerts",
    ], render["endpoints"]
    assert render["hooks"] == ["data-runtime-save", "data-dev-session-revoke", "data-dev-alert-read", "data-dev-explain",
                               "data-dev-flush", "data-dev-backup-now", "data-dev-backup-verify",
                               "data-dev-audit-reload", "data-dev-liveness-apply",
                               "data-dev-shadow-evaluate", "data-dev-checkpoint", "data-dev-integrity",
                               "data-dev-engine-restart", "data-dev-tamper-filter"], (
        f"a control is missing its data- hook: {render['hooks']}"
    )
    # And what the paint deliberately does *not* do. Two of these routes are acts rather than
    # observations - a full ``PRAGMA integrity_check`` reads every page of the database, and a
    # checkpoint blocks writers - and a tab paint that made either call would spend that on
    # every visit. They are buttons, and this is the assertion that keeps them buttons.
    assert render["paint_acts"] == [], (
        f"the tab paint performed a deliberate act: {render['paint_acts']}"
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


def test_the_developer_tab_wears_the_developer_glyph(results):
    """The root tier's own mark, and never the Admin tab's sliders a second time."""
    icon = results["icon"]
    assert icon["icon_key"] == "developer", icon
    assert icon["glyph_defined"] == "true"
    assert icon["is_brackets"] == "true", "the glyph is the angle brackets a developer reads"
    assert icon["not_the_admin_icon"] == "true", "two tabs must not wear one picture"
    assert icon["svg"] == "true", "icons are inline SVG (the icon-set rule), never emoji"
    assert icon["aria_hidden"] == "true", "and decorative, announced by the label beside it"


def test_the_force_in_roster_never_offers_the_developer(results):
    """The root tier is not a rota, on screen exactly as the server refuses it.

    A button that always answers 403 is a trap; the server is the enforcement and the
    roster is the courtesy - the same split as the head admin's exclusion above it.
    """
    roster = results["force_in_roster"]
    assert roster["worker_listed"], "a worker is exactly who the panel exists to offer"
    assert roster["admin_listed"], "an administrator is offered to the root tier"
    assert not roster["head_admin_listed"], "the head admin was never offered - unchanged"
    assert not roster["developer_listed"], "and the developer joins that exclusion"


def test_the_sessions_section_lists_every_account_with_its_lever(results):
    """One row per account that could hold a token, newest sign-in first."""
    sessions = results["sessions"]
    assert sessions["section_present"]
    assert sessions["row_keys"] == 3, sessions
    assert sessions["worker_listed"] and sessions["head_admin_listed"]
    # A worker who has never signed in is still on the list - that is the question it answers -
    # so the never-signed-in fields read as an em dash rather than the string "null".
    assert sessions["dash_for_never_signed_in"]
    # The root tier's own row marks its lever as self-revoking.
    assert sessions["self_marked"]


def test_revoking_a_session_asks_the_server_for_exactly_one_account(results):
    """The lever is per-row: one account per press, never a blast."""
    assert results["session_revoke"]["asked"] == 1


def test_the_backups_panel_offers_the_button_and_tells_its_three_kinds_apart(results):
    """A backup surface has one job before any of its controls: say what is in the directory.

    A snapshot written by ``tools/backup.py`` (source, database, assets, manifest, verdict), a
    single-file database copy, and a directory with no verdict at all are three different things
    to restore from, and the row is where an operator reads which one they are looking at.
    """
    backups = results["backups"]
    assert backups["section"], "the developer console drew no backups panel"
    assert backups["rows"] == 3, backups
    assert backups["kinds"] == ["snapshot", "database", "unverified"], backups["kinds"]
    assert backups["now_button"], "there is no way to take a snapshot from the console"
    assert backups["assets_box"], (
        "the assets choice is missing - a snapshot without the templates cannot restore a face"
    )
    # The Verify lever belongs on what has a manifest, and only on that: a button that always
    # answered 400 would be a trap, the same rule the force-in roster follows.
    assert backups["verify_button"], "the snapshot's row offers no Verify"
    assert not backups["verify_on_the_copy"], "a database copy was offered a verification it cannot have"
    assert backups["no_manifest_note"], "a row with nothing to check is not told so"
    # What each row claims about itself.
    assert backups["pass_badge"], f"the recorded verdict is not on the row: {backups}"
    assert backups["badged_rows"] >= 3, backups
    assert backups["verdict_dashes"] >= 2, (
        "a row with no verdict wears something other than the em dash a reader expects"
    )
    # Where the directory is, what is in it, how big and how old it is.
    assert backups["directory_attr"], backups
    assert backups["counts"], "the counts of each kind are not on the panel"
    assert backups["total_bytes"] and backups["size_column"], (
        "sizes are not shown as anything a person reads"
    )
    assert backups["stale_warning"] and backups["stale_hours"], (
        "a backup directory whose newest snapshot is a day old does not say so"
    )
    assert backups["report_line"] and backups["verify_hint"], (
        "a verification's answer has nowhere to appear"
    )


def test_the_snapshot_button_and_the_verify_lever_reach_their_own_endpoints(results):
    assert results["backup_actions"]["posts"] == 1, (
        "Take a snapshot did not POST to /developer/db/snapshot"
    )
    assert results["backup_verify"]["posts"] == 1, (
        "Verify did not POST to the named snapshot's verify route"
    )


# ---------------------------------------------------------------------------
# the diagnostic domains
# ---------------------------------------------------------------------------
def test_the_ml_panel_is_painted_from_the_engines_own_payload(results):
    """The models, the queue, the bands and the liveness policy - in one place.

    Two things here are not formatting. The band table marks the one the live pipeline decides
    by, because a table of four model ids is not an answer to "what is scoring my punches"; and
    the mode select is drawn from ``liveness_modes``, which the endpoint computes from
    ``liveness.MODES``. A frontend listing ``off/advisory/enforce`` itself would be a fourth
    copy of a three-word vocabulary that already has a test holding two of the copies together.
    """
    domains = results["domains"]
    assert domains["sections"] == [True, True, True, True], (
        "one of the four diagnostic sections was not drawn"
    )
    assert domains["bands"] == ["facenet128", "facenet512"], domains["bands"]
    assert domains["active_bands"] == ["facenet128"], (
        "the band the live pipeline decides by is not marked"
    )
    assert domains["two_line_warning"], (
        "a band whose review line is above its approve line is drawn as if the two were one"
    )
    assert domains["liveness_options"] == ["off", "advisory", "enforce"], domains["liveness_options"]
    assert domains["liveness_selected"], "the mode select does not open on the mode in force"
    assert domains["liveness_reason_box"], (
        "the reason the audit row is read for is not asked for before the endpoint refuses one"
    )
    assert domains["liveness_facts"] == ["configured_mode", "reject_threshold"], domains["liveness_facts"]
    assert domains["detector_facts"] == ["pipeline", "min_subject_px"], domains["detector_facts"]
    # The gate is three-valued and this payload says *not ready*, not "not evaluated": those
    # call for opposite next steps, so the panel may not flatten them into one word.
    assert domains["gate"] == "false", f"the gate's verdict is not on the panel: {domains['gate']}"
    assert domains["gate_words"], "the gate's verdict is not said in words"
    assert not domains["gate_words_absent"], (
        "an evaluated gate is being reported as unevaluated"
    )
    assert domains["shadow_facts"] == ["paired_samples", "verdict_agreement"], domains["shadow_facts"]
    assert domains["gallery_box"], (
        "the gate cannot be asked to run without somewhere to name its gallery"
    )


def test_the_database_panel_reads_the_journal_and_gates_the_full_check(results):
    """The cheap reads are painted; the one that walks every page is a button.

    ``PRAGMA integrity_check`` is a full read of the database - seconds on a large one - and a
    tab paint is not the place for it. This is the same judgement the backups panel makes about
    re-hashing a snapshot, and it is asserted rather than described: the paint's endpoint list
    (above) contains no integrity read, and this section's report line is where the answer goes.
    """
    domains = results["domains"]
    assert domains["checkpoint_options"] == ["PASSIVE", "FULL", "RESTART", "TRUNCATE"], (
        f"the checkpoint select is not the tuple the endpoint accepts: {domains['checkpoint_options']}"
    )
    assert domains["checkpoint_default"], (
        "the select must open on TRUNCATE, the mode that gives the disk space back"
    )
    assert domains["wal_facts"] == [
        "pages_in_log", "frames_checkpointed", "report_is_a_passive_checkpoint"
    ], domains["wal_facts"]
    assert domains["counters"] == ["statements", "lock_timeouts"], domains["counters"]
    assert domains["scope_note"], (
        "the statement counters are drawn as the deployment's rather than one process's"
    )
    assert domains["pragmas"] == ["journal_mode", "wal_autocheckpoint"], domains["pragmas"]
    assert domains["integrity_button"] and domains["integrity_report"], (
        "the full check has no lever, or nowhere to put its answer"
    )
    assert not domains["integrity_not_painted"], (
        "the tab paint ran the full integrity check"
    )


def test_the_engine_panel_offers_its_lever_only_where_there_is_a_child(results):
    """A restart lever against in-process inference would answer 409 every time.

    The panel must therefore draw two different screens, and the difference is not cosmetic:
    with no separate process there is nothing to replace, so the lever is absent and the queue
    the models are actually served from is shown instead. A button that always refuses is the
    trap the force-in roster and the snapshot's Verify lever already refuse to set.
    """
    domains = results["domains"]
    assert domains["engine_state"] == "alive", domains["engine_state"]
    assert domains["engine_flags"] == ["alive", "ping_ms", "in_flight"], domains["engine_flags"]
    assert domains["engine_restart"], "there is no way to replace a wedged model process"
    assert domains["engine_rss"], (
        "the child's resident set is not shown as a size a person reads"
    )
    in_process = results["engine_in_process"]
    assert in_process["state"] == "in_process", in_process
    assert in_process["note"], "the in-process state is not explained"
    assert in_process["server_message"], "the endpoint's own message was dropped"
    assert in_process["restart"] is False, (
        "the console drew a restart lever the endpoint answers 409 to"
    )
    assert in_process["queue_facts"] >= 3, (
        "with the models in-process, the queue they are served from is the load panel"
    )


def test_the_offline_panel_shows_two_ledgers_and_keeps_the_salt_masked(results):
    """Devices and refusals, told apart, with key material printed as the server sent it."""
    domains = results["domains"]
    assert domains["device_cards"] == 2, domains
    assert domains["device_revoked"] == 1, "a revoked device is not marked as one"
    assert domains["salt_masked"] and not domains["salt_whole"], (
        "the device ledger printed key material the endpoint had masked"
    )
    assert domains["anchors"], "the anchor ledger is not on the panel"
    assert domains["tamper_cards"] == 2, domains
    assert domains["tamper_flagged"] == 1, (
        "a tampering refusal and an ordinary rejection look the same on the panel"
    )
    assert domains["tamper_skew"], "the derived skew is not on the row it explains"
    assert domains["tamper_counts"], domains
    assert domains["filter_box"], "the tamper list cannot be narrowed to one worker"
    assert domains["escaped"] and not domains["raw_tag"], (
        "server text in a diagnostic panel reached the DOM as markup"
    )


def test_each_diagnostic_lever_reaches_its_own_endpoint(results):
    """Six controls, six calls - and the two answers a repaint would have thrown away."""
    levers = results["levers"]
    assert levers["liveness_method"] == "POST", levers
    assert levers["liveness_body"] == {
        "mode": "advisory", "reason": "the gate is refusing honest workers"
    }, levers
    assert levers["checkpoint_url"] == "/developer/db/wal-checkpoint?mode=PASSIVE", levers
    assert levers["checkpoint_method"] == "POST", levers
    assert levers["liveness_header"] == "application/json", levers["liveness_header"]
    assert levers["integrity_url"] == "/developer/db/integrity" and levers["integrity_method"] == "GET"
    assert levers["restart_url"] == "/developer/engine/restart-worker"
    assert levers["restart_method"] == "POST", levers
    assert levers["shadow_url"] == (
        "/developer/ml/shadow-summary?gallery=%2Fsrv%2Fattendance%2Fassets%2Fshadow_gallery.json"
    ), levers["shadow_url"]
    # The two deliberate reads answer where they were asked, not in a panel a repaint replaces:
    # a repaint would re-spend the read *and* bring back the answer the operator just stopped
    # relying on.
    assert "Schema version 34 of 34." in levers["integrity_report"], levers["integrity_report"]
    assert "Integrity check: ok" in levers["integrity_report"], levers["integrity_report"]
    assert "Not ready" in levers["shadow_report"], levers["shadow_report"]
    assert "coverage=0.9756" in levers["shadow_report"], levers["shadow_report"]
    # The filter is the *read*, not a view over one: the endpoint is asked for one worker, and
    # the answer replaces the ledger in place - the box keeps the question, so the list below it
    # is never ambiguous about which question it is answering.
    assert levers["filtered_url"] == "/developer/offline/tamper-alerts?worker_id=999999", levers
    assert levers["filtered_box"] == "999999", levers["filtered_box"]
    assert levers["filtered_rows"] == 0 and levers["filtered_empty"], levers
    # An empty box is the whole window, not a refused request.
    assert levers["cleared_url"] == "/developer/offline/tamper-alerts", levers["cleared_url"]
    assert levers["cleared_rows"] == 2, levers["cleared_rows"]
