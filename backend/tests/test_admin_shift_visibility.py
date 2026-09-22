"""An administrator who works a shift is a worker with a different role.

A normal administrator can step onto the clock and punch like anybody else, and their
hours are then approved by the same gate as a worker's. Four surfaces have to say so,
for the same reason each of them exists:

1. **Live Ops**, because that board is where somebody decides what to do about who is on
   site. A row that showed the wire code, and a phone card that showed nothing at all,
   left the reader to recognise a name before they could tell that the person on site is
   an administrator;
2. **the Shifts tab**, because "whose rows are these" is a question about a column - so
   the role is a column, its cell is written in the reader's words, and the search box
   answers to it in both of the forms a reader has: the word on screen, and the code
   that arrives on the wire;
3. **the attendance report**, because that report is what attendance gets reconciled
   from, and a role that exists only in the console leaves the file's readers
   reconciling a month by hand;
4. **the review queues**, which is the one that matters most: an administrator's own long
   shift waits for a decision like anybody else's, its hours stay out of the approved
   figure while it waits, and the queue says whose shift it is.

Node is optional; the browser half skips rather than fails.
"""

from __future__ import annotations

import csv
import io
import re
import sqlite3
from datetime import datetime, timedelta

import pytest
import harness
from harness import ADMIN, DB_PATH, MOALLEM, WORKER, bearer, clock_in, db_scalar

import frontend_vm

TS = "%Y-%m-%d %H:%M:%S"
SITE = "Downtown Tower A"


# ---------------------------------------------------------------------------
# backend helpers
# ---------------------------------------------------------------------------
def _seconds(hours: float) -> int:
    return int(round(hours * 3600))


def _plant_open_shift(worker_id: str, seconds_on_site: int) -> str:
    """Open a shift that started ``seconds_on_site`` seconds ago, straight in the database.

    The API has no way to backdate a clock-in - correctly - so a test that needs a shift
    already past the paid day plants one. Closed explicitly: a connection left open can
    hold a lock the per-test database reset trips over.
    """
    clock_in_time = (datetime.now() - timedelta(seconds=seconds_on_site)).strftime(TS)
    conn = sqlite3.connect(str(DB_PATH), timeout=30.0)
    try:
        conn.execute("DELETE FROM active_sessions WHERE worker_id = ?", (worker_id,))
        conn.execute(
            "INSERT INTO active_sessions (worker_id, site_name, clock_in_time) VALUES (?,?,?)",
            (worker_id, SITE, clock_in_time),
        )
        conn.commit()
    finally:
        conn.close()
    return clock_in_time


def _worked_a_shift(client, user_id: str, *, hours_on_site: float | None = None) -> int:
    """One complete shift for ``user_id``, and the clock-out row's id.

    Priced from a planted clock-in when the test needs the shift to have lasted a while,
    otherwise a real punch pair - which is also what writes the Clock In that the
    attendance report counts a day from. ``confirmed`` answers the early-clock-out
    question: the gate is not what this suite is about.

    Nobody already on shift is punched: the seeded worker is (a second clock-in is
    refused), which is why the tests below use an account the fixture leaves free.
    """
    if hours_on_site is not None:
        _plant_open_shift(user_id, _seconds(hours_on_site))
    else:
        opened = clock_in(client, user_id, headers=bearer(user_id))
        assert opened.status_code == 200, opened.text[:300]
    closed = clock_in(client, user_id, action="Clock Out", headers=bearer(user_id), confirmed=True)
    assert closed.status_code == 200, closed.text[:300]
    return int(db_scalar("SELECT MAX(id) FROM attendance_logs WHERE worker_id = ?", (user_id,)))


def _shifts_report(client) -> dict:
    response = client.get("/api/v1/admin/reports/shifts", headers=bearer(ADMIN))
    assert response.status_code == 200, response.text[:300]
    return response.json()


# ---------------------------------------------------------------------------
# 1. Live Ops
# ---------------------------------------------------------------------------
def test_live_ops_carries_the_role_of_the_administrator_on_site(client):
    """The board's own payload: without the role there is nothing for a card to label."""
    response = clock_in(client, ADMIN, headers=bearer(ADMIN), coordinates=harness.INSIDE_DOWNTOWN)
    assert response.status_code == 200, response.text[:300]

    sessions = client.get("/api/v1/admin/active_sessions", headers=bearer(ADMIN)).json()
    theirs = [session for session in sessions if session["worker_id"] == ADMIN]
    assert len(theirs) == 1, f"the administrator's own shift is on the board: {sessions}"
    assert theirs[0]["role"] == "admin"
    assert theirs[0]["name"] == "Seed Admin"
    assert theirs[0]["site_name"] == SITE


