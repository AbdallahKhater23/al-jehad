"""Editing and removing an account, from the Credentials tab.

The three endpoints this drives (``/admin/users/edit|status|delete``) are covered by
``test_admin_user_management.py``; what can only be checked here is what the console offers
and what it sends:

1. the row carries the three corrections and the account's state, and *not* for an account
   this administrator may not touch (an administrator's row is somebody else's job) or for
   their own (they cannot deactivate or delete themselves, so there is no button to press);
2. the edit form is the record read back from the server, not a guess at it - including the
   hourly rate, which the roster payload deliberately does not carry - and it says why the
   id and the role are not editable instead of showing fields that cannot work;
3. a refusal from the server reaches the administrator as the reason it gave. Deleting an
   account with shifts is the case that matters: it is not an error, it is the endpoint
   saying no, with the count, and offering the thing that can be done instead;
4. the **reference photo** is here too, because it is the one part of a person that is not a
   field on their row: a first photo is what makes the account able to clock in at all and a
   replacement silently stops the template on file working, so the panel says which of the
   two it is about to do and the file travels to the endpoint that takes files.

Node is optional; without it these skip rather than fail.
"""

from __future__ import annotations

import pytest

import frontend_vm

pytestmark = pytest.mark.skipif(frontend_vm.NODE is None, reason="node is not installed")

