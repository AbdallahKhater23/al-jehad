"""The admin Credentials tab, exercised for real.

WHY THIS EXISTS
---------------
The console used to have three account tabs - **Users**, **Pass**, **Enroll** - of which
one was wired to anything, and its only action was capturing an enrollment photo. Two of
them rendered "module coming soon", which is a screen that promises a feature the app does
not have.

They are one tab now, and this suite pins what "one tab" means:

1. the enroll dashboard is gone: no tab in the console is named Enroll/Users/Pass, no tab
   renders an enrollment form, and no tab posts to ``/admin/enroll``; and
2. the tab that replaced it shows what an admin actually needs when a worker calls -
   role, contact, whether a face template exists, the password *state*, and how many
   sessions a reset killed; and
3. a new password can be generated, read once, and set through
   ``/admin/users/edit_password`` for exactly the account on screen; and
4. no password value is ever *shown* for an existing account, because none exists to
   show: passwords are bcrypt hashes. The one readable moment is the one the admin
   creates, which is why the reveal is tested rather than the "view" of a stored value.

Permission is part of the contract too: a standard admin cannot change an
administrator's password (the server answers 403), so the button is absent rather than
present-and-refused, and a direct call is reported as a failure instead of a success.

Node is optional; without it these skip rather than fail.
"""

from __future__ import annotations

import html as html_module

import pytest

import frontend_vm

pytestmark = pytest.mark.skipif(frontend_vm.NODE is None, reason="node is not installed")

