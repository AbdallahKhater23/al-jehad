"""The notes screens, exercised for real: the admin inbox and the worker's tab.

WHY THIS EXISTS
---------------
A note is the only feature in this app where a worker writes free text and an
administrator acts on it. The parts that can only break in the browser are exactly the
parts an API test cannot see:

1. the worker's queue is *wired* - the tab is on the bar, the list says what is waiting
   for an answer, and a reply goes to that note's endpoint rather than to a general one;
2. the admin's inbox shows the person, the type and the state before anything is opened,
   and the filters narrow what is on screen without a request;
3. an internal message is *marked* as internal on the admin's screen, because the value
   of writing one is knowing you are writing one;
4. what a worker or a moallem typed is text, not markup: a note about a site called
   ``<b>`` must not become bold in the middle of the inbox;
5. the password reset offered on a note goes to ``/admin/users/edit_password`` - the same
   hashed path the Credentials tab uses - reveals the generated password once, and never
   sends it into the note itself.

Node is optional; without it these skip rather than fail.
"""

from __future__ import annotations

import html as html_module

import pytest

import frontend_vm

pytestmark = pytest.mark.skipif(frontend_vm.NODE is None, reason="node is not installed")

HARNESS = r"""
// --- the server's answers, faked ----------------------------------------

// Three notes, one per interesting state: waiting for an answer, being handled, and
// resolved. The subject of the second one is markup, because a note is free text a
// worker typed and the inbox renders it inside a table.
const NOTES = [
    {
        id: 11, worker_id: '601', worker_name: 'Bilal Khan', worker_role: 'worker',
        category: 'password_reset', category_label: 'Password', subject: 'Password not working',
        body: 'I cannot sign in since yesterday.', status: 'open', priority: 'high',
        created_at: '2026-09-14 07:10:00', updated_at: '2026-09-14 07:10:00',
        last_reply_at: null, resolved_at: null, resolved_by: null, closed_at: null,
        closed_by: null, admin_unread: 1, worker_unread: 0, open: true,
        last_message: {
            id: 1, author_id: '601', author_role: 'worker', from_admin: false, internal: false,
            body: 'I cannot sign in since yesterday.', created_at: '2026-09-14 07:10:00'
        }
    },
    {
        id: 12, worker_id: '600', worker_name: 'Ana Torres', worker_role: 'moallem',
        category: 'missing_item', category_label: 'Missing item or material',
        subject: '<img src=x onerror=alert(1)>Cement', body: 'The cement for tower B never arrived.',
        status: 'in_progress', priority: 'normal', created_at: '2026-09-13 06:00:00',
        updated_at: '2026-09-14 09:00:00', last_reply_at: '2026-09-14 09:00:00',
        resolved_at: null, resolved_by: null, closed_at: null, closed_by: null,
        admin_unread: 0, worker_unread: 1, open: true,
        last_message: {
            id: 4, author_id: '1000', author_role: 'admin', from_admin: true, internal: false,
            body: 'Ordered - it arrives Thursday.', created_at: '2026-09-14 09:00:00'
        }
    },
    {
        id: 13, worker_id: '1', worker_name: 'Seed Worker', worker_role: 'worker',
        category: 'shift_hours', category_label: 'Shift or hours', subject: 'Missing Wednesday',
        body: 'Wednesday is not in my timesheet.', status: 'resolved', priority: 'normal',
        created_at: '2026-09-10 06:00:00', updated_at: '2026-09-11 08:00:00',
        last_reply_at: '2026-09-11 08:00:00', resolved_at: '2026-09-11 08:00:00',
        resolved_by: '1000', closed_at: null, closed_by: null, admin_unread: 0,
        worker_unread: 1, open: false,
        last_message: {
            id: 6, author_id: '1000', author_role: 'admin', from_admin: true, internal: false,
            body: 'Corrected - check your history.', created_at: '2026-09-11 08:00:00'
        }
    }
];

// The thread of note 11: the worker's own words, an internal working note, and the
// answer. The internal one is the assertion surface for "marked, not merely stored".
const THREAD_11 = [
    { id: 1, author_id: '601', author_role: 'worker', from_admin: false, internal: false,
      body: 'I cannot sign in since yesterday.', created_at: '2026-09-14 07:10:00' },
    { id: 2, author_id: '1000', author_role: 'admin', from_admin: true, internal: true,
      body: 'internal: the old import created this account', created_at: '2026-09-14 07:40:00' },
    { id: 3, author_id: '1000', author_role: 'admin', from_admin: true, internal: false,
      body: 'Come to the office and I will set a new one.', created_at: '2026-09-14 07:41:00' }
];

let notesFail = false;
const replies = [];
const statusCalls = [];
const resets = [];

function threadOf(id) {
    const base = NOTES.filter((note) => note.id === id)[0];
    return Object.assign({}, base, {
        messages: id === 11 ? THREAD_11 : [base.last_message],
        can_reset_password: base.worker_role !== 'head_admin'
    });
}

function responders(url, init) {
    if (url.indexOf('/admin/users/edit_password') >= 0) {
        resets.push(JSON.parse(init.body));
        return { status: 200, body: { status: 'success', message: 'Password successfully updated.' } };
    }
    if (/\/admin\/notes\/\d+$/.test(url)) {
        return { status: 200, body: threadOf(parseInt(url.split('/').pop(), 10)) };
    }
    if (/\/admin\/notes\/\d+\/replies$/.test(url)) {
        replies.push({ url: url.split('/api/v1')[1], body: JSON.parse(init.body) });
        return { status: 200, body: { status: 'success' } };
    }
    if (/\/admin\/notes\/\d+\/status$/.test(url)) {
        statusCalls.push({ url: url.split('/api/v1')[1], body: JSON.parse(init.body) });
        return { status: 200, body: { status: 'success' } };
    }
    if (url.indexOf('/admin/notes') >= 0) {
        if (notesFail) return { status: 503, body: { detail: 'Database is locked.' } };
        return {
            status: 200,
            body: {
                notes: NOTES,
                counts: { open: 1, in_progress: 1, resolved: 1, closed: 0 },
                unread: 1, open: 1, categories: ['password_reset', 'missing_item', 'other']
            }
        };
    }
    return { status: 200, body: {} };
}

// The worker half answers its own two endpoints.
const workerReplies = [];
let workerList = null;

function workerResponders(url, init) {
    if (/\/worker\/notes\/\d+\/replies$/.test(url)) {
        workerReplies.push({ url: url.split('/api/v1')[1], body: JSON.parse(init.body) });
        return { status: 200, body: { status: 'success', reopened: false, note: workerNote() } };
    }
    if (/\/worker\/notes\/\d+\/close$/.test(url)) {
        workerReplies.push({ url: url.split('/api/v1')[1], body: JSON.parse(init.body || 'null') });
        return { status: 200, body: { status: 'success' } };
    }
    if (/\/worker\/notes\/\d+$/.test(url)) {
        return { status: 200, body: workerNote() };
    }
    if (url.indexOf('/worker/notes') >= 0) {
        return { status: 200, body: workerList };
    }
    return { status: 200, body: {} };
}

function workerNote() {
    return {
        id: 21, worker_id: '601', category: 'password_reset', subject: 'Password not working',
        body: 'I cannot sign in since yesterday.', status: 'open', priority: 'normal',
        created_at: '2026-09-14 07:10:00', updated_at: '2026-09-14 07:10:00',
        last_reply_at: '2026-09-14 08:00:00', resolved_at: null, resolved_by: null,
        closed_at: null, closed_by: null, admin_unread: 0, worker_unread: 1, open: true,
        last_message: {
            id: 3, from_admin: true, internal: false, author_role: 'admin',
            body: 'Come to the office.', created_at: '2026-09-14 08:00:00'
        },
        messages: [
            { id: 1, from_admin: false, internal: false, author_role: 'worker',
              body: 'I cannot sign in since yesterday.', created_at: '2026-09-14 07:10:00' },
            { id: 2, from_admin: true, internal: true, author_role: 'admin',
              body: 'internal: check the roster', created_at: '2026-09-14 07:50:00' },
            { id: 3, from_admin: true, internal: false, author_role: 'admin',
              body: 'Come to the office.', created_at: '2026-09-14 08:00:00' }
        ]
    };
}

// --- reading the rendered page -------------------------------------------

function textOf(markup) {
    return markup.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();
}

function inputValue(markup, id) {
    const match = new RegExp('id="' + id + '"[^>]*value="([^"]*)"').exec(markup);
    return match ? match[1] : null;
}

/** Every note row on the desktop layout, read the way an admin sees it. */
function rowsOf(markup) {
    const rows = markup.match(/<tr data-note="[^"]*"[\s\S]*?<\/tr>/g) || [];
    return rows.map((row) => ({
        id: (/data-note="([^"]*)"/.exec(row) || [])[1],
        unread: row.indexOf('data-admin-unread') >= 0,
        urgent: row.indexOf('data-urgent="1"') >= 0,
        text: textOf(row)
    }));
}

function chipsOf(markup) {
    const chips = markup.match(/<button type="button" data-status="[^"]*"[\s\S]*?<\/button>/g) || [];
    return chips.map((chip) => ({
        status: (/data-status="([^"]*)"/.exec(chip) || [])[1],
        active: /data-active="true"/.test(chip),
        text: textOf(chip)
    }));
}

function toasts(env) {
    return env.evaluate("(document.getElementById('toastRoot').__children || []).map((el) => el.textContent)");
}

function render(env) {
    return env.evaluate("document.getElementById('adminContent').innerHTML");
}

function inboxEnv() {
    notesFail = false;
    replies.length = 0;
    statusCalls.length = 0;
    resets.length = 0;
    const env = boot();
    env.setResponder(responders);
    env.evaluate("State.saveUser(" + JSON.stringify({
        id: '1000', name: 'Head Admin', role: 'head_admin', token: 'tok-admin'
    }) + ")");
    return env;
}

function workerEnv() {
    workerReplies.length = 0;
    workerList = {
        notes: [{
            id: 21, worker_id: '601', category: 'password_reset', subject: '<b>Password</b> not working',
            body: 'I cannot sign in since yesterday.', status: 'open', priority: 'normal',
            created_at: '2026-09-14 07:10:00', last_reply_at: '2026-09-14 08:00:00',
            resolved_at: null, closed_at: null, admin_unread: 0, worker_unread: 1, open: true,
            last_message: {
                id: 3, from_admin: true, internal: false, author_role: 'admin',
                body: 'Come to the office.', created_at: '2026-09-14 08:00:00'
            }
        }],
        open: 1, unread: 1, max_open: 20, categories: ['password_reset', 'other']
    };
    const env = boot();
    env.setResponder(workerResponders);
    env.evaluate("State.saveUser(" + JSON.stringify({
        id: '601', name: 'Bilal Khan', role: 'worker', token: 'tok-worker'
    }) + ")");
    return env;
}

const results = {};

// 1. the console has the tab, and it opens on the whole queue
{
    const env = inboxEnv();
    results.tabs = env.evaluate("ADMIN_TABS.map((tab) => [tab.id, tab.key])");
    await env.evaluate("UI.renderAdminTab('Notes')");
    const markup = render(env);
    results.inbox = {
        rows: rowsOf(markup),
        chips: chipsOf(markup),
        has_search_box: markup.indexOf('id="notesQuery"') >= 0,
        title_shown: textOf(markup).indexOf('Worker notes') >= 0,
        requests: env.requests.filter((r) => r.url.indexOf('/admin/notes') >= 0).length
    };
}

// 2. the status chips and the search narrow the list without another request
{
    const env = inboxEnv();
    await env.evaluate("UI.renderAdminTab('Notes')");
    const before = env.requests.length;
    await env.evaluate("UI_MODULES.filterNotesByStatus('resolved')");
    const resolvedOnly = rowsOf(render(env)).map((row) => row.id);
    await env.evaluate("UI_MODULES.filterNotesByStatus('')");
    const all = rowsOf(render(env)).map((row) => row.id);
    const search = async (term) => {
        env.evaluate("document.getElementById('notesQuery').value = " + JSON.stringify(term));
        await env.evaluate("UI_MODULES.applyNotesSearch()");
        return rowsOf(render(env)).map((row) => row.id);
    };
    const byName = await search('torres');
    const byBody = await search('cement');
    const byCategory = await search('password');
    const andTerms = await search('khan password');
    const none = await search('nobody-here');
    const emptyMarkup = render(env);
    await env.evaluate("UI_MODULES.clearNotesSearch()");
    results.filter = {
        resolved_only: resolvedOnly,
        all: all,
        by_name: byName,
        by_body: byBody,
        by_category: byCategory,
        and_terms: andTerms,
        none: none,
        cleared: rowsOf(render(env)).map((row) => row.id),
        requests_added: env.requests.length - before,
        no_matches_flag: emptyMarkup.indexOf('data-no-matches') >= 0
    };
}

// 3. opening a note shows the whole thread, internal working note included and marked
{
    const env = inboxEnv();
    await env.evaluate("UI.renderAdminTab('Notes')");
    await env.evaluate("UI_MODULES.openNote(11)");
    const markup = render(env);
    const messages = (markup.match(/<div class="flex (?:justify-start|justify-end)" data-message="\d+" data-internal="[01]"/g) || [])
        .map((tag) => ({
            id: (/data-message="(\d+)"/.exec(tag) || [])[1],
            internal: /data-internal="1"/.test(tag),
            mine: tag.indexOf('justify-end') >= 0
        }));
    results.thread = {
        messages: messages,
        internal_text_visible: markup.indexOf('internal: the old import') >= 0,
        internal_tagged: markup.indexOf('data-internal="1"') >= 0,
        has_reply_box: markup.indexOf('id="noteReplyBody"') >= 0,
        has_back: markup.indexOf('data-notes-back') >= 0,
        worker_named: textOf(markup).indexOf('Bilal Khan') >= 0,
        has_password_panel: markup.indexOf('data-note-password-panel') >= 0,
        can_reset_offered: markup.indexOf('resetPasswordFromNote') >= 0
    };
}

// 4. answering sends the text, the internal flag and the chosen status to that note
{
    const env = inboxEnv();
    await env.evaluate("UI.renderAdminTab('Notes')");
    await env.evaluate("UI_MODULES.openNote(11)");
    env.evaluate("document.getElementById('noteReplyBody').value = 'Signed in again - closing this.'");
    env.evaluate("document.getElementById('noteInternal').checked = true");
    env.evaluate("document.getElementById('noteReplyStatus').value = 'resolved'");
    await env.evaluate("UI_MODULES.replyToNote(11)");
    // A copy, not the live array: a later scenario empties it, and a shared reference
    // would turn this assertion into a race with the next block of the harness.
    results.reply = {
        calls: replies.slice(),
        toast: toasts(env).slice(-1)[0],
        reopened: env.requests.filter((r) => /\/admin\/notes\/11$/.test(r.url)).length
    };
}

// 5. a note answered with nothing typed must not reach the server
{
    const env = inboxEnv();
    await env.evaluate("UI.renderAdminTab('Notes')");
    await env.evaluate("UI_MODULES.openNote(11)");
    await env.evaluate("UI_MODULES.replyToNote(11)");
    results.empty_reply = {
        calls: replies.length,
        toast: toasts(env).slice(-1)[0]
    };
}

// 6. moving a note on is its own action, on its own endpoint
{
    const env = inboxEnv();
    await env.evaluate("UI.renderAdminTab('Notes')");
    await env.evaluate("UI_MODULES.openNote(12)");
    await env.evaluate("UI_MODULES.setNoteStatus(12, 'resolved')");
    results.status = { calls: statusCalls.slice() };
}

// 7. the password reset: same hashed path, revealed once, never written into the note
{
    const env = inboxEnv();
    await env.evaluate("UI.renderAdminTab('Notes')");
    await env.evaluate("UI_MODULES.openNote(11)");
    await env.evaluate("UI_MODULES.resetPasswordFromNote()");
    const markup = render(env);
    // The page publishes the password escaped into an attribute (a generated password
    // contains ``&`` often enough for it to matter); the Python side unescapes it.
    const shownInMarkup = inputValue(markup, 'noteRevealedPassword');
    const rawPassword = resets.length === 1 ? resets[0].new_password : null;
    results.reset = {
        call: resets.length === 1 ? resets[0] : null,
        resets: resets.length,
        in_markup: shownInMarkup,
        reveal_panel_for: (/data-note-password-reveal="([^"]*)"/.exec(markup) || [])[1],
        reply_prefilled: env.evaluate("(document.getElementById('noteReplyBody') || {}).value"),
        reply_calls: replies.length,
        // Nothing about a password may have gone to a notes endpoint.
        notes_calls_with_password: replies.concat(statusCalls).filter(
            (call) => rawPassword && JSON.stringify(call.body).indexOf(rawPassword) >= 0
        ).length,
        toast: toasts(env).slice(-1)[0]
    };
    await env.evaluate("UI_MODULES.copyNotePassword()");
    results.reset.copied = env.copiedUrls.slice(-1)[0];

    // Closing the panel is what makes it unreadable again - it is stored nowhere.
    await env.evaluate("UI_MODULES.dismissNotePassword()");
    const closed = render(env);
    results.reset.after_dismiss = {
        html_has_password: closed.indexOf(shownInMarkup) >= 0,
        reveal: closed.indexOf('data-note-password-reveal') >= 0,
        panel_back: closed.indexOf('data-note-password-panel') >= 0
    };
}

// 7b. the password for a note can be typed, and an empty box still generates one
{
    const env = inboxEnv();
    await env.evaluate("UI.renderAdminTab('Notes')");
    await env.evaluate("UI_MODULES.openNote(11)");
    const panel = render(env);
    const typed = 'Remember-This-One-2026';
    await env.evaluate("UI_MODULES.setNotePasswordDraft(" + JSON.stringify(typed) + ")");
    await env.evaluate("UI_MODULES.resetPasswordFromNote()");
    const markup = render(env);
    results.note_typed = {
        field_offered: panel.indexOf('noteSetPasswordManual') >= 0,
        has_input_handler: panel.indexOf('oninput="UI_MODULES.setNotePasswordDraft(this.value)"') >= 0,
        password: typed,
        sent: resets.length === 1 ? resets[0].new_password : null,
        resets: resets.length,
        shown: inputValue(markup, 'noteRevealedPassword'),
        copied: null
    };
    await env.evaluate("UI_MODULES.copyNotePassword()");
    results.note_typed.copied = env.copiedUrls.slice(-1)[0];
}

// 7c. an admin with no password in mind leaves the box alone and gets a generated one
{
    const env = inboxEnv();
    await env.evaluate("UI.renderAdminTab('Notes')");
    await env.evaluate("UI_MODULES.openNote(11)");
    await env.evaluate("UI_MODULES.resetPasswordFromNote()");
    results.note_generated = {
        resets: resets.length,
        sent: resets.length === 1 ? resets[0].new_password : null,
        draft_after: env.evaluate("UI_MODULES._notePasswordDraft")
    };
}

// 8. a note body is text: what a worker typed must not become markup in the inbox
{
    const env = inboxEnv();
    await env.evaluate("UI.renderAdminTab('Notes')");
    const markup = render(env);
    results.escape = {
        raw_tag: markup.indexOf('<img src=x') >= 0,
        escaped_tag: markup.indexOf('&lt;img src=x') >= 0,
        subject_present: markup.indexOf('Cement') >= 0
    };
}

// 9. a dead server says so, and offers the filters it still has
{
    const env = inboxEnv();
    notesFail = true;
    await env.evaluate("UI.renderAdminTab('Notes')");
    const markup = render(env);
    results.dead_server = {
        message: markup.indexOf('Database is locked.') >= 0,
        rows: rowsOf(markup).length,
        has_search_box: markup.indexOf('id="notesQuery"') >= 0,
        cached: env.evaluate("UI_MODULES._notes")
    };
}

// 10. the worker's side: the tab is on the bar, and the queue says what is waiting
{
    const env = workerEnv();
    results.worker_tabs = env.evaluate("UI.workerTabIds");
    results.worker_tab_labels = env.evaluate("UI.workerTabIds.map((id) => id)");
    env.evaluate("localStorage.setItem('layoutOverride', 'mobile')");
    await env.evaluate("UI.renderWorkerMobile()");
    await env.evaluate("UI.setWorkerTab('notes')");
    // The notes view paints into its own host element (the stub DOM does not nest an
    // innerHTML string inside another), which is also where a re-render looks for it.
    results.worker = {
        tab_bar: env.evaluate("document.getElementById('app').innerHTML").indexOf('notes') >= 0,
        list_markup: env.evaluate("document.getElementById('workerNotes').innerHTML"),
        host_is_the_one_rendered_into: env.evaluate("WORKER_MODULES._notesHost === document.getElementById('workerNotes')"),
        requests: env.requests.filter((r) => r.url.indexOf('/worker/notes') >= 0).length
    };
}

// 11. opening a note, and answering it, from the worker's phone
{
    const env = workerEnv();
    env.evaluate("localStorage.setItem('layoutOverride', 'mobile')");
    await env.evaluate("UI.renderWorkerView('notes')");
    const list = env.evaluate("document.getElementById('workerNotes').innerHTML");
    await env.evaluate("WORKER_MODULES.openNote(21)");
    const thread = env.evaluate("document.getElementById('workerNotes').innerHTML");
    env.evaluate("document.getElementById('noteReplyBody').value = 'Still locked out.'");
    await env.evaluate("WORKER_MODULES.sendNoteReply(21, null)");
    await env.evaluate("WORKER_MODULES.closeNote(21)");
    results.worker_thread = {
        list_markup: list,
        thread_markup: thread,
        calls: workerReplies.slice(),
        toast: toasts(env).slice(-1)[0]
    };
}

// 12. the worker's composer refuses to send an empty request
{
    const env = workerEnv();
    env.evaluate("localStorage.setItem('layoutOverride', 'mobile')");
    await env.evaluate("UI.renderWorkerView('notes')");
    const before = env.requests.length;
    await env.evaluate("WORKER_MODULES.startNote()");
    await env.evaluate("WORKER_MODULES.submitNote(null)");
    const afterSubject = env.requests.length;
    env.evaluate("document.getElementById('noteSubject').value = 'Missing material'");
    await env.evaluate("WORKER_MODULES.submitNote(null)");
    results.worker_guard = {
        posted_without_subject: afterSubject - before,
        posted_without_body: env.requests.length - before - (afterSubject - before),
        toast: toasts(env).slice(-1)[0],
        composer_shown: env.evaluate("document.getElementById('workerNotes').innerHTML").indexOf('data-note-composer') >= 0
    };
}

// 13. a code this build has no wording for is opened up, not printed raw
//
// The API can hold a category or status this build predates - the same way the live one
// held "answered" while the labels only named the four the console moves a note through.
// ``I18n.__`` answers with the key in that case, so the naive label put
// ``noteCat_undefined`` on the worker's card. A code is opened up into words instead, and
// a field the server did not send renders no chip at all.
{
    const env = workerEnv();
    const labels = (key) => env.evaluate(key);
    results.code_labels = {
        known_category: labels("WORKER_MODULES.noteCategoryLabel('password_reset')"),
        known_status: labels("WORKER_MODULES.noteStatusLabel('in_progress')"),
        unknown_category: labels("WORKER_MODULES.noteCategoryLabel('tool_allowance')"),
        unknown_status: labels("WORKER_MODULES.noteStatusLabel('archived')"),
        missing_category: labels("WORKER_MODULES.noteCategoryLabel(undefined)"),
        null_status: labels("WORKER_MODULES.noteStatusLabel(null)"),
        missing_chip: labels("WORKER_MODULES.noteCategoryChip(undefined)"),
        unknown_chip: labels("WORKER_MODULES.noteCategoryChip('tool_allowance')")
    };
    // Arabic: the four the console ships are translated, an unknown code cannot be.
    env.evaluate("I18n.setLang('ar')");
    results.code_labels.arabic_known = labels("WORKER_MODULES.noteStatusLabel('resolved')");
    results.code_labels.arabic_unknown = labels("WORKER_MODULES.noteCategoryLabel('tool_allowance')");
    env.evaluate("I18n.setLang('en')");
}
"""


