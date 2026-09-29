"""Creating accounts from the console, exercised in a real browser-like environment.

WHY THIS EXISTS
---------------
The Credentials tab can start an account in the two ways an administrator actually needs:
type it in here (id, name, role, generated password, and a photo that registers the face),
or hand out the *registration* link, which somebody who has no account opens to apply. The API
tests in ``test_account_creation.py`` and ``test_walk_up_registration.py`` prove the server
does the right thing; what they cannot see is whether the screen reaches it - which fields
travel, whether the photo is attached, whether the password is shown once, whether a 6 MB photo
is refused before it is uploaded, and whether the link the panel copies is the one the server is
telling it to hand out.

So this suite drives the real ``admin_modules.js``:

1. both actions are offered, and merely opening the tab posts nothing;
2. the create panel generates a password, states the 5 MB / photos-only rule, and its file
   picker accepts exactly the three image types the server accepts;
3. a photo that is too big, or is not a photo, is refused *client-side*, with the reason on
   screen, and the account is not sent at all;
4. creating posts a multipart body carrying every field and the photo itself, and shows the
   password exactly once afterwards - then forgets it when the panel closes;
5. an id that is not a whole number never leaves the browser, and one the deleted per-role
   id blocks would have refused does - the server is the authority on which ids it accepts;
6. opening the link panel *reads* the link - there is nothing to create, so there is no form
   to fill in - and the answer is offered as a copyable URL, a WhatsApp message and a QR code,
   with the intake switch's state beside it;
7. replacing the link posts once and shows the new URL, which is the only way an administrator
   can take back a link that has reached the wrong person;
8. a server refusal (an id already taken) is reported as the reason, not as a success - and
   answered, because that one refusal is about a number: the form offers the next free id and
   takes it in one tap.

Node is optional; without it these skip rather than fail.
"""

from __future__ import annotations

import html as html_module
import re
from pathlib import Path

import pytest

import frontend_vm

#: The console mirrors the server's taken-id sentence verbatim, so the two files are read
#: together in the test below.
BACKEND = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(frontend_vm.NODE is None, reason="node is not installed")