HARNESS = r"""
// --- the server's answers, faked ----------------------------------------

const HEAD = { id: '5000', name: 'Head Admin', role: 'head_admin' };
const OPS = { id: '1000', name: 'Ops Admin', role: 'admin' };

function roster() {
    return [
        {
            id: '1', name: 'Seed Worker', role: 'worker', phone: '+200000000001',
            email: 'seed@example.test', status: 'active', face_enrolled: true,
            enrolled_at: '2026-08-01 07:00:00', password_set: true,
            password_changed_at: null, sessions_revoked: 0
        },
        {
            // Inactive, and with a past-due name: the two things this screen has to show
            // and could not change before.
            id: '600', name: 'Ana Torrez', role: 'moallem', phone: '', email: 'ana@example.test',
            status: 'inactive', face_enrolled: false, enrolled_at: null, password_set: true,
            password_changed_at: null, sessions_revoked: 1
        },
        {
            id: '1001', name: 'Second Admin', role: 'admin', phone: '', email: 'ops2@example.test',
            status: 'active', face_enrolled: true, enrolled_at: null, password_set: true,
            password_changed_at: null, sessions_revoked: 0
        },
        { ...HEAD, phone: '', email: 'head@example.test', status: 'active', face_enrolled: true,
          enrolled_at: null, password_set: true, password_changed_at: null, sessions_revoked: 0 }
    ];
}

const DETAIL = {
    id: '600', name: 'Ana Torrez', email: 'ana@example.test', phone: '',
    role: 'moallem', status: 'inactive', hourly_rate: 9.5,
    face_enrolled: false, enrolled_at: null
};

const edits = [];
const statuses = [];
const deletes = [];
//: The reference-photo write, which is a different endpoint because it is a different
//: content type: ``/admin/enroll`` takes multipart, the account's own fields are JSON.
const enrolls = [];
let enrollFails = false;
let deleteStatus = 200;
let deleteReply = { status: 'success', message: 'User 600 deleted.', deleted: true };
let statusFails = false;

function responders(url, init) {
    const method = (init && init.method) || 'GET';
    // Matched before the roster fallback, and by its own path: ``/admin/enroll`` is a
    // separate endpoint from ``/admin/enrollment/invites``, not a prefix of it.
    if (url.indexOf('/admin/enroll') >= 0 && url.indexOf('/admin/enrollment') < 0) {
        enrolls.push({ body: (init && init.body) || null, headers: (init && init.headers) || {} });
        if (enrollFails) {
            return { status: 422, body: { detail: 'No face detected. Please ensure good lighting and clear view.' } };
        }
        return { status: 200, body: { status: 'success', message: 'Facial data for worker 600 successfully enrolled.' } };
    }
    if (url.indexOf('/admin/users/edit') >= 0) {
        edits.push({ body: JSON.parse(init.body), headers: init.headers });
        if (edits[edits.length - 1].body.name === 'Refuse Me') {
            return { status: 400, body: { detail: 'Name must not be empty.' } };
        }
        return { status: 200, body: { status: 'success', user_id: '600', message: 'User 600 updated.' } };
    }
    if (url.indexOf('/admin/users/status') >= 0) {
        statuses.push(JSON.parse(init.body));
        if (statusFails) return { status: 403, body: { detail: 'Standard Admins cannot edit administrator accounts.' } };
        return { status: 200, body: { status: 'success', user_id: '600', active: false, message: 'User 600 deactivated: no access, history kept.' } };
    }
    if (url.indexOf('/admin/users/delete') >= 0) {
        deletes.push(JSON.parse(init.body));
        return { status: deleteStatus, body: deleteReply };
    }
    if (url.indexOf('/admin/users/') >= 0 && method === 'GET') {
        return { status: 200, body: DETAIL };
    }
    if (url.indexOf('/admin/users') >= 0) return { status: 200, body: roster() };
    return { status: 200, body: {} };
}

function toasts(env) {
    return env.evaluate("(document.getElementById('toastRoot').__children || []).map((el) => el.textContent)");
}

function render(env) {
    return env.evaluate("document.getElementById('adminContent').innerHTML");
}

/** Every row, the way the admin sees it: the id, its actions, and the published state. */
function rowsOf(markup) {
    const rows = markup.match(/<tr data-user="[^"]*"[\s\S]*?<\/tr>/g) || [];
    return rows.map((row) => ({
        id: (/data-user="([^"]*)"/.exec(row) || [])[1],
        status: (/data-status="([^"]*)"/.exec(row) || [])[1],
        edit: row.indexOf('data-edit-user=') >= 0,
        remove: row.indexOf('data-delete-user=') >= 0,
        toggle: (/data-user-status="([^"]*)"/.exec(row) || [])[1] || null,
        self_hint: row.indexOf('data-self="true"') >= 0,
        protected_hint: row.indexOf('data-protected="true"') >= 0,
        text: row.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim()
    }));
}

function valueOf(markup, id) {
    const match = new RegExp('id="' + id + '"[^>]*value="([^"]*)"').exec(markup);
    return match ? match[1] : null;
}

async function credentialsEnv(actor) {
    edits.length = 0;
    statuses.length = 0;
    deletes.length = 0;
    deleteStatus = 200;
    statusFails = false;
    enrolls.length = 0;
    enrollFails = false;
    const env = boot();
    env.setResponder(responders);
    env.evaluate('State.saveUser(' + JSON.stringify({ ...actor, token: 'tok-' + actor.id }) + ')');
    await env.evaluate("UI.renderAdminTab('Credentials')");
    return env;
}

const results = {};

// 1. what the roster offers
{
    const env = await credentialsEnv(HEAD);
    const rows = rowsOf(render(env));
    results.roster = {
        rows,
        by_id: rows.reduce((acc, row) => { acc[row.id] = row; return acc; }, {}),
        // The inactive row says so, in the row and in the card layout phones get.
        inactive_card: env.evaluate("Device.isMobile = true; UI_MODULES.repaintCredentialsFromCache()")
            .then(() => render(env).indexOf('data-status="inactive"') >= 0)
    };
    results.roster.inactive_card = await results.roster.inactive_card;
    env.evaluate("Device.isMobile = false");
}

// 2. an administrator's row, seen by a standard admin
{
    const env = await credentialsEnv(OPS);
    results.admin_actor = rowsOf(render(env));
}

// 3. the edit form is the record, read back from the server
{
    const env = await credentialsEnv(HEAD);
    await env.evaluate("UI_MODULES.openUserEdit('600')");
    const markup = render(env);
    results.form = {
        fetched: env.requests.filter((r) => /\/admin\/users\/600$/.test(r.url) && r.method === 'GET').length,
        for_account: (/data-user-edit="([^"]*)"/.exec(markup) || [])[1],
        name: valueOf(markup, 'userEditName'),
        email: valueOf(markup, 'userEditEmail'),
        phone: valueOf(markup, 'userEditPhone'),
        rate: valueOf(markup, 'userEditRate'),
        role_shown: (/data-user-edit-role="([^"]*)"/.exec(markup) || [])[1],
        role_editable: markup.indexOf('id="userEditRole"') >= 0,
        explains_id: markup.indexOf(env.evaluate("I18n.__('credentialsEditIdNote')")) >= 0,
        closes: env.evaluate("(UI_MODULES.closeUserEdit(), document.getElementById('adminContent').innerHTML.indexOf('data-user-edit=') < 0)")
    };
}

// 4. saving sends what the form shows, and nothing it does not
{
    const env = await credentialsEnv(HEAD);
    await env.evaluate("UI_MODULES.openUserEdit('600')");
    env.evaluate("document.getElementById('userEditName').value = 'Ana Torres'");
    env.evaluate("document.getElementById('userEditEmail').value = 'ana.torres@example.test'");
    env.evaluate("document.getElementById('userEditPhone').value = '+200000000600'");
    env.evaluate("document.getElementById('userEditRate').value = '12.5'");
    await env.evaluate("UI_MODULES.saveUserEdit()");
    results.saved = {
        calls: edits.length,
        body: edits[0] ? edits[0].body : null,
        authorized: edits[0] ? edits[0].headers.Authorization : null,
        toast: toasts(env).slice(-1)[0],
        panel_closed: render(env).indexOf('data-user-edit=') < 0
    };
}

// 4b. an emptied rate is left alone, a zero clears it
{
    const env = await credentialsEnv(HEAD);
    await env.evaluate("UI_MODULES.openUserEdit('600')");
    env.evaluate("document.getElementById('userEditRate').value = ''");
    await env.evaluate("UI_MODULES.saveUserEdit()");
    const left_alone = edits[edits.length - 1].body;
    edits.length = 0;

    await env.evaluate("UI_MODULES.openUserEdit('600')");
    env.evaluate("document.getElementById('userEditRate').value = '0'");
    await env.evaluate("UI_MODULES.saveUserEdit()");
    results.rate = {
        empty_omits_the_key: !('hourly_rate' in left_alone),
        zero_is_sent: edits[edits.length - 1].body.hourly_rate === 0
    };
}

// 4c. a rate that is not a rate is refused here, before the round trip
{
    const env = await credentialsEnv(HEAD);
    await env.evaluate("UI_MODULES.openUserEdit('600')");
    env.evaluate("document.getElementById('userEditRate').value = 'lots'");
    await env.evaluate("UI_MODULES.saveUserEdit()");
    results.bad_rate = { calls: edits.length, toast: toasts(env).slice(-1)[0] };
}

// 4d. the server's refusal is the message the admin gets, and the form stays
{
    const env = await credentialsEnv(HEAD);
    await env.evaluate("UI_MODULES.openUserEdit('600')");
    env.evaluate("document.getElementById('userEditName').value = 'Refuse Me'");
    await env.evaluate("UI_MODULES.saveUserEdit()");
    results.refused_edit = {
        toast: toasts(env).slice(-1)[0],
        still_open: render(env).indexOf('data-user-edit=') >= 0
    };
}

// 5. deactivating sends the state and reports what the server did
{
    const env = await credentialsEnv(HEAD);
    await env.evaluate("UI_MODULES.setUserStatus('1', false)");
    results.deactivate = {
        calls: statuses.length,
        body: statuses[0] || null,
        toast: toasts(env).slice(-1)[0],
        // The console's own words: the panel is read in one of three languages and the
        // server answers in one.
        expected: env.evaluate("I18n.__('credentialsDeactivated')")
    };
}

// 5b. a refusal reaches the admin as the reason
{
    const env = await credentialsEnv(HEAD);
    statusFails = true;
    await env.evaluate("UI_MODULES.setUserStatus('1', false)");
    results.refused_status = { toast: toasts(env).slice(-1)[0] };
}

// 6. deleting, and the refusal that is the interesting part
{
    const env = await credentialsEnv(HEAD);
    await env.evaluate("UI_MODULES.deleteUser('600')");
    results.deleted = {
        calls: deletes.length,
        body: deletes[0] || null,
        toast: toasts(env).slice(-1)[0]
    };
}

{
    const env = await credentialsEnv(HEAD);
    deleteStatus = 409;
    deleteReply = {
        detail: "Ana Torrez has 12 attendance record(s). Deleting the account would take those "
            + "shifts out of the reports they are paid from, so it is refused. Deactivate the "
            + "account instead: it removes their access and keeps their hours."
    };
    await env.evaluate("UI_MODULES.deleteUser('600')");
    results.refused_delete = {
        calls: deletes.length,
        toast: toasts(env).slice(-1)[0],
        row_still_there: render(env).indexOf('data-user="600"') >= 0
    };
}

// 7. the reference photo: the one thing about a person that is not a field on their row
{
    const env = await credentialsEnv(HEAD);
    await env.evaluate("UI_MODULES.openUserEdit('600')");
    const markup = render(env);
    results.photo = {
        // What is on file is stated, because the two cases are different jobs: a first
        // photo is what makes the account able to clock in at all, and a replacement stops
        // the template on file working.
        face_state: (/data-edit-face="([^"]*)"/.exec(markup) || [])[1],
        // Which account the picker writes to - carried by the control, not guessed from
        // "the panel that happens to be open".
        picker_for: (/data-edit-photo="([^"]*)"/.exec(markup) || [])[1],
        accept: (/id="userEditPhoto"[^>]*accept="([^"]*)"/.exec(markup) || [])[1],
        // A ``data-`` hook, not an inline attribute: the document CSP's allowance for those
        // may only fall, and an injected ``<img onerror>`` is what it buys.
        inline_handler: /id="userEditPhoto"[^>]*\son[a-z]+\s*=/.test(markup),
        hint: markup.indexOf(env.evaluate("I18n.__('credentialsPhotoEditHint')")) >= 0
    };
}

// 7b. the same upload rule as the create form: refused here, nothing sent
{
    const env = await credentialsEnv(HEAD);
    await env.evaluate("UI_MODULES.openUserEdit('600')");
    await env.evaluate(
        "UI_MODULES.pickEditPhoto({ files: [{ name: 'cv.pdf', size: 1200, type: 'application/pdf' }] })"
    );
    results.photo_refused_here = {
        held: env.evaluate('UI_MODULES._credentialsEditPhoto'),
        error_on_screen: render(env).indexOf('data-edit-photo-error') >= 0,
        toast: toasts(env).slice(-1)[0],
        requests: enrolls.length
    };
    // And a real one is held, shown with its size, and can be taken back off.
    await env.evaluate(
        "globalThis.__photo = (function () { const b = new Blob([new Uint8Array([0xff,0xd8,0xff,0xe0])], { type: 'image/jpeg' }); b.name = 'ana.jpg'; return b; })()"
    );
    await env.evaluate("UI_MODULES.pickEditPhoto({ files: [globalThis.__photo] })");
    results.photo_chosen = {
        name: env.evaluate('UI_MODULES._credentialsEditPhoto && UI_MODULES._credentialsEditPhoto.name'),
        shown: render(env).indexOf('data-edit-photo-chosen') >= 0,
        clear_offered: render(env).indexOf('data-clear-edit-photo') >= 0
    };
    await env.evaluate("UI_MODULES.clearEditPhoto()");
    results.photo_chosen.after_clear = env.evaluate('UI_MODULES._credentialsEditPhoto');
}

// 7c. saving with a photo sends both halves, to both endpoints
{
    const env = await credentialsEnv(HEAD);
    await env.evaluate("UI_MODULES.openUserEdit('600')");
    await env.evaluate(
        "globalThis.__photo = (function () { const b = new Blob([new Uint8Array([0xff,0xd8,0xff,0xe0])], { type: 'image/jpeg' }); b.name = 'ana.jpg'; return b; })()"
    );
    await env.evaluate("UI_MODULES.pickEditPhoto({ files: [globalThis.__photo] })");
    env.evaluate("document.getElementById('userEditName').value = 'Ana Torres'");
    await env.evaluate("UI_MODULES.saveUserEdit()");
    const call = enrolls[enrolls.length - 1] || null;
    const sent = call && call.body && call.body.get ? call.body.get('photo') : null;
    results.photo_saved = {
        edits: edits.length,
        enrolls: enrolls.length,
        // The account the template was written for. On the wire, so the endpoint can check
        // it against the caller's role - unlike the self-enroll path, where the token is
        // the subject and there is deliberately nothing to point at somebody else.
        worker_id: call && call.body && call.body.get ? call.body.get('worker_id') : null,
        carries_the_file: !!(call && call.body && call.body.has && call.body.has('photo')),
        filename: sent && sent.name,
        authorized: call ? call.headers.Authorization : null,
        authorized_edit: edits[0] ? edits[0].headers.Authorization : null,
        panel_closed: render(env).indexOf('data-user-edit=') < 0,
        toasts: toasts(env),
        saved_label: env.evaluate("I18n.__('credentialsPhotoSaved')")
    };
}

// 7d. an edit with no photo chosen is the one request it always was
{
    const env = await credentialsEnv(HEAD);
    await env.evaluate("UI_MODULES.openUserEdit('600')");
    await env.evaluate("UI_MODULES.saveUserEdit()");
    results.photo_omitted = { edits: edits.length, enrolls: enrolls.length };
}

// 7e. a photo the server will not take: the fields are saved, the panel stays, the file is
// still held so the admin can retry without picking it again
{
    const env = await credentialsEnv(HEAD);
    await env.evaluate("UI_MODULES.openUserEdit('600')");
    await env.evaluate(
        "globalThis.__photo = (function () { const b = new Blob([new Uint8Array([0xff,0xd8,0xff,0xe0])], { type: 'image/jpeg' }); b.name = 'ana.jpg'; return b; })()"
    );
    await env.evaluate("UI_MODULES.pickEditPhoto({ files: [globalThis.__photo] })");
    enrollFails = true;
    await env.evaluate("UI_MODULES.saveUserEdit()");
    const markup = render(env);
    results.photo_rejected = {
        edits: edits.length,
        enrolls: enrolls.length,
        still_open: markup.indexOf('data-user-edit=') >= 0,
        file_still_held: env.evaluate('UI_MODULES._credentialsEditPhoto && UI_MODULES._credentialsEditPhoto.name'),
        reason_on_screen: markup.indexOf('No face detected') >= 0,
        toast: toasts(env).slice(-1)[0]
    };
}
"""