@pytest.fixture(scope="module")
def results() -> dict:
    return frontend_vm.run(HARNESS)


def test_the_console_has_a_notes_tab_that_opens_on_the_queue(results):
    assert ["Notes", "notes"] in results["tabs"], "the tab is on the console"
    inbox = results["inbox"]
    assert [row["id"] for row in inbox["rows"]] == ["11", "12", "13"], "one row per note"
    assert [chip["status"] for chip in inbox["chips"]] == ["all", "open", "in_progress", "resolved", "closed"]
    assert inbox["chips"][0]["active"] is True, "the tab opens on everything, not on a filter"
    assert "(1)" in inbox["chips"][1]["text"], "each chip carries how many notes are in that state"
    waiting = next(row for row in inbox["rows"] if row["id"] == "11")
    assert waiting["unread"] is True, "a note nobody has opened is flagged"
    assert waiting["urgent"] is True, "so is one the worker marked urgent"
    assert "Bilal Khan" in waiting["text"] and "Password" in waiting["text"]
    assert inbox["has_search_box"] is True
    assert inbox["requests"] == 1


def test_the_filters_narrow_what_is_on_screen_without_asking_again(results):
    filtered = results["filter"]
    assert filtered["resolved_only"] == ["13"]
    assert filtered["all"] == ["11", "12", "13"]
    assert filtered["by_name"] == ["12"], "a moallem's note is found by their name"
    assert filtered["by_body"] == ["12"], "searching the text the worker wrote, not only the title"
    assert filtered["by_category"] == ["11"]
    assert filtered["and_terms"] == ["11"], "every term has to match, so two terms narrow"
    assert filtered["none"] == []
    assert filtered["no_matches_flag"] is True, "a no-match view says so instead of showing zeros"
    assert filtered["cleared"] == ["11", "12", "13"]
    assert filtered["requests_added"] == 0, "filtering repaints from the rows already in hand"