HARNESS = r"""
// --- the server's answers, faked ----------------------------------------

const ACTOR = {
    id: '5000', name: 'Head Admin', role: 'head_admin', phone: '+200000005000',
    email: 'head@example.test', status: 'active', face_enrolled: true,
    enrolled_at: '2026-08-20 09:00:00', password_set: true,
    password_changed_at: '2026-08-21 10:00:00', sessions_revoked: 0
};
const OPERATOR = {
    id: '1000', name: 'Ops Admin', role: 'admin', phone: '', email: 'ops@example.test',
    status: 'active', face_enrolled: true, enrolled_at: '2026-08-22 09:00:00',
    password_set: true, password_changed_at: null, sessions_revoked: 0
};

const ROSTER = [ACTOR, OPERATOR];

const createCalls = [];
const linkCalls = [];
//: Every write to the intake switch, in order, and the position the fake server is holding.
//: The switch starts *open* here so the panels drawn in every other scenario are the ordinary
//: ones; the scenario about moving it closes it first.
const intakeCalls = [];
let intakeOpen = true;
//: Ids the roster already carries, as the server's unique index would answer: 400, with the
//: sentence ``/admin/users/create`` raises. The status matters as much as the text - the
//: console has to be reacting to the refusal the server actually sends.
const takenIds = [];
let createFail = false;
let linkFail = false;
//: The link's own address for this run. Generation 0 is the generation a deployment that has
//: never replaced its link carries; a ``POST`` to the panel's route answers with generation 1,
//: so the two are different URLs and "the panel re-read what the server just gave it" is a
//: claim a test can tell apart from "the panel remembered the first one".
const LINK_URLS = {
    0: 'https://site.example.test/register/0.tok-one',
    1: 'https://site.example.test/register/1.tok-two'
};

function responders(url, init) {
    // The create endpoint lives under /admin/users, so it is matched first: a roster
    // answer for a create request would look like a success and hide the bug.
    if (url.indexOf('/admin/users/create') >= 0) {
        const body = (init && init.body) || null;
        createCalls.push({ url: String(url), method: (init && init.method) || 'GET', body: body, headers: (init && init.headers) || {} });
        const wanted = body && body.get ? String(body.get('user_id')) : '';
        if (createFail || takenIds.indexOf(wanted) >= 0) {
            return { status: 400, body: { detail: 'User ID already exists.' } };
        }
        return {
            status: 200,
            body: {
                status: 'success',
                user_id: body && body.get ? body.get('user_id') : null,
                role: 'worker',
                face_enrolled: body && body.has ? body.has('photo') : false,
                message: 'User created.'
            }
        };
    }
    if (url.indexOf('/admin/registrations/intake') >= 0) {
        const wanted = (init && String(init.method || 'GET').toUpperCase() === 'POST')
            ? JSON.parse(init.body) : null;
        if (wanted) {
            intakeCalls.push(wanted);
            intakeOpen = !!wanted.open;
        }
        return {
            status: 200,
            body: {
                status: 'success',
                accepting: intakeOpen,
                reason: intakeOpen ? 'open' : 'closed_by_console',
                deployment_enabled: false,
                decided: true,
                updated_at: null,
                updated_by: '5000'
            }
        };
    }
    if (url.indexOf('/admin/registrations/link') >= 0) {
        const replaced = (init && init.method) === 'POST';
        linkCalls.push({ url: String(url), method: (init && init.method) || 'GET', body: (init && init.body) || null, headers: (init && init.headers) || {} });
        if (linkFail) return { status: 500, body: { detail: 'The registration link could not be read.' } };
        return {
            status: 200,
            body: {
                status: 'success',
                url: replaced ? LINK_URLS[1] : LINK_URLS[0],
                generation: replaced ? 1 : 0,
                rotated_at: replaced ? '2026-09-19 08:30:00' : null,
                rotated_by: replaced ? '5000' : null,
                qr_png_data_uri: 'data:image/png;base64,AAAA',
                // The link read carries the switch's state, and this deployment ships with it
                // off: what the panel offers to move is the switch, on a form that starts shut.
                accepting: intakeOpen,
                reason: intakeOpen ? 'open' : 'closed_by_default',
                deployment_enabled: false
            }
        };
    }
    if (url.indexOf('/admin/users') >= 0) return { status: 200, body: ROSTER };
    return { status: 200, body: {} };
}

// --- reading the rendered page -------------------------------------------

function inputValue(markup, id) {
    const match = new RegExp('id="' + id + '"[^>]*value="([^"]*)"').exec(markup);
    return match ? match[1] : null;
}

function attr(markup, pattern) {
    const match = new RegExp(pattern).exec(markup);
    return match ? match[1] : null;
}

function optionsOf(markup, selectId) {
    const at = markup.indexOf('id="' + selectId + '"');
    if (at < 0) return [];
    const rest = markup.slice(at, markup.indexOf('</select>', at));
    return (rest.match(/value="([^"]*)"/g) || []).map((token) => token.replace(/.*value="([^"]*)".*/, '$1'));
}

function toasts(env) {
    return env.evaluate("(document.getElementById('toastRoot').__children || []).map((el) => el.textContent)");
}

function render(env) {
    return env.evaluate("document.getElementById('adminContent').innerHTML");
}

function consoleEnv(role) {
    const actor = role === 'admin' ? OPERATOR : ACTOR;
    createFail = false;
    linkFail = false;
    // Each scenario counts its own requests: a shared tally would make "one tap, one
    // account" true only for whichever scenario ran first.
    createCalls.length = 0;
    linkCalls.length = 0;
    intakeCalls.length = 0;
    intakeOpen = true;
    takenIds.length = 0;
    const env = boot();
    env.setResponder(responders);
    env.evaluate('State.saveUser(' + JSON.stringify({
        id: actor.id, name: actor.name, role: actor.role, token: 'tok-' + actor.id
    }) + ')');
    return env;
}

async function openCreate(env) {
    await env.evaluate("UI.renderAdminTab('Credentials')");
    await env.evaluate("UI_MODULES.openCredentialsMode('create')");
}

async function fill(env, values) {
    for (const [id, value] of Object.entries(values)) {
        env.evaluate("document.getElementById(" + JSON.stringify(id) + ").value = " + JSON.stringify(value));
    }
}

async function openLink(env) {
    await env.evaluate("UI.renderAdminTab('Credentials')");
    await env.evaluate("UI_MODULES.openCredentialsMode('link')");
}

// How many requests a panel added on top of the roster fetch that opened the tab.
function afterOpening(env, count) {
    return env.requests.length - count;
}

const results = {};

// 1. both ways in are offered, and opening the tab asks for nothing but the roster
{
    const env = consoleEnv('head_admin');
    await env.evaluate("UI.renderAdminTab('Credentials')");
    const markup = render(env);
    results.entry = {
        create_button: markup.indexOf('data-open-create') >= 0,
        link_button: markup.indexOf('data-open-link') >= 0,
        // Read through ``evaluate``: the app's ``const`` bindings live in the context the
        // frontend files were run with, so the suite asks that context rather than
        // referencing them directly.
        create_labelled: markup.indexOf(env.evaluate("I18n.__('credentialsNewAccount')")) >= 0,
        link_labelled: markup.indexOf(env.evaluate("I18n.__('credentialsRegistrationLink')")) >= 0,
        link_is_a_registration_link:
            env.evaluate("I18n.__('credentialsRegistrationLink')").indexOf('Registration') >= 0,
        no_create_form_yet: markup.indexOf('data-create-panel') < 0,
        requests: env.requests.map((r) => r.url.replace(/^.*\/api\/v1/, ''))
    };
}

// 2. the create panel: a generated password, the upload rule, and the id rule
{
    const env = consoleEnv('head_admin');
    await env.evaluate("UI.renderAdminTab('Credentials')");
    const rosterOnly = env.requests.length;
    await env.evaluate("UI_MODULES.openCredentialsMode('create')");
    const markup = render(env);
    results.panel = {
        has_panel: markup.indexOf('data-create-panel') >= 0,
        password: inputValue(markup, 'credentialsNewPasswordShown'),
        accept: attr(markup, 'id="credentialsNewPhoto"[^>]*accept="([^"]*)"'),
        states_5mb: markup.indexOf('5 MB') >= 0,
        states_photos_only: /JPEG, PNG/.test(markup),
        // The rule the server enforces, in one sentence that does not change with the
        // role - because neither does the rule.
        id_rule: markup.indexOf(env.evaluate("I18n.__('credentialsIdRule')")) >= 0,
        names_no_band: /(1-499|500-749|750-999|1000-4999|5000\+)/.test(markup) === false,
        roles: optionsOf(markup, 'credentialsNewRole'),
        selected_role: attr(markup, '<option value="([^"]*)" selected>'),
        requests: env.requests.length - rosterOnly
    };
    // A standard admin cannot create administrators anywhere on the server, so the option
    // is absent: a choice that always answers 403 is not a choice.
    const limited = consoleEnv('admin');
    await openCreate(limited);
    results.panel.roles_for_standard_admin = optionsOf(render(limited), 'credentialsNewRole');
}

// 3. the photo rule, checked before anything is uploaded
{
    const env = consoleEnv('head_admin');
    await openCreate(env);
    const check = (file) => env.evaluate('UI_MODULES.checkPhotoFile(' + JSON.stringify(file) + ')');
    results.photo_rules = {
        too_large: check({ name: 'big.jpg', size: 6 * 1024 * 1024, type: 'image/jpeg' }),
        pdf: check({ name: 'cv.pdf', size: 1200, type: 'application/pdf' }),
        empty: check({ name: 'nothing.jpg', size: 0, type: 'image/jpeg' }),
        jpeg: check({ name: 'me.jpg', size: 900000, type: 'image/jpeg' }),
        png: check({ name: 'me.png', size: 900000, type: 'image/png' }),
        webp: check({ name: 'me.webp', size: 900000, type: 'image/webp' }),
        extension_fallback: check({ name: 'IMG_2026.JPG', size: 900000, type: '' }),
        nothing_chosen: check(null),
        policy: env.evaluate("UI_MODULES.PHOTO_POLICY.maxBytes")
    };

    // And the refusal is what the panel does: nothing is held, the reason is on screen,
    // and no request is made.
    await env.evaluate(
        "UI_MODULES.pickCredentialsPhoto({ files: [{ name: 'big.jpg', size: 6291456, type: 'image/jpeg' }] })"
    );
    const refused = {
        held: env.evaluate('UI_MODULES._credentialsNewPhoto'),
        error: env.evaluate('UI_MODULES._credentialsPhotoError'),
        on_screen: render(env).indexOf('data-photo-error') >= 0,
        toast: toasts(env).slice(-1)[0],
        requests: env.requests.filter((r) => r.url.indexOf('/create') >= 0).length
    };
    // A real image is held, shown with its size, and can be removed again.
    await env.evaluate(
        "globalThis.__photo = (function () { const b = new Blob([new Uint8Array([0xff,0xd8,0xff,0xe0])], { type: 'image/jpeg' }); b.name = 'me.jpg'; return b; })()"
    );
    await env.evaluate("UI_MODULES.pickCredentialsPhoto({ files: [globalThis.__photo] })");
    const chosen = {
        held: env.evaluate('UI_MODULES._credentialsNewPhoto && UI_MODULES._credentialsNewPhoto.name'),
        error_cleared: env.evaluate('UI_MODULES._credentialsPhotoError'),
        shown: render(env).indexOf('data-photo-chosen') >= 0,
        size_shown: render(env).indexOf('0.0 MB') >= 0
    };
    await env.evaluate("UI_MODULES.clearCredentialsPhoto()");
    results.photo_pick = {
        refused: refused,
        chosen: chosen,
        after_clear: env.evaluate('UI_MODULES._credentialsNewPhoto')
    };
}

// 4. creating an account: every field and the photo travel, the password is shown once
{
    const env = consoleEnv('head_admin');
    await openCreate(env);
    const shown = html_module_unescape(inputValue(render(env), 'credentialsNewPasswordShown'));
    await fill(env, {
        credentialsNewId: '42',
        credentialsNewName: 'New Worker',
        credentialsNewEmail: 'new@example.test',
        credentialsNewPhone: '+201000000042'
    });
    await env.evaluate(
        "globalThis.__photo = (function () { const b = new Blob([new Uint8Array([0xff, 0xd8, 0xff, 0xe0])], { type: 'image/jpeg' }); b.name = 'me.jpg'; return b; })()"
    );
    await env.evaluate("UI_MODULES.pickCredentialsPhoto({ files: [globalThis.__photo] })");
    await env.evaluate("UI_MODULES.createCredentialsAccount()");

    const call = createCalls[createCalls.length - 1];
    const body = call.body;
    results.create_call = {
        count: createCalls.length,
        path: call.url.replace(/^.*\/api\/v1/, ''),
        method: call.method,
        user_id: body.get('user_id'),
        name: body.get('name'),
        role: body.get('role'),
        email: body.get('email'),
        phone: body.get('phone'),
        password_matches_shown: body.get('password') === shown,
        photo_attached: body.has('photo'),
        photo_is_a_file: typeof body.get('photo') === 'object' && String(body.get('photo')) !== '[object Object]',
        content_type_not_forced: !call.headers['Content-Type'],
        authorized: call.headers['Authorization']
    };

    const markup = render(env);
    results.created = {
        panel_for: attr(markup, 'data-account-created="([^"]*)"'),
        shown: shown,
        sent: body.get('password'),
        revealed: html_module_unescape(inputValue(markup, 'credentialsCreatedPassword')),
        has_copy: markup.indexOf('copyCredentialsPassword') >= 0,
        // The roster is refetched, so the row on screen is the server's answer.
        roster_requests: env.requests.filter((r) => r.url.indexOf('/admin/users') >= 0 && r.url.indexOf('/create') < 0).length,
        toast: toasts(env).slice(-1)[0]
    };
    await env.evaluate("UI_MODULES.copyCredentialsPassword()");
    results.created.copied = env.copiedUrls.slice(-1)[0];
    await env.evaluate("UI_MODULES.closeCredentialsMode()");
    const closed = render(env);
    results.created.forgotten_after_close = closed.indexOf('credentialsCreatedPassword') < 0;
}

// 4b. a server refusal is the reason, not a success, and no password is revealed
{
    const env = consoleEnv('head_admin');
    await openCreate(env);
    await fill(env, { credentialsNewId: '42', credentialsNewName: 'New Worker' });
    createFail = true;
    await env.evaluate("UI_MODULES.createCredentialsAccount()");
    results.create_refused = {
        panel: render(env).indexOf('data-account-created') >= 0,
        toast: toasts(env).slice(-1)[0],
        calls: createCalls.length
    };
}

// 4c. the new account's password can be typed, and it survives the repaint that follows
//
// The panel opens on a generated password and the field used to be read-only. It is an
// input now, so the value has to live in the draft rather than only in the DOM: changing
// the role re-renders every field in this form, and a password typed beside them must not
// be the one thing that disappears.
{
    const env = consoleEnv('head_admin');
    await openCreate(env);
    const panel = render(env);
    const generated = html_module_unescape(inputValue(panel, 'credentialsNewPasswordShown'));
    const typed = 'Chosen-For-Ali-2026!';
    await env.evaluate("UI_MODULES.setCredentialsNewPassword(" + JSON.stringify(typed) + ")");
    await env.evaluate("UI_MODULES.credentialsRoleChanged('moallem')");
    const afterRoleChange = html_module_unescape(inputValue(render(env), 'credentialsNewPasswordShown'));
    await fill(env, { credentialsNewId: '612', credentialsNewName: 'Typed Password' });
    await env.evaluate("UI_MODULES.createCredentialsAccount()");
    const call = createCalls[createCalls.length - 1];
    results.typed_create = {
        field_is_editable: /<input[^>]*id="credentialsNewPasswordShown"(?![^>]* readonly)/.test(panel),
        has_input_handler: panel.indexOf('oninput="UI_MODULES.setCredentialsNewPassword(this.value)"') >= 0,
        generated: generated,
        password: typed,
        after_role_change: afterRoleChange,
        sent: call ? call.body.get('password') : null,
        calls: createCalls.length
    };
}

// 5. an id that is not a whole number is refused before the round trip
{
    const env = consoleEnv('head_admin');
    await openCreate(env);
    await fill(env, { credentialsNewId: '12a', credentialsNewName: 'Not a Number' });
    const before = createCalls.length;
    await env.evaluate("UI_MODULES.createCredentialsAccount()");
    results.id_refused = {
        toast: toasts(env).slice(-1)[0],
        said: env.evaluate("I18n.__('credentialsIdWholeNumber')"),
        called: createCalls.length - before,
        // And nothing was created, so the roster still shows the accounts it had.
        panel: render(env).indexOf('data-account-created') >= 0
    };
}

// 5a. the blocks are gone: an id they refused is sent, and the server decides
{
    const env = consoleEnv('head_admin');
    await openCreate(env);
    // 750-999 belonged to off-office workers, so this was refused here for a worker
    // without the server ever seeing it.
    await fill(env, { credentialsNewId: '900', credentialsNewName: 'Out of the Old Block' });
    const before = createCalls.length;
    await env.evaluate("UI_MODULES.createCredentialsAccount()");
    const calls = createCalls.slice(before);
    const last = calls.length ? calls[calls.length - 1] : null;
    results.old_block_id = {
        called: calls.length,
        sent: last ? last.body.get('user_id') : null,
        role: last ? last.body.get('role') : null
    };
}

// 5b. the panel keeps what was typed when the role changes
{
    const env = consoleEnv('head_admin');
    await openCreate(env);
    await fill(env, { credentialsNewId: '642', credentialsNewName: 'Lead Worker' });
    await env.evaluate("UI_MODULES.credentialsRoleChanged('moallem')");
    const markup = render(env);
    results.draft = {
        keeps_id: markup.indexOf('value="642"') >= 0,
        keeps_name: markup.indexOf('value="Lead Worker"') >= 0,
        selected: /<option value="moallem" selected>/.test(markup),
        note_unchanged: markup.indexOf(env.evaluate("I18n.__('credentialsIdRule')")) >= 0
    };
}

// 6. the registration link: it is read, not created, and it is offered back to be sent
{
    const env = consoleEnv('head_admin');
    await env.evaluate("UI.renderAdminTab('Credentials')");
    const rosterOnly = env.requests.length;
    await env.evaluate("UI_MODULES.openCredentialsMode('link')");
    const markup = render(env);
    results.link_panel = {
        has_panel: markup.indexOf('data-registration-link="true"') >= 0,
        url_field: markup.indexOf('credentialsLinkUrl') >= 0,
        // No field of its own: the link is not *for* anybody, which is the whole difference
        // between it and the enrollment link this panel used to hold.
        has_worker_field: markup.indexOf('credentialsLinkId') >= 0,
        has_name_field: markup.indexOf('credentialsLinkName') >= 0,
        asks_on_open: afterOpening(env, rosterOnly),
        // Whether the form is accepting is answered beside the URL, and moved beside it too:
        // the panel is where an administrator stands when they send the link.
        accepting: attr(markup, 'data-link-accepting="([^"]*)"'),
        why: attr(markup, 'data-link-why="([^"]*)"'),
        has_replace: markup.indexOf('replaceCredentialsLink') >= 0
    };

    const call = linkCalls[linkCalls.length - 1];
    results.link_call = {
        count: linkCalls.length,
        path: call.url.replace(/^.*\/api\/v1/, ''),
        method: call.method,
        authorized: call.headers['Authorization']
    };

    const shown = html_module_unescape(inputValue(markup, 'credentialsLinkUrl'));
    await env.evaluate("UI_MODULES.copyCredentialsLink()");
    results.link_shown = {
        url: shown,
        qr: markup.indexOf('data:image/png;base64,AAAA') >= 0,
        has_whatsapp: markup.indexOf('shareCredentialsLink') >= 0,
        copied: env.copiedUrls.slice(-1)[0]
    };
    await env.evaluate("UI_MODULES.shareCredentialsLink()");
    const shared = env.lastAnchor();
    results.link_shared = {
        href: shared ? String(shared.href) : null,
        carries_the_link: shared ? String(shared.href).indexOf(encodeURIComponent(shown)) >= 0 : false
    };
}

// 6b. replacing the link: one POST, and the panel shows what came back
{
    const env = consoleEnv('head_admin');
    // The panel asks before it can be replaced, and the confirmation is answered for it - the
    // one dialog in this file, because the action it guards is the only irreversible one here.
    env.evaluate("globalThis.confirm = () => true");
    await openLink(env);
    const before = linkCalls.length;
    await env.evaluate("UI_MODULES.replaceCredentialsLink()");
    const replaced = render(env);
    const call = linkCalls[linkCalls.length - 1];
    results.link_replaced = {
        calls: linkCalls.length - before,
        method: call.method,
        path: call.url.replace(/^.*\/api\/v1/, ''),
        url: html_module_unescape(inputValue(replaced, 'credentialsLinkUrl')),
        toast: toasts(env).slice(-1)[0],
        says: env.evaluate("I18n.__('credentialsRegistrationLinkReplaced')")
    };
}

// 6c. the switch beside the link: the panel handing the URL out is where it gets opened
{
    const env = consoleEnv('head_admin');
    // The state the applicant's own closed page is refusing from - and on this deployment the
    // flag is off underneath it, so this panel is the only lever there is.
    intakeOpen = false;
    await openLink(env);
    const closedMarkup = render(env);
    const lever = attr(closedMarkup, 'data-registration-intake="([^"]*)"');
    await env.evaluate("UI_MODULES.toggleRegistrationsIntake('open')");
    const openedMarkup = render(env);
    results.link_switch = {
        accepting_before: attr(closedMarkup, 'data-link-accepting="([^"]*)"'),
        why_before: attr(closedMarkup, 'data-link-why="([^"]*)"'),
        lever: lever,
        labelled: closedMarkup.indexOf(env.evaluate("I18n.__('registrationsIntakeOpen')")) >= 0,
        posts: intakeCalls.slice(),
        accepting_after: attr(openedMarkup, 'data-link-accepting="([^"]*)"'),
        why_after: attr(openedMarkup, 'data-link-why="([^"]*)"'),
        // The repaint follows the reader rather than the lever: the switch has two homes and
        // this is the one the panel's reader was standing in.
        still_the_panel: openedMarkup.indexOf('data-registration-link="true"') >= 0,
        tab: env.evaluate('State.adminTab'),
        url_kept: html_module_unescape(inputValue(openedMarkup, 'credentialsLinkUrl')),
        toast: toasts(env).slice(-1)[0],
        says: env.evaluate("I18n.__('registrationsIntakeOpened')")
    };
}

// 6d. a link the server will not hand over is reported, and nothing is offered as if it existed
{
    const env = consoleEnv('head_admin');
    linkFail = true;
    await openLink(env);
    const failed = render(env);
    results.link_refused = {
        panel: failed.indexOf('data-registration-link="true"') >= 0,
        says_so: failed.indexOf(env.evaluate("I18n.__('credentialsRegistrationLinkFailed')")) >= 0,
        offers_retry: failed.indexOf('loadCredentialsLink') >= 0,
        toast: toasts(env).slice(-1)[0],
        calls: linkCalls.length
    };
}

// 6e. a taken id comes back with a free one beside it, and one tap takes it
{
    const env = consoleEnv('head_admin');
    await openCreate(env);
    // 1000 is the standard admin on the roster: taken, and the walk starts above it.
    takenIds.push('1000');
    await fill(env, { credentialsNewId: '1000', credentialsNewName: 'Clashing Admin' });
    const before = createCalls.length;
    await env.evaluate("UI_MODULES.createCredentialsAccount()");
    const markup = render(env);
    results.id_taken = {
        asked: createCalls.length - before,
        server_said: env.evaluate('UI_MODULES.TAKEN_ID_REFUSAL'),
        toast: toasts(env).slice(-1)[0],
        note: attr(markup, 'data-id-taken="([^"]*)"'),
        offers: attr(markup, 'data-use-free-id="([^"]*)"'),
        highlighted: /id="credentialsNewId"[^>]*class="[^"]*is-danger/.test(markup),
        // The sentence beside the field, with this id in it - the value the reader needs.
        said: markup.indexOf(
            env.evaluate("I18n.__('credentialsIdTaken')").replace('{id}', '1000')
        ) >= 0,
        // Nothing was created, and no password was revealed for an account that is not there.
        created_panel: markup.indexOf('data-account-created') >= 0
    };

    // One tap: the field carries the free number, the refusal goes away with it, and the
    // retry is a create rather than the same refusal again.
    await env.evaluate('UI_MODULES.useSuggestedCredentialsId()');
    const adoptedMarkup = render(env);
    const sent = createCalls.length;
    await env.evaluate('UI_MODULES.createCredentialsAccount()');
    results.id_adopted = {
        field: inputValue(adoptedMarkup, 'credentialsNewId'),
        note_gone: adoptedMarkup.indexOf('data-id-taken') < 0,
        still_highlighted: /id="credentialsNewId"[^>]*class="[^"]*is-danger/.test(adoptedMarkup),
        created: createCalls.length - sent,
        panel_for: attr(render(env), 'data-account-created="([^"]*)"')
    };
}

// 7. the link is the same link until somebody replaces it, and it is read every time
{
    const env = consoleEnv('head_admin');
    await openLink(env);
    const url = html_module_unescape(inputValue(render(env), 'credentialsLinkUrl'));
    // Closed and opened again: the panel asks the server a second time rather than reusing
    // what it painted, because another administrator may have replaced the link since.
    await env.evaluate("UI_MODULES.closeCredentialsMode()");
    await env.evaluate("UI_MODULES.openCredentialsMode('link')");
    const again = html_module_unescape(inputValue(render(env), 'credentialsLinkUrl'));
    results.link_read_again = {
        first: url,
        second: again,
        calls: linkCalls.length,
        same: url === again
    };
}

// 8. closing either panel leaves the roster exactly as it was
{
    const env = consoleEnv('head_admin');
    await openCreate(env);
    await env.evaluate("UI_MODULES.closeCredentialsMode()");
    const closed = render(env);
    results.closed = {
        no_panel:
            closed.indexOf('data-create-panel') < 0 &&
            closed.indexOf('data-registration-link') < 0,
        still_has_rows: closed.indexOf('data-user=') >= 0,
        still_has_both_buttons: closed.indexOf('data-open-create') >= 0 && closed.indexOf('data-open-link') >= 0,
        requests: env.requests.filter((r) => r.url.indexOf('/create') >= 0 || r.url.indexOf('/registrations/link') >= 0).length
    };
}
"""