@pytest.fixture(scope="module")
def results() -> dict:
    return frontend_vm.run(HARNESS)


def test_the_row_offers_the_three_corrections_and_the_accounts_state(results):
    rows = results["roster"]["by_id"]
    assert results["roster"]["inactive_card"] is True, "the phone layout says the same thing"

    for user_id in ("1", "600", "1001"):
        row = rows[user_id]
        assert row["edit"] is True, f"{user_id} cannot be edited from its own row"
        assert row["remove"] is True, f"{user_id} has no Delete"
        assert row["toggle"] in ("activate", "deactivate")

    assert rows["600"]["status"] == "inactive"
    assert rows["1"]["status"] == "active"
    assert rows["600"]["toggle"] == "activate", "an inactive account offers the way back"
    assert rows["1"]["toggle"] == "deactivate"


def test_your_own_account_is_not_offered_a_way_to_switch_yourself_off(results):
    """The server refuses it; a button that always fails is a trap, not an affordance."""
    mine = results["roster"]["by_id"]["5000"]
    assert mine["self_hint"] is True
    assert mine["remove"] is False and mine["toggle"] is None
    assert mine["edit"] is True, "a name or a rate is not a lockout, so this one stays"


def test_a_standard_admin_is_not_offered_the_administrators_rows(results):
    rows = {row["id"]: row for row in results["admin_actor"]}
    for user_id in ("1001", "5000"):
        assert rows[user_id]["protected_hint"] is True
        assert rows[user_id]["edit"] is False and rows[user_id]["remove"] is False
    assert rows["1"]["edit"] is True and rows["1"]["remove"] is True, "workers are the job"
    assert rows["600"]["edit"] is True