HARNESS = r"""
// --- the server's answer, faked ------------------------------------------

// One account per role, one with no password ever set, one with no face enrolled, one
// whose name and phone are markup - a two-field fixture cannot tell a working roster
// from a broken one.
const HEAD_ADMIN = {
    id: '1000', name: 'Head Admin', role: 'head_admin', phone: '+200000001000',
    email: 'head@example.test', status: 'active', face_enrolled: true,
    enrolled_at: '2026-08-20 09:00:00', password_set: true,
    password_changed_at: '2026-08-21 10:00:00', sessions_revoked: 2
};
const OPS_ADMIN = {
    id: '1001', name: 'Ops Admin', role: 'admin', phone: '', email: 'ops@example.test',
    status: 'active', face_enrolled: true, enrolled_at: '2026-08-22 09:00:00',
    password_set: true, password_changed_at: null, sessions_revoked: 1
};
const ROSTER = [
    {
        id: '1', name: 'Seed Worker', role: 'worker', phone: '+200000000001',
        email: 'seed@example.test', status: 'active', face_enrolled: true,
        enrolled_at: '2026-08-01 07:00:00', password_set: true,
        password_changed_at: '2026-08-02 08:30:00', sessions_revoked: 3
    },
    {
        id: '600', name: 'Ana Torres', role: 'moallem', phone: '+200000000600', email: '',
        status: 'active', face_enrolled: false, enrolled_at: null, password_set: true,
        password_changed_at: '2026-09-01 06:15:00', sessions_revoked: 0
    },
    {
        id: '601', name: 'Bilal Khan', role: 'worker', phone: '', email: 'bilal@example.test',
        status: 'active', face_enrolled: true, enrolled_at: '2026-09-10 06:00:00',
        password_set: false, password_changed_at: null, sessions_revoked: 0
    },
    // The other end of the screen from those two: an account somebody switched off, with a
    // reset behind it. Both facts exist so the two that are drawn *only* when they carry news
    // have something to be drawn for - and so the rows that carry no news can be asserted to
    // say nothing at all.
    {
        id: '602', name: 'Tariq Aziz', role: 'worker', phone: '+200000000602', email: '',
        status: 'inactive', face_enrolled: true, enrolled_at: '2026-09-12 06:00:00',
        password_set: true, password_changed_at: null, sessions_revoked: 4
    },
    {
        id: '777', name: '<img src=x onerror=alert(1)>Mallory', role: 'worker',
        phone: '<b>123</b>', email: '', status: 'active', face_enrolled: true,
        enrolled_at: '2026-09-11 06:00:00', password_set: true, password_changed_at: null,
        sessions_revoked: 0
    },
    // The endpoint orders by id, so the fixture does too: a fake that is easier than the
    // server hides exactly the bugs the ordering would have caused.
    HEAD_ADMIN,
    OPS_ADMIN
];

// A deployment with a roster worth paging: twelve accounts across the five roles, so the
// page (ten), the "Show more" button and every chip's count have something to be wrong
// about. The six-account fixture above cannot tell a capped list from an uncapped one.
const WIDE = (() => {
    const people = [];
    for (let i = 0; i < 6; i += 1) {
        people.push({
            id: String(200 + i), name: 'Worker ' + (i + 1), role: 'worker',
            phone: '+200000000' + (200 + i), email: '', status: 'active',
            face_enrolled: i % 2 === 0, enrolled_at: null, password_set: true,
            password_changed_at: null, sessions_revoked: 0
        });
    }
    people.push({
        id: '300', name: 'Ana Torres', role: 'moallem', phone: '+200000000300', email: '',
        status: 'active', face_enrolled: true, enrolled_at: null, password_set: true,
        password_changed_at: null, sessions_revoked: 0
    });
    people.push({
        id: '301', name: 'Noor Haddad', role: 'moallem', phone: '', email: '',
        status: 'active', face_enrolled: true, enrolled_at: null, password_set: true,
        password_changed_at: null, sessions_revoked: 0
    });
    people.push({
        id: '400', name: 'Office Clerk', role: 'off_office', phone: '', email: '',
        status: 'active', face_enrolled: true, enrolled_at: null, password_set: true,
        password_changed_at: null, sessions_revoked: 0
    });
    people.push({
        id: '500', name: 'Ops One', role: 'admin', phone: '', email: '',
        status: 'active', face_enrolled: true, enrolled_at: null, password_set: true,
        password_changed_at: null, sessions_revoked: 0
    });
    people.push({
        id: '501', name: 'Ops Two', role: 'admin', phone: '', email: '',
        status: 'active', face_enrolled: true, enrolled_at: null, password_set: true,
        password_changed_at: null, sessions_revoked: 0
    });
    people.push({
        id: '900', name: 'Head One', role: 'head_admin', phone: '', email: '',
        status: 'active', face_enrolled: true, enrolled_at: null, password_set: true,
        password_changed_at: null, sessions_revoked: 0
    });
    return people;
})();

let actorRole = 'head_admin';
let usersFail = false;
let wideRoster = false;

const editCalls = [];

function responders(url, init) {
    if (url.indexOf('/admin/users/edit_password') >= 0) {
        const body = JSON.parse(init.body);
        editCalls.push({ body: body, method: init.method, headers: init.headers });
        const target = ROSTER.filter((u) => u.id === body.worker_id)[0];
        if (!target) return { status: 404, body: { detail: 'User ID not found.' } };
        if (actorRole === 'admin' && (target.role === 'admin' || target.role === 'head_admin')) {
            return {
                status: 403,
                body: { detail: "Standard Admins cannot change admin or head admin passwords." }
            };
        }
        return { status: 200, body: { status: 'success', message: 'Password for user ' + body.worker_id + ' successfully updated.' } };
    }
    if (url.indexOf('/admin/users') >= 0) {
        if (usersFail) return { status: 503, body: { detail: 'Database is locked.' } };
        return { status: 200, body: wideRoster ? WIDE : ROSTER };
    }
    return { status: 200, body: {} };
}

// --- reading the rendered page -------------------------------------------

function inputValue(markup, id) {
    const match = new RegExp('id="' + id + '"[^>]*value="([^"]*)"').exec(markup);
    return match ? match[1] : null;
}

function textOf(cell) {
    return cell.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();
}

// Every account row, read the way the admin sees it: the facts by name rather than by
// column index, plus the state attributes the module publishes. A row is a list item now
// rather than a table row, and reading it by *name* is what makes this parser survive the
// next layout change - the old one counted cells, so a new column moved every assertion.
function rowsOf(markup) {
    const rows = markup.match(/<li class="roster-row"[^>]*>[\s\S]*?<\/li>/g) || [];
    return rows.map((row) => {
        const facts = factsOf(row);
        return {
            id: (/data-user="([^"]*)"/.exec(row) || [])[1],
            role: (/data-role="([^"]*)"/.exec(row) || [])[1],
            status: (/data-status="([^"]*)"/.exec(row) || [])[1],
            face: (/data-face="([^"]*)"/.exec(row) || [])[1],
            password: (/data-password="([^"]*)"/.exec(row) || [])[1],
            sessions: (/data-sessions="([^"]*)"/.exec(row) || [])[1],
            // ``[^>]*`` after the class list: the name carries a ``title`` as well, because
            // the one name long enough to be ellipsised is the one a reader needs to read.
            name: textOf((/class="ops-name roster-name"[^>]*>([\s\S]*?)<\/span>/.exec(row) || [])[1] || ''),
            role_label: textOf((/class="roster-role">([\s\S]*?)<\/span>/.exec(row) || [])[1] || ''),
            contact: textOf((/roster-contact">([\s\S]*?)<\/span>/.exec(row) || [])[1] || ''),
            button: row.indexOf('data-set-password=') >= 0,
            protected_hint: row.indexOf('data-protected="true"') >= 0,
            facts: facts
        };
    });
}

// The access facts of a row, by the name the module publishes rather than by counting whatever
// order they happen to be in - and only the ones the row actually draws. Two of the four are
// drawn only when they carry news (an account somebody switched off, a reset that killed
// sessions), so an absent fact is a state this parser has to report as *absent* rather than as
// empty. Each slice runs to the next fact that is on the row: slicing to the next name in the
// list instead ran a lone fact through the button block after it, which is how "3" became
// "3 Set password Edit Deactivate Delete".
function factsOf(row) {
    const names = ['face', 'password', 'sessions', 'status'];
    const drawn = names
        .map((name) => ({ name: name, at: row.indexOf('data-fact="' + name + '"') }))
        .filter((fact) => fact.at >= 0);
    const out = {};
    drawn.forEach((fact, index) => {
        const stop = index + 1 < drawn.length ? drawn[index + 1].at : row.indexOf('roster-actions');
        out[fact.name] = textOf(row.slice(fact.at, stop > fact.at ? stop : row.length));
    });
    return out;
}

// The role chips, as the reader sees them: the label, the count it offers, and whether it
// is the one in force.
function chipsOf(markup) {
    const chips = markup.match(/<button[^>]*data-credentials-role="[^"]*"[\s\S]*?<\/button>/g) || [];
    return chips.map((chip) => ({
        role: (/data-credentials-role="([^"]*)"/.exec(chip) || [])[1],
        count: textOf((/data-role-count="[^"]*">([\s\S]*?)<\/span>/.exec(chip) || [])[1] || ''),
        label: textOf(chip.slice(0, chip.indexOf('<span class="roster-chip-count"'))),
        pressed: /aria-pressed="true"/.test(chip)
    }));
}

function listedIds(markup) {
    return rowsOf(markup).map((row) => row.id);
}

function rosterCount(markup) {
    const match = /data-roster-count="true">([\s\S]*?)<\/p>/.exec(markup);
    return match ? textOf(match[1]) : null;
}

function showMoreLabel(markup) {
    const match = /<button[^>]*data-credentials-more[^>]*>([\s\S]*?)<\/button>/.exec(markup);
    return match ? textOf(match[1]) : null;
}

function toasts(env) {
    return env.evaluate("(document.getElementById('toastRoot').__children || []).map((el) => el.textContent)");
}

function render(env) {
    return env.evaluate("document.getElementById('adminContent').innerHTML");
}

function credentialsEnv(role) {
    actorRole = role || 'head_admin';
    const actor = actorRole === 'admin' ? OPS_ADMIN : HEAD_ADMIN;
    editCalls.length = 0;
    usersFail = false;
    const env = boot();
    env.setResponder(responders);
    env.evaluate('State.saveUser(' + JSON.stringify({
        id: actor.id, name: actor.name, role: actor.role, token: 'tok-' + actor.id
    }) + ')');
    return env;
}

const results = {};

// 1. the console is one account tab, and nothing else claims to enroll
{
    const env = credentialsEnv();
    results.tabs = env.evaluate("ADMIN_TABS.map((tab) => [tab.id, tab.key])");
    results.labels = env.evaluate("ADMIN_TABS.map((tab) => I18n.__(tab.key))");

    // Drive every tab: none may offer an enrollment form or call the enroll endpoint.
    // The path is matched exactly - ``/admin/enrollment/invites`` also begins with
    // ``/admin/enroll`` and is a different endpoint entirely (the registration link).
    const retiredEndpoints = [/\/admin\/enroll(?!ment)/];
    const enrollForms = [];
    for (const [id] of results.tabs) {
        await env.evaluate("UI.renderAdminTab('" + id + "')");
        const markup = render(env);
        if (markup.indexOf('enrollForm') >= 0) enrollForms.push(id);
        if (/\/admin\/enroll(?!ment)/.test(markup)) enrollForms.push(id + ':url');
    }
    results.no_enroll = {
        forms: enrollForms,
        enroll_requests: env.requests.filter((r) => retiredEndpoints.some((re) => re.test(r.url))).length,
        add_user_requests: env.requests.filter((r) => r.url.indexOf('/admin/users/add') >= 0).length
    };

    // A tab that no longer exists must not render a screen that pretends it does.
    await env.evaluate("UI.renderAdminTab('Enroll')");
    const stale = render(env);
    results.retired_tabs = {
        enroll_coming_soon: stale.indexOf('coming soon') >= 0,
        enroll_form: stale.indexOf('enrollForm') >= 0,
        requests: env.requests.filter((r) => r.url.indexOf('/admin/enroll') >= 0).length
    };
}

// 2. the roster: one row per account, with the state an admin needs
{
    const env = credentialsEnv();
    await env.evaluate("UI.renderAdminTab('Credentials')");
    const markup = render(env);
    results.roster = {
        rows: rowsOf(markup),
        has_hashed_note: markup.indexOf('data-hashed-note') >= 0,
        has_search_box: markup.indexOf('id="credentialsQuery"') >= 0,
        bcrypt_looking: /\$2[aby]\$/.test(markup),
        users_requests: env.requests.filter((r) => r.url.indexOf('/admin/users') >= 0).length,
        counts: env.evaluate("UI_MODULES._credentials.length")
    };
}

// 3. a standard admin gets no button for an administrator, only the reason
{
    const env = credentialsEnv('admin');
    await env.evaluate("UI.renderAdminTab('Credentials')");
    results.admin_actor = rowsOf(render(env));
}

// 3b. and a direct call is reported as the server's refusal, never as a success
{
    const env = credentialsEnv('admin');
    await env.evaluate("UI.renderAdminTab('Credentials')");
    await env.evaluate("UI_MODULES.openCredentialsPanel('1000')");
    const shown = inputValue(render(env), 'credentialsNewPassword');
    await env.evaluate("UI_MODULES.saveCredentialsPassword()");
    results.refused = {
        shown: shown,
        calls: editCalls.length,
        attempts: env.requests.filter((r) => r.url.indexOf('edit_password') >= 0).length,
        toast: toasts(env).slice(-1)[0],
        revealed: inputValue(render(env), 'credentialsRevealed')
    };
}

// 4. generating a password, and what the panel says about it
{
    const env = credentialsEnv();
    await env.evaluate("UI.renderAdminTab('Credentials')");
    await env.evaluate("UI_MODULES.openCredentialsPanel('600')");
    const markup = render(env);
    results.panel = {
        password: inputValue(markup, 'credentialsNewPassword'),
        panel_for: (/data-password-panel="([^"]*)"/.exec(markup) || [])[1],
        has_confirm: markup.indexOf('saveCredentialsPassword') >= 0,
        has_copy: markup.indexOf('copyCredentialsPassword') >= 0,
        has_regenerate: markup.indexOf('regenerateCredentialsPassword') >= 0,
        warns_about_signout: markup.indexOf('signs this account out') >= 0,
        requests_added: env.requests.length
    };
    // Regenerating must produce a different password, not the same one again.
    const first = inputValue(markup, 'credentialsNewPassword');
    await env.evaluate("UI_MODULES.regenerateCredentialsPassword()");
    const second = inputValue(render(env), 'credentialsNewPassword');
    await env.evaluate("UI_MODULES.regenerateCredentialsPassword()");
    const third = inputValue(render(env), 'credentialsNewPassword');
    results.regenerated = { first: first, second: second, third: third };
}

// 5. saving sends exactly the account and password on screen, and shows it back once
{
    const env = credentialsEnv();
    await env.evaluate("UI.renderAdminTab('Credentials')");
    await env.evaluate("UI_MODULES.openCredentialsPanel('601')");
    const shown = inputValue(render(env), 'credentialsNewPassword');
    await env.evaluate("UI_MODULES.saveCredentialsPassword()");
    const markup = render(env);
    const revealed = inputValue(markup, 'credentialsRevealed');
    results.saved = {
        shown: shown,
        call: editCalls.length === 1 ? editCalls[0] : null,
        revealed: revealed,
        reveal_panel_for: (/data-password-reveal="([^"]*)"/.exec(markup) || [])[1],
        toast: toasts(env).slice(-1)[0],
        edit_requests: env.requests.filter((r) => r.url.indexOf('edit_password') >= 0).length,
        html_has_password: markup.indexOf(shown) >= 0
    };

    // Copying hands over exactly the password that was set.
    await env.evaluate("UI_MODULES.copyCredentialsPassword()");
    results.saved.copied = env.copiedUrls.slice(-1)[0];

    // Closing the panel is what makes the password unreadable again: nothing stored it.
    await env.evaluate("UI_MODULES.closeCredentialsPanel()");
    const closed = render(env);
    results.saved.after_close = {
        html_has_password: closed.indexOf(shown) >= 0,
        reveal: closed.indexOf('data-password-reveal') >= 0
    };
}

// 5b. saving with nothing generated must not reach the server
{
    const env = credentialsEnv();
    await env.evaluate("UI.renderAdminTab('Credentials')");
    await env.evaluate("UI_MODULES._credentialsTarget = '1'");
    await env.evaluate("UI_MODULES._credentialsPassword = ''");
    await env.evaluate("UI_MODULES.saveCredentialsPassword()");
    results.nothing_to_save = {
        attempts: env.requests.filter((r) => r.url.indexOf('edit_password') >= 0).length,
        toast: toasts(env).slice(-1)[0]
    };
}

// 5c. a password the admin types is the one that gets saved, not the generated one
//
// The generator used to be the only way in: the field was a read-only echo of whatever it
// produced. An admin who already has a password in mind - a temporary one, a phrase the
// worker will remember - types it here, and every path that reads the password has to
// follow the typed value: the save, the one-time reveal, and Copy.
{
    const env = credentialsEnv();
    await env.evaluate("UI.renderAdminTab('Credentials')");
    await env.evaluate("UI_MODULES.openCredentialsPanel('601')");
    const beforeTyping = render(env);
    const generated = inputValue(beforeTyping, 'credentialsNewPassword');
    // An ``&`` and an apostrophe on purpose: a typed password is re-rendered into an
    // attribute, so it is escaped like any other value the admin types.
    const typed = "Rock&Roll-Bilal's-9!";
    await env.evaluate("UI_MODULES.setCredentialsPassword(" + JSON.stringify(typed) + ")");
    const afterTyping = render(env);
    const callsBefore = editCalls.length;
    await env.evaluate("UI_MODULES.saveCredentialsPassword()");
    const markup = render(env);
    results.typed = {
        generated: generated,
        password: typed,
        field_is_editable: /<input[^>]*id="credentialsNewPassword"(?![^>]* readonly)/.test(beforeTyping),
        has_input_handler: beforeTyping.indexOf('oninput="UI_MODULES.setCredentialsPassword(this.value)"') >= 0,
        says_it_can_be_typed: beforeTyping.indexOf(env.evaluate("I18n.__('credentialsPasswordManual')")) >= 0,
        // Typing must not repaint: re-rendering the field the caret is in would put the
        // caret at the end after every character. So the markup still holds what the last
        // repaint drew, and what moved is the state the save reads.
        no_repaint_while_typing: inputValue(afterTyping, 'credentialsNewPassword') === generated,
        calls: editCalls.length - callsBefore,
        sent: editCalls.length > callsBefore ? editCalls[editCalls.length - 1].body.new_password : null,
        revealed: inputValue(markup, 'credentialsRevealed'),
        copied: null,
        after_close: false
    };
    await env.evaluate("UI_MODULES.copyCredentialsPassword()");
    results.typed.copied = env.copiedUrls.slice(-1)[0];
    await env.evaluate("UI_MODULES.closeCredentialsPanel()");
    results.typed.after_close = render(env).indexOf('credentialsRevealed') < 0;
}

// 6. the search narrows the roster without asking the server again
{
    const env = credentialsEnv();
    await env.evaluate("UI.renderAdminTab('Credentials')");
    const before = env.requests.length;
    const search = async (term) => {
        env.evaluate("document.getElementById('credentialsQuery').value = " + JSON.stringify(term));
        await env.evaluate("UI_MODULES.applyCredentialsSearch()");
        return rowsOf(render(env)).map((row) => row.id);
    };
    const byName = await search('torres');
    const byRole = await search('Moallem');
    const byPhone = await search('+200000000001');
    const byId = await search('601');
    const andTerms = await search('khan 601');
    const none = await search('nobody-here');
    const markupNone = render(env);
    await env.evaluate("UI_MODULES.clearCredentialsSearch()");
    results.search = {
        by_name: byName,
        by_role: byRole,
        by_phone: byPhone,
        by_id: byId,
        and_terms: andTerms,
        none: none,
        cleared: rowsOf(render(env)).map((row) => row.id),
        requests_added: env.requests.length - before,
        no_matches_flag: markupNone.indexOf('data-no-matches') >= 0,
        no_list_when_empty: markupNone.indexOf('data-credentials-list') < 0,
        chips_still_there: markupNone.indexOf('data-credentials-filters') >= 0
    };
}

// 7. a name or a phone number must not become markup
{
    const env = credentialsEnv();
    await env.evaluate("UI.renderAdminTab('Credentials')");
    const markup = render(env);
    results.escaped = {
        raw_tag: markup.indexOf('<img src=x') >= 0,
        escaped_tag: markup.indexOf('&lt;img src=x') >= 0,
        raw_phone: markup.indexOf('<b>123</b>') >= 0,
        escaped_phone: markup.indexOf('&lt;b&gt;123&lt;/b&gt;') >= 0,
        name_present: markup.indexOf('Mallory') >= 0
    };
}

// 8. a phone gets the same list, not a second layout
//
// It used to be cards below 768px and a nine-column table above it: two renderers saying the
// same facts, and the table is the half that had to go. So what a phone gets now is the same
// row, stacked by the stylesheet - which is why this scenario reads the *row* hooks rather
// than a card shape, and why the desktop assertion in it is worth making.
{
    const env = credentialsEnv();
    env.evaluate("localStorage.setItem('layoutOverride', 'mobile')");
    await env.evaluate("UI.renderAdminTab('Credentials')");
    const markup = render(env);
    const rows = rowsOf(markup);
    results.phone = {
        rows: rows.map((row) => row.id),
        has_table: markup.indexOf('<table') >= 0,
        has_list: markup.indexOf('data-credentials-list') >= 0,
        first_row_name: rows.length > 0 ? rows[0].name : null,
        first_row_facts: rows.length > 0 ? rows[0].facts : null,
        count_line: rosterCount(markup),
        chips: chipsOf(markup).map((chip) => chip.role),
        has_search_box: markup.indexOf('id="credentialsQuery"') >= 0
    };
    env.evaluate("localStorage.removeItem('layoutOverride')");
}

// 9. a dead server says so instead of showing an empty roster
{
    const env = credentialsEnv();
    usersFail = true;
    await env.evaluate("UI.renderAdminTab('Credentials')");
    const markup = render(env);
    results.dead_server = {
        message: markup.indexOf('Database is locked.') >= 0,
        rows: rowsOf(markup).length,
        cached: env.evaluate("UI_MODULES._credentials"),
        has_search_box: markup.indexOf('id="credentialsQuery"') >= 0
    };
}

// 10. every action on a row is an icon, and every icon carries a name
//
// The row is roomy now - three blocks rather than nine columns - but four labelled buttons
// still cost the line they sit on, so the four actions stay glyphs with a tooltip and an
// accessible name. That trade is only honest if the name is really there: an icon-only
// button with neither is a control nobody can explain, and one whose text is a translation
// *key* would be read out as "credentialsDelete". Both halves are checked here, and the
// glyphs are checked too - a button that quietly grew a word back is a row that wraps.
{
    const env = credentialsEnv();
    await env.evaluate("UI.renderAdminTab('Credentials')");
    const markup = render(env);
    const row = (markup.match(/<li class="roster-row"[^>]*>[\s\S]*?<\/li>/g) || [])
        .filter((entry) => entry.indexOf('data-user="1"') >= 0)[0] || '';
    const buttons = row.match(/<button[^>]*data-(?:set-password|edit-user|user-status|delete-user)[^>]*>[\s\S]*?<\/button>/g) || [];
    results.action_names = {
        count: buttons.length,
        // What is left of a button once its glyph is taken out: an empty string means the
        // button *is* the glyph.
        words: buttons.map((button) => button.slice(button.indexOf('>') + 1)
            .replace(/<\/button>[\s\S]*$/, '')
            .replace(/<svg[\s\S]*?<\/svg>/g, '')
            .replace(/<[^>]*>/g, '')
            .replace(/\s+/g, ' ')
            .trim()),
        titles: buttons.map((button) => (/title="([^"]*)"/.exec(button) || [])[1] || null),
        labels: buttons.map((button) => (/aria-label="([^"]*)"/.exec(button) || [])[1] || null),
        nowrap: row.indexOf('nowrap') >= 0
    };
}

// 11. twelve accounts, read ten at a time, and narrowed by role without asking again
//
// The page is the answer to "do not show me the whole crew": ten rows, a line that says how
// many there really are, and a button for the rest. The chips answer the other half - "show
// me the moallems", "show me the admins" - out of the same roster, so neither control costs
// a request. Both are driven through their own binder rather than by calling the methods, so
// what is asserted is what a tap does.
{
    const env = credentialsEnv();
    wideRoster = true;
    await env.evaluate("UI.renderAdminTab('Credentials')");
    const before = env.requests.length;
    const first = render(env);
    const firstPage = listedIds(first);

    await env.evaluate(`(async () => {
        const button = { addEventListener: (type, handler) => { if (type === 'click') button.handler = handler; } };
        UI_MODULES.bindCredentialsControls({ querySelectorAll: (selector) => (selector === '[data-credentials-more]' ? [button] : []) });
        await button.handler({ preventDefault: () => {} });
    })()`);
    const opened = render(env);

    const tapRole = async (role) => {
        await env.evaluate(`(async () => {
            const chip = {
                getAttribute: () => ${JSON.stringify(role)},
                addEventListener: (type, handler) => { if (type === 'click') chip.handler = handler; }
            };
            UI_MODULES.bindCredentialsControls({ querySelectorAll: (selector) => (selector === '[data-credentials-role]' ? [chip] : []) });
            await chip.handler({ preventDefault: () => {} });
        })()`);
        return render(env);
    };
    const moallems = await tapRole('moallem');
    const workers = await tapRole('worker');

    // A search that matches nobody *in the chosen role*: the message has to blame the role
    // rather than the search, and the chips have to stay on screen so there is a way back.
    env.evaluate("document.getElementById('credentialsQuery').value = 'nobody-here'");
    await env.evaluate("UI_MODULES.applyCredentialsSearch()");
    const empty = render(env);

    results.listing = {
        page_size: firstPage.length,
        first_page: firstPage,
        count_line: rosterCount(first),
        more_label: showMoreLabel(first),
        chips: chipsOf(first),
        after_more: listedIds(opened).length,
        count_after_more: rosterCount(opened),
        more_after_more: showMoreLabel(opened),
        moallems: listedIds(moallems),
        moallem_count_line: rosterCount(moallems),
        moallem_pressed: chipsOf(moallems).filter((chip) => chip.pressed).map((chip) => chip.role),
        moallem_chip_counts: chipsOf(moallems).map((chip) => [chip.role, chip.count]),
        workers: listedIds(workers),
        workers_pressed: chipsOf(workers).filter((chip) => chip.pressed).map((chip) => chip.role),
        empty: {
            flagged: empty.indexOf('data-no-matches') >= 0,
            list: empty.indexOf('data-credentials-list') >= 0,
            words: textOf((/ui-empty-title">([\s\S]*?)<\/p>/.exec(empty) || [])[1] || ''),
            chips: chipsOf(empty).map((chip) => chip.role)
        },
        requests_added: env.requests.length - before
    };
    wideRoster = false;
}
"""