# The harness is a JavaScript string, and the page escapes what it puts in an attribute:
# comparing a shown value against the value that was sent has to unescape first.
HARNESS = HARNESS.replace(
    "html_module_unescape",
    "((value) => value === null ? null : value"
    ".replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '\"')"
    ".replace(/&#39;/g, \"'\").replace(/&amp;/g, '&'))",
)


@pytest.fixture(scope="module")
def results() -> dict:
    return frontend_vm.run(HARNESS)


def unescaped(value: str) -> str:
    return html_module.unescape(value)


def test_both_ways_to_start_an_account_are_on_the_credentials_tab(results):
    """A button that creates an account, and a button that hands out the way to apply."""
    assert results["entry"]["create_button"] is True
    assert results["entry"]["link_button"] is True
    assert results["entry"]["create_labelled"] is True
    assert results["entry"]["link_labelled"] is True
    assert results["entry"]["link_is_a_registration_link"] is True, (
        "the button hands out the registration link: somebody who does not exist yet is the "
        "case this screen cannot answer by creating an account, and the enrollment link it "
        "used to hold (for an account that already exists) is gone from here"
    )
    assert results["entry"]["no_create_form_yet"] is True, "the form opens on request, not by default"
    assert results["entry"]["requests"] == ["/admin/users"], "opening a tab fetches the roster and nothing else"