def test_the_edit_form_is_the_account_read_back_from_the_server(results):
    form = results["form"]
    assert form["fetched"] == 1, "one read of the account being edited"
    assert form["for_account"] == "600"
    assert form["name"] == "Ana Torrez" and form["email"] == "ana@example.test"
    assert form["rate"] == "9.5", "the hourly rate is not on the roster, so it comes from here"
    assert form["role_shown"] == "moallem"
    assert form["role_editable"] is False, "a role is fixed by the id block"
    assert form["explains_id"] is True, "the form says why instead of showing a dead field"
    assert form["closes"] is True


def test_saving_sends_the_account_and_what_was_typed(results):
    saved = results["saved"]
    assert saved["calls"] == 1
    assert saved["body"] == {
        "user_id": "600",
        "name": "Ana Torres",
        "email": "ana.torres@example.test",
        "phone": "+200000000600",
        "hourly_rate": 12.5,
    }
    assert "id" not in saved["body"] and "role" not in saved["body"], (
        "the server does not accept either, and sending them would be cargo cult"
    )
    assert saved["authorized"] == "Bearer tok-5000"
    assert "updated" in saved["toast"].lower()
    assert saved["panel_closed"] is True


def test_an_empty_rate_is_left_alone_and_a_zero_clears_it(results):
    rate = results["rate"]
    assert rate["empty_omits_the_key"] is True, "an untouched field must not clear a rate"
    assert rate["zero_is_sent"] is True, "0 is how an operator removes a rate"


