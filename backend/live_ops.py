"""The Live Ops board's own figures, counted rather than downloaded.

WHY THIS MODULE EXISTS
---------------------
The board is three numerals and a list: how many people are on site, how many places (or
categories) that is spread across, how many arrived late, and which shift has been open
longest. Every one of those figures used to be produced by **fetching payloads and counting
what was in them** - ``Promise.all`` of ``/admin/active_sessions``, ``/admin/users``,
``/admin/sites`` and ``/admin/shift_rules`` - and the roster read is the expensive half:
``/admin/users`` answers ``password_set`` per row and joins the audit log for every account's
last password change, so a board whose whole job is to say "12 on site" paid a price that
grew with the number of *accounts*, on every render, on every Refresh and inside its own
poll. It is the right shape for the screen that edits the roster and the wrong shape for the
screen that counts the gate.

So the figures are counted here, in SQL, and the board reads them. What is left of the four
payloads is what a board actually draws with: the open shifts themselves, the sites (a picker
and a category map, one row per *site*), the shift rules (one row), and - only when somebody
opens the force-in panel - the roster, which is the one payload that is allowed to be
roster-sized because that panel is a roster.

AND THEN THE BOARD STOPPED ASKING
---------------------------------
A counted read made the question cheap; it did not make it unnecessary. The board is a screen
somebody *watches*, so it was still asking "has anything moved?" every 45 seconds to find out
that nothing had - and the answer was always the same on a quiet afternoon. The second half of
this module is the other direction: ``/admin/live_ops/stream`` holds one connection open, the
punch paths say when they have written something (``board_changed``), and the figures travel
only when they are new. An untouched board now costs the socket it is sitting on and three
comment lines a minute.

The read itself did not change shape when this happened, and that is deliberate: the stream
sends the same payload ``/live_ops/count`` answers with, counted by the same ``summary``, so
the channel and the fallback cannot hold different opinions about what "12 on site" means.
The poll is still there - a deployment that cannot carry a stream falls back to it (see
``_STREAM_SWEEP_SECONDS`` for the floor under a missed notification, and the console's
``LIVE_OPS_POLL_MS`` for the other half of that story).

THE BOARD'S OWN JOIN, VERBATIM
------------------------------
Every query here is
``FROM active_sessions a JOIN users u ON a.worker_id = u.id`` - the same join
``GET /admin/active_sessions`` uses, row for row - so a session whose account has been deleted
is dropped by the figures and by the rows in the same breath and the two cannot report
different totals for the same afternoon. That is also why ``dashboard._now`` borrows
``summary`` for its ``on_shift``/``by_site`` instead of keeping its own copy of the join: it
used to hold the board's query verbatim, and a verbatim copy is a copy.

Two properties are inherited from the board rather than decided here, and both are deliberate:

* it is **not scoped by the concealment clause** the roster is scoped by. The board names who
  is standing at the gate, and a count that hid a session the board names would be two answers
  to one question on the same deployment (``dashboard._now``'s docstring says the same thing
  beside the figure it borrows);
* ``late`` is the **flag's own reading**, not the window rule. Whether an arrival was late is
  decided once, when the punch is filed, and what the row carries is that decision
  (``shift_windows.describe``, or ``NULL`` when the arrival was inside the window). A count
  that re-derived it here would be a second implementation of the shift window - and the one
  number that could disagree with the badge on the row.

WHAT IS *NOT* COUNTED, AND WHY
------------------------------
``longest`` cannot be an aggregate. "Which open shift has run longest" is a question about
*time elapsed*, and elapsed time is computed from a stored stamp by ``shift_hours`` (see
``seconds_on_site`` below for the zone it is stored in). So the query orders by the stored
stamp - which is monotone for the one format this application writes - and the seconds are
computed by the same function the card, the clock-out path and the recorded hours use. The
row is picked with the client's own rule: the first row whose stamp the shared arithmetic can
read, because a stamp nobody can parse is not a shift the board can show a duration for.

A NOTE FOR WHOEVER LANDS THE UTC MIGRATION
------------------------------------------
``clock_in_time`` is the zone-less wall-clock convention the attendance tables use today, and
``seconds_on_site`` is the same arithmetic ``main`` and ``shift_hours`` use, so this module
moves with them when ``docs/RUNBOOK_ATTENDANCE_UTC.md`` is executed - nothing here parses a
stamp on its own.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from collections.abc import AsyncIterator, Mapping
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse

import shift_hours
from database import db
from security import CurrentUser, admin_only

log = logging.getLogger("attendance.live_ops")

router = APIRouter(prefix="/admin", tags=["live ops"])

#: ``%Y-%m-%d %H:%M:%S`` - the format every stored timestamp in this application uses, and
#: therefore the format SQLite's own string comparison can be trusted to sort chronologically.
_TS = "%Y-%m-%d %H:%M:%S"

#: The board's own join, written once.
_BOARD_JOIN = "FROM active_sessions a JOIN users u ON a.worker_id = u.id"

#: How long an open stream may stay silent before it says something anyway.
#:
#: Not for the browser - for everything between it and us. An idle HTTP response is reaped by
#: the middle: Cloudflare closes a request that has sent nothing for about 100 seconds, and an
#: operator's own router may be quicker. Fifteen seconds is a comment line (``: keep-alive``,
#: a few bytes with no event in it) often enough that no hop can decide the connection is
#: dead. It is deliberately *not* a re-read: the whole point of this endpoint is that an
#: unchanged board costs nothing, and a keep-alive that counted the roster would be the poll
#: again with more machinery.
_STREAM_KEEPALIVE_SECONDS = 15.0

#: How often an open stream re-reads the board even though nothing has told it to.
#:
#: The floor under the notifications below, and it exists because a notification is a thing a
#: *writer* has to remember: a session opened by some future code path that does not call
#: ``board_changed`` would otherwise leave the board showing yesterday's gate until somebody
#: pressed Refresh. Two minutes is the compromise - a missed notification costs a stale board
#: for at most that long, which is a third of what the 45-second poll could cost, and an idle
#: stream costs one count every two minutes instead of one request every 45 seconds.
_STREAM_SWEEP_SECONDS = 120.0

#: How long one stream lives before it ends on purpose and the console opens another.
#:
#: A stream that never ends is a connection nobody reaps: an administrator whose account was
#: deactivated mid-shift would keep reading the gate on a session that was authorised fifteen
#: minutes ago. Ending it puts the console through authorisation and the board through a fresh
#: read, and the reconnect is one request. Fifteen minutes is long enough that the ordinary
#: board never sees it and short enough that nothing can go stale unnoticed.
_STREAM_LIFETIME_SECONDS = 900.0

#: What the console is told to wait before reconnecting (``retry:``, an SSE field). The
#: console's own loop owns its reconnects and uses its own delay; this is here so that a plain
#: ``EventSource`` against this endpoint - a diagnostic, or some future screen - does not
#: hammer a deployment whose stream just ended.
_SSE_RETRY = "retry: 5000\n\n"

#: The two lines an open stream sends when it has nothing else to say, and when it is done.
_SSE_KEEPALIVE = ": keep-alive\n\n"
_SSE_BYE = "event: bye\ndata: {}\n\n"

#: How far down the clock-in order to look for a shift the board can measure. One row is the
#: answer - the oldest readable stamp - and the small window exists only so that a stamp
#: nothing can parse is stepped over rather than reported as the board's longest shift.
_LONGEST_SCAN = 5


def _parse_ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    for fmt in (_TS, _TS + ".%f"):
        try:
            return datetime.strptime(str(value), fmt)
        except (TypeError, ValueError):
            continue
    return None


def seconds_on_site(clock_in_time: Any, now: datetime | None = None) -> int | None:
    """Whole seconds since a stored clock-in, or ``None`` if it cannot be read.

    Sent with every open shift so a client that draws a *live* counter starts at the
    server's own figure. The stored clock-in is a zone-less wall-clock string
    (``%Y-%m-%d %H:%M:%S``, written by ``datetime.now()``, i.e. in *this* host's zone);
    a phone or a console in another zone reads those digits as its own local time, which
    puts a constant offset on the counter - three hours, for a server in UTC and staff in
    Kuwait, on a shift that has just started. Sending the count alongside the stamp means
    the client never has to know what zone the digits were written in: it starts from this
    number and adds only the seconds it has watched pass.

    "This host's zone" is the company's zone now: ``clock.py`` pins the process (and the
    container pins the image) to ``Asia/Kuwait``, so the stored digits, this counter and the
    wall clock at the gate all agree.

    The same function the clock-out path measures a closed shift with
    (``shift_hours.elapsed_seconds``), so the counter on the card and the hours that are
    actually recorded cannot disagree about what "so far" means.
    """
    clock_in = _parse_ts(clock_in_time)
    if clock_in is None:
        return None
    return shift_hours.elapsed_seconds(clock_in, now if now is not None else datetime.now())


def summary(conn: sqlite3.Connection, *, now: datetime | None = None) -> dict[str, Any]:
    """The board's figures, on an open connection. One pass, three queries, no payload.

    ``worker_ids`` is the board's *identity* rather than one of its numerals: it is what the
    board's own poll compares to answer "has anything moved" without downloading the rows to
    find out, and it is the smallest thing that answers it - a handover is two ids and the
    same count. Names are deliberately not here; a renamed worker is picked up by the rows
    the poll then fetches, or by the operator's own Refresh. The ids come back in no
    particular order, so a reader that compares them sorts them first (the board does).
    """
    now = now if now is not None else datetime.now()
    totals = conn.execute(
        f"""
        SELECT COUNT(*) AS workers,
               SUM(CASE WHEN {_LATE_SQL} THEN 1 ELSE 0 END) AS late,
               GROUP_CONCAT(a.worker_id, ',') AS ids
        {_BOARD_JOIN}
        """
    ).fetchone()
    sites = [
        {"site_name": str(name), "workers": int(count)}
        for name, count in conn.execute(
            f"""
            SELECT a.site_name, COUNT(*) AS workers
            {_BOARD_JOIN}
            GROUP BY a.site_name
            ORDER BY workers DESC, a.site_name COLLATE NOCASE ASC
            """
        ).fetchall()
        if name is not None and str(name) != ""
    ]
    # The oldest *readable* clock-in. Ordered by the stored stamp and stepped past anything
    # this application's own arithmetic cannot read, so the figure cannot name a shift no
    # duration can be shown for (see the module docstring).
    longest: dict[str, Any] | None = None
    for row in conn.execute(
        f"""
        SELECT a.worker_id, u.name, a.site_name, a.clock_in_time
        {_BOARD_JOIN}
        WHERE a.clock_in_time IS NOT NULL AND TRIM(a.clock_in_time) <> ''
        ORDER BY a.clock_in_time ASC
        LIMIT ?
        """,
        (_LONGEST_SCAN,),
    ).fetchall():
        seconds = seconds_on_site(row["clock_in_time"], now)
        if seconds is None:
            continue
        longest = {
            "worker_id": str(row["worker_id"]),
            "name": row["name"],
            "site_name": row["site_name"],
            "clock_in_time": row["clock_in_time"],
            "seconds_on_site": seconds,
        }
        break
    return {
        "as_of": now.strftime(_TS),
        "on_site": int(totals["workers"] or 0),
        "late": int(totals["late"] or 0),
        "sites": sites,
        "worker_ids": [part for part in str(totals["ids"] or "").split(",") if part],
        "longest": longest,
    }


#: What "late" means, in SQL, exactly as the rows mean it on the client.
#:
#: ``active_sessions.late_flag`` is either ``NULL`` (the arrival was inside the site's window)
#: or the sentence ``shift_windows.describe`` wrote when it was not - a description for a
#: notification body, never a boolean. So the test is "somebody wrote that this was late",
#: and the two values that mean the opposite are named rather than assumed: a column that can
#: hold prose can also hold ``"0"``. The console's ``liveOpsIsLate`` is the same rule in the
#: reader's hands, and a test holds the two together.
_LATE_SQL = (
    "a.late_flag IS NOT NULL AND TRIM(a.late_flag) <> '' "
    "AND LOWER(TRIM(a.late_flag)) NOT IN ('0', 'false')"
)


def board_fingerprint(board: Mapping[str, Any]) -> str:
    """The five facts a board draws, as one string that can be compared.

    This is the server's half of the question the console's own ``liveOpsMoved`` answers, and
    the two lists have to agree: who is on shift (the ids, which come back in no order and are
    therefore sorted), where they are (a site and how many of them), how many arrived late,
    and which shift is the oldest. **A name is deliberately not here** - renaming somebody on
    shift leaves every fact in this string unchanged, which is the trade the console's
    docstring already documents and the reason the event below may arrive without the new
    name on it.

    ``as_of`` is excluded for the reason it is excluded there: it moves on every read, and a
    stream that fired because the clock advanced would be the poll again, in comments.
    """
    longest = board.get("longest") or None
    return json.dumps(
        {
            "on_site": int(board.get("on_site") or 0),
            "late": int(board.get("late") or 0),
            "sites": [
                [str(site.get("site_name") or ""), int(site.get("workers") or 0)]
                for site in board.get("sites") or []
            ],
            "ids": sorted(str(worker) for worker in board.get("worker_ids") or []),
            "longest": (
                [str(longest.get("worker_id") or ""), str(longest.get("clock_in_time") or "")]
                if longest
                else None
            ),
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def board_now() -> dict[str, Any]:
    """The board's figures on a connection of its own, for a caller that has none open."""
    with db() as conn:
        return summary(conn)


