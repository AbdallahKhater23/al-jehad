"""Creating accounts from the console, exercised in a real browser-like environment.

WHY THIS EXISTS
---------------
The Credentials tab can now start an account in the two ways an administrator actually
needs: type it in here (id, name, role, generated password, and a photo that registers the
face), or send a one-time link and let the person do it themselves. The API tests in
``test_account_creation.py`` prove the server does the right thing; what they cannot see is
whether the screen reaches it - which fields travel, whether the photo is attached, whether
the password is shown once, and whether a 6 MB photo is refused before it is uploaded.

So this suite drives the real ``admin_modules.js``:

1. both actions are offered, and merely opening the tab posts nothing;
2. the create panel generates a password, states the 5 MB / photos-only rule, and its file
   picker accepts exactly the three image types the server accepts;
3. a photo that is too big, or is not a photo, is refused *client-side*, with the reason on
   screen, and the account is not sent at all;
4. creating posts a multipart body carrying every field and the photo itself, and shows the
   password exactly once afterwards - then forgets it when the panel closes;
5. an id outside the role's range never leaves the browser;
6. issuing a link posts the register kind with the admin's name and role, and the answer is
   offered as a copyable URL, a WhatsApp message and a QR code;
7. a server refusal (an id already taken) is reported as the reason, not as a success.

Node is optional; without it these skip rather than fail.
"""

from __future__ import annotations

import html as html_module

import pytest

import frontend_vm

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
const inviteCalls = [];
let createFail = false;
let inviteFail = false;

