"""The notes screens, exercised for real: the admin inbox and the worker's tab.

WHY THIS EXISTS
---------------
A note is the only feature in this app where a worker writes free text and an
administrator acts on it. The parts that can only break in the browser are exactly the
parts an API test cannot see:

1. the worker's queue is *wired* - the tab is on the bar, the list says what is waiting
   for an answer, and a reply goes to that note's endpoint rather than to a general one;
2. the admin's queue shows the person, what was said *last* and whose turn it is before
   anything is opened, and the filters narrow what is on screen without a request;
   opening a note keeps the queue beside it and leaves it exactly as it was;
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
// worker typed and the queue renders it as text.
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

// The signed-in administrator's *own* note, filed through the worker's endpoints - the same
// request a worker's handset makes, because an administrator with something missing on site
// is the same person making the same request. It is deliberately not one of ``NOTES``: the
// mailbox below is other people's requests, and this is the reader's own.
const ADMIN_OWN_NOTE = {
    id: 31, worker_id: '1000', category: 'missing_item', category_label: 'Missing item or material',
    subject: 'Rebar for tower B', body: 'The rebar for tower B never arrived.',
    status: 'open', priority: 'normal', created_at: '2026-09-15 06:30:00',
    updated_at: '2026-09-15 06:30:00', last_reply_at: null, resolved_at: null,
    resolved_by: null, closed_at: null, closed_by: null, admin_unread: 0, worker_unread: 0, open: true,
    last_message: {
        id: 41, author_id: '1000', author_role: 'admin', from_admin: false, internal: false,
        body: 'The rebar for tower B never arrived.', created_at: '2026-09-15 06:30:00'
    }
};

function responders(url, init) {
    // The administrator's own notes, answered before the mailbox patterns above: both live
    // under /worker/notes and /admin/notes, and this is the reader's column.
    if (/\/worker\/notes\/\d+$/.test(url)) return { status: 200, body: ADMIN_OWN_NOTE };
    if (url.indexOf('/worker/notes') >= 0) {
        return {
            status: 200,
            body: {
                notes: [ADMIN_OWN_NOTE], open: 1, unread: 0, max_open: 20,
                categories: ['missing_item', 'other']
            }
        };
    }
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

/**
 * Every conversation in the queue, read the way an admin sees it.
 *
 * The rows are list items now rather than table rows - the queue is a list of
 * conversations, not a record of notes - but the three things each row is asked about are
 * the three it was asked about before: which note it is, whether it is waiting for an
 * answer, and whether the worker marked it urgent. That is the point of reading the row
 * through this one function instead of pattern-matching the markup in each assertion.
 */
function rowsOf(markup) {
    const rows = markup.match(/<li class="notes-thread-item[^"]*"[\s\S]*?data-note="[^"]*"[\s\S]*?<\/li>/g) || [];
    return rows.map((row) => ({
        id: (/data-note="([^"]*)"/.exec(row) || [])[1],
        unread: row.indexOf('data-admin-unread') >= 0,
        urgent: row.indexOf('data-urgent="1"') >= 0,
        text: textOf(row)
    }));
}

/** The turn segments down the top of the queue, with the state each one is in. */
function turnsOf(markup) {
    const chips = markup.match(/<button type="button" data-turn="[^"]*"[\s\S]*?<\/button>/g) || [];
    return chips.map((chip) => ({
        turn: (/data-turn="([^"]*)"/.exec(chip) || [])[1],
        active: /data-active="true"/.test(chip),
        text: textOf(chip)
    }));
}

function toasts(env) {
    return env.evaluate("(document.getElementById('toastRoot').__children || []).map((el) => el.textContent)");
}

/**
 * The mailbox region of the Notes screen, read where it is painted.
 *
 * The tab is two regions: the signed-in administrator's own notes above, the queue below -
 * and the stub DOM does not nest one ``innerHTML`` string inside another, so reading the
 * tab's markup would hand back the two host elements and none of the rows. The queue, its
 * thread, its error line and its empty state all live in the second region, which is what
 * every assertion below is about; section 10 reads the worker's own host for the same
 * reason.
 */
function render(env) {
    return env.evaluate("document.getElementById('notesInbox').innerHTML");
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
        turns: turnsOf(markup),
        has_search_box: markup.indexOf('id="notesQuery"') >= 0,
        title_shown: textOf(markup).indexOf('Worker notes') >= 0,
        requests: env.requests.filter((r) => r.url.indexOf('/admin/notes') >= 0).length,
        // The row that is waiting carries the last thing said in words, not the worker's
        // own first message under a full timestamp.
        waiting_row: rowsOf(markup).filter((row) => row.id === '11')[0].text,
        // Two panes, and the thread pane is an invitation until something is opened.
        has_list_pane: markup.indexOf('data-notes-list') >= 0,
        has_thread_pane: markup.indexOf('data-notes-thread') >= 0,
        blank_thread: markup.indexOf(env.evaluate("I18n.__('notesThreadEmpty')")) >= 0
    };
}

// 1b. whose turn a conversation is on, and the way back out of a segment
{
    const env = inboxEnv();
    await env.evaluate("UI.renderAdminTab('Notes')");
    const before = env.requests.length;
    const ids = () => rowsOf(render(env)).map((row) => row.id);
    await env.evaluate("UI_MODULES.filterNotesByTurn('you')");
    const you = ids();
    await env.evaluate("UI_MODULES.filterNotesByTurn('them')");
    const them = ids();
    await env.evaluate("UI_MODULES.filterNotesByTurn('done')");
    const done = ids();
    // Pressing the segment that is already on is the way back out of it, which is the only
    // thing a reader can do with a filter they no longer want.
    await env.evaluate("UI_MODULES.filterNotesByTurn('done')");
    const backOut = ids();
    await env.evaluate("UI_MODULES.filterNotesByTurn('all')");
    const all = ids();
    // The status select is the narrower, literal question, and it answers the same way.
    await env.evaluate("document.getElementById('notesStatus').value = 'resolved'");
    await env.evaluate("document.getElementById('notesStatus').onchange()");
    const resolved = ids();
    results.turns = {
        you: you,
        them: them,
        done: done,
        back_out: backOut,
        all: all,
        resolved: resolved,
        requests_added: env.requests.length - before
    };
}

// 1c. the queue's controls are one delegated listener, not a handler per row
//
// The CSP's allowance for inline handlers may only fall, and a list of conversations is
// mostly rows; every control on the tab is a data hook answered in one place. What the
// stub can prove is that the hooks are the ones the markup carries and that pressing them
// reaches the right note - the open control reads its note from the row it sits in, and
// the reply button answers the thread that is on screen.
{
    const env = inboxEnv();
    await env.evaluate("UI.renderAdminTab('Notes')");
    const press = (selector, note) => env.evaluate(
        "document.getElementById('notesInbox').onclick({ target: { closest: (s) => {" +
        " if (s === " + JSON.stringify(selector) + ") return { dataset: " + JSON.stringify(note || {}) + " };" +
        (note && note.note ? " if (s === '[data-note]') return { dataset: " + JSON.stringify(note) + " };" : '') +
        " return null; } } })"
    );
    await press('[data-turn]', { turn: 'you' });
    const afterTurn = rowsOf(render(env)).map((row) => row.id);
    await press('[data-open-note]', { note: '12' });
    const opened = render(env);
    env.evaluate("document.getElementById('noteReplyBody').value = 'On its way.'");
    await press('[data-send-reply]');
    results.routing = {
        after_turn: afterTurn,
        opened_the_row_pressed: opened.indexOf('Ana Torres') >= 0,
        // The send carries the note the thread is showing, not the row that was clicked.
        sent: replies.slice(),
        back_hook: render(env).indexOf('data-notes-back') >= 0
    };
    // The queue is still narrowed by the turn segment that was pressed, which is the point of
    // reading beside the list rather than instead of it.
    results.routing.threads_before_back = rowsOf(render(env)).map((row) => row.id);
    await press('[data-notes-back]');
    results.routing.threads_after_back = rowsOf(render(env)).map((row) => row.id);
    results.routing.blank_after_back = render(env).indexOf('data-notes-thread') >= 0
        && render(env).indexOf(env.evaluate("I18n.__('notesThreadEmpty')")) >= 0;
}

// 1d. what the tab does with the thread when the reader comes back to it
//
// Leaving the tab is not a decision about the conversation, so the two ways of leaving get
// two answers: back says "done with this one" and the tab then opens on the queue, while a
// thread left open is where the reader lands. Either way the conversation is *re-read*
// rather than remembered - the queue's payload carries only each note's last message, so a
// thread held in memory would be a conversation frozen at the hour it was last read,
// sitting beside a queue read a minute ago.
{
    const env = inboxEnv();
    const reads = () => env.requests.filter((r) => /\/admin\/notes\/11$/.test(r.url)).length;
    const blank = env.evaluate("I18n.__('notesThreadEmpty')");
    await env.evaluate("UI.renderAdminTab('Notes')");
    await env.evaluate("UI_MODULES.openNote(11)");
    await env.evaluate("UI_MODULES.backToNotes()");
    const afterBack = render(env);
    const beforeFirst = reads();
    await env.evaluate("UI.renderAdminTab('Notes')");
    const reentered = render(env);
    results.reentry = {
        blank_after_back: afterBack.indexOf(blank) >= 0,
        list_after_reentry: reentered.indexOf(blank) >= 0,
        open_row_after_reentry: reentered.indexOf('is-open') >= 0,
        read_again: reads() - beforeFirst
    };
    // The other way of leaving: the thread was never put down, so it is still there - and
    // re-read, so what it shows is this minute's conversation and not last hour's.
    await env.evaluate("UI_MODULES.openNote(11)");
    const beforeSecond = reads();
    await env.evaluate("UI.renderAdminTab('Notes')");
    const reopened = render(env);
    results.reentry.reopened_thread = reopened.indexOf('data-notes-back') >= 0
        && reopened.indexOf('id="noteReplyBody"') >= 0;
    results.reentry.reopened_read_again = reads() - beforeSecond;
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
    const folded = render(env);
    // A message is identified by the two data attributes, and "which side is it on" by the
    // bubble's own variant class - the same class the stylesheet uses to put it there, so a
    // message that stopped being the admin's would fail here as well as on screen.
    const messages = (folded.match(/<div class="hand-bubble-row[^"]*" data-message="\d+" data-internal="[01]"/g) || [])
        .map((tag) => ({
            id: (/data-message="(\d+)"/.exec(tag) || [])[1],
            internal: /data-internal="1"/.test(tag),
            mine: tag.indexOf('is-mine') >= 0
        }));
    // The reset form is folded away until it is asked for: a conversation is read far more
    // often than a password is issued, and a credential form parked above the messages made
    // every thread open on one. Both states are read here, so a form that appeared unasked
    // for and a button that opened onto nothing are each their own failure.
    await env.evaluate("UI_MODULES.showNotePasswordPanel()");
    const markup = render(env);
    results.thread = {
        messages: messages,
        internal_text_visible: folded.indexOf('internal: the old import') >= 0,
        internal_tagged: folded.indexOf('data-internal="1"') >= 0,
        has_reply_box: folded.indexOf('id="noteReplyBody"') >= 0,
        has_back: folded.indexOf('data-notes-back') >= 0,
        worker_named: textOf(folded).indexOf('Bilal Khan') >= 0,
        folded_panel_hidden: folded.indexOf('data-note-password-panel') < 0,
        reset_toggle_offered: folded.indexOf('data-show-password') >= 0,
        has_password_panel: markup.indexOf('data-note-password-panel') >= 0,
        // The reset is offered as a data hook rather than an inline handler: this assertion
        // used to name the handler, which meant it could only fail in the browser after the
        // handler had already been removed from the markup.
        can_reset_offered: markup.indexOf('data-reset-password') >= 0,
        // Opening a note keeps the queue beside it, and the thread is the note.
        queue_still_there: rowsOf(markup).map((row) => row.id),
        day_marker: folded.indexOf('notes-day') >= 0
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
        // One Close puts both away - the password that was on screen and the form that
        // produced it - because that is what the button in front of the reader says.
        panel_back: closed.indexOf('data-note-password-panel') >= 0,
        toggle_back: closed.indexOf('data-show-password') >= 0
    };
}

// 7b. the password for a note can be typed, and an empty box still generates one
{
    const env = inboxEnv();
    await env.evaluate("UI.renderAdminTab('Notes')");
    await env.evaluate("UI_MODULES.openNote(11)");
    // The form has to be asked for first: it is folded behind the thread head's button.
    await env.evaluate("UI_MODULES.showNotePasswordPanel()");
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

// 14. the administrator's own notes, above the mailbox they read
//
// An administrator works a shift of their own (``handsetRoles``, ``SELF_ENROLL_ROLES``), and
// the note a worker writes to ask for something is a note they have to be able to write -
// but this tab offered only the mailbox: every note as a reviewer, and no way to open one of
// their own, which is what "open notes like a worker" means. The card above is the *worker's*
// own view, rendered here rather than copied, so the two cannot drift apart.
{
    const env = inboxEnv();
    // The reader this is for: an administrator who also works the site, not the head admin
    // the other console blocks sign in as.
    env.evaluate("State.saveUser(" + JSON.stringify({
        id: '1000', name: 'Site Admin', role: 'admin', token: 'tok-1000'
    }) + ")");
    await env.evaluate("UI.renderAdminTab('Notes')");
    const card = env.evaluate("document.getElementById('adminMyNotes').innerHTML");
    results.my_notes = {
        region_on_the_tab: env.evaluate("document.getElementById('adminContent').innerHTML")
            .indexOf('data-my-notes') >= 0,
        // Painted by the worker's own module, into the console's host: one implementation of
        // "my notes", not a second one that would answer the same question differently.
        host_is_the_one_rendered_into:
            env.evaluate("WORKER_MODULES._notesHost === document.getElementById('adminMyNotes')"),
        heading: card.indexOf(env.evaluate("I18n.__('notesMine')")) >= 0,
        has_new_note: card.indexOf('data-new-note') >= 0,
        own_note_ids: (card.match(/data-note="[0-9]+"/g) || []).map((a) => a.replace(/[^0-9]/g, '')),
        own_reads: env.requests.filter((r) => r.url.indexOf('/worker/notes') >= 0).length,
        // The mailbox below is untouched by any of this.
        mailbox_ids: rowsOf(render(env)).map((row) => row.id)
    };

    await env.evaluate("WORKER_MODULES.openNote(31)");
    results.my_notes.thread_in_the_card =
        env.evaluate("document.getElementById('adminMyNotes').innerHTML").indexOf('Rebar') >= 0;
    results.my_notes.thread_read = env.requests.filter((r) => /\/worker\/notes\/31$/.test(r.url)).length;

    // ... and filing one is the worker's composer, not a form this tab grew on its own.
    await env.evaluate("WORKER_MODULES.startNote()");
    env.evaluate("document.getElementById('noteSubject').value = 'Cement for tower B'");
    env.evaluate("document.getElementById('noteBody').value = 'It never arrived.'");
    await env.evaluate("WORKER_MODULES.submitNote(null)");
    results.my_notes.filed = env.requests
        .filter((r) => r.method === 'POST' && r.url.indexOf('/worker/notes') >= 0)
        .length;
}
"""


