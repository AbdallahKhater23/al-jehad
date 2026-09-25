"""Whose company this is: resolved from settings, drawn on every sheet.

WHY THIS SUITE EXISTS
---------------------
The wordmark, the legal suffix, the founding year, the tagline and the mark used to be a
constant in ``frontendjavascript.js`` and an SVG file beside it. So a company that renamed
itself, or wanted its own logo at the top of the sheet it hands to payroll, needed a code
change and a redeploy - and the login panel, which exists to answer "is this the app my
administrator told me about?", named whatever the constant said.

They are a settings row now (``company_settings``, edited on the console's Admin tab), and
``Brand`` is the one place that turns that row into what the app draws. What can only be
checked in a browser, and not in a backend test:

1. **The merge.** ``null`` is "nobody configured this" and the shipped line is used; ``''``
   is "the company removed this line" and nothing is drawn. A reader that flattened those
   would put a legal suffix back on the sheet of a company that deliberately took it off,
   which is why both states are driven here as well as in the backend suite.
2. **The boot, which must not wait.** The cache is applied before the first paint and the
   network answer only corrects what changed - and the repaint it causes is skipped when
   somebody has already started typing, because a company name is not worth losing a
   half-typed password.
3. **Offline.** A handset with no connection keeps the lockup it already had: the settings
   read failing is not a reason for the sign-in screen to say the wrong company.
4. **The panel.** The boxes show what is *in force*, saving sends exactly what is on screen
   (including an emptied line), the reset sends ``null``s, the logo upload is a multipart
   post and its refusals happen before the upload, not after it.
5. **The paper.** Every printed sheet - the console's period, a worker's month, the worker's
   own hours - opens with the company's name and mark, because that header lives in the
   shared frame rather than in either reader. That is the whole point of the feature: the
   document handed to payroll belongs to somebody.

Node is optional; without it these skip rather than fail.
"""

from __future__ import annotations

import pytest

import frontend_vm

pytestmark = pytest.mark.skipif(frontend_vm.NODE is None, reason="node is not installed")