def test_a_note_opens_as_a_thread_with_the_workers_own_words(results):
    thread = results["thread"]
    assert thread["messages"] == [
        {"id": "1", "internal": False, "mine": False},
        {"id": "2", "internal": True, "mine": True},
        {"id": "3", "internal": False, "mine": True},
    ], "your messages on the right, the worker's on the left"
    assert thread["internal_text_visible"] is True, "the admin sees their own working note"
    assert thread["internal_tagged"] is True, "and can tell it is one"
    assert thread["worker_named"] is True
    assert thread["has_reply_box"] is True and thread["has_back"] is True
    assert thread["has_password_panel"] is True and thread["can_reset_offered"] is True


def test_answering_goes_to_that_note_with_the_text_the_flag_and_the_status(results):
    reply = results["reply"]
    assert len(reply["calls"]) == 1, "one press, one message"
    call = reply["calls"][0]
    assert call["url"] == "/admin/notes/11/replies"
    assert call["body"]["body"] == "Signed in again - closing this."
    assert call["body"]["internal"] is True
    assert call["body"]["status"] == "resolved"
    assert reply["reopened"] >= 1, "the thread is re-read so the sent message is on screen"


def test_an_empty_reply_never_reaches_the_server(results):
    assert results["empty_reply"]["calls"] == 0
    assert results["empty_reply"]["toast"], "the admin is told why instead of nothing happening"