@pytest.fixture(scope="module")
def results() -> dict:
    return frontend_vm.run(HARNESS)


def test_the_console_has_a_notes_tab_that_opens_on_the_queue(results):
    assert ["Notes", "notes"] in results["tabs"], "the tab is on the console"
    inbox = results["inbox"]
    assert [row["id"] for row in inbox["rows"]] == ["11", "12", "13"], "one row per note"
    assert [chip["turn"] for chip in inbox["turns"]] == ["all", "you", "them", "done"], (
        "the queue is filtered by whose turn it is; the five status chips answered a "
        "question the administrator did not have"
    )
    assert inbox["turns"][0]["active"] is True, "the tab opens on everything, not on a filter"
    assert "(1)" in inbox["turns"][1]["text"], "each segment carries how many notes are in that state"
    waiting = next(row for row in inbox["rows"] if row["id"] == "11")
    assert waiting["unread"] is True, "a note nobody has opened is flagged"
    assert waiting["urgent"] is True, "so is one the worker marked urgent"
    assert "Bilal Khan" in waiting["text"] and "Password" in waiting["text"]
    assert inbox["has_search_box"] is True
    assert inbox["requests"] == 1
    # A row is a conversation: it says which side owes an answer. The table had no column
    # that could say it, because the status alone cannot - an open note the administrator
    # just answered is still open.
    assert "Waiting on you" in inbox["waiting_row"], inbox["waiting_row"]
    # And the tab is two panes: the queue, and the note that opens beside it.
    assert inbox["has_list_pane"] and inbox["has_thread_pane"], (
        "opening a note must not replace the queue - that is what lost the filter, the "
        "search and the reader's place in it"
    )
    assert inbox["blank_thread"], "the thread pane says what to do before anything is opened"