def test_the_create_panel_states_the_policy_and_picks_a_password(results):
    panel = results["panel"]
    assert panel["has_panel"] is True
    password = unescaped(panel["password"])
    assert password and len(password) >= 16, "a generated password, satisfying the server's policy by construction"
    assert panel["accept"] == "image/jpeg,image/png,image/webp", (
        "the picker must not invite a document it will only refuse after the upload"
    )
    assert panel["states_5mb"] is True
    assert panel["states_photos_only"] is True
    assert panel["id_rule"] is True, "the panel states the rule the server applies: a whole number"
    assert panel["names_no_band"] is True, (
        "the deleted per-role id blocks must not survive on screen: the server refuses no id "
        "for falling outside one, and a form that says otherwise sends the admin looking for "
        "a block that no longer exists"
    )
    assert panel["selected_role"] == "worker", "the default role, before anything is chosen"
    assert panel["roles"] == ["worker", "moallem", "off_office", "admin", "head_admin"]
    assert panel["requests"] == 0, "rendering a form is not a reason to talk to the server"


def test_a_standard_admin_is_not_offered_roles_it_cannot_create(results):
    """The server refuses these; offering them would be a button that always fails."""
    assert results["panel"]["roles_for_standard_admin"] == ["worker", "moallem", "off_office"]