def test_moving_a_note_on_is_its_own_action(results):
    calls = results["status"]["calls"]
    assert len(calls) == 1
    assert calls[0]["url"] == "/admin/notes/12/status"
    assert calls[0]["body"] == {"status": "resolved"}


def test_the_password_reset_reveals_once_and_never_lands_in_the_note(results):
    reset = results["reset"]
    password = html_module.unescape(reset["in_markup"] or "")
    assert reset["resets"] == 1, "exactly one reset, for the account on screen"
    assert reset["call"]["worker_id"] == "601"
    assert reset["call"]["new_password"] == password, "the shown password is the one that was set"
    assert len(password) == 16
    assert not set('O0Il1') & set(password), (
        "a password an admin reads out over the phone must not contain O/0 or I/l/1"
    )
    assert reset["reveal_panel_for"] == "601"
    assert reset["notes_calls_with_password"] == 0, (
        "the generated password must never be posted into the note - a note is stored in "
        "plaintext, and this database holds password hashes only"
    )
    assert reset["reply_prefilled"], "the reply is prefilled so the worker is actually told"
    assert reset["copied"] == password, "the copy button hands over the password that was set"
    assert reset["after_dismiss"]["html_has_password"] is False, "it is stored nowhere"
    assert reset["after_dismiss"]["reveal"] is False
    assert reset["after_dismiss"]["panel_back"] is True, "the reset offer is still there"