def test_the_queue_narrows_by_whose_turn_it_is_without_asking_again(results):
    turns = results["turns"]
    assert turns["you"] == ["11"], "a note the worker wrote last is waiting on the administrator"
    assert turns["them"] == ["12"], "one the administrator answered is waiting on the worker"
    assert turns["done"] == ["13"], "resolved and closed are a state, not a turn"
    assert turns["back_out"] == ["11", "12", "13"], "pressing the active segment clears it"
    assert turns["all"] == ["11", "12", "13"], "and the first segment means every turn"
    assert turns["resolved"] == ["13"], (
        "the literal status filter is still there - \"show me everything closed\" is a real "
        "errand that a turn cannot express"
    )
    assert turns["requests_added"] == 0, "all of it repaints from the rows already in hand"


def test_the_queue_is_one_delegated_listener_over_data_hooks(results):
    """Every control on the tab is a ``data-`` hook, answered in one place.

    The CSP's inline-handler allowance may only fall, and a list of conversations is mostly
    rows - seven handlers left this tab for one listener. What is worth asserting is that
    the hooks are the ones the markup actually carries and that a press reaches the right
    note: the open control reads its note from the row it sits in, and the reply button
    answers the thread on screen rather than the row that was clicked.
    """
    routing = results["routing"]
    assert routing["after_turn"] == ["11"], "the turn segment answered the press"
    assert routing["opened_the_row_pressed"], "the open control opened the note in its own row"
    assert len(routing["sent"]) == 1, "one press, one reply"
    assert routing["sent"][0]["url"] == "/admin/notes/12/replies", (
        "the reply must go to the thread on screen, not to the row that was clicked"
    )
    assert routing["sent"][0]["body"]["body"] == "On its way."
    assert routing["back_hook"], "there is a way back out of a thread"
    assert routing["threads_after_back"] == routing["threads_before_back"], (
        "back leaves the queue exactly as it was - no request, nothing lost"
    )
    assert routing["threads_after_back"] == ["11"], (
        "including the filter the reader had set: coming back out of a thread must not widen "
        "the queue they were working through"
    )
    assert routing["blank_after_back"], "back empties the thread pane rather than leaving it"


