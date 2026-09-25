"""Where a live counter starts counting from.

WHY THIS EXISTS
---------------
Two screens tick a live "on shift for" figure: the worker's own clock card, and the Live
Ops board an administrator watches. Both used to derive it the same way - subtract the
stored clock-in from ``Date.now()`` - and both were wrong whenever the reader's clock was
not in the server's zone.

The stored clock-in is a *zone-less* wall clock (``2026-09-25 06:00:00``, written by the
server's own ``datetime.now()``), and ``new Date`` reads such a string in the *reader's*
zone. So a phone in Kuwait reading a server in UTC starts its counter three hours into a
shift that began five minutes ago, and then counts correctly - the origin is wrong, not the
rate, which is exactly how it was reported ("it starts from 3h but it counts correctly").
It is not merely cosmetic: the same arithmetic decides the hour state on the admin board,
and the state a shift is in is what an operator acts on.

The fix is that the count travels with the shift: ``seconds_on_site``, computed by the same
``shift_hours.elapsed_seconds`` the clock-out path measures a closed shift with, so the
figure on screen and the figure that gets paid cannot disagree. These tests pin both halves:

* the three payloads that carry an open shift really carry it, measured on the server's own
  clock, and answer ``None`` rather than blowing up on an unreadable stamp;
* the two boards tick from it - including the *same* skewed stamp that used to start them
  three hours in, which is kept below as the control that fails without the count.

Node is optional; the browser half skips rather than fails.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest
from harness import ADMIN, DB_PATH, WORKER, bearer

import frontend_vm

TS = "%Y-%m-%d %H:%M:%S"
SITE = "Downtown Tower A"

pytestmark = pytest.mark.skipif(frontend_vm.NODE is None, reason="node is not installed")


# ---------------------------------------------------------------------------
# backend: an open shift carries its own count
# ---------------------------------------------------------------------------
def _plant_open_shift(worker_id: str, *, seconds_on_site: int | None = None, stamp: str | None = None) -> str:
    """One open shift, straight in the database.

    The API has no way to backdate a clock-in - correctly - so a shift that needs to have
    lasted a while is planted. ``stamp`` overrides the text entirely, for the case where the
    stored value is not a timestamp at all.
    """
    clock_in_time = stamp if stamp is not None else (
        datetime.now() - timedelta(seconds=seconds_on_site or 0)
    ).strftime(TS)
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


def test_the_worker_card_is_sent_the_count_not_only_the_stamp(client):
    """``/worker/me/stats`` carries ``seconds_on_site`` for the open shift.

    This is the number the card ticks from. Without it the panel has nothing to count from
    but a wall clock whose zone it cannot know, which is the bug this file exists for.
    """
    _plant_open_shift(WORKER, seconds_on_site=5 * 60)
    response = client.get("/api/v1/worker/me/stats", headers=bearer(WORKER))
    assert response.status_code == 200, response.text[:300]

    session = response.json()["active_session"]
    assert session is not None, "the planted shift is the worker's open shift"
    assert session["clock_in_time"] is not None, "the stamp still travels, for display"
    # Five minutes, to the second: the plant and the read are two statements against the
    # clock, so the count cannot be expected to be exact to the microsecond - only to the
    # second it is stored and compared at.
    assert session["seconds_on_site"] == pytest.approx(300, abs=3)


def test_the_admin_board_carries_the_count_for_every_row(client):
    """``/admin/active_sessions`` carries it too - the board's "on site for" column.

    One number, one meaning: the administrator's board and the worker's own card measure the
    same shift with the same basis, so somebody watching both cannot be shown two answers.
    """
    _plant_open_shift(WORKER, seconds_on_site=90 * 60)
    _plant_open_shift(ADMIN, seconds_on_site=30)
    response = client.get("/api/v1/admin/active_sessions", headers=bearer(ADMIN))
    assert response.status_code == 200, response.text[:300]

    counts = {row["worker_id"]: row["seconds_on_site"] for row in response.json()}
    assert counts[WORKER] == pytest.approx(5400, abs=3)
    assert counts[ADMIN] == pytest.approx(30, abs=3)


def test_the_per_site_live_read_carries_it_as_well(client):
    """``/admin/workers_live/{site}`` answers the same field, so a drill-down agrees."""
    _plant_open_shift(WORKER, seconds_on_site=12 * 60)
    response = client.get(f"/api/v1/admin/workers_live/{SITE}", headers=bearer(ADMIN))
    assert response.status_code == 200, response.text[:300]

    rows = [row for row in response.json() if row["worker_id"] == WORKER]
    assert len(rows) == 1, f"the planted shift is in the site's list: {response.json()}"
    assert rows[0]["seconds_on_site"] == pytest.approx(720, abs=3)


def test_an_unreadable_stamp_is_answered_with_none_rather_than_a_500(client):
    """A stored value that is not a timestamp is a null count, not a broken screen.

    The stamp is hand-editable data - it lives in ``sqlite3`` beside everything else - and a
    counter is not worth a 500 on the one endpoint the clock panel needs. ``None`` is the
    honest answer: nothing readable says how long the shift has run.
    """
    _plant_open_shift(WORKER, stamp="not a timestamp")
    response = client.get("/api/v1/worker/me/stats", headers=bearer(WORKER))
    assert response.status_code == 200, response.text[:300]
    assert response.json()["active_session"]["seconds_on_site"] is None

    board = client.get("/api/v1/admin/active_sessions", headers=bearer(ADMIN))
    assert board.status_code == 200, board.text[:300]
    theirs = [row for row in board.json() if row["worker_id"] == WORKER]
    assert theirs and theirs[0]["seconds_on_site"] is None


# ---------------------------------------------------------------------------
# frontend: both boards tick from that count
# ---------------------------------------------------------------------------
HARNESS = r"""
// --- the server's answers, faked ----------------------------------------