def test_the_upload_rule_is_enforced_before_anything_is_uploaded(results):
    rules = results["photo_rules"]
    assert rules["policy"] == 5 * 1024 * 1024
    assert rules["too_large"] and "5 MB" in rules["too_large"]
    assert rules["pdf"] and "not a photo" in rules["pdf"]
    assert rules["empty"]
    assert rules["jpeg"] is None and rules["png"] is None and rules["webp"] is None
    assert rules["extension_fallback"] is None, "a camera-app .JPG with no reported type is still a photo"
    assert rules["nothing_chosen"] is None, "choosing no photo is allowed - it is simply not a face"

    refused = results["photo_pick"]["refused"]
    assert refused["held"] is None, "a refused file must not be held for a later submit"
    assert "5 MB" in refused["error"]
    assert refused["on_screen"] is True, "the admin is told in the panel, not only in a toast"
    assert "5 MB" in refused["toast"]
    assert refused["requests"] == 0, "and nothing was sent"

    chosen = results["photo_pick"]["chosen"]
    assert chosen["held"] == "me.jpg"
    assert chosen["error_cleared"] == ""
    assert chosen["shown"] is True
    assert results["photo_pick"]["after_clear"] is None


def test_creating_an_account_sends_every_field_and_the_photo(results):
    call = results["create_call"]
    assert call["count"] == 1, "one tap, one account"
    assert call["path"].endswith("/admin/users/create")
    assert call["method"] == "POST"
    assert call["user_id"] == "42"
    assert call["name"] == "New Worker"
    assert call["role"] == "worker"
    assert call["email"] == "new@example.test"
    assert call["phone"] == "+201000000042"
    assert call["password_matches_shown"] is True, "the password that is shown is the password that is set"
    assert call["photo_attached"] is True and call["photo_is_a_file"] is True
    assert call["content_type_not_forced"] is True, "a multipart body sets its own boundary"
    assert call["authorized"] == "Bearer tok-5000"


