"""The Live Ops board's stream: the same figures, pushed, and only when they move.

WHY THIS SUITE EXISTS
---------------------
The board used to be asked: ``GET /admin/live_ops/count`` every 45 seconds, from every console
somebody had left open on a counter, to be told on a quiet afternoon that nothing had happened.
``GET /admin/live_ops/stream`` replaces the asking with a connection that stays open and says
nothing until the board changes, and the two claims worth a test are the two the console cannot
check for itself:

* **it is the same read.** The figures pushed down the wire are the payload the counted read
  answers with, so the stream cannot become a second answer to one question - and the console's
  two paths (the stream it opens, the poll it falls back to) cannot disagree about what "12 on
  site" means;
* **an unchanged board sends nothing.** That is the whole feature, and it is why the fingerprint
  ignores names and the clock stamp: a rename, or a new ``as_of`` on the same board, is not the
  board moving, and a repaint nobody asked for is what steals focus from the search box somebody
  is typing in.

The rest is what those two rest on: a write tells the open boards to look again, a *run* of
writes is one wake-up rather than a queue of them, and the connection ends by itself so a
session authorised fifteen minutes ago is authorised again rather than trusted for ever.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta
from typing import AsyncIterator

import pytest

import live_ops
from harness import ADMIN, DB_PATH, MOALLEM, WORKER, bearer

TS = "%Y-%m-%d %H:%M:%S"
SITE = "Downtown Tower A"
OTHER_SITE = "Harbour Depot"
STREAM = "/api/v1/admin/live_ops/stream"
COUNT = "/api/v1/admin/live_ops/count"


# ---------------------------------------------------------------------------
# a board to watch
# ---------------------------------------------------------------------------
def _plant(worker_id: str, *, hours_ago: float = 1.0, site_name: str = SITE,
           late_flag: str | None = None) -> None:
    """Open a shift straight in the database - the API has no way to backdate a clock-in."""
    stamp = (datetime.now() - timedelta(seconds=int(round(hours_ago * 3600)))).strftime(TS)
    conn = sqlite3.connect(str(DB_PATH), timeout=30.0)
    try:
        conn.execute("DELETE FROM active_sessions WHERE worker_id = ?", (worker_id,))
        conn.execute(
            "INSERT INTO active_sessions (worker_id, site_name, clock_in_time, late_flag) "
            "VALUES (?,?,?,?)",
            (worker_id, site_name, stamp, late_flag),
        )
        conn.commit()
    finally:
        conn.close()


def _clear_board() -> None:
    conn = sqlite3.connect(str(DB_PATH), timeout=30.0)
    try:
        conn.execute("DELETE FROM active_sessions")
        conn.commit()
    finally:
        conn.close()


def _count(client) -> dict:
    response = client.get(COUNT, headers=bearer(ADMIN))
    assert response.status_code == 200, response.text[:300]
    return response.json()


# ---------------------------------------------------------------------------
# reading the wire
# ---------------------------------------------------------------------------
def _sse_frames(body: str) -> list[list[str]]:
    """A response body as frames - a block of lines ending in a blank one.

    The assertions below describe what a client reads off the wire rather than what the handler
    returns, so a change to the writer that broke the format would fail here instead of passing
    on a dict comparison.
    """
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in body.split("\n"):
        if line.strip("\r"):
            current.append(line.rstrip("\r"))
            continue
        if current:
            blocks.append(current)
        current = []
    if current:
        blocks.append(current)
    return blocks


def _data(block: list[str]) -> dict:
    """The ``data:`` line of one frame, as the payload it is."""
    payload = next(line for line in block if line.startswith("data:"))
    return json.loads(payload[len("data:"):].strip())


class _Request:
    """The part of a Starlette ``Request`` the generator asks anything of.

    ``leave_after`` is how a client going away is driven: the loop asks once per turn round,
    and a test that wants the connection to end on its own says so with a number. ``None`` is a
    reader that stays, which is the case every other test here is about.
    """

    def __init__(self, *, leave_after: int | None = None) -> None:
        self.checks = 0
        self.leave_after = leave_after

    async def is_disconnected(self) -> bool:
        self.checks += 1
        return self.leave_after is not None and self.checks > self.leave_after


async def _drain(generator: AsyncIterator[str], *, seconds: float) -> list[str]:
    """Every frame ``generator`` sends within ``seconds``, then let it go.

    The deadline is the test's, not the stream's: a connection with nothing to say is *silent*,
    which is indistinguishable from a wedged one unless the reader gives up on its own terms.
    """
    frames: list[str] = []

    async def pump() -> None:
        async for frame in generator:
            frames.append(frame)

    try:
        await asyncio.wait_for(pump(), timeout=seconds)
    except asyncio.TimeoutError:
        pass
    return frames


# ---------------------------------------------------------------------------
# the wire, and the read behind it
# ---------------------------------------------------------------------------
def test_the_route_answers_an_event_stream(client, monkeypatch):
    """The mounted route, its headers, and the frames - the shape of the response itself.

    The connection is *ended* for the test that reads it whole: a real stream is held open until
    the client leaves or the session's lifetime is up, and a client that reads one to the end is
    waiting for that (see the lifetime test below). What is being pinned here is the response, so
    the lifetime is made to arrive at once.
    """
    _clear_board()
    _plant(WORKER, hours_ago=10)
    _plant(MOALLEM, hours_ago=2, site_name=OTHER_SITE, late_flag="outside the 05:00-06:00 window")
    counted = _count(client)
    monkeypatch.setattr(live_ops, "_STREAM_LIFETIME_SECONDS", 0.0)

    response = client.get(STREAM, headers=bearer(ADMIN))

    assert response.status_code == 200, response.text[:300]
    # What a middle box is told about the response, as much as the browser: event-stream is what
    # stops a proxy from treating it as a document, and the other two are the opt-outs that stop
    # one from treating it as a *complete* document and buffering the whole reply.
    assert response.headers["content-type"].startswith("text/event-stream"), response.headers
    assert response.headers["cache-control"] == "no-store", response.headers
    assert response.headers["x-accel-buffering"] == "no", response.headers

    frames = _sse_frames(response.text)
    assert frames[0] == ["retry: 5000"], frames[0]
    board_frames = [frame for frame in frames if frame and frame[0].startswith("event: board")]
    assert len(board_frames) == 1, frames
    pushed = _data(board_frames[0])
    assert "as_of" in pushed, "the pushed figures are a snapshot and say when they were taken"
    for key in ("on_site", "late", "sites", "worker_ids"):
        assert pushed[key] == counted[key], f"the stream and the count disagree about {key}"
    assert pushed["on_site"] == 2
    assert pushed["longest"]["worker_id"] == counted["longest"]["worker_id"] == WORKER
    # ``seconds_on_site`` travels with it for the same reason it travels in the count: a console
    # in another zone must not parse a zone-less stamp to start its counter.
    assert pushed["longest"]["seconds_on_site"] >= 10 * 3600 - 5, pushed["longest"]
    assert frames[-1] == ["event: bye", "data: {}"], frames[-1]


def test_only_the_boards_own_audience_may_hold_it_open(client):
    """``admin_only``, like every other read of the gate - and the session is checked at all."""
    assert client.get(STREAM, headers=bearer(WORKER)).status_code == 403
    assert client.get(STREAM, headers=bearer(MOALLEM)).status_code == 403
    assert client.get(STREAM).status_code in (401, 403)


# ---------------------------------------------------------------------------
# nothing to say
# ---------------------------------------------------------------------------
def test_a_board_nobody_touched_is_never_an_event(monkeypatch):
    """The feature, in one test: counting an unchanged board is not the same as sending it."""
    _clear_board()
    _plant(WORKER, hours_ago=3)

    counts: list[int] = []
    real_board_now = live_ops.board_now

    def counted() -> dict:
        counts.append(1)
        return real_board_now()

    monkeypatch.setattr(live_ops, "board_now", counted)
    # The sweep is the floor under a missed wake-up, and the only thing that re-counts an idle
    # board. Shortened so a test can watch it come round several times: the wait between turns
    # is floored at a second, so this is a sweep a second, not a busy loop.
    monkeypatch.setattr(live_ops, "_STREAM_SWEEP_SECONDS", 0.05)

    frames = asyncio.run(_drain(live_ops._board_events(_Request()), seconds=2.4))

    assert frames[0] == live_ops._SSE_RETRY
    assert frames[1].startswith("event: board"), "the way in is the figures, not silence"
    assert len(counts) >= 2, f"the sweep never came round, so this proves nothing ({len(counts)})"
    assert [frame for frame in frames if frame.startswith("event: board")] == [frames[1]], (
        "a count that found the same board sent an event anyway"
    )
    assert frames[2:] == [], f"and nothing else was said: {frames[2:]}"


# ---------------------------------------------------------------------------
# the write, the wake, and the push
# ---------------------------------------------------------------------------
def test_a_write_tells_the_open_board_to_look_again(client):
    """End to end: an ordinary route moves the board, and the open connection hears about it.

    The console that matters is somebody else's: an administrator ends a shift from another
    machine, and the board on the counter updates because the *write* said so rather than
    because a timer asked. The token crosses threads on the way (``call_soon_threadsafe``), and
    a board that only worked on the loop that opened it would fail exactly here.
    """
    _clear_board()
    _plant(WORKER, hours_ago=3)

    async def scenario() -> list[str]:
        generator = live_ops._board_events(_Request())
        frames = [await generator.__anext__()]                        # retry
        frames.append(await generator.__anext__())                    # the board as it is
        # The write, through the ordinary route: closing the only open shift empties the board.
        response = client.post(
            "/api/v1/admin/force_clock_out", headers=bearer(ADMIN), json={"worker_id": WORKER}
        )
        assert response.status_code == 200, response.text[:300]
        frames.append(await asyncio.wait_for(generator.__anext__(), timeout=5))
        await generator.aclose()
        return frames

    frames = asyncio.run(scenario())

    assert frames[1].startswith("event: board"), frames[1]
    assert _data(frames[1].splitlines())["on_site"] == 1, (
        "the board before the write had somebody on it"
    )
    assert frames[2].startswith("event: board"), frames[2]
    assert _data(frames[2].splitlines())["on_site"] == 0, (
        "the push after the write is not the board the write left"
    )


def test_a_run_of_writes_is_one_wake_up():
    """A wake-up is a signal, not an event: a slow reader coalesces rather than queueing."""
    async def scenario() -> None:
        queue = await live_ops.BOARD.listen()
        try:
            live_ops.board_changed()
            live_ops.board_changed()
            live_ops.board_changed()
            await asyncio.wait_for(queue.get(), timeout=1.0)
            with pytest.raises(asyncio.TimeoutError):
                # The backlog is one wake-up deep, however many writes said so.
                await asyncio.wait_for(queue.get(), timeout=0.05)
        finally:
            live_ops.BOARD.stop(queue)
        # Nobody is listening now, and a write nobody hears is the ordinary case: it must not
        # be an error, and it must not touch a queue that belongs to a closed loop.
        live_ops.board_changed()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# and it ends by itself
# ---------------------------------------------------------------------------
def test_the_connection_ends_by_itself_and_says_so(monkeypatch):
    """The session is re-checked rather than trusted for ever, and an idle line stays warm."""
    _clear_board()
    monkeypatch.setattr(live_ops, "_STREAM_LIFETIME_SECONDS", 0.05)
    monkeypatch.setattr(live_ops, "_STREAM_KEEPALIVE_SECONDS", 0.05)

    frames = asyncio.run(_drain(live_ops._board_events(_Request()), seconds=2.5))

    assert frames[0] == live_ops._SSE_RETRY
    assert frames[-1] == live_ops._SSE_BYE, frames
    assert live_ops._SSE_KEEPALIVE in frames, (
        "a long-lived connection has to say something periodically, or a middle box times it out"
    )


# ---------------------------------------------------------------------------
# the fingerprint, which is the whole reason the above is cheap
# ---------------------------------------------------------------------------
def _fingerprint(**overrides) -> str:
    board = {
        "as_of": "2026-09-29 06:00:00",
        "on_site": 2,
        "late": 1,
        "sites": [{"site_name": SITE, "workers": 1}, {"site_name": OTHER_SITE, "workers": 1}],
        "worker_ids": [WORKER, MOALLEM],
        "longest": {
            "worker_id": WORKER, "name": "Seed Lead Worker", "site_name": SITE,
            "clock_in_time": "2026-09-28 20:00:00", "seconds_on_site": 36000,
        },
    }
    board.update(overrides)
    return live_ops.board_fingerprint(board)


def test_a_rename_is_not_the_board_moving():
    """The trade the console's own ``liveOpsMoved`` documents, kept by the server too."""
    base = _fingerprint()
    assert _fingerprint(as_of="2026-09-29 06:12:48") == base, (
        "the clock advancing would make this the poll again, in comments"
    )
    renamed = _fingerprint(longest={
        "worker_id": WORKER, "name": "Somebody Else Entirely", "site_name": SITE,
        "clock_in_time": "2026-09-28 20:00:00", "seconds_on_site": 40000,
    })
    assert renamed == base, "a new name (and a counter that moved with it) is not the board"
    # The ids come back in no order, and a board whose fingerprint depended on that order would
    # fire on every read.
    assert _fingerprint(worker_ids=[str(value) for value in reversed([WORKER, MOALLEM])]) == base


def test_every_fact_the_board_draws_moves_the_fingerprint():
    """The other half: a fingerprint blind to a real change is a board that never updates."""
    base = _fingerprint()
    for change in (
        {"on_site": 3},
        {"late": 0},
        {"worker_ids": [WORKER, MOALLEM, "90001"]},
        {"sites": [{"site_name": SITE, "workers": 2}, {"site_name": OTHER_SITE, "workers": 1}]},
        {"longest": {
            "worker_id": MOALLEM, "name": "Seed Lead", "site_name": OTHER_SITE,
            "clock_in_time": "2026-09-28 18:00:00", "seconds_on_site": 43200,
        }},
        {"longest": None},
    ):
        assert _fingerprint(**change) != base, f"{change} is a different board"