HARNESS = r"""
// --- the server's answers, faked ----------------------------------------

const ADMIN = { id: '1000', name: 'Site Admin', role: 'admin' };

//: A company that has told us its own name. ``est`` is deliberately empty: the third line
//: of the lockup is one this company decided not to print, which is the state a fixture
//: with four filled-in lines can never exercise.
const CONFIGURED = {
    name: 'Al-Jehad Stone Works',
    legal: 'MARBLE & GRANITE',
    est: '',
    tagline: 'SINCE 1983',
    logo_url: '/branding/logo?v=7',
    logo: { version: 7, mime: 'image/png', width: 512, height: 300, bytes: 42111 },
    updated_at: '2026-09-19 10:00:00'
};

//: A company that has opened the panel and taken every line off except its name, and set no
//: mark. What is left has to print without a blank row where the other three were.
const LEAN = {
    name: 'Harbour Works',
    legal: '', est: '', tagline: '',
    logo_url: null, logo: null, updated_at: '2026-09-19 11:00:00'
};

//: What a deployment that never opened the panel answers: every line ``None``.
const SHIPPED = {
    name: null, legal: null, est: null, tagline: null,
    logo_url: null, logo: null, updated_at: null
};

//: Rows written before the boundary validated anything, or by a path that does not go
//: through it. Every interpolation has to escape them - the sheet is a document this
//: application writes.
const HOSTILE = {
    name: '<img src=x onerror=alert(1)>',
    legal: 'MARBLE & GRANITE',
    est: '', tagline: '', logo_url: null, logo: null, updated_at: null
};

//: The lockup the console's save answers with, so "the app adopts what the server stored"
//: is observable rather than assumed.
const RENAMED = Object.assign({}, CONFIGURED, { name: 'Renamed Works Ltd' });

//: The mark as the upload endpoint answers: a different version, so the URL changes.
const WITH_MARK = Object.assign({}, CONFIGURED, {
    logo_url: '/branding/logo?v=8',
    logo: { version: 8, mime: 'image/jpeg', width: 512, height: 512, bytes: 30333 }
});

let branding = SHIPPED;
let brandingStatus = 200;
let logoAnswer = WITH_MARK;
const posted = [];

function shift(overrides) {
    return Object.assign({
        log_id: 901, date: '2026-08-07', timestamp: '2026-08-07 16:02:11',
        worker_id: '600', worker_name: 'Seed Lead', role: 'moallem',
        site_name: 'Downtown Tower A', hours: 8, recorded_hours: 8.5, approved_hours: null,
        break_hours: 0.5, status_code: 'approved', status: 'Approved by Admin',
        awaiting_approval: false, open_notes: 0,
        arrival_time: '2026-08-07 04:20:00', arrival_verdict: 'on_time', arrival_minutes: 0
    }, overrides || {});
}

const ROWS = [
    shift({ log_id: 901, worker_id: '600', worker_name: 'Seed Lead' }),
    shift({ log_id: 902, date: '2026-08-06', worker_id: '601', worker_name: 'Ana Torres', role: 'worker', site_name: 'Harbour Depot', hours: 4 })
];

const TOTALS = {
    shifts: 2, workers: 2, hours: 12, approved_hours: 12, awaiting_approval_hours: 0,
    awaiting_approval: 0, break_hours: 1, workers_with_open_notes: 0, late_arrivals: 0
};

function shiftsBody(start, end) {
    return {
        period: { start: start, end: end },
        filters: { site: null, worker_id: null },
        fields: ['log_id', 'date', 'worker_id', 'worker_name', 'role', 'site_name', 'hours',
                 'recorded_hours', 'approved_hours', 'break_hours', 'status_code', 'status',
                 'awaiting_approval', 'open_notes', 'arrival_time', 'arrival_verdict', 'arrival_minutes'],
        note: 'A timesheet: one row per shift.',
        rows: ROWS,
        totals: TOTALS
    };
}

//: The worker's own report, shaped like the one the handset reads (see
//: ``test_frontend_admin_handset``): the columns travel with the rows they describe.
const MY_HOURS = {
    worker_id: '1000', worker_name: 'Site Admin',
    period: { start: '2026-09-01', end: '2026-09-19' },
    columns: ['date', 'site', 'arrival', 'break', 'hours', 'approved', 'status'],
    rows: [{
        log_id: 1, date: '2026-09-10', site_name: 'Downtown Tower A',
        hours: 8.0, recorded_hours: 8.0, approved_hours: 8.0, awaiting_approval: false,
        status: 'Approved by Admin', arrival_time: '04:55', arrival_verdict: 'on_time',
        arrival_minutes: 0, break_hours: 0.0
    }],
    by_site: [],
    totals: { shifts: 1, sites: 1, hours: 8, approved_hours: 8, awaiting_approval_hours: 0,
              awaiting_approval: 0, break_hours: 0, late_arrivals: 0, overtime_hours: 0 }
};

function responders(url, init) {
    const path = url.split('/api/v1')[1] || '';
    // Before the plain branding read, because this is a branding path with a tail.
    if (path.startsWith('/admin/branding/logo')) {
        posted.push({ path: path, method: (init && init.method) || 'GET', body: init && init.body });
        // The answer is the lockup the caller decides on, so "the app adopted what the
        // server stored" is observable rather than assumed - after an upload as well as
        // after a take-down.
        const answer = (init && init.method) === 'DELETE'
            ? { status: 'success', removed: true, branding: branding }
            : { status: 'success', branding: logoAnswer };
        return { status: 200, body: answer };
    }
    if (path.startsWith('/admin/branding')) {
        posted.push({ path: path, method: (init && init.method) || 'GET', body: init && init.body });
        return { status: brandingStatus < 400 ? 200 : brandingStatus, body: { status: 'success', branding: branding } };
    }
    if (path.startsWith('/branding')) return { status: brandingStatus, body: branding };
    if (path.includes('/admin/reports/shifts')) {
        const params = new URLSearchParams((url.split('?')[1] || ''));
        return { status: 200, body: shiftsBody(params.get('start'), params.get('end')) };
    }
    if (path.includes('/worker/me/report')) return { status: 200, body: MY_HOURS };
    if (path.includes('/worker/me/')) return { status: 200, body: {} };
    if (path.includes('/admin/shift_rules')) {
        return {
            status: 200,
            body: { regular_hours: 8, break_minutes: 30, break_after_hours: 4,
                    overtime_notify_hours: 8.1, auto_close_at_regular: 1, day_end: {} }
        };
    }
    if (path.includes('/developer/notifications')) return { status: 200, body: { unread: 0, notifications: [] } };
    if (path.includes('/admin/')) return { status: 200, body: [] };
    return { status: 200, body: {} };
}

async function bootAs(user) {
    const env = boot();
    env.setResponder(responders);
    if (user) {
        env.evaluate('State.saveUser(' + JSON.stringify({ ...user, token: 'tok-test' }) + ')');
        env.evaluate('Brand.restore()');
        await env.evaluate('Brand.load()');
        await env.evaluate('UI.renderApp()');
    }
    return env;
}

const loginMarkup = (env) => env.evaluate("document.getElementById('app').innerHTML");
const adminMarkup = (env) => env.evaluate("document.getElementById('adminContent').innerHTML");
const lockup = (env) => env.evaluate('Brand.fullName()');

const results = {};

// 1. the merge: the company's own words, and a line it removed
{
    branding = CONFIGURED;
    const env = boot();
    env.setResponder(responders);
    const changed = await env.evaluate('Brand.load()');
    env.evaluate('UI.renderApp()');
    const markup = loginMarkup(env);
    results.configured = {
        changed: changed,
        asked_for: env.requests.map((r) => r.url.split('/api/v1')[1]),
        name: env.evaluate('BRAND.name'),
        legal: env.evaluate('BRAND.legal'),
        est: env.evaluate('BRAND.est'),
        tagline: env.evaluate('BRAND.tagline'),
        // A configured mark is a path the API serves, resolved against the origin this
        // browser is actually talking to - not the file this app ships with.
        mark: env.evaluate('BRAND.mark'),
        api_base: env.evaluate('API.baseURL'),
        // What the login panel drew: the company's name, its suffix, its tagline...
        has_name: markup.indexOf(env.evaluate('UI.escapeHtml(BRAND.name)')) >= 0,
        has_legal: markup.indexOf(env.evaluate('UI.escapeHtml(BRAND.legal)')) >= 0,
        has_tagline: markup.indexOf('SINCE 1983') >= 0,
        has_the_mark: markup.indexOf(env.evaluate('BRAND.mark')) >= 0,
        // ...and not a paragraph for the line the company removed, nor the shipped wordmark.
        drew_blank_line: markup.indexOf('login-est') >= 0,
        named_the_old_company: markup.indexOf('AL-JEHAD') >= 0,
        footer: env.evaluate('UI.companyFooterHtml()')
    };
}

// 2. nothing configured: the shipped lockup, byte for byte
{
    branding = SHIPPED;
    const env = boot();
    env.setResponder(responders);
    await env.evaluate('Brand.load()');
    env.evaluate('UI.renderApp()');
    const markup = loginMarkup(env);
    results.shipped = {
        name: env.evaluate('BRAND.name'),
        legal: env.evaluate('BRAND.legal'),
        est: env.evaluate('BRAND.est'),
        tagline: env.evaluate('BRAND.tagline'),
        same_as_shipped: env.evaluate(
            "['name', 'legal', 'est', 'tagline', 'mark'].every((key) => BRAND[key] === BRAND_DEFAULTS[key])"),
        mark: env.evaluate('BRAND.mark'),
        // The four lines are all drawn, and the footer names the company in prose.
        lines: (markup.match(/login-(company|legal|est|tagline)/g) || []).length,
        footer: env.evaluate('UI.companyFooterHtml()')
    };
}

// 3. a company that removed every line but its name, and set no mark
{
    branding = LEAN;
    const env = boot();
    env.setResponder(responders);
    await env.evaluate('Brand.load()');
    env.evaluate('UI.renderApp()');
    const markup = loginMarkup(env);
    results.lean = {
        name: env.evaluate('BRAND.name'),
        mark: env.evaluate('BRAND.mark'),
        drew_only_the_name: (markup.match(/login-(company|legal|est|tagline)/g) || []).length === 1,
        has_the_name: markup.indexOf('Harbour Works') >= 0
    };
}

// 4. offline, with a cache: the last lockup that was known is what is drawn
{
    branding = CONFIGURED;
    const prime = boot();
    prime.setResponder(responders);
    await prime.evaluate('Brand.load()');
    const cached = prime.evaluate("localStorage.getItem('branding')");

    const env = boot();
    env.setResponder(responders);
    env.evaluate('localStorage.setItem(' + JSON.stringify('branding') + ', ' + JSON.stringify(cached) + ')');
    env.evaluate('Brand.restore()');
    brandingStatus = 503;
    const changed = await env.evaluate('Brand.load()');
    brandingStatus = 200;
    env.evaluate('UI.renderApp()');
    const markup = loginMarkup(env);
    results.offline = {
        restored: env.evaluate('BRAND.name'),
        load_changed_anything: changed,
        still_named: env.evaluate('BRAND.name'),
        // A dead settings read is not a reason for the sign-in screen to name the wrong
        // company: the cache is what the handset is holding.
        drawn: markup.indexOf('Al-Jehad Stone Works') >= 0
    };
}

// 5. the repaint, and the half-typed password it must not take away
{
    const fresh = boot();
    fresh.setResponder(responders);
    fresh.evaluate(`(function () {
        window.__renders = 0;
        UI.renderApp = () => { window.__renders += 1; };
    })()`);
    // An empty field: the brand's answer is worth a repaint.
    fresh.evaluate(`(function () {
        document.querySelectorAll = () => [];
        UI.repaintAfterBrand();
    })()`);
    const empty = fresh.evaluate('window.__renders');
    // A field somebody has typed into: the values stay, and the next paint is correct.
    const typing = boot();
    typing.setResponder(responders);
    typing.evaluate(`(function () {
        window.__renders = 0;
        UI.renderApp = () => { window.__renders += 1; };
        document.querySelectorAll = (selector) => (String(selector).indexOf('#app input') === 0 ? [{ value: 'site-admin' }] : []);
    })()`);
    results.repaint = {
        mid_entry: typing.evaluate('UI.isMidEntry()'),
        renders_when_typing: typing.evaluate('(function () { UI.repaintAfterBrand(); return window.__renders; })()'),
        renders_when_idle: empty
    };
}

// 6. the console's Company panel
{
    branding = CONFIGURED;
    const env = await bootAs(ADMIN);
    await env.evaluate("UI.renderAdminTab('Admin')");
    const first = adminMarkup(env);
    // What the boxes hold *before* anything is touched: what is in force, which for a line
    // nobody configured is the shipped one rather than an empty box.
    branding = SHIPPED;
    const shippedPanel = await bootAs(ADMIN);
    await shippedPanel.evaluate("UI.renderAdminTab('Admin')");
    branding = CONFIGURED;

    results.panel = {
        drawn: first.indexOf('data-company-panel') >= 0,
        title: env.evaluate("I18n.__('companyTitle')"),
        shown: first.indexOf(env.evaluate("I18n.__('companyTitle')")) >= 0,
        // The panel says what happens to a line left empty, because "empty" here is a
        // decision about the paper rather than a missing value.
        blank_note: first.indexOf(env.evaluate("I18n.__('companyBlankNote')")) >= 0,
        reset_button: first.indexOf('id="companyReset"') >= 0,
        logo_picker: first.indexOf('id="companyLogoInput"') >= 0,
        logo_preview_is_the_configured_mark: first.indexOf(env.evaluate('BRAND.mark')) >= 0,
        preview_note: first.indexOf(env.evaluate(
            "I18n.__('companyLogoConfigured').replace('{width}', '512').replace('{height}', '300').replace('{size}', '41 KB')")) >= 0,
        // A deployment that configured nothing gets the shipped mark, and is told so. The
        // sentence is escaped where it is drawn like every other string on this panel.
        shipped_mark_note: adminMarkup(shippedPanel).indexOf(
            env.evaluate("UI.escapeHtml(I18n.__('companyLogoShipped'))")) >= 0,
        shipped_preview: shippedPanel.evaluate('BRAND.mark')
    };
}

// 6b. saving, resetting, and the lockup the server answered with
{
    branding = CONFIGURED;
    const env = await bootAs(ADMIN);
    await env.evaluate("UI.renderAdminTab('Admin')");
    posted.length = 0;
    // What the boxes really hold on screen is the input's own value; the panel writes them
    // from what the server sent, and the save reads them back.
    env.evaluate(`(function () {
        document.getElementById('companyName').value = 'Renamed Works Ltd';
        document.getElementById('companyLegal').value = 'MARBLE & GRANITE';
        document.getElementById('companyEst').value = '';
        document.getElementById('companyTagline').value = 'SINCE 1983';
        return null;
    })()`);
    branding = RENAMED;  // what the server will answer with
    await env.evaluate('UI_MODULES.saveCompany()');
    const save = posted[posted.length - 1];
    const rail = env.evaluate("document.getElementById('app').innerHTML");

    results.save = {
        path: save.path,
        method: save.method,
        // Exactly what is on screen: strings, the empty one included - an emptied box is
        // "this line is not printed", and only the reset sends ``null``.
        body: JSON.parse(save.body),
        // The app adopted the server's answer and repainted with it.
        brand_name: env.evaluate('BRAND.name'),
        rail_renamed: rail.indexOf('Renamed Works Ltd') >= 0,
        toast: env.evaluate("document.getElementById('toastRoot').__children.map((el) => el.textContent)")
    };

    // The reset: four nulls, which is a different edit from four empty strings
    branding = CONFIGURED;
    posted.length = 0;
    await env.evaluate('UI_MODULES.resetCompanyLines()');
    results.reset = {
        body: JSON.parse(posted[posted.length - 1].body),
        toast: env.evaluate("document.getElementById('toastRoot').__children.map((el) => el.textContent)")
    };
}

// 6c. the mark: uploaded, refused before it is sent, and taken down again
{
    branding = CONFIGURED;
    const env = await bootAs(ADMIN);
    await env.evaluate("UI.renderAdminTab('Admin')");
    posted.length = 0;
    env.evaluate(`(function () {
        window.__logo = new Blob(['not really a png'], { type: 'image/png' });
        window.__logo.name = 'mark.png';
        return null;
    })()`);
    await env.evaluate('UI_MODULES.pickCompanyLogo({ files: [window.__logo] })');
    const upload = posted[posted.length - 1];

    // A file the policy refuses: no request at all, so a 6 MB image does not have to make
    // it over a site tether to be refused
    posted.length = 0;
    const refused = await env.evaluate(
        "UI_MODULES.pickCompanyLogo({ files: [{ name: 'huge.png', type: 'image/png', size: 6 * 1024 * 1024 }] })");

    results.logo = {
        upload_path: upload.path,
        upload_method: upload.method,
        is_multipart: String(upload.body && upload.body.constructor && upload.body.constructor.name) === 'FormData',
        url_after_upload: env.evaluate('BRAND.mark'),
        version_in_the_url: env.evaluate('BRAND.mark').indexOf('v=8') > 0,
        refused: refused === null,
        requests_when_refused: posted.length,
        refusal_toast: env.evaluate("document.getElementById('toastRoot').__children.map((el) => el.textContent)").slice(-1)
    };

    posted.length = 0;
    branding = SHIPPED;
    await env.evaluate('UI_MODULES.removeCompanyLogo()');
    const remove = posted[posted.length - 1];
    results.logo.remove = {
        path: remove.path,
        method: remove.method,
        back_to_the_shipped_mark: env.evaluate('BRAND.mark === BRAND_DEFAULTS.mark')
    };
}

// 7. the paper: the company's name and mark at the top of every sheet
{
    branding = CONFIGURED;
    const env = await bootAs(ADMIN);
    // The console's own period sheet, printed through the real path: the tab loads its
    // report, the print button builds the sheet, and the dialog opens on it.
    await env.evaluate("UI.renderAdminTab('Shifts')");
    await env.evaluate('UI_MODULES.printShiftsReport()');
    const consoleSheet = (env.printed[env.printed.length - 1] || {}).sheet || '';
    env.fireWindowEvent('afterprint');

    const range = { start: '2026-08-01', end: '2026-08-31' };
    const body = shiftsBody(range.start, range.end);
    const workerMonth = env.evaluate(
        'UI_MODULES.workerMonthSheetHtml(' + JSON.stringify(body) + ', "600")');
    const myHours = env.evaluate('WORKER_MODULES.myHoursPrintHtml(' + JSON.stringify(MY_HOURS) + ')');

    // The one-line lockup: ``legal`` set and the other two removed
    branding = LEAN;
    const lean = boot();
    lean.setResponder(responders);
    await lean.evaluate('Brand.load()');
    const leanSheet = lean.evaluate('WORKER_MODULES.myHoursPrintHtml(' + JSON.stringify(MY_HOURS) + ')');

    results.sheets = {
        console: consoleSheet,
        worker_month: workerMonth,
        my_hours: myHours,
        configured_name: env.evaluate('BRAND.name'),
        mark: env.evaluate('BRAND.mark'),
        url_in_sheet: consoleSheet.indexOf(env.evaluate('BRAND.mark')) >= 0,
        // The header is the first thing on the paper, above the report's own title
        brand_is_first: consoleSheet.indexOf('print-sheet-brand') >= 0
            && consoleSheet.indexOf('print-sheet-brand') < consoleSheet.indexOf('print-sheet-title'),
        // Two lines that both start with "Period", joined by the helper's own separator
        sub_line: consoleSheet.indexOf('MARBLE &amp; GRANITE') >= 0
            && consoleSheet.indexOf('print-sheet-est') < 0,
        lean_sheet: leanSheet,
        lean_only_the_name: (leanSheet.match(/print-sheet-(company|sub|tagline)/g) || []).join(',')
    };
}

// 8. a company name that is markup cannot become markup, on the screen or on the paper
{
    branding = HOSTILE;
    const login = boot();
    login.setResponder(responders);
    await login.evaluate('Brand.load()');
    login.evaluate('UI.renderApp()');
    const loginHtml = loginMarkup(login);
    const sheet = login.evaluate('WORKER_MODULES.myHoursPrintHtml(' + JSON.stringify(MY_HOURS) + ')');
    const escaped = login.evaluate('UI.escapeHtml(BRAND.name)');
    results.hostile = {
        raw_tag_on_the_login_screen: loginHtml.indexOf('<img src=x') >= 0,
        escaped_on_the_login_screen: loginHtml.indexOf(escaped) >= 0,
        raw_tag_on_the_sheet: sheet.indexOf('<img src=x') >= 0,
        escaped_on_the_sheet: sheet.indexOf(escaped) >= 0
    };
}

// 9. every string this feature added exists in all four tables
{
    const probe = boot();
    probe.setResponder(responders);
    const KEYS = [
        'companyTitle', 'companyHint', 'companyName', 'companyLegal', 'companyEst',
        'companyTagline', 'companyReset', 'companyBlankNote', 'companySaved',
        'companyResetDone', 'companyLogoShipped', 'companyLogoConfigured', 'companyLogoChoose',
        'companyLogoRemove', 'companyLogoSaved', 'companyLogoRemoved', 'companyLogoTooLarge',
        'companyLogoWrongType', 'companyLogoUnreadable'
    ];
    results.translations = probe.evaluate(`(function () {
        const KEYS = ${JSON.stringify(KEYS)};
        return {
            missing_from_english: KEYS.filter((key) => !(key in TRANSLATIONS.en)),
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


def branded(sheet):
    """The header at the top of a sheet, so a test can name one thing at a time."""
    start = sheet.find('print-sheet-brand')
    assert start >= 0, f"no company header on this sheet: {sheet[:400]}"
    return sheet[start : sheet.find('print-sheet-title')]


# ---------------------------------------------------------------------------
# the merge
# ---------------------------------------------------------------------------
def test_the_company_name_comes_from_the_settings_row(results):
    """The read is one request, it is the public one, and what comes back is what is drawn."""
    configured = results["configured"]
    assert configured["asked_for"] == ["/branding"], configured["asked_for"]
    assert configured["name"] == "Al-Jehad Stone Works"
    assert configured["legal"] == "MARBLE & GRANITE"
    assert configured["tagline"] == "SINCE 1983"
    assert configured["est"] == "", "an emptied line is not the shipped line"


def test_a_configured_mark_is_a_url_on_the_origin_the_browser_is_talking_to(results):
    """A path from the server, resolved here: the page may be on a LAN address, a chosen
    port or a tunnel, and the version in the query string is what makes a replacement a
    different URL rather than a cached image of the old mark."""
    configured = results["configured"]
    assert configured["api_base"].endswith("/api/v1")
    assert configured["mark"] == f"{configured['api_base']}/branding/logo?v=7"
    assert configured["has_the_mark"], "the login panel drew the shipped mark"


def test_the_login_panel_draws_the_company_and_not_the_shipped_wordmark(results):
    """The one screen whose job is to answer "is this the app my administrator told me
    about?" - and the one that had a constant typed into it."""
    configured = results["configured"]
    assert configured["has_name"] and configured["has_legal"] and configured["has_tagline"]
    assert not configured["named_the_old_company"], "the shipped wordmark is still on the screen"