def test_the_created_password_is_shown_once_and_then_forgotten(results):
    created = results["created"]
    assert created["panel_for"] == "42"
    assert created["shown"] == created["sent"], "the password shown is the one that was set"
    assert created["revealed"] == created["shown"], "and it is readable exactly here"
    assert created["has_copy"] is True
    assert created["roster_requests"] == 2, "the row that appears is the server's answer, so the roster is refetched"
    assert created["copied"] == created["shown"], "Copy hands over what was set, not a placeholder"
    assert created["forgotten_after_close"] is True, "nothing stored it, so closing is the whole cleanup"


def test_a_refused_creation_is_reported_as_the_reason(results):
    refused = results["create_refused"]
    assert refused["calls"] == 1, "the server is the authority on the id"
    assert refused["panel"] is False, "no password is revealed for an account that was not created"
    assert "already exists" in refused["toast"]


def test_a_taken_id_comes_back_with_a_free_one_beside_it(results):
    """The one refusal the form can answer itself, so it answers it.

    An id already on the roster is the single 400 this endpoint returns that the console has
    the answer to: the roster it is already holding says which numbers are free. The admin
    still reads the server's sentence, and the fix is offered beside the box that has to
    change rather than applied behind their back.
    """
    taken = results["id_taken"]
    assert taken["asked"] == 1, "the server is still the authority on which ids are free"
    assert taken["toast"] == taken["server_said"], "the admin reads the server's own sentence"
    assert taken["note"] == "1000", "the note names the id that was refused"
    assert taken["said"] is True, "and it says so in the reader's language, with the id in it"
    assert taken["highlighted"] is True, "the box that has to change is pointed at"
    assert taken["offers"] == "1001", "the next free number, out of the roster the panel holds"
    assert taken["created_panel"] is False, "and no account was created"