# ---------------------------------------------------------------------------
# 2. the timesheet behind the Shifts tab
# ---------------------------------------------------------------------------
def test_the_timesheet_attributes_every_shift_to_a_role(client):
    _worked_a_shift(client, MOALLEM)
    _worked_a_shift(client, ADMIN)

    rows = _shifts_report(client)["rows"]
    roles = {row["worker_id"]: row["role"] for row in rows}
    assert roles[ADMIN] == "admin"
    assert roles[MOALLEM] == "moallem"
    # And no row is left roleless: a row the console cannot label is a row somebody has
    # to identify by name before they can read it.
    assert all(row["role"] for row in rows), rows


# ---------------------------------------------------------------------------
# 3. the attendance report
# ---------------------------------------------------------------------------
def test_the_attendance_report_names_the_role_beside_the_name(client):
    # Only the administrator punches here. The seeded worker already has a clock-in on
    # file, and which *day* somebody is present is what these figures count - so the two
    # rows below are two people who worked the same day, one of them the administrator.
    _worked_a_shift(client, ADMIN)

    body = client.get("/api/v1/admin/reports/attendance", headers=bearer(ADMIN)).json()
    rows = {row["worker_id"]: row for row in body["rows"]}
    assert ADMIN in rows and WORKER in rows, body["rows"]
    assert rows[ADMIN]["role"] == "admin"
    assert rows[WORKER]["role"] == "worker", "a worker's row says worker, not nothing"

    # Reviewed like anybody else: the same day on site is the same day counted, and the
    # same arithmetic turns it into the same rate. There is no branch for a role anywhere
    # in the query, and this is the assertion that would fail if somebody added one.
    theirs, ours = rows[ADMIN], rows[WORKER]
    assert theirs["days_present"] == ours["days_present"] == 1
    assert theirs["expected_days"] == ours["expected_days"]
    assert theirs["attendance_rate"] == ours["attendance_rate"]
    assert theirs["worker_name"] == "Seed Admin"


def test_the_attendance_export_carries_the_role_column(client):
    """The file is what attendance is reconciled from, so the role travels with it."""
    _worked_a_shift(client, ADMIN)

    response = client.get("/api/v1/admin/reports/export?kind=attendance", headers=bearer(ADMIN))
    assert response.status_code == 200, response.text[:300]
    assert response.text.splitlines()[0].split(",")[:3] == ["worker_id", "worker_name", "role"]

    rows = list(csv.DictReader(io.StringIO(response.text)))
    theirs = [row for row in rows if row["worker_id"] == ADMIN]
    assert theirs, rows
    assert theirs[0]["role"] == "admin"
    assert theirs[0]["worker_name"] == "Seed Admin"
    # Every line says whose it is, not just the administrator's: a column that is blank
    # for the workers would be a column somebody has to fill in by hand.
    assert all(row["role"] for row in rows), rows


# ---------------------------------------------------------------------------
# 4. the review queue
# ---------------------------------------------------------------------------
def test_an_administrators_long_shift_waits_for_a_decision_like_anybody_elses(client):
    """Past the paid day, an administrator's own hours need approval - and wait for it."""
    log_id = _worked_a_shift(client, ADMIN, hours_on_site=9.5)
    status = db_scalar("SELECT status_code FROM attendance_logs WHERE id = ?", (log_id,))
    assert status == "pending_overtime", f"9.5h on site is past the paid day (status: {status})"

    row = [item for item in _shifts_report(client)["rows"] if item["log_id"] == log_id]
    assert row, "their shift is in the timesheet, like anybody else's"
    assert row[0]["role"] == "admin"
    assert row[0]["awaiting_approval"] is True, "and its hours are not credited until decided"

    # Both queues an operator works through: the console's Approvals tab reads the first,
    # the second is the same list for anything reading reports directly. Both have to say
    # whose shift is waiting, or the reviewer approves an administrator without noticing.
    reviews = client.get("/api/v1/admin/pending_reviews", headers=bearer(ADMIN)).json()
    waiting = [item for item in reviews if item["id"] == log_id]
    assert waiting, f"the shift is in the queue: {reviews}"
    assert waiting[0]["role"] == "admin"
    assert waiting[0]["name"] == "Seed Admin"

    pending = client.get("/api/v1/admin/reports/pending", headers=bearer(ADMIN)).json()
    reported = [item for item in pending["items"] if item["id"] == log_id]
    assert reported, pending["items"]
    assert reported[0]["role"] == "admin"