def test_a_line_the_company_removed_is_not_drawn(results):
    """``''`` is a decision about the paper. A paragraph that exists to hold nothing is a
    blank row in the middle of the lockup, so the line is dropped rather than emptied."""
    assert results["configured"]["drew_blank_line"] is False
    assert results["lean"]["drew_only_the_name"] is True
    assert results["lean"]["has_the_name"] is True


def test_nothing_configured_is_exactly_what_this_application_ships(results):
    """The deployment that never opens the panel keeps the lockup it shipped with, byte for
    byte - which is what makes the settings row an override rather than a migration."""
    shipped = results["shipped"]
    assert shipped["same_as_shipped"] is True
    assert shipped["mark"] == "logo-mark.svg", "the shipped mark is a local file"
    assert shipped["lines"] == 4
    assert shipped["footer"] == "AL-JEHAD INTERNATIONAL CO. - stone, marble and granite since 1983."


def test_a_company_with_no_mark_keeps_the_one_this_application_ships(results):
    """Removing the mark is not the same as having none: the shipped one prints again."""
    assert results["lean"]["mark"] == "logo-mark.svg"


def test_the_login_footer_names_the_company_rather_than_a_constant(results):
    """It was a translated sentence with the name typed into it, which is how the login
    screen would have gone on naming the old company after a rename.

    The sentence is built from the lockup and then escaped like any other value on its way
    into the page, which is why the ampersand in this company's suffix reads ``&amp;`` in
    the markup and as an ampersand on screen.
    """
    assert results["configured"]["footer"] == (
        "Al-Jehad Stone Works MARBLE &amp; GRANITE - stone, marble and granite since 1983."
    )