def test_a_password_for_a_note_can_be_typed_or_generated(results):
    """A support call is exactly when an admin has a password in mind - or does not."""
    typed = results["note_typed"]
    assert typed["field_offered"] is True, "the box beside the button is how one is typed"
    assert typed["has_input_handler"] is True
    assert typed["resets"] == 1, "one reset, for the note on screen"
    assert typed["sent"] == typed["password"], "the typed password is the one that is set"
    assert html_module.unescape(typed["shown"]) == typed["password"], "revealed once, as typed"
    assert typed["copied"] == typed["password"], "Copy hands over what was set"

    generated = results["note_generated"]
    assert generated["resets"] == 1
    assert len(generated["sent"] or "") == 16, "an empty box still gets a generated password"
    assert not set('O0Il1') & set(generated["sent"]), "and the same readable-aloud alphabet"
    assert generated["draft_after"] == "", "the reset forgets it, like it forgets the password"


def test_a_note_is_text_not_markup(results):
    escaped = results["escape"]
    assert escaped["raw_tag"] is False, "a subject with an <img> must not become an image"
    assert escaped["escaped_tag"] is True
    assert escaped["subject_present"] is True, "the note is still readable"


def test_a_dead_server_says_so(results):
    dead = results["dead_server"]
    assert dead["message"] is True
    assert dead["rows"] == 0
    assert dead["has_search_box"] is True, "the filter stays usable instead of the tab going blank"
    assert dead["cached"] is None, "nothing may be reused from an empty response"