def test_a_rate_that_is_not_a_rate_never_leaves_the_browser(results):
    bad = results["bad_rate"]
    assert bad["calls"] == 0, "the round trip is not worth making for 'lots'"
    assert "between 0 and 1000" in bad["toast"]


def test_a_refused_edit_is_the_servers_reason_and_the_form_stays(results):
    refused = results["refused_edit"]
    assert refused["toast"] == "Name must not be empty."
    assert refused["still_open"] is True, "the admin fixes the field the message named"


def test_deactivating_sends_the_state_and_reports_the_result(results):
    deactivate = results["deactivate"]
    assert deactivate["calls"] == 1
    assert deactivate["body"] == {"user_id": "1", "active": False}
    assert deactivate["toast"] == deactivate["expected"], (
        "the confirmation is the console's own words, not the server's English"
    )
    assert "deactivated" in deactivate["toast"].lower()


def test_a_refused_status_change_is_shown_as_a_failure(results):
    assert "Standard Admins cannot" in results["refused_status"]["toast"]


def test_deleting_sends_the_account_and_reports_the_result(results):
    deleted = results["deleted"]
    assert deleted["calls"] == 1
    assert deleted["body"] == {"user_id": "600"}
    assert "deleted" in deleted["toast"].lower()


def test_the_refusal_to_delete_an_account_with_history_reaches_the_admin(results):
    """Not an error: the endpoint saying no, why, and what to do instead."""
    refused = results["refused_delete"]
    assert refused["calls"] == 1, "the call is made - the server is the authority"
    assert "attendance record(s)" in refused["toast"]
    assert "Deactivate" in refused["toast"]
    assert refused["row_still_there"] is True, "nothing was repainted as if it had worked"