# ---------------------------------------------------------------------------
# the boot, and offline
# ---------------------------------------------------------------------------
def test_a_warm_cache_names_the_company_before_anything_answers(results):
    """The first paint on a returning phone is the company's own name, and a settings read
    that fails does not take it away: the screen a worker sees is the last one that was
    known, not the one this application shipped with."""
    offline = results["offline"]
    assert offline["restored"] == "Al-Jehad Stone Works"
    assert offline["load_changed_anything"] is False, "a dead read must not change anything"
    assert offline["still_named"] == "Al-Jehad Stone Works"
    assert offline["drawn"] is True, "the cached lockup is what the login panel drew"


def test_the_answer_arriving_late_never_takes_a_typed_field_away(results):
    """The repaint is skipped while somebody is mid-entry: a name is not worth losing a
    half-typed password over, and the next paint has the answer either way."""
    repaint = results["repaint"]
    assert repaint["renders_when_idle"] == 1, "an idle screen repaints with the company's name"
    assert repaint["mid_entry"] is True
    assert repaint["renders_when_typing"] == 0, "a field with something in it must survive"


# ---------------------------------------------------------------------------
# the console's panel
# ---------------------------------------------------------------------------
def test_the_panel_shows_what_is_in_force(results):
    """A line nobody configured shows the shipped one - the administrator reads what will
    print, not what happens to be stored - and the panel says what an empty box means."""
    panel = results["panel"]
    assert panel["drawn"] and panel["shown"]
    assert panel["blank_note"], "the panel has to say that an empty line is not printed"
    assert panel["reset_button"], "and offer the way back to the shipped lockup"
    assert panel["logo_picker"] and panel["preview_note"]
    assert panel["shipped_mark_note"], "a deployment with no mark is told it is the shipped one"
    assert panel["shipped_preview"] == "logo-mark.svg"