# ---------------------------------------------------------------------------
# the console, in a browser
# ---------------------------------------------------------------------------
#: The browser half skips without Node; the backend half above never does.
VM = pytest.mark.skipif(frontend_vm.NODE is None, reason="node is not installed")

HARNESS = r"""
// --- the server's answers, faked ------------------------------------------

const NOW = Date.now();

function ago(hours) {
    const at = new Date(NOW - hours * 3600 * 1000);
    const pad = (value) => String(value).padStart(2, '0');
    return `${at.getFullYear()}-${pad(at.getMonth() + 1)}-${pad(at.getDate())} ` +
        `${pad(at.getHours())}:${pad(at.getMinutes())}:${pad(at.getSeconds())}`;
}

const RULES = {
    regular_hours: 8.0, overtime_notify_hours: 8.1, break_minutes: 30.0,
    break_after_hours: 4.0, auto_close_at_regular: 1
};

// Two people on site, one of them the administrator who runs the site and works it: the
// role is the only difference between the two rows.
function sessions() {
    return [
        { worker_id: '1000', name: 'Seed Admin', site_name: 'Downtown Tower A',
          clock_in_time: ago(3), role: 'admin', late_flag: 0 },
        { worker_id: '1', name: 'Seed Worker', site_name: 'Downtown Tower A',
          clock_in_time: ago(2), role: 'worker', late_flag: 0 }
    ];
}

function shift(overrides) {
    return Object.assign({
        log_id: 900, date: '2026-09-15', timestamp: '2026-09-15 16:02:11',
        worker_id: '1', worker_name: 'Seed Worker', role: 'worker',
        site_name: 'Downtown Tower A', hours: 8, recorded_hours: 8.5, approved_hours: null,
        break_hours: 0.5, status_code: 'approved', status: 'Approved by Admin',
        awaiting_approval: false, open_notes: 0,
        arrival_time: '2026-09-15 06:20:00', arrival_verdict: 'on_time', arrival_minutes: 0
    }, overrides || {});
}

const ROWS = [
    shift({ worker_id: '1000', worker_name: 'Seed Admin', role: 'admin', log_id: 901, hours: 9 }),
    shift({ worker_id: '1', worker_name: 'Seed Worker', role: 'worker', log_id: 902, hours: 8 })
];

function totalsOf(rows) {
    const sum = (key) => rows.reduce((total, row) => total + (Number(row[key]) || 0), 0);
    return {
        shifts: rows.length,
        workers: new Set(rows.map((row) => row.worker_id)).size,
        hours: sum('hours'),
        approved_hours: rows.reduce(
            (total, row) => total + (row.awaiting_approval ? 0 : Number(row.hours) || 0), 0
        ),
        awaiting_approval_hours: 0,
        awaiting_approval: rows.filter((row) => row.awaiting_approval).length,
        break_hours: sum('break_hours'),
        workers_with_open_notes: 0,
        late_arrivals: 0
    };
}

function shiftsReport() {
    return {
        period: { start: '2026-09-01', end: '2026-09-20' },
        filters: { site: null, worker_id: null },
        fields: [
            'log_id', 'date', 'timestamp', 'worker_id', 'worker_name', 'role', 'site_name',
            'arrival_time', 'arrival_verdict', 'arrival_minutes',
            'hours', 'recorded_hours', 'approved_hours', 'break_hours', 'status_code', 'status',
            'awaiting_approval', 'open_notes'
        ],
        note: 'A timesheet: one row per shift.',
        rows: ROWS,
        totals: totalsOf(ROWS)
    };
}

function responders(url) {
    if (url.indexOf('/admin/active_sessions') >= 0) return { status: 200, body: sessions() };
    if (url.indexOf('/admin/shift_rules') >= 0) return { status: 200, body: RULES };
    if (url.indexOf('/admin/reports/shifts') >= 0) return { status: 200, body: shiftsReport() };
    return { status: 200, body: {} };
}

// An administrator at the console: the reader is one of the two people on the board.
const ADMIN = { id: '1000', name: 'Seed Admin', role: 'admin', token: 'tok-admin' };

function adminEnv() {
    const env = boot();
    env.setResponder(responders);
    env.evaluate('State.saveUser(' + JSON.stringify(ADMIN) + ')');
    return env;
}

function rendered(env) {
    return env.evaluate("document.getElementById('adminContent').innerHTML");
}

/** The row or card for one session, as markup. */
function sessionMarkup(markup, id) {
    const match = new RegExp('<(?:tr|article)[^>]*data-session="' + id + '"[\\s\\S]*?</(?:tr|article)>').exec(markup);
    return match ? match[0] : '';
}

/** The small line under the name: the role, then what else that layout has room for. */
function subOf(markup, id) {
    const match = /<span class="ops-sub">([\s\S]*?)<\/span>/.exec(sessionMarkup(markup, id));
    return match ? match[1].replace(/<[^>]*>/g, '').trim() : null;
}

function boardRowIds(env) {
    return env.evaluate("UI_MODULES.liveOpsRows(UI_MODULES._liveOps).map((row) => row.session.worker_id)");
}

function shiftsRow(markup, workerId) {
    const match = new RegExp('<tr[^>]*data-worker="' + workerId + '"[\\s\\S]*?</tr>').exec(markup);
    return match ? match[0] : '';
}

function cellsOf(row) {
    return (row.match(/<td>[\s\S]*?<\/td>/g) || []).map((cell) => cell.replace(/<[^>]*>/g, '').trim());
}

async function searchShifts(env, query) {
    await env.evaluate(`(async () => {
        document.getElementById('shiftsQuery').value = ${JSON.stringify(query)};
        await UI_MODULES.applyShiftsSearch();
    })()`);
    return rendered(env);
}

const env = adminEnv();
const results = {};

// 1. the board, as the operator first sees it
{
    await env.evaluate("UI.renderAdminTab('Live Ops')");
    const markup = rendered(env);
    results.board = {
        sub_admin: subOf(markup, '1000'),
        sub_worker: subOf(markup, '1'),
        has_table: markup.indexOf('data-live-ops-table') >= 0,
        filtered: boardRowIds(env)
    };
}

// 2. search: by the word the board shows, by the code that arrives, and in Arabic
{
    const byLabel = env.evaluate(`(function () {
        UI_MODULES.setLiveOpsQuery('administrator');
        return UI_MODULES.liveOpsRows(UI_MODULES._liveOps).map((row) => row.session.worker_id);
    })()`);
    const byCode = env.evaluate(`(function () {
        UI_MODULES.setLiveOpsQuery('admin');
        return UI_MODULES.liveOpsRows(UI_MODULES._liveOps).map((row) => row.session.worker_id);
    })()`);
    const byRoleWord = env.evaluate(`(function () {
        UI_MODULES.setLiveOpsQuery('worker');
        return UI_MODULES.liveOpsRows(UI_MODULES._liveOps).map((row) => row.session.worker_id);
    })()`);
    // The console read in Arabic: the code never matches the row, the translated word has to.
    const byArabic = env.evaluate(`(function () {
        I18n.lang = 'ar';
        UI_MODULES.setLiveOpsQuery(I18n.__('roleAdmin'));
        const rows = UI_MODULES.liveOpsRows(UI_MODULES._liveOps).map((row) => row.session.worker_id);
        I18n.lang = 'en';
        UI_MODULES.setLiveOpsQuery('');
        return rows;
    })()`);
    results.board_search = {
        by_label: byLabel, by_code: byCode, by_role_word: byRoleWord, by_arabic: byArabic,
        arabic_word: 'مدير'
    };
}

// 3. the same board on a phone: cards, and the role on the card
{
    const mobile = adminEnv();
    mobile.evaluate("localStorage.setItem('layoutOverride', 'mobile')");
    await mobile.evaluate("UI.renderAdminTab('Live Ops')");
    const markup = rendered(mobile);
    results.mobile = {
        sub_admin: subOf(markup, '1000'),
        sub_worker: subOf(markup, '1'),
        has_cards: markup.indexOf('ops-cards') >= 0,
        has_table: markup.indexOf('data-live-ops-table') >= 0
    };
}

// 4. the Shifts tab: the role is a column, and it filters
{
    const shifts = adminEnv();
    await shifts.evaluate("UI.renderAdminTab('Shifts')");
    const markup = rendered(shifts);
    const byLabel = await searchShifts(shifts, 'administrator');
    const byCode = await searchShifts(shifts, 'admin');
    const byWorker = await searchShifts(shifts, 'worker');
    results.shifts = {
        headers: (markup.match(/<th>[\s\S]*?<\/th>/g) || []).map((cell) => cell.replace(/<[^>]*>/g, '').trim()),
        admin_cells: cellsOf(shiftsRow(markup, '1000')),
        worker_cells: cellsOf(shiftsRow(markup, '1')),
        label_rows: (byLabel.match(/data-worker="([^"]*)"/g) || []).map((a) => /"([^"]*)"/.exec(a)[1]),
        code_rows: (byCode.match(/data-worker="([^"]*)"/g) || []).map((a) => /"([^"]*)"/.exec(a)[1]),
        label_row_cells: cellsOf(shiftsRow(byLabel, '1000')),
        worker_rows: (byWorker.match(/data-worker="([^"]*)"/g) || []).map((a) => /"([^"]*)"/.exec(a)[1]),
        columns: shifts.evaluate("UI_MODULES.shiftsColumns().join(',')")
    };
}
"""