# ---------------------------------------------------------------------------
# 7. The reference photo, which is the part of an account that is not a field
# ---------------------------------------------------------------------------
def test_the_edit_panel_offers_the_photo_and_says_which_state_it_is_in(results):
    """A first photo and a replacement are different jobs, so the panel says which it is.

    The picker is bound with a ``data-`` hook rather than an inline attribute - the CSP's
    allowance for those is pinned per file and may only fall - and it carries the account it
    writes to, so "the panel that is open" is never the answer to whose template this is.
    """
    photo = results["photo"]
    assert photo["face_state"] == "missing", "the seeded account has no template, and it says so"
    assert photo["picker_for"] == "600", "the control names the account it writes"
    assert photo["accept"] == "image/jpeg,image/png,image/webp", "the same types the server accepts"
    assert photo["inline_handler"] is False, "bound with addEventListener, not an inline attribute"
    assert photo["hint"] is True, "and it says what a replacement does to the photo on file"


def test_a_photo_the_policy_refuses_never_leaves_the_browser(results):
    """The create form's rule and this one are the same rule: one photo is not looser."""
    refused = results["photo_refused_here"]
    assert refused["requests"] == 0, "a 5 MB mispick is not worth a round trip"
    assert refused["held"] is None, "and nothing unusable is held"
    assert refused["error_on_screen"] is True, "beside the picker, not only in a toast"
    assert "not a photo" in refused["toast"]

    chosen = results["photo_chosen"]
    assert chosen["name"] == "ana.jpg"
    assert chosen["shown"] is True and chosen["clear_offered"] is True
    assert chosen["after_clear"] is None, "taking it back off leaves nothing behind"


