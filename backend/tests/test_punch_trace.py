"""The instrument that makes a stuck punch say where it is stuck.

WHY THIS EXISTS
---------------
Four punches on 2026-10-03 ran for 48.2 s, 263.2 s, 275.0 s and 283.5 s and were abandoned by
the phone; the deployment's logs contain nothing for those minutes, because a request that
never finishes reports nothing. ``punch_trace`` is the answer to that, so what is pinned here is
exactly the two properties it has to have:

1. **A punch that is still open says which stage it is in** - and says so repeatedly, because
   the number that matters is how long it has been *there*, not how long it has been alive.
2. **A punch that finished reports where its seconds went, and a fast one says nothing.**
   An instrument that logs every punch is an instrument nobody reads.

The sweeper is driven directly, with a stand-in task for the request: the pass is the whole
behaviour (``sweep``) and the sleeping is the only part that needs a loop.

WHAT IS DELIBERATELY NOT TESTED HERE
------------------------------------
That uvicorn gives each request its own task. It does, and it is the reason the sweeper can
prune without any endpoint's help - but asserting it against a live server would be testing
uvicorn, and the two properties above are what this module owns.
"""

from __future__ import annotations

import logging
import time

import pytest

import punch_trace
from config import settings


class _Task:
    """The only thing the sweeper asks a request: is it done?"""

    def __init__(self, done: bool = False) -> None:
        self._done = done

    def done(self) -> bool:
        return self._done


@pytest.fixture(autouse=True)
def _empty_registry():
    """No test inherits another's punches, and none leaks into the next."""
    punch_trace.clear()
    yield
    punch_trace.clear()


def test_a_trace_names_the_stage_it_is_in_and_closes_the_one_before_it():
    trace = punch_trace.PunchTrace(worker_id="7", action="Clock In")
    trace.enter(punch_trace.STAGE_UPLOAD)
    time.sleep(0.05)
    trace.enter(punch_trace.STAGE_MODEL)

    assert trace.stage == punch_trace.STAGE_MODEL
    assert trace.spent[punch_trace.STAGE_SESSION] >= 0.0
    assert trace.spent[punch_trace.STAGE_UPLOAD] >= 0.05
    # The open stage is reported as "so far" rather than as a finished number, which is what
    # makes the line honest while the punch is still in it.
    assert "upload=" in trace.stages() and "model=" in trace.stages()
    assert trace.where().startswith("stage=model for ")


def test_an_open_punch_is_reported_every_sweep_with_the_stage_it_is_in(caplog):
    trace = punch_trace.PunchTrace(worker_id="7", action="Clock Out", request_id="abc123")
    trace.enter(punch_trace.STAGE_MODEL)
    punch_trace.register(trace, task=_Task(done=False))

    with caplog.at_level(logging.WARNING, logger=punch_trace.__name__):
        open_now = punch_trace.sweep()

    assert open_now == 1
    line = caplog.text
    assert "punch still open" in line
    assert "stage=model" in line
    assert "request=abc123" in line, "the platform's request id is what joins the two logs"
    assert "worker=7" in line
    # Still registered: the next sweep has to report it again, because a punch that hangs for
    # four minutes is four minutes of samples, not one.
    assert [t.request_id for t in punch_trace.inflight()] == ["abc123"]


def test_a_finished_punch_is_pruned_and_only_a_slow_one_is_reported(caplog, monkeypatch):
    monkeypatch.setattr(settings, "punch_slow_seconds", 5.0)
    quick = punch_trace.PunchTrace(worker_id="1", action="Clock In", request_id="fast")
    slow = punch_trace.PunchTrace(worker_id="2", action="Clock In", request_id="slow")
    slow.started -= 30.0  # a punch that took half a minute, without waiting for one
    punch_trace.register(quick, task=_Task(done=True))
    punch_trace.register(slow, task=_Task(done=True))

    with caplog.at_level(logging.WARNING, logger=punch_trace.__name__):
        open_now = punch_trace.sweep()

    assert open_now == 0
    assert punch_trace.inflight() == [], "finished punches do not accumulate"
    assert caplog.text.count("slow punch:") == 1
    assert "request=slow" in caplog.text
    assert "stages:" in caplog.text, "the slow line has to say where the seconds went"
    assert "request=fast" not in caplog.text, "a punch that was fine is not log noise"


def test_a_trace_nothing_can_follow_is_dropped_rather_than_kept_forever(caplog, monkeypatch):
    monkeypatch.setattr(punch_trace, "ABANDONED_SECONDS", 10.0)
    trace = punch_trace.PunchTrace(worker_id="9", action="Clock In", request_id="ghost")
    trace.started -= 60.0
    punch_trace.register(trace, task=_Task(done=False))

    with caplog.at_level(logging.WARNING, logger=punch_trace.__name__):
        open_now = punch_trace.sweep()

    assert open_now == 0
    assert punch_trace.inflight() == [], "a registry that can only grow is a leak"
    assert "abandoning an untrackable punch" in caplog.text


def test_the_watchdog_is_off_with_a_zero_setting(monkeypatch):
    monkeypatch.setattr(settings, "punch_watchdog_seconds", 0.0)
    assert punch_trace.start_watcher() is None, "0 has to mean off, not 'every second'"