const NOW = Date.now();

//: How far the reader's clock is from the server's. Nothing swings on the number being
//: three hours - only that the *digits* below are wrong by a fixed offset, which is what a
//: zone difference does to a zone-less timestamp.
const SKEW_MS = 3 * 3600 * 1000;

/** The server's own stamp format, for an instant in this process's zone. */
function stampAt(ms) {
    const at = new Date(ms);
    const pad = (value) => String(value).padStart(2, '0');
    return `${at.getFullYear()}-${pad(at.getMonth() + 1)}-${pad(at.getDate())} ` +
        `${pad(at.getHours())}:${pad(at.getMinutes())}:${pad(at.getSeconds())}`;
}

//: The shipped rules, with the overtime line *below* the paid day so both states are
//: reachable - the same retune the Live Ops suite uses.
const RULES = {
    regular_hours: 8.0,
    overtime_notify_hours: 7.5,
    break_minutes: 30,
    break_after_hours: 4,
    auto_close_at_regular: 1
};

const WORKER = { id: '600', name: 'Seed Lead', role: 'moallem', token: 'tok-600' };

let activeOverride = null;

function stats(overrides) {
    return Object.assign({
        worker_id: '600',
        total_hours: 12,
        regular_hours: 8,
        pending_hours: 0,
        overtime_hours: 0,
        overtime_notify_hours: 8.1,
        break_minutes: 30,
        break_after_hours: 4,
        paid_day_hours: 8.0,
        on_site_day_hours: 8.5,
        auto_close_at_regular: 1,
        flagged_for_review: false,
        active_session: null
    }, overrides || {});
}

function responders(url) {
    if (url.indexOf('/worker/me/stats') >= 0) {
        return { status: 200, body: stats({ active_session: activeOverride }) };
    }
    if (url.indexOf('/admin/active_sessions') >= 0) return { status: 200, body: [] };
    if (url.indexOf('/developer/notifications') >= 0) return { status: 200, body: { unread: 0, notifications: [] } };
    return { status: 200, body: {} };
}

function workerEnv(active) {
    activeOverride = active || null;
    const env = boot();
    env.setResponder(responders);
    env.evaluate('State.saveUser(' + JSON.stringify(WORKER) + ')');
    return env;
}

/**
 * A shift the server counts as `truth` seconds old, described by a stamp this reader's
 * clock would place `skew` earlier than that - which is what another zone does to it.
 */
function shift(truth, skew, readAgoSeconds) {
    return {
        site_name: 'Downtown Tower A',
        late_flag: 0,
        clock_in_time: stampAt(NOW - truth * 1000 - skew),
        seconds_on_site: truth,
        read_at: readAgoSeconds ? NOW - readAgoSeconds * 1000 : NOW
    };
}

/** "0:05:00" as seconds, so an assertion is about the figure and not the digits. */
function secondsOf(text) {
    const match = /^(\d+):(\d{2}):(\d{2})$/.exec(String(text || '').trim());
    if (!match) return null;
    return Number(match[1]) * 3600 + Number(match[2]) * 60 + Number(match[3]);
}

/** The panel's own figure, painted by the render and then by the first tick. */
async function cardSeconds(active) {
    const env = workerEnv(active);
    await env.evaluate("WORKER_MODULES.renderClockPanel(document.getElementById('workerPanel'))");
    return {
        seconds: secondsOf(env.evaluate("document.getElementById('shiftElapsed').textContent")),
        markup: env.evaluate("document.getElementById('workerPanel').innerHTML")
    };
}