def test_saving_sends_exactly_what_is_on_screen(results):
    """Including the empty line, which is the difference between "do not print this" and
    "put the shipped one back" - only the reset button means the second."""
    save = results["save"]
    assert save["path"] == "/admin/branding" and save["method"] == "POST"
    assert save["body"] == {
        "name": "Renamed Works Ltd",
        "legal": "MARBLE & GRANITE",
        "est": "",
        "tagline": "SINCE 1983",
    }, save["body"]


def test_the_app_adopts_the_servers_answer_and_repaints_with_it(results):
    """Not the values that were typed: a value the server stored differently is a value the
    administrator has to see, and the console's own rail is drawn from the same record."""
    save = results["save"]
    assert save["brand_name"] == "Renamed Works Ltd"
    assert save["rail_renamed"] is True
    assert save["toast"] and save["toast"][-1] == (
        "Company details saved. Every screen and every sheet is drawn from them now."
    )


def test_the_reset_asks_for_the_shipped_lockup_rather_than_sending_blanks(results):
    """``null`` is "nobody configured this". Sending ``""`` would print nothing at all,
    which is the opposite of what the button says."""
    assert results["reset"]["body"] == {"name": None, "legal": None, "est": None, "tagline": None}


def test_the_mark_is_uploaded_as_a_file_and_the_new_version_is_used(results):
    """Multipart, one field, and the mark the URL names afterwards is the one the server
    stored - a different version, so nothing can serve the replaced bytes from a cache."""
    logo = results["logo"]
    assert logo["upload_path"] == "/admin/branding/logo"
    assert logo["upload_method"] == "POST"
    assert logo["is_multipart"], "the image travels as a file, not as a base64 string"
    assert logo["version_in_the_url"], logo["url_after_upload"]