@pytest.fixture(scope="module")
def results() -> dict:
    return frontend_vm.run(HARNESS)


def password_of(results) -> str:
    return html_module.unescape(results["panel"]["password"])


def test_the_console_has_one_account_tab_and_no_enroll_dashboard(results):
    """Three tabs, one of them dead and one of them an enroll form: now one screen."""
    ids = [entry[0] for entry in results["tabs"]]
    # ``Notes``, ``Links``, ``Alerts``, ``Registrations`` and (the root tier's own) ``Developer``
    # joined later and are their own screens (see test_frontend_notes.py,
    # test_frontend_quick_links.py, test_frontend_admin_alerts.py,
    # test_frontend_registrations_queue.py and test_frontend_developer_console.py); none of them
    # reinstates the enrollment dashboard this test is about - ``Registrations`` is the closest
    # of the five, and it is the *opposite*: the walk-up form has already been filled in and
    # photographed by the time that tab sees it, so it creates an account from a submission
    # rather than offering a console form to capture a face with.
    assert ids == [
        # The console's front door, and first for that reason: ``renderAdminTab`` lands on the
        # first tab this reader is offered, so this list's *order* is what a landing screen is
        # (see test_frontend_dashboard.py, which pins the screen itself).
        "Dashboard",
        "Live Ops",
        "Approvals",
        "Alerts",
        "Sites",
        "Shifts",
        "Credentials",
        "Registrations",
        "Links",
        "Notes",
        "Admin",
        "Developer",
    ]
    assert "Enroll" not in ids and "Users" not in ids and "Pass" not in ids
    assert "Credentials" in results["labels"], "the tab is labelled, not left as a key"