/** One scenario on the admin board, evaluated inside the page's own realm. */
function boardRoundTrip(session, rules, now) {
    const env = boot();
    env.setResponder(responders);
    return env.evaluate(`
        (function () {
            const facts = UI_MODULES.liveOpsFacts(${JSON.stringify(session)}, ${JSON.stringify(rules)}, ${now});
            const attrs = UI_MODULES.liveOpsFactAttrs(facts);
            const attr = (name) => attrs.split(name + '="')[1].split('"')[0];
            // Read the row back the way its one-second tick does.
            const ticked = UI_MODULES.liveOpsFacts({
                clock_in_time: attr('data-start'), origin_at: attr('data-origin'), late_flag: '0'
            }, ${JSON.stringify(rules)}, ${now});
            const round = (value) => (value === null ? null : Math.round(value));
            return {
                seconds: round(facts.seconds), state: facts.state,
                tick_seconds: round(ticked.seconds), tick_state: ticked.state,
                carries_origin: attr('data-origin') !== '',
                start_is_display_only: facts.start instanceof Date && facts.start.getTime() !== facts.origin
            };
        })()
    `);
}

const results = {};

// 1. the worker's card, five minutes into a shift whose stamp reads three hours old
{
    const card = await cardSeconds(shift(300, SKEW_MS, 0));
    results.card_with_count = {
        seconds: card.seconds,
        // The stamp itself still travels and is still printed beside the timer: this fix is
        // about the count, and hiding the recorded time would be a different change.
        shows_the_stamp: card.markup.indexOf('hand-policy-value') >= 0
            && card.markup.indexOf(stampAt(NOW - 300 * 1000 - SKEW_MS)) >= 0
    };
}

// 2. the control: the same panel read the old way, which is what the offset did
{
    const noCount = shift(300, SKEW_MS, 0);
    delete noCount.seconds_on_site;
    delete noCount.read_at;
    results.card_from_digits_alone = { seconds: (await cardSeconds(noCount)).seconds };
}

// 3. the count ages from the moment it was *read*, not from the moment it is drawn. The
//    panel stamps that instant onto the answer itself (the alert banner is awaited between
//    the read and the paint), so a minute later the shift reads five minutes plus one.
{
    const active = shift(300, SKEW_MS, 0);
    delete active.read_at;                       // a server answer does not carry one
    const env = workerEnv(active);
    await env.evaluate("WORKER_MODULES.renderClockPanel(document.getElementById('workerPanel'))");
    const stamped = activeOverride.read_at;
    results.card_read_at = {
        // The panel stamped the instant the answer landed...
        panel_stamped: typeof stamped === 'number' && stamped > 0,
        // ...and the count ages from there: the five minutes the server counted, plus the
        // minute since. Without the stamp the wait would be time the shift never gets.
        a_minute_later: env.evaluate(
            'SHIFT_CLOCK.elapsed(SHIFT_CLOCK.origin(' + JSON.stringify(activeOverride) + '), ' +
            (stamped + 60 * 1000) + ')'
        )
    };
}

// 4. an offline re-render keeps counting from the same origin
{
    const env = workerEnv(null);
    const cached = shift(300, SKEW_MS, 0);
    const origin = env.evaluate('SHIFT_CLOCK.origin(' + JSON.stringify(cached) + ')');
    results.offline_cache = {
        // Five minutes as the server counted them, half an hour later - and this is the
        // cached record the phone stored, not a live answer.
        seconds: env.evaluate('SHIFT_CLOCK.elapsed(' + origin + ', ' + (NOW + 1800 * 1000) + ')'),
        // The rate is the device's, from that one origin: ninety seconds on, it reads 390.
        ninety_seconds_on: env.evaluate(
            'SHIFT_CLOCK.elapsed(' + origin + ', ' + (NOW + 90 * 1000) + ')'
        )
    };
}

// 5. the Live Ops board: eight hours in as the server counts it, against a stamp an
//    administrator's clock reads as eleven - over the line is not the same as closing
{
    const truth = 8 * 3600 + 5 * 60;
    results.board_with_count = boardRoundTrip({
        worker_id: 'w1', name: 'Youssef Adel', site_name: 'Downtown Tower A',
        late_flag: 0,
        clock_in_time: stampAt(NOW - truth * 1000 - SKEW_MS),
        seconds_on_site: truth,
        read_at: NOW
    }, RULES, NOW);
}