def test_the_suggested_id_is_taken_in_one_tap(results):
    adopted = results["id_adopted"]
    assert adopted["field"] == "1001", "one tap fills the id box"
    assert adopted["note_gone"] is True, "the refusal was about an id that is no longer there"
    assert adopted["still_highlighted"] is False, "so the highlight goes with it"
    assert adopted["created"] == 1, "and the next click is a create"
    assert adopted["panel_for"] == "1001", "for the account the form now names"


def test_the_console_looks_for_the_sentence_the_server_actually_raises(results):
    """The one thing here identified by its text, so both sides are pinned to each other.

    ``/admin/users/create`` answers a duplicate id with a plain string and no error code, so
    the sentence is the only thing there is to recognise the refusal by. That is the whole
    risk in the feature: reword the server's answer and the form silently goes back to a
    toast with no way out - which is why the string is read out of ``main.py`` here instead
    of being asserted against a copy in this file.
    """
    server = (BACKEND / "main.py").read_text(encoding="utf-8")
    # The endpoint that form posts to, and nothing else: the same duplicate-key shape is used
    # for a site name and a category name, and those sentences are not this one.
    route = server.index('@router.post("/admin/users/create")')
    endpoint = server[route:server.index("@router.", route + 10)]
    raised = set(
        re.findall(
            r'except sqlite3\.IntegrityError:\s*\n\s*raise HTTPException\(\s*'
            r'status_code=400,\s*detail="([^"]*)"',
            endpoint,
        )
    )
    assert raised, "the create endpoint no longer refuses a duplicate id as a plain sentence"
    assert raised == {results["id_taken"]["server_said"]}, (
        f"the console looks for {results['id_taken']['server_said']!r} and the server answers "
        f"{sorted(raised)}: the form would stop offering a free id"
    )


def test_an_id_that_is_not_a_whole_number_never_leaves_the_browser(results):
    refused = results["id_refused"]
    assert refused["called"] == 0
    assert refused["toast"] == refused["said"], (
        "the refusal names the rule the server would have named"
    )
    assert refused["panel"] is False


def test_an_id_the_old_per_role_block_refused_is_created_anyway(results):
    """The blocks are gone, and the form must not be the last place still enforcing them.

    ``900`` is a worker in this scenario. The deleted blocks handed 750-999 to off-office
    workers, so the console refused this id without asking, for a rule the server no longer
    has; whether 900 is a free number is the server's answer, and the round trip happens.
    """
    sent = results["old_block_id"]
    assert sent["called"] == 1, "the server is the authority on which ids it accepts"
    assert sent["sent"] == "900"
    assert sent["role"] == "worker"


def test_the_form_keeps_what_was_typed_when_the_role_changes(results):
    draft = results["draft"]
    assert draft["keeps_id"] is True, "repainting must not empty a form the admin is filling in"
    assert draft["keeps_name"] is True
    assert draft["selected"] is True, "the role select follows the change"
    assert draft["note_unchanged"] is True, "the id rule does not depend on the role"


def test_the_new_account_password_can_be_typed_instead_of_generated(results):
    typed = results["typed_create"]
    assert typed["field_is_editable"] is True, "a read-only field cannot take a typed password"
    assert typed["has_input_handler"] is True, "and nothing would reach the draft without one"
    assert typed["after_role_change"] == typed["password"], (
        "the form repaints for other reasons, so a typed password has to live in the draft"
    )
    assert typed["calls"] == 1, "one account, one request"
    assert typed["sent"] == typed["password"], "the typed password is what the server is told"
    assert typed["sent"] != typed["generated"], "and not the one the field opened on"