def test_no_tab_renders_an_enrollment_form_or_calls_the_enroll_endpoint(results):
    """What was removed stays removed - but not everything that could create an account was.

    The retired dashboard posted a photo to ``/admin/enroll`` from a form that lived in a
    tab of its own. Account *creation* is a different job and came back deliberately, as an
    explicit form on the Credentials tab (see ``test_frontend_account_creation.py``); what
    this guards is the old shape - a tab that renders an enrollment form, and the old JSON
    endpoint that the console used to call without one.
    """
    no_enroll = results["no_enroll"]
    assert no_enroll["forms"] == [], "an enrollment form belongs to the dashboard that was removed"
    assert no_enroll["enroll_requests"] == 0
    assert no_enroll["add_user_requests"] == 0, "the console creates accounts through the form, not blindly"


def test_a_retired_tab_does_not_quietly_still_work(results):
    """A stale button or a bookmark must not resurrect the screen that was deleted."""
    retired = results["retired_tabs"]
    assert retired["enroll_form"] is False
    assert retired["requests"] == 0


def test_the_roster_shows_every_account_with_its_access_state(results):
    """This is the information the enroll dashboard never had, next to the password.

    Read by *name* rather than by column index: the row is a list item now, and the facts are
    four labelled spans, so this test survives the next time the layout moves - the old
    cell-index version would have moved every assertion with it.
    """
    rows = {row["id"]: row for row in results["roster"]["rows"]}
    assert sorted(rows, key=int) == ["1", "600", "601", "602", "777", "1000", "1001"]
    seed = rows["1"]
    assert seed["name"] == "Seed Worker"
    assert seed["role_label"] == "Worker", "roles are labelled for the reader, not raw codes"
    assert seed["role"] == "worker", "and the raw role is published, for a filter to read"
    assert seed["contact"] == "+200000000001 · seed@example.test", "phone and email together"
    assert seed["face"] == "enrolled"
    assert seed["password"] == "set"
    assert "Set" in seed["facts"]["password"], "the state, plus when it last changed"
    assert "2026-08-02 08:30:00" in seed["facts"]["password"]
    assert seed["sessions"] == "3", "sessions the last reset killed"
    assert seed["button"] is True