function responders(url, init) {
    // The create endpoint lives under /admin/users, so it is matched first: a roster
    // answer for a create request would look like a success and hide the bug.
    if (url.indexOf('/admin/users/create') >= 0) {
        const body = (init && init.body) || null;
        createCalls.push({ url: String(url), method: (init && init.method) || 'GET', body: body, headers: (init && init.headers) || {} });
        if (createFail) return { status: 409, body: { detail: 'User ID already exists.' } };
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
    if (url.indexOf('/admin/enrollment/invites') >= 0) {
        inviteCalls.push({ url: String(url), method: (init && init.method) || 'GET', body: (init && init.body) || null, headers: (init && init.headers) || {} });
        if (inviteFail) return { status: 409, body: { detail: 'Account 77 already exists. Reserve a free id.' } };
        return {
            status: 200,
            body: {
                status: 'success', kind: 'register', invite_id: 9, worker_id: '77',
                worker_name: 'Link Worker', role: 'worker',
                url: 'https://site.example.test/enroll/tok-one-time',
                token: 'tok-one-time', expires_at: '2026-09-18 12:00:00', max_uses: 1,
                qr_png_data_uri: 'data:image/png;base64,AAAA',
                note: 'Send this link once.'
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
    inviteFail = false;
    // Each scenario counts its own requests: a shared tally would make "one tap, one
    // account" true only for whichever scenario ran first.
    createCalls.length = 0;
    inviteCalls.length = 0;
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

async function openInvite(env) {
    await env.evaluate("UI.renderAdminTab('Credentials')");
    await env.evaluate("UI_MODULES.openCredentialsMode('invite')");
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
        invite_button: markup.indexOf('data-open-invite') >= 0,
        // Read through ``evaluate``: the app's ``const`` bindings live in the context the
        // frontend files were run in, so the suite asks that context rather than
        // referencing them directly.
        create_labelled: markup.indexOf(env.evaluate("I18n.__('credentialsNewAccount')")) >= 0,
        invite_labelled: markup.indexOf(env.evaluate("I18n.__('credentialsLink')")) >= 0,
        no_create_form_yet: markup.indexOf('data-create-panel') < 0,
        requests: env.requests.map((r) => r.url.replace(/^.*\/api\/v1/, ''))
    };
}

// 2. the create panel: a generated password, the upload rule, the id range
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
        id_range: attr(markup, 'data-id-range="([^"]*)"'),
        range_shown: markup.indexOf('1-499') >= 0,
        roles: optionsOf(markup, 'credentialsNewRole'),
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

// 5. an id outside the role's range never leaves the browser
{
    const env = consoleEnv('head_admin');
    await openCreate(env);
    await fill(env, { credentialsNewId: '900', credentialsNewName: 'Wrong Block' });
    const before = createCalls.length;
    await env.evaluate("UI_MODULES.createCredentialsAccount()");
    results.range_refused = {
        toast: toasts(env).slice(-1)[0],
        called: createCalls.length - before,
        // And nothing was created, so the roster still shows the accounts it had.
        panel: render(env).indexOf('data-account-created') >= 0
    };
}

// 5b. the panel keeps what was typed when the role - and therefore the hint - changes
{
    const env = consoleEnv('head_admin');
    await openCreate(env);
    await fill(env, { credentialsNewId: '642', credentialsNewName: 'Lead Worker' });
    await env.evaluate("UI_MODULES.credentialsRoleChanged('moallem')");
    const markup = render(env);
    results.draft = {
        keeps_id: markup.indexOf('value="642"') >= 0,
        keeps_name: markup.indexOf('value="Lead Worker"') >= 0,
        role: attr(markup, 'data-id-range="([^"]*)"'),
        range_shown: markup.indexOf('500-999') >= 0,
        selected: /<option value="moallem" selected>/.test(markup)
    };
}

// 6. the registration link: what is posted, and what is offered back
{
    const env = consoleEnv('head_admin');
    await env.evaluate("UI.renderAdminTab('Credentials')");
    const rosterOnly = env.requests.length;
    await env.evaluate("UI_MODULES.openCredentialsMode('invite')");
    const markup = render(env);
    results.invite_panel = {
        has_panel: markup.indexOf('data-invite-panel') >= 0,
        roles: optionsOf(markup, 'credentialsLinkRole'),
        requests: afterOpening(env, rosterOnly)
    };
    await fill(env, {
        credentialsLinkId: '77',
        credentialsLinkName: 'Link Worker',
        credentialsLinkEmail: 'link@example.test',
        credentialsLinkPhone: '+201000000077'
    });
    await env.evaluate("UI_MODULES.issueCredentialsLink()");

    const call = inviteCalls[inviteCalls.length - 1];
    const sent = JSON.parse(call.body);
    results.invite_call = {
        count: inviteCalls.length,
        path: call.url.replace(/^.*\/api\/v1/, ''),
        method: call.method,
        kind: sent.kind,
        worker_id: sent.worker_id,
        name: sent.name,
        role: sent.role,
        email: sent.email,
        phone: sent.phone,
        authorized: call.headers['Authorization']
    };

    const issued = render(env);
    const url = html_module_unescape(inputValue(issued, 'credentialsLinkUrl'));
    const anchor = env.lastAnchor();
    await env.evaluate("UI_MODULES.copyCredentialsLink()");
    results.invite_issued = {
        panel_for: attr(issued, 'data-link-issued="([^"]*)"'),
        url: url,
        qr: issued.indexOf('data:image/png;base64,AAAA') >= 0,
        expiry_shown: issued.indexOf('2026-09-18 12:00:00') >= 0,
        has_whatsapp: issued.indexOf('shareCredentialsLink') >= 0,
        copied: env.copiedUrls.slice(-1)[0]
    };
    await env.evaluate("UI_MODULES.shareCredentialsLink()");
    const shared = env.lastAnchor();
    results.invite_shared = {
        href: shared ? String(shared.href) : null,
        carries_the_link: shared ? String(shared.href).indexOf(encodeURIComponent(url)) >= 0 : false,
        first_anchor_was_the_same_kind: !!anchor
    };
}

// 6b. a refused invite is reported, and no link is offered as if it existed
{
    const env = consoleEnv('head_admin');
    await openInvite(env);
    await fill(env, { credentialsLinkId: '77', credentialsLinkName: 'Link Worker' });
    inviteFail = true;
    await env.evaluate("UI_MODULES.issueCredentialsLink()");
    results.invite_refused = {
        panel: render(env).indexOf('data-link-issued') >= 0,
        toast: toasts(env).slice(-1)[0],
        calls: inviteCalls.length
    };
}

// 7. the link only needs an id and a name; without them nothing is sent
{
    const env = consoleEnv('head_admin');
    await openInvite(env);
    const before = inviteCalls.length;
    await env.evaluate("UI_MODULES.issueCredentialsLink()");
    await fill(env, { credentialsLinkId: '77' });
    await env.evaluate("UI_MODULES.issueCredentialsLink()");
    results.invite_needs_name = {
        calls: inviteCalls.length - before,
        toast: toasts(env).slice(-1)[0]
    };
}

// 8. closing either panel leaves the roster exactly as it was
{
    const env = consoleEnv('head_admin');
    await openCreate(env);
    await env.evaluate("UI_MODULES.closeCredentialsMode()");
    const closed = render(env);
    results.closed = {
        no_panel: closed.indexOf('data-create-panel') < 0 && closed.indexOf('data-invite-panel') < 0,
        still_has_rows: closed.indexOf('data-user=') >= 0,
        still_has_both_buttons: closed.indexOf('data-open-create') >= 0 && closed.indexOf('data-open-invite') >= 0,
        requests: env.requests.filter((r) => r.url.indexOf('/create') >= 0 || r.url.indexOf('/invites') >= 0).length
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
    """A button that creates an account, and a button that hands the job over."""
    assert results["entry"]["create_button"] is True
    assert results["entry"]["invite_button"] is True
    assert results["entry"]["create_labelled"] is True
    assert results["entry"]["invite_labelled"] is True
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
    assert panel["id_range"] == "worker"
    assert panel["range_shown"] is True, "the id block for the chosen role, before the server has to say it"
    assert panel["roles"] == ["worker", "moallem", "admin", "head_admin"]
    assert panel["requests"] == 0, "rendering a form is not a reason to talk to the server"


def test_a_standard_admin_is_not_offered_roles_it_cannot_create(results):
    """The server refuses these; offering them would be a button that always fails."""
    assert results["panel"]["roles_for_standard_admin"] == ["worker", "moallem"]


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


def test_an_id_outside_the_role_block_never_leaves_the_browser(results):
    refused = results["range_refused"]
    assert refused["called"] == 0
    assert "1-499" in refused["toast"]
    assert refused["panel"] is False


def test_the_form_keeps_what_was_typed_when_the_role_changes(results):
    draft = results["draft"]
    assert draft["keeps_id"] is True, "repainting must not empty a form the admin is filling in"
    assert draft["keeps_name"] is True
    assert draft["role"] == "moallem"
    assert draft["range_shown"] is True
    assert draft["selected"] is True


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


def test_a_registration_link_is_issued_with_the_admins_own_id_name_and_role(results):
    panel = results["invite_panel"]
    assert panel["has_panel"] is True
    assert panel["roles"] == ["worker", "moallem"], "a link may only create a worker or a moallem"
    assert panel["requests"] == 0

    call = results["invite_call"]
    assert call["count"] == 1
    assert call["path"].endswith("/admin/enrollment/invites")
    assert call["method"] == "POST"
    assert call["kind"] == "register"
    assert call["worker_id"] == "77"
    assert call["name"] == "Link Worker"
    assert call["role"] == "worker"
    assert call["email"] == "link@example.test"
    assert call["phone"] == "+201000000077"
    assert call["authorized"] == "Bearer tok-5000"


def test_the_issued_link_is_copyable_shareable_and_shows_its_expiry(results):
    issued = results["invite_issued"]
    assert issued["panel_for"] == "77"
    assert issued["url"].endswith("/enroll/tok-one-time")
    assert issued["copied"] == issued["url"], "Copy hands over the link, not something that looks like it"
    assert issued["qr"] is True, "the QR is offered when the server can render one"
    assert issued["expiry_shown"] is True
    assert issued["has_whatsapp"] is True

    shared = results["invite_shared"]
    assert shared["href"] and shared["href"].startswith("https://wa.me/?text=")
    assert shared["carries_the_link"] is True, "the message carries the link itself, ready to send"


def test_a_refused_link_does_not_pretend_to_exist(results):
    refused = results["invite_refused"]
    assert refused["calls"] == 1
    assert refused["panel"] is False
    assert "already exists" in refused["toast"]


def test_a_link_needs_an_id_and_a_name(results):
    assert results["invite_needs_name"]["calls"] == 0
    assert "ID and a name" in results["invite_needs_name"]["toast"]


def test_closing_a_panel_returns_to_the_roster_untouched(results):
    closed = results["closed"]
    assert closed["no_panel"] is True
    assert closed["still_has_rows"] is True
    assert closed["still_has_both_buttons"] is True
    assert closed["requests"] == 0, "closing a form is not an event the server needs to hear about"