@pytest.fixture(scope="module")
def board() -> dict:
    return frontend_vm.run(HARNESS)


@VM
def test_the_board_names_the_administrator_in_the_readers_words(board):
    """Not the wire code, and not nothing: the role, spelled out, on both layouts."""
    assert board["board"]["sub_admin"] == "Administrator · 1000", board["board"]
    assert board["board"]["sub_worker"] == "Worker · 1", board["board"]
    assert board["board"]["has_table"] is True

    mobile = board["mobile"]
    assert mobile["has_cards"] is True and mobile["has_table"] is False, "a phone gets cards"
    # The card has one line for all of it - the role, the site, the clock-in - and the
    # clock-in is the only part this suite cannot write down in advance.
    clock = re.compile(r"^\d{2}:\d{2}$")
    for sub, label in ((mobile["sub_admin"], "Administrator"), (mobile["sub_worker"], "Worker")):
        parts = sub.split(" · ")
        assert parts[0] == label and parts[1] == "Downtown Tower A", sub
        assert clock.match(parts[2]), sub


@VM
def test_the_board_finds_the_administrator_by_the_word_on_it(board):
    """A filter that only knows the wire code cannot be used by the person reading Arabic."""
    search = board["board_search"]
    assert search["by_label"] == ["1000"], search
    assert search["by_code"] == ["1000"], "the code is what the API uses, so it searches too"
    assert search["by_role_word"] == ["1"], "and 'worker' is not everybody"
    assert search["by_arabic"] == ["1000"], (
        f"the Arabic word for the role ({search['arabic_word']}) has to find the row"
    )


@VM
def test_the_shifts_tab_paints_the_role_and_filters_by_it(board):
    shifts = board["shifts"]
    assert "Role" in shifts["headers"], shifts["headers"]
    # Column order: date, employee, role, id, site, arrival, hours, awaiting, notes.
    assert shifts["admin_cells"][2] == "Administrator", shifts["admin_cells"]
    assert shifts["worker_cells"][2] == "Worker", shifts["worker_cells"]
    assert shifts["admin_cells"][0] == "2026-09-15", "the rest of the row is where it was"

    # And it is a filter, in both forms: the word on screen and the code on the wire. The
    # code form also finds the worker's row, and that is the search working as designed -
    # every term matches anywhere in the row, and an approved shift's status reads
    # "Approved by Admin". It is written down here so the day somebody narrows the
    # haystack, the change is a visible one rather than a surprise.
    assert shifts["label_rows"] == ["1000"], shifts
    assert shifts["code_rows"] == ["1000", "1"], shifts
    assert shifts["label_row_cells"][2] == "Administrator"
    assert shifts["worker_rows"] == ["1"], "the worker query narrows to the worker"
    assert "role" in shifts["columns"].split(",")