def test_a_tab_switch_lands_in_the_conversation_that_was_left_open(results):
    """What the tab does with the thread when the reader comes back to it.

    Two answers, and they differ by how the reader left. Back is how someone says they are
    done with a conversation, so a tab switch after that opens on the queue; a thread that
    was simply left open is where they land - because that is what a conversation does, and
    losing your place is the cost of the old page-swap inbox.
    """
    reentry = results["reentry"]
    assert reentry["blank_after_back"] is True, "back empties the thread pane"
    assert reentry["list_after_reentry"] is True, (
        "and the tab opened after that is the queue, not a conversation that was put down"
    )
    assert reentry["open_row_after_reentry"] is False, "with no row left marked open"
    assert reentry["read_again"] == 0, "the queue already in hand is not re-read on the way in"
    assert reentry["reopened_thread"] is True, (
        "a conversation left open is where the reader lands"
    )
    assert reentry["reopened_read_again"] >= 1, (
        "re-read rather than remembered: the queue payload carries only each note's last "
        "message, so a thread held in memory would be a conversation frozen at the hour it "
        "was last read, sitting beside a queue read a minute ago"
    )


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
    assert thread["folded_panel_hidden"] is True, (
        "the reset form does not open with the thread - the conversation does"
    )
    assert thread["reset_toggle_offered"] is True, (
        "but the way to it is in the head, where the reader already is"
    )
    assert thread["has_password_panel"] is True and thread["can_reset_offered"] is True, (
        "and asking for it produces the form, not an empty card"
    )
    assert thread["queue_still_there"] == ["11", "12", "13"], (
        "the queue stays on screen - reading a note is not a page swap"
    )
    assert thread["day_marker"], "the thread marks the day instead of leaving it to be computed"


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
    assert reset["after_dismiss"]["panel_back"] is False, (
        "closing the reveal closes the form with it - one Close, one meaning"
    )
    assert reset["after_dismiss"]["toggle_back"] is True, (
        "while the offer itself stays in the thread head"
    )


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