def test_saving_with_a_photo_sends_the_fields_as_json_and_the_file_as_multipart(results):
    """Two requests, because the wire has two halves - and both carry the session.

    The account's own fields and its template are different content types, so they are
    different requests; the panel is one form and one tap, which is the point. The photo
    goes to the endpoint that takes a file from disk, with the *account* on the wire: that
    is how the server can refuse a standard admin writing an administrator's face, which the
    token alone could not tell it (the console's writer is always some administrator).
    """
    saved = results["photo_saved"]
    assert saved["edits"] == 1, "the fields were saved once"
    assert saved["enrolls"] == 1, "and the photo written once"
    assert saved["worker_id"] == "600"
    assert saved["carries_the_file"] is True and saved["filename"] == "ana.jpg"
    assert saved["authorized"] == "Bearer tok-5000" and saved["authorized_edit"] == "Bearer tok-5000"
    assert saved["panel_closed"] is True
    assert saved["saved_label"] in saved["toasts"], "the photo says it landed in its own words"


def test_an_edit_without_a_photo_is_still_one_request(results):
    omitted = results["photo_omitted"]
    assert omitted["edits"] == 1 and omitted["enrolls"] == 0, (
        "the usual correction must not grow a second round trip"
    )


def test_a_photo_the_server_refuses_keeps_the_panel_and_the_file(results):
    """The fields are saved by then, so the panel is not closed on a face that was not.

    Every refusal this endpoint makes is about the photo - no face in it, more than one face, a
    frame the model cannot use - and each is answered by trying another one. So the reason is
    shown beside the picker, and the file stays held: a repaint cannot put a file back into an
    ``<input type="file">``, so the object in memory is what makes a retry possible without
    walking back to the phone's gallery.
    """
    rejected = results["photo_rejected"]
    assert rejected["edits"] == 1 and rejected["enrolls"] == 1
    assert rejected["still_open"] is True
    assert rejected["file_still_held"] == "ana.jpg"
    assert rejected["reason_on_screen"] is True, "the server's own sentence, beside the picker"
    assert "No face detected" in rejected["toast"]