def test_the_link_panel_reads_the_link_instead_of_creating_one(results):
    """There is no form here: the link exists, and the panel asks the server what it is.

    That is the difference from the enrollment link this panel used to hold - that one was
    *minted* per account, so it had an id field and a Create button and a warning that the
    token could never be shown again. This one is the address of a form, so the panel's whole
    job is to hand it over: one read on the way in, and the URL, the copy button, the WhatsApp
    message and the QR code beside it.
    """
    panel = results["link_panel"]
    assert panel["has_panel"] is True
    assert panel["url_field"] is True, "the panel has nowhere to show the link it read"
    assert panel["has_worker_field"] is False, "an account id here would be the old per-person link"
    assert panel["has_name_field"] is False, "a name here would be a second place to write one"
    assert panel["asks_on_open"] == 1, "opening the panel asks for the link, and nothing else"
    assert panel["has_replace"] is True, "the only way back from a link that leaked"

    call = results["link_call"]
    assert call["count"] == 1
    assert call["path"].endswith("/admin/registrations/link")
    assert call["method"] == "GET", "reading the link must not mint or rotate anything"
    assert call["authorized"] == "Bearer tok-5000"


def test_the_acceptance_state_travels_with_the_link(results):
    """An administrator about to send a link is told whether the form will take it.

    Two objects, and the panel says so: the link is what makes the form *reachable* and the
    intake switch is what makes it *accept*. Handing out a URL while the switch is shut is the
    one way this panel can waste somebody's message, so the answer comes back with the URL
    rather than being discovered by the applicant.
    """
    panel = results["link_panel"]
    assert panel["accepting"] == "true"
    assert panel["why"] == "open", "the server's own reason code, not a guess from the flag"


def test_the_link_panel_opens_the_closed_form_it_is_handing_out(results):
    """The switch is drawn where the link is, and it moves the form the applicant is looking at.

    This is the half that made the feature work for the person the form refuses: the public page
    says "ask your site administrator to open it", and a panel that only *reported* the state - in
    a different tab, behind a switch that a deployment shipping with registration off never drew
    - left that sentence with nobody who could act on it. Here the lever is beside the URL, on a
    deployment whose own default is closed, and the panel is redrawn from the server's answer.
    """
    switch = results["link_switch"]
    assert switch["accepting_before"] == "false", "this deployment starts with the form shut"
    assert switch["why_before"] == "closed_by_default", (
        "the panel did not say why the form it is handing out refuses everybody"
    )
    assert switch["lever"] == "open", "the one lever that can open the form was not drawn"
    assert switch["labelled"], "the lever has no words on it"
    assert switch["posts"] == [{"open": True}], switch["posts"]
    assert switch["accepting_after"] == "true", "the form opened and the panel still says closed"
    assert switch["why_after"] == "open", switch["why_after"]
    assert switch["says"] in (switch["toast"] or ""), "a silent switch is one nobody trusts"
    # ...and the reader stays where they were: the switch has two homes, and this is the one
    # they pressed it in (the URL they were about to send is still on the screen).
    assert switch["still_the_panel"] is True, "the panel was replaced by the queue"
    assert switch["tab"] == "Credentials", switch["tab"]
    assert switch["url_kept"].endswith("/register/0.tok-one"), switch["url_kept"]


def test_the_link_is_copyable_shareable_and_carries_a_qr(results):
    shown = results["link_shown"]
    assert shown["url"].endswith("/register/0.tok-one"), (
        "the panel invented a URL instead of showing the one the server sent"
    )
    assert shown["copied"] == shown["url"], "Copy hands over the link, not something that looks like it"
    assert shown["qr"] is True, "the QR is offered when the server can render one"
    assert shown["has_whatsapp"] is True

    shared = results["link_shared"]
    assert shared["href"] and shared["href"].startswith("https://wa.me/?text=")
    assert shared["carries_the_link"] is True, "the message carries the link itself, ready to send"


def test_replacing_the_link_posts_once_and_shows_what_came_back(results):
    """Replacing is the revocation, so it is a POST that hands back the link to use now.

    The panel must not keep painting the URL it read on the way in: the whole point of the
    action is that the old address stops working, so a screen still showing it would be a
    screen handing out a dead link.
    """
    replaced = results["link_replaced"]
    assert replaced["calls"] == 1, "replacing hands out one link, not a second one behind it"
    assert replaced["method"] == "POST"
    assert replaced["path"].endswith("/admin/registrations/link")
    assert replaced["url"].endswith("/register/1.tok-two"), (
        "the panel is still showing the link it read before it was replaced"
    )
    assert replaced["toast"] == replaced["says"], (
        "the administrator is told the replace happened"
    )


def test_a_link_the_server_will_not_hand_over_says_so_and_offers_a_retry(results):
    """The read can fail, and the answer is the panel's own sentence - not a blank panel."""
    refused = results["link_refused"]
    assert refused["calls"] == 1
    assert refused["panel"] is False, "a URL was painted for a link the server never sent"
    assert refused["says_so"] is True, "the panel does not say the read failed"
    assert refused["offers_retry"] is True, "there is nothing one-shot here, so a retry is offered"
    assert "could not be read" in refused["toast"], "the server's reason, not a success"


def test_the_link_is_the_same_link_until_it_is_replaced_and_is_read_every_time(results):
    """Closed and opened again, the panel asks rather than reusing the screen it painted.

    The link is stable - it is a function of the deployment's secret and one integer, so there
    is nothing that goes stale by itself - but the *panel* must not be the authority on it:
    another administrator can replace the link between two openings, and a screen that reused
    the first answer would hand out a URL that no longer opens anything.
    """
    read = results["link_read_again"]
    assert read["same"] is True, "the link changed without anybody replacing it"
    assert read["calls"] == 2, "the second opening reused the first answer instead of asking"


def test_closing_a_panel_returns_to_the_roster_untouched(results):
    closed = results["closed"]
    assert closed["no_panel"] is True
    assert closed["still_has_rows"] is True
    assert closed["still_has_both_buttons"] is True
    assert closed["requests"] == 0, "closing a form is not an event the server needs to hear about"