def test_the_worker_has_a_notes_tab_and_a_queue_that_says_what_is_waiting(results):
    assert results["worker_tabs"] == ["clock", "history", "notes", "profile"]
    worker = results["worker"]
    assert worker["tab_bar"] is True, "the tab is on the bar the worker actually taps"
    assert worker["requests"] >= 1, "the tab reads the worker's own notes"
    assert worker["list_markup"], "the tab renders rather than failing silently"


def test_the_worker_opens_answers_and_closes_their_own_note(results):
    thread = results["worker_thread"]
    assert "Bilal Khan" not in thread["list_markup"], "a worker's own note needs no author label"
    assert "Password" in thread["list_markup"], "the category is labelled, not left as a code"
    assert "internal: check the roster" not in thread["thread_markup"], (
        "an internal note must not be rendered on the worker's phone"
    )
    assert "Come to the office." in thread["thread_markup"], "the answer is there"
    urls = [call["url"] for call in thread["calls"]]
    assert "/worker/notes/21/replies" in urls
    assert "/worker/notes/21/close" in urls
    reply = next(call for call in thread["calls"] if call["url"].endswith("/replies"))
    assert reply["body"] == {"body": "Still locked out."}


def test_a_code_with_no_wording_in_this_build_is_opened_up_not_printed_raw(results):
    labels = results["code_labels"]
    assert labels["known_category"] == "Password", "a code the build knows keeps its wording"
    assert labels["known_status"] == "Being handled"
    assert labels["unknown_category"] == "Tool allowance", (
        "a category the API can hold but this build predates reads as words, not as noteCat_..."
    )
    assert labels["unknown_status"] == "Archived"
    assert labels["missing_category"] == "", "no category is not a category called undefined"
    assert labels["null_status"] == ""
    assert labels["missing_chip"] == "", "no category means no empty badge"
    assert "tool_allowance" not in labels["unknown_chip"].replace("Tool allowance", "")
    assert labels["arabic_known"] == "تم حلها", "the shipped statuses are still translated"
    assert labels["arabic_unknown"] == "Tool allowance", (
        "an unknown code has no Arabic, so it shows the words rather than a key"
    )


def test_the_workers_composer_refuses_an_empty_request(results):
    guard = results["worker_guard"]
    assert guard["posted_without_subject"] == 0
    assert guard["posted_without_body"] == 0, "an empty note is not a request"
    assert guard["toast"], "and the worker is told which part is missing"
    assert guard["composer_shown"] is True, "the form stays open so nothing typed is lost"