def test_a_missing_face_or_password_is_visible_rather_than_blank(results):
    """The two "why can this person not get in" facts, which the table used to park off-screen."""
    rows = {row["id"]: row for row in results["roster"]["rows"]}
    assert rows["600"]["face"] == "missing", "no template on file"
    assert "None" in rows["600"]["facts"]["face"]
    assert rows["600"]["role_label"] == "Moallem"
    assert rows["601"]["password"] == "never"
    assert "Never set" in rows["601"]["facts"]["password"]
    assert rows["601"]["contact"] == "bilal@example.test", "one contact field is enough"
    assert results["roster"]["users_requests"] == 1, "one roster, one request"


def test_a_row_says_nothing_it_has_nothing_to_say_about(results):
    """Only the facts that are news, so ten rows read down the page rather than across it.

    Every account is active until somebody switches it off, and most have never had a session
    revoked - so a green "Active" badge and a "Sessions revoked 0", on every row, were two
    facts an administrator never scans *for*, and between them they pushed the access block
    onto a second line of every row on the laptop this screen is read on. What is always there
    is the pair that decides whether the man at the gate can get in at all: his face, and his
    password. The other two are drawn where they are news - which is also what makes an
    exception visible at a glance instead of being one badge among four.
    """
    rows = {row["id"]: row for row in results["roster"]["rows"]}
    assert sorted(rows["600"]["facts"]) == ["face", "password"], rows["600"]["facts"]
    assert "status" not in rows["600"]["facts"], "an active account needs no badge to say so"
    assert "sessions" not in rows["601"]["facts"], "and no reset is not a fact either"
    assert "sessions" in rows["1"]["facts"], "but one that killed sessions is the news itself"
    # The state is still *on the row* - it is the drawing that is conditional, not the fact.
    assert rows["1"]["status"] == "active"
    assert rows["601"]["sessions"] == "0"
    # And the two that are news, drawn on the account that has them.
    assert "Deactivated" in rows["602"]["facts"]["status"], "a switched-off account says so"
    assert rows["602"]["sessions"] == "4", "sessions the last reset killed"
    assert "4" in rows["602"]["facts"]["sessions"]
    assert rows["602"]["button"] is True, "the exception explains the button beside it"