def test_an_administrator_can_open_notes_of_their_own(results):
    """The tab is two regions: the reader's own notes, and the mailbox they work through.

    Without the first, an administrator who works a site could only *review* notes - no way to
    open one of their own from the console they are already in - which is the gap this closes.
    The card is ``WORKER_MODULES.renderNotes`` painting into the console's own host, so there
    is one "my notes" in the codebase rather than two that disagree.
    """
    own = results["my_notes"]
    assert own["region_on_the_tab"], "the console has nowhere to put the reader's own notes"
    assert own["heading"], "the region does not say what it is"
    assert own["has_new_note"], "there is no way to open a note of your own"
    assert own["host_is_the_one_rendered_into"], (
        "the console painted the worker's view into a host the module does not re-render into, "
        "so the next reply would land somewhere else"
    )
    assert own["own_note_ids"] == ["31"], own["own_note_ids"]
    assert own["own_reads"] >= 1, "the card never asked the server for the reader's own notes"
    # Opening it, and filing a new one, both go through the worker's own endpoints - the note a
    # worker writes is the note an administrator writes.
    assert own["thread_in_the_card"], "the reader's own note does not open in place"
    assert own["thread_read"] >= 1, own["thread_read"]
    assert own["filed"] == 1, "opening a note of your own posts nowhere"

    # And the mailbox is exactly where it was: two questions on one screen, neither sacrificed.
    assert own["mailbox_ids"] == ["11", "12", "13"], own["mailbox_ids"]


def test_the_worker_has_a_notes_tab_and_a_queue_that_says_what_is_waiting(results):
    # ``alerts`` joins the bar in the order it has to be in: the system's own notices, then
    # the notes the worker writes back. The other half of this list - the inbox behind that
    # tab - is the subject of ``test_frontend_worker_alerts.py``.
    assert results["worker_tabs"] == ["clock", "history", "alerts", "notes", "profile"]
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