class BoardBroadcast:
    """Which boards are listening, and how a write tells them to look again.

    IN PROCESS, AND THAT IS THE HONEST SHAPE
    ----------------------------------------
    A listener is an ``asyncio.Queue`` living in this process's event loop, so this reaches
    exactly the connections *this* process is serving. One process is what the deployment is
    (Railway runs one service, and the tests run one), and where it is not - a second worker,
    or a future horizontal scale - the difference is a board that waits for the sweep above
    instead of updating instantly. It is not a correctness boundary: the figures are re-read
    from the database on every event, so a missed notification is a late board and never a
    wrong one.

    WHY ``call_soon_threadsafe`` AND NOT ``put_nowait``
    ---------------------------------------------------
    Two reasons, and the first is the subtle one. A queue may only be touched from the loop
    that owns it, and a punch handler is not guaranteed to be that loop's thread - so the wake
    is scheduled rather than made. The second is a race that scheduling happens to close: a
    writer calls ``board_changed`` from *inside* its write transaction, before the commit, and
    a listener woken at that instant would re-count the state the write is about to replace
    and then wait for a change that has already happened. Because a scheduled callback cannot
    run until the coroutine that scheduled it next yields - which is after the synchronous
    ``with`` block has committed - the wake always lands after the write it is about.
    """

    def __init__(self) -> None:
        self._listeners: set[asyncio.Queue[int]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    async def listen(self) -> asyncio.Queue[int]:
        """A queue that receives one token per "look again", for as long as it is kept."""
        # ``maxsize=1`` on purpose: the tokens are not events, they are *wake-ups*, and a
        # second one that arrives while the first is still unread says nothing new. A slow or
        # wedged reader therefore cannot grow a backlog - it coalesces, which is the property
        # that keeps a punch from ever waiting on a browser.
        queue: asyncio.Queue[int] = asyncio.Queue(maxsize=1)
        self._listeners.add(queue)
        self._loop = asyncio.get_running_loop()
        return queue

    def stop(self, queue: asyncio.Queue[int]) -> None:
        self._listeners.discard(queue)
        if not self._listeners:
            self._loop = None

    @staticmethod
    def _wake(queue: asyncio.Queue[int]) -> None:
        try:
            queue.put_nowait(1)
        except asyncio.QueueFull:
            pass   # already awake, and one wake-up is all a listener needs

    def changed(self) -> None:
        """Wake every open board. Cheap, synchronous, and safe to call from any thread."""
        loop = self._loop
        if loop is None:
            return   # nobody is watching: this is the ordinary case, and it costs nothing
        for queue in list(self._listeners):
            try:
                loop.call_soon_threadsafe(self._wake, queue)
            except RuntimeError:
                # The loop went away between the read and the call - a shutdown, or a test
                # that closed the loop while a request was still in flight.
                return


#: The one broadcast. A write that changes what the board draws says so through
#: ``board_changed`` below; nothing else knows or cares that it exists.
BOARD = BoardBroadcast()


def board_changed() -> None:
    """Say that something the board draws has been written.

    Called by the punch paths - and by the offline sync and the quick-link punch, which are
    punches - immediately after the row they insert, update or delete. It answers one question
    ("should an open board look again?") and it is deliberately called *at the write* rather
    than after the transaction: see ``BoardBroadcast`` for why the wake cannot be processed
    before the commit that follows it.

    The lines it watches are the five facts in ``board_fingerprint``. That means a write which
    changes none of them - retiring an account that is not on shift, a site renamed under a
    session that keeps its old ``site_name``, a punch that only moved ``overtime_notified_at``
    - is allowed to say so anyway; the stream re-counts and finds nothing to send, which costs
    one count and is the right side of the trade to be on.
    """
    BOARD.changed()


async def _board_events(request: Request) -> AsyncIterator[str]:
    """The board's figures, as they change, for as long as the caller stays.

    The shape of one connection's life: the figures straight away (a console that just opened
    the tab has nothing of its own worth keeping), then nothing at all until either a writer
    says the board moved, the sweep above comes round, or a keep-alive is due. Nothing is sent
    to a board that has not changed, which is the entire difference between this and the poll
    it replaces: an idle gate now costs one open socket and three short comment lines a
    minute, instead of a request and a count every 45 seconds.
    """
    queue = await BOARD.listen()
    sent: str | None = None
    started = time.monotonic()
    sweep_at = started          # read the board on the way in, not after the first wait
    keepalive_at = started + _STREAM_KEEPALIVE_SECONDS
    try:
        yield _SSE_RETRY
        while True:
            if time.monotonic() >= sweep_at:
                sweep_at = time.monotonic() + _STREAM_SWEEP_SECONDS
                board = await run_in_threadpool(board_now)
                fingerprint = board_fingerprint(board)
                if fingerprint != sent:
                    sent = fingerprint
                    # Any bytes reset every idle timer between here and the browser, so a real
                    # event *is* a keep-alive; the comment below is only for a stream with
                    # nothing to say.
                    keepalive_at = time.monotonic() + _STREAM_KEEPALIVE_SECONDS
                    yield f"event: board\ndata: {json.dumps(board)}\n\n"
            if time.monotonic() - started >= _STREAM_LIFETIME_SECONDS:
                yield _SSE_BYE
                return
            wait_for = max(1.0, min(sweep_at, keepalive_at) - time.monotonic())
            try:
                await asyncio.wait_for(queue.get(), timeout=wait_for)
                # A write said the board moved: count now rather than waiting for the sweep.
                sweep_at = time.monotonic()
            except asyncio.TimeoutError:
                if time.monotonic() >= keepalive_at:
                    keepalive_at = time.monotonic() + _STREAM_KEEPALIVE_SECONDS
                    yield _SSE_KEEPALIVE
            # Checked once per wait rather than on a timer of its own: a client that has gone
            # is discovered the next time this loop comes round, which is never more than a
            # keep-alive away, and until then the connection costs one queue and one task.
            if await request.is_disconnected():
                return
    finally:
        BOARD.stop(queue)


@router.get("/live_ops/count")
async def count_live_ops(current: CurrentUser = Depends(admin_only)):
    """The board's headline figures, without the payloads it used to count them from.

    Read once per board render and once per poll instead of the roster, the site list and the
    open-shift rows. The rows are still fetched - a board is a list of who is at the gate -
    but the *figures* are counted here, and the poll asks this question ("has anything moved")
    rather than downloading the rows and counting them to decide it.

    Still here with the stream below, and not deprecated: it is what the console falls back to
    when a deployment cannot carry a stream (see ``/live_ops/stream``), and it is the read a
    browser, a script or a diagnostic asks for when it wants an answer rather than a channel.
    """
    with db() as conn:
        return summary(conn)


@router.get("/live_ops/stream")
async def stream_live_ops(
    request: Request, current: CurrentUser = Depends(admin_only)
):  # noqa: ARG001 - the guard is the point
    """The board's figures as they change, pushed instead of asked for.

    WHY A STREAM AT ALL
    -------------------
    The board is watched, not read: an administrator leaves it open on a counter while the gate
    fills up, and the only thing it has to say is "this changed". A poll can only answer that
    by asking, so an unchanged gate used to cost a request and a count every 45 seconds - and
    the poll was the *third* shape of this read, after the four-payload version and the counted
    one. Here the direction is reversed: the connection stays open, writers say when something
    moved (``board_changed``), and an untouched board costs nothing at all.

    WHY NOT ``EventSource``
    -----------------------
    The obvious client for this format cannot carry an ``Authorization`` header, and the two
    ways round that are both worse than a stream: a session token in the query string is a
    live credential in every access log between here and the browser, and a cookie for one
    endpoint is a second authentication scheme for the same session. So the console reads this
    with ``fetch`` and a stream reader (``API.stream``), which sends the same bearer token as
    every other call, and this endpoint is an ordinary authenticated route because of it:
    ``admin_only``, checked once at the door, with the connection ending after
    ``_STREAM_LIFETIME_SECONDS`` so that the check is made again rather than trusted for ever.

    Note the direction the *data* travels. A change to the gate is counted in SQL
    (``summary``) and sent as the same payload ``/live_ops/count`` answers with, so the
    console's two paths - the stream it opens on the tab, and the poll it falls back to -
    read the same object and cannot disagree about what "12 on site" means.
    """
    return StreamingResponse(
        _board_events(request),
        media_type="text/event-stream",
        headers={
            # A stream is not a document to cache, and no proxy in front of a deployment has
            # any business storing one: the figures are somebody's shift.
            "Cache-Control": "no-store",
            # nginx (and anything modelled on it, Cloudflare's origin rules included) buffers a
            # proxied response by default, which turns a stream into a very slow JSON reply.
            # The header is the documented opt-out, and it is harmless where nothing buffers.
            "X-Accel-Buffering": "no",
        },
    )