def test_the_screen_never_carries_a_password_or_a_hash(results):
    """Nothing readable is stored, so nothing readable may be on screen either."""
    roster = results["roster"]
    assert roster["bcrypt_looking"] is False, "no hash may reach the browser"
    assert roster["has_hashed_note"] is True, "and the admin is told why there is no value column"
    saved = results["saved"]
    # Both read the same way (as the browser would): the password shown after saving is
    # the one that was on screen before it, not a second, different value.
    assert saved["revealed"] == saved["shown"], "the new password is echoed once"
    assert saved["after_close"]["html_has_password"] is False, "and gone when the panel closes"
    assert saved["after_close"]["reveal"] is False


def test_a_generated_password_satisfies_the_server_policy(results):
    password = password_of(results)
    assert len(password) >= 16
    assert any(char.islower() for char in password)
    assert any(char.isupper() for char in password)
    assert any(char.isdigit() for char in password)
    assert any(not char.isalnum() for char in password)
    for ambiguous in ("O", "0", "I", "l", "1"):
        assert ambiguous not in password, "it has to survive being read out over a phone"


def test_regenerating_produces_a_new_password_each_time(results):
    regenerated = results["regenerated"]
    assert regenerated["first"] != regenerated["second"]
    assert regenerated["second"] != regenerated["third"]
    assert regenerated["first"] != regenerated["third"]