// 6. the control: the same eleven-hour stamp with no count beside it
{
    results.board_from_digits_alone = boardRoundTrip({
        worker_id: 'w1', name: 'Youssef Adel', site_name: 'Downtown Tower A', late_flag: 0,
        clock_in_time: stampAt(NOW - (8 * 3600 + 5 * 60) * 1000 - SKEW_MS)
    }, RULES, NOW);
}

// 7. an unreadable stamp is still "unknown", not a crash and not a count
{
    results.board_unreadable = boardRoundTrip({ clock_in_time: 'not a date', late_flag: 0 }, RULES, NOW);
}
"""


@pytest.fixture(scope="module")
def results():
    return frontend_vm.run(HARNESS)


def test_the_card_counts_from_the_servers_figure(results):
    """Five minutes in, on a stamp that a three-hour-slipped clock reads as hours old."""
    card = results["card_with_count"]
    assert card["seconds"] is not None, "the timer rendered a figure"
    assert card["seconds"] == pytest.approx(300, abs=3), (
        f"the card showed {card['seconds']}s of a five-minute shift; the stamp it was also "
        "sent reads three hours old in this zone, which is the offset the count removes"
    )
    assert card["shows_the_stamp"] is True, "the recorded time is still shown, just not counted"


def test_the_old_arithmetic_is_the_bug_this_fixes(results):
    """The control: without the count, the same panel starts three hours in.

    Kept as a test rather than a comment. It is the behaviour that was reported ("it starts
    from 3h but it counts correctly"), and it is also the proof that the scenario above is
    the one that would fail without the fix - a stamp skewed by three hours, in a suite that
    runs in whatever zone the machine happens to have.
    """
    control = results["card_from_digits_alone"]
    assert control["seconds"] == pytest.approx(300 + 3 * 3600, abs=5), (
        "the fallback is the old arithmetic: correct only while the reader's clock shares the "
        "server's zone, which is why it is now the fallback and not the path"
    )


def test_the_count_keeps_aging_from_the_moment_it_was_read(results):
    """A card read a minute before it is drawn shows the minute too.

    The panel awaits the alert banner between the read and the paint, so the origin has to be
    the instant the figure was true rather than the instant the markup landed - otherwise the
    wait is time the shift silently never gets.
    """
    card = results["card_read_at"]
    assert card["panel_stamped"] is True, "the panel stamps the instant the answer landed"
    assert card["a_minute_later"] == pytest.approx(360, abs=1)


def test_an_offline_re_render_counts_from_the_same_origin(results):
    """The cached shift carries the count, so a phone that is offline keeps counting.

    Half an hour after the cache was written the card is thirty-five minutes in: five as the
    server counted them, plus the half hour the phone has watched since.
    """
    offline = results["offline_cache"]
    assert offline["seconds"] == pytest.approx(2100, abs=1)
    assert offline["ninety_seconds_on"] == pytest.approx(390, abs=1)


def test_the_board_decides_from_the_count_and_forwards_it_to_its_tick(results):
    """Eight hours in is "over the line"; the digits alone would say "closing"."""
    board = results["board_with_count"]
    assert board["seconds"] == pytest.approx(8 * 3600 + 5 * 60, abs=3)
    assert board["state"] == "over"
    assert board["carries_origin"] is True, "the row carries its origin for its own tick"
    # The row's tick must land on the same figure and the same verdict as the render it
    # ticked away from, or the board would change its mind a second after opening.
    assert board["tick_seconds"] == board["seconds"]
    assert board["tick_state"] == board["state"]
    assert board["start_is_display_only"] is True, (
        "the stamp stays a display value: the count comes from the origin, which is the "
        "instant the shift began rather than the digits a console in another zone misreads"
    )


def test_the_same_stamp_without_the_count_mislabels_the_shift(results):
    """The control, on the board: eleven hours read where the server says eight."""
    control = results["board_from_digits_alone"]
    assert control["seconds"] == pytest.approx(11 * 3600 + 5 * 60, abs=3)
    assert control["state"] == "closing", (
        "an operator would be shown a shift about to be closed under the worker instead of one "
        "that has merely crossed the overtime line"
    )
    assert control["carries_origin"] is True, "the digits alone are still an origin, just a wrong one"


def test_an_unreadable_stamp_is_unknown_rather_than_a_wrong_count(results):
    """No readable stamp and no count is "unknown" - the board's existing answer."""
    unreadable = results["board_unreadable"]
    assert unreadable["seconds"] is None
    assert unreadable["state"] == "unknown"
    assert unreadable["carries_origin"] is False, "nothing to forward to the tick either"
