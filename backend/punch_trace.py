"""Where a punch's seconds went - and a line for the punch that never comes back.

WHY THIS EXISTS
---------------
Four punches on 2026-10-03 (18:59:05, 19:03:30 and 19:04:12, deployment ``a7a9a29c``) sat
open for 48.2 s, 263.2 s, 275.0 s and 283.5 s and were then abandoned by the phone; the
platform recorded all four as ``499`` - "client has closed the request before the server
could send a response". The deployment's own logs contain *nothing* for those minutes: no
access line (uvicorn writes one when a response is sent, and none was sent), no model line,
no error, no traceback. A request that never finishes cannot report anything from the code
path it is stuck in, which is exactly why those four are still unexplained - and why "it is
slow when several people punch at once" could not be attributed to a stage rather than
guessed at.

So this module keeps the one fact a stuck punch cannot lose: how far it had got, and since
when. The endpoint registers a trace on its way in and names each stage as it enters it;
one sweeper task serves the whole process, and every ``PUNCH_WATCHDOG_SECONDS`` it writes a
warning for each punch that is *still open*. A punch that hangs for four minutes leaves a
trail of stage lines instead of silence, and a punch that merely took longer than
``PUNCH_SLOW_SECONDS`` reports where its seconds went the moment it ends.

WHY A SWEEPER AND NOT A TIMER PER PUNCH
---------------------------------------
A timer per punch would have to be cancelled by that punch - and the case that matters is
the one where the punch never gets there. It would then log forever, for every early
refusal, and the log would be worthless. Registration is instead a dictionary entry, and
the sweep prunes by asking the request's own *task* whether it is done: uvicorn runs one
task per request, so ``task.done()`` is true however the request ended - a 200, a 403, an
exception, a client disconnect - and no endpoint has to remember to clean up.

COST
----
A handful of dictionary writes per punch: no timer, no thread, and no lock held across an
await. The sweeper is one task, twice a minute, that does nothing at all when nothing is
open.

WHAT IT IS NOT
--------------
Not a timeout and not a limiter: nothing here cancels, delays, or answers a request. It is
an instrument, and it is off with ``PUNCH_WATCHDOG_SECONDS=0``.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from config import settings

log = logging.getLogger(__name__)

#: The stages a punch passes through, in the order ``verify_worker`` reaches them. Named here
#: so a log line and the endpoint cannot disagree about what "model" means.
STAGE_SESSION = "session"
STAGE_GEOFENCE = "geofence"
STAGE_UPLOAD = "upload"
STAGE_MODEL = "model"
STAGE_DATABASE = "database"
STAGE_RESPONSE = "response"

#: A trace this old is dropped even if its task never completes. A punch cannot legitimately
#: be open for a quarter of an hour, and a registry that can only grow is a leak with a log
#: line attached - so the last line is written and the entry goes.
ABANDONED_SECONDS = 900.0


@dataclass
class PunchTrace:
    """One in-flight punch: who, which action, which stage, and how long so far."""

    worker_id: str
    action: str
    request_id: str = "-"
    stage: str = STAGE_SESSION
    started: float = field(default_factory=time.perf_counter)
    entered: float = field(default_factory=time.perf_counter)
    #: Seconds spent in each stage already closed, in the order they were left.
    spent: dict[str, float] = field(default_factory=dict)

    def enter(self, stage: str) -> None:
        """Name the stage this punch is entering, closing the one it was in."""
        now = time.perf_counter()
        self.spent[self.stage] = round(now - self.entered, 3)
        self.stage = stage
        self.entered = now

    def elapsed(self) -> float:
        """Seconds since the punch was registered."""
        return time.perf_counter() - self.started

    def in_stage(self) -> float:
        """Seconds spent in the stage it is in now."""
        return time.perf_counter() - self.entered

    def where(self) -> str:
        """The half of the line a *stuck* punch can still answer."""
        return f"stage={self.stage} for {self.in_stage():.1f}s"

    def stages(self) -> str:
        """Every closed stage's duration, plus the open one timed so far."""
        closed = [f"{name}={seconds:.3f}s" for name, seconds in self.spent.items()]
        closed.append(f"{self.stage}={self.in_stage():.3f}s so far")
        return " ".join(closed)

    def line(self) -> str:
        """The identifying half of both lines - ``request`` is Railway's ``requestId``."""
        return (
            f"worker={self.worker_id} action={self.action} request={self.request_id} "
            f"total={self.elapsed():.3f}s"
        )