def test_the_panel_says_who_it_is_for_and_what_saving_costs(results):
    panel = results["panel"]
    assert panel["panel_for"] == "600", "the panel names the account being changed"
    assert panel["has_confirm"] and panel["has_copy"] and panel["has_regenerate"]
    assert panel["warns_about_signout"] is True, "a reset kills the worker's phone session"
    assert panel["requests_added"] == 1, "opening the panel is not a reason to ask the server again"


def test_saving_sends_exactly_the_account_and_password_on_screen(results):
    """The bug this guards: a password set for one worker landing on another."""
    saved = results["saved"]
    call = saved["call"]
    assert call is not None, "one request, not zero and not two"
    assert call["body"]["worker_id"] == "601"
    assert call["body"]["new_password"] == html_module.unescape(saved["shown"])
    assert call["body"]["admin_id"] == "1000", "the acting admin is named for the audit log"
    assert call["method"] == "POST"
    assert call["headers"].get("Authorization") == "Bearer tok-1000"
    assert saved["reveal_panel_for"] == "601"
    assert "Password updated" in saved["toast"]
    assert saved["copied"] == html_module.unescape(saved["shown"]), "Copy hands over what was set"


def test_nothing_is_sent_when_there_is_no_password_to_set(results):
    nothing = results["nothing_to_save"]
    assert nothing["attempts"] == 0, "an empty password is refused here, not by the server"
    assert "Generate or type a password first." == nothing["toast"]


def test_a_typed_password_is_the_one_that_gets_saved(results):
    """The field is an input, not an echo: what the admin types is what the worker gets."""
    typed = results["typed"]
    assert typed["field_is_editable"] is True, "a read-only field cannot take a typed password"
    assert typed["has_input_handler"] is True, "and nothing would reach the state without one"
    assert typed["says_it_can_be_typed"] is True, "the panel has to say the field may be typed in"
    assert typed["no_repaint_while_typing"] is True, "a repaint mid-typing would move the caret"
    assert typed["calls"] == 1, "one save, for the account on screen"
    assert typed["sent"] == typed["password"], "the typed password is what the server is told"
    assert typed["sent"] != typed["generated"], "and not the one the generator had put there"
    assert html_module.unescape(typed["revealed"]) == typed["password"], "shown once, as typed"
    assert html_module.unescape(typed["copied"]) == typed["password"], "Copy hands over what was set"
    assert typed["after_close"] is True, "and it is gone when the panel closes"


def test_a_standard_admin_is_not_offered_an_administrators_password(results):
    """A button that always answers 403 is a trap, not an affordance."""
    rows = {row["id"]: row for row in results["admin_actor"]}
    assert rows["1001"]["button"] is False and rows["1001"]["protected_hint"] is True
    assert rows["1000"]["button"] is False and rows["1000"]["protected_hint"] is True
    assert rows["1"]["button"] is True, "worker passwords are exactly what this admin is for"
    assert rows["600"]["button"] is True


def test_a_refused_reset_is_reported_as_a_failure(results):
    """The server refuses it, and the screen must not claim otherwise."""
    refused = results["refused"]
    assert refused["calls"] == 1, "the call is made - the server is the authority"
    assert refused["attempts"] == 1
    assert "Standard Admins cannot change" in refused["toast"]
    assert refused["revealed"] is None, "no password may be shown for a reset that did not happen"