def test_a_file_the_policy_refuses_never_reaches_the_network(results):
    """The reason the check is in the browser at all: a 6 MB image refused here is one nobody
    waits for over a site tether before being told."""
    logo = results["logo"]
    assert logo["refused"] is True
    assert logo["requests_when_refused"] == 0, "the refusal has to happen before the upload"
    assert "too large" in logo["refusal_toast"][0]


def test_removing_the_mark_puts_the_shipped_one_back(results):
    remove = results["logo"]["remove"]
    assert remove["path"] == "/admin/branding/logo" and remove["method"] == "DELETE"
    assert remove["back_to_the_shipped_mark"] is True


# ---------------------------------------------------------------------------
# the paper
# ---------------------------------------------------------------------------
def test_every_sheet_opens_with_the_company(results):
    """The console's period sheet, a worker's month, and the worker's own hours: three
    sheets, one header, because it lives in the frame both readers compose through."""
    sheets = results["sheets"]
    for name in ("console", "worker_month", "my_hours"):
        header = branded(sheets[name])
        assert sheets["configured_name"] in header, f"{name} does not name the company"
        assert sheets["mark"] in header, f"{name} does not carry the mark"


def test_the_header_comes_before_the_report_has_said_anything(results):
    """Whose document it is comes first, and then what it is about."""
    assert results["sheets"]["brand_is_first"] is True
    assert results["sheets"]["url_in_sheet"] is True


def test_a_removed_line_leaves_no_empty_row_in_the_header(results):
    """The visible half of ``''``: the suffix and the tagline are joined without a dangling
    separator, and the tagline that was removed has no paragraph at all."""
    sheets = results["sheets"]
    assert sheets["sub_line"] is True, branded(sheets["console"])
    assert sheets["lean_only_the_name"] == "print-sheet-company", sheets["lean_sheet"]


def test_a_company_name_cannot_become_markup_on_the_sheet(results):
    """A sheet is a document this application writes. The name is a value off the wire like
    any other, and the header escapes what it interpolates."""
    hostile = results["hostile"]
    assert hostile["raw_tag_on_the_login_screen"] is False
    assert hostile["escaped_on_the_login_screen"] is True
    assert hostile["raw_tag_on_the_sheet"] is False
    assert hostile["escaped_on_the_sheet"] is True


# ---------------------------------------------------------------------------
# translations
# ---------------------------------------------------------------------------
def test_every_new_string_exists_in_every_language(results):
    """An English word on an Arabic console is a bug with a keyboard."""
    translations = results["translations"]
    assert translations["missing_from_english"] == []
    missing = {lang: keys for lang, keys in translations["missing"] if keys}
    assert missing == {}, missing