#: Every punch that has not finished yet, keyed by the trace's own identity. The value pairs
#: the trace with the task running it, which is what tells the sweeper the request is over.
_INFLIGHT: dict[int, tuple[PunchTrace, asyncio.Task[Any] | None]] = {}
_LOCK = threading.Lock()


def begin(*, worker_id: str, action: str, request_id: str | None = None) -> PunchTrace:
    """Register a punch under the task running it. Call it as the endpoint's first statement."""
    try:
        task: asyncio.Task[Any] | None = asyncio.current_task()
    except RuntimeError:  # pragma: no cover - no loop: a direct call from a tool or a test
        task = None
    return register(
        PunchTrace(
            worker_id=str(worker_id), action=str(action), request_id=request_id or "-"
        ),
        task=task,
    )


def register(trace: PunchTrace, *, task: Any = None) -> PunchTrace:
    """Register a trace the caller owns. For a test, or a tool with its own task.

    ``task`` is only ever *asked* whether it is done (see ``sweep``), so anything with a
    ``done()`` answers the question - which is what lets a test drive the sweeper without a
    running loop or a real request.
    """
    with _LOCK:
        _INFLIGHT[id(trace)] = (trace, task)
    return trace


def inflight() -> list[PunchTrace]:
    """The open punches, longest first. For a test, and for a process asked what it is doing."""
    with _LOCK:
        traces = [trace for trace, _task in _INFLIGHT.values()]
    return sorted(traces, key=PunchTrace.elapsed, reverse=True)


def clear() -> None:
    """Forget every registration. For tests only."""
    with _LOCK:
        _INFLIGHT.clear()


def sweep() -> int:
    """One pass: report what is open, drop what has finished, and answer how many are open.

    The pass is the whole behaviour and the sleeping is the only part that needs a loop, so
    a test drives this directly rather than waiting on a real clock.
    """
    slow_seconds = max(0.0, float(settings.punch_slow_seconds))
    with _LOCK:
        items = list(_INFLIGHT.items())
    open_now = 0
    for key, (trace, task) in items:
        if task is None or task.done():
            # Finished - however it finished. A punch that took long enough to be worth
            # explaining says where its seconds went; a fast one says nothing, which is what
            # keeps this instrument quiet on a working deployment.
            with _LOCK:
                _INFLIGHT.pop(key, None)
            if slow_seconds and trace.elapsed() >= slow_seconds:
                log.warning("slow punch: %s stages: %s", trace.line(), trace.stages())
            continue
        if trace.elapsed() >= ABANDONED_SECONDS:
            with _LOCK:
                _INFLIGHT.pop(key, None)
            log.warning("abandoning an untrackable punch: %s %s", trace.line(), trace.where())
            continue
        open_now += 1
        log.warning(
            "punch still open after %.0fs: %s %s", trace.elapsed(), trace.line(), trace.where()
        )
    return open_now


async def watch() -> None:
    """Sweep every ``PUNCH_WATCHDOG_SECONDS`` until cancelled."""
    interval = max(1.0, float(settings.punch_watchdog_seconds))
    while True:
        await asyncio.sleep(interval)
        sweep()


def start_watcher() -> asyncio.Task[Any] | None:
    """Start the sweeper in the running loop (the lifespan), or nothing when it is off."""
    if float(settings.punch_watchdog_seconds) <= 0:
        return None
    return asyncio.get_running_loop().create_task(watch(), name="punch-watchdog")


def stop_watcher(task: asyncio.Task[Any] | None) -> None:
    """Cancel the sweeper. Safe on ``None``, which is what a disabled watchdog starts as."""
    if task is not None:
        task.cancel()