def test_the_search_narrows_the_roster_without_a_new_request(results):
    search = results["search"]
    assert search["by_name"] == ["600"]
    assert search["by_role"] == ["600"], "'Moallem' finds the moallem"
    assert search["by_phone"] == ["1"]
    assert search["by_id"] == ["601"]
    assert search["and_terms"] == ["601"], "two terms narrow to one account, not two"
    assert search["none"] == []
    assert search["no_matches_flag"] is True
    assert search["no_list_when_empty"] is True, "an empty list reads as 'everyone is fine'"
    assert search["chips_still_there"] is True, "and the way back out stays on screen"
    assert search["cleared"] == ["1", "600", "601", "602", "777", "1000", "1001"]
    assert search["requests_added"] == 0, "the roster was already in hand"


def test_a_name_or_phone_number_cannot_inject_markup(results):
    escaped = results["escaped"]
    assert escaped["name_present"] is True
    assert escaped["raw_tag"] is False and escaped["escaped_tag"] is True
    assert escaped["raw_phone"] is False and escaped["escaped_phone"] is True


def test_a_phone_gets_the_same_list_rather_than_a_second_layout(results):
    """One renderer, two widths: the table that slid sideways was the half that had to go."""
    phone = results["phone"]
    assert phone["has_table"] is False, "no table anywhere, at any width"
    assert phone["has_list"] is True
    assert phone["rows"] == ["1", "600", "601", "602", "777", "1000", "1001"]
    assert phone["first_row_name"] == "Seed Worker"
    assert "Never set" not in phone["first_row_facts"]["password"], (
        "the row belongs to the first account"
    )
    assert phone["count_line"] is not None, "the phone gets the roster's own size too"
    assert phone["chips"] == ["all", "worker", "moallem", "admin", "head_admin"], (
        "the role filter is a control a phone needs most"
    )
    assert phone["has_search_box"] is True


def test_every_action_on_a_row_is_a_glyph_with_a_name_to_say(results):
    """The four actions are icons, so the tooltip and the accessible name are the whole label."""
    row = results["action_names"]
    assert row["count"] == 4, "set the password, edit, deactivate, delete"
    for words in row["words"]:
        assert words == "", "a button that grew a word back is a row that wraps"
    for name in row["titles"] + row["labels"]:
        assert name, "an icon-only button with no name is a control nobody can explain"
        assert not name.startswith("credentials"), f"a raw translation key reached the markup: {name}"
    assert row["nowrap"] is True, "the four actions stay on the line they were designed for"


def test_the_roster_is_read_ten_at_a_time_and_the_rest_is_a_button(results):
    """Twelve accounts, ten rows, and the count line that says so.

    A deployment's roster is hundreds of accounts on a phone somebody carries; the page is
    what keeps the screen about the person being looked up rather than about the list.
    """
    listing = results["listing"]
    assert listing["page_size"] == 10, listing["first_page"]
    assert listing["first_page"] == [
        "200", "201", "202", "203", "204", "205", "300", "301", "400", "500"
    ], "the server's own order, cut at ten"
    assert listing["count_line"] == "Showing 10 of 12", listing["count_line"]
    assert listing["more_label"] == "Show 2 more", listing["more_label"]
    assert listing["after_more"] == 12, "the button hands over exactly the rest"
    assert listing["count_after_more"] == "Showing 12 of 12", listing["count_after_more"]
    assert listing["more_after_more"] is None, "and goes away when there is nothing left"
    assert listing["requests_added"] == 0, "a page is not a read of the roster"


def test_the_role_chips_narrow_the_list_without_a_request(results):
    """"Who are my moallems" is one tap, and the counts are what the tap would hand over."""
    listing = results["listing"]
    assert [(chip["role"], chip["label"], chip["count"]) for chip in listing["chips"]] == [
        ("all", "Everyone", "12"),
        ("worker", "Worker", "6"),
        ("moallem", "Moallem", "2"),
        ("off_office", "Off-Office Worker", "1"),
        ("admin", "Administrator", "2"),
        ("head_admin", "Head administrator", "1"),
    ], listing["chips"]
    assert listing["chips"][0]["pressed"] is True, "everybody is the state it starts in"
    assert listing["moallems"] == ["300", "301"], listing["moallems"]
    assert listing["moallem_pressed"] == ["moallem"], "the chip says it is the one in force"
    assert listing["moallem_chip_counts"] == [[chip["role"], chip["count"]] for chip in listing["chips"]], (
        "a chip's count is what tapping it would hand over, so choosing a role must not zero the rest"
    )
    assert listing["moallem_count_line"] == "Showing 2 of 2", listing["moallem_count_line"]
    assert listing["workers"] == ["200", "201", "202", "203", "204", "205"]
    assert listing["workers_pressed"] == ["worker"]
    assert listing["requests_added"] == 0, "a filter is a question about the list in hand"


def test_a_role_with_nothing_in_it_says_so_and_keeps_the_way_back(results):
    """An empty list under a pressed chip must not read as a roster that failed to load."""
    empty = results["listing"]["empty"]
    assert empty["flagged"] is True
    assert empty["list"] is False, "no headers over nothing: that shape reads as a broken load"
    assert empty["words"] == "No accounts with this role.", empty["words"]
    assert empty["chips"] == ["all", "worker", "moallem", "off_office", "admin", "head_admin"], (
        "the chips stay, so the reader can pick another role"
    )


def test_a_dead_server_says_so_instead_of_showing_an_empty_roster(results):
    dead = results["dead_server"]
    assert dead["message"] is True
    assert dead["rows"] == 0
    assert dead["cached"] is None, "a failed load leaves nothing to act on"
    assert dead["has_search_box"] is True
