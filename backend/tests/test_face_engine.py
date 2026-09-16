"""The capacity face verification runs under.

WHY THIS EXISTS
---------------
A punch is a VGG-Face embedding plus an MTCNN detection - half a second of CPU each on the
deployment host - and before this engine existed that work ran wherever the caller happened
to be: on anyio's shared forty-thread pool for the punches, and **on the event loop** for the
liveness check and three of the four enrollment paths. Nothing bounded how much of it ran at
once, nothing stopped one upload from freezing every other request in the process, and when
the machine was overwhelmed the worker at the gate was told their *photo* was the problem.

These tests pin what replaced it:

1. **The ceiling is the ceiling.** No more than ``capacity`` model calls run at once, however
   many callers there are, and no more model threads exist than capacity says.
2. **The queue is bounded, and being full is answered, not ignored.** A caller that cannot be
   served is refused (503 + ``Retry-After``) instead of being stacked up invisibly; and a
   refusal never breaks the pool for whoever comes next.
3. **A failing job does not poison the pool** - a model that raises, or a frame it cannot
   use, leaves the workers healthy and the counters honest.
4. **A job cannot submit to its own engine.** That is a deadlock, and inside a punch it would
   present as a hang rather than as an error, so it is refused loudly.
5. **The event loop is not the model's thread.** While an inference runs, another request is
   answered.
6. **Nothing bypasses the engine.** A future endpoint must not reach for the model directly,
   because that is how the last three unbounded call sites got there.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

import face_engine
import harness
import readiness
from harness import MOALLEM, bearer, jpeg_bytes

#: Small, deliberate capacities: these tests are about policy, and a small pool makes the
#: policy observable. The shipped defaults are in ``config``.
FAST = {"wait_seconds": 0.05}


def make_engine(**overrides) -> face_engine.FaceEngine:
    settings = {"capacity": 2, "queue_depth": 4, **FAST, **overrides}
    name = settings.pop("name", None) or f"test-engine-{id(settings)}"
    return face_engine.FaceEngine(name=name, **settings)


def release_after(engine: face_engine.FaceEngine, blocking: threading.Event, **overrides):
    """Occupy every worker with a job that waits for ``blocking``."""
    holders = []
    for _ in range(engine.capacity):
        holders.append(engine.submit(blocking.wait, 5))
    return holders


# ---------------------------------------------------------------------------
# 1. the ceiling
# ---------------------------------------------------------------------------
def test_no_more_than_capacity_model_calls_run_at_once():
    engine = make_engine(capacity=2, queue_depth=16)
    lock = threading.Lock()
    state = {"active": 0, "peak": 0}
    threads: set[str] = set()

    def job() -> str:
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
            threads.add(threading.current_thread().name)
        time.sleep(0.05)
        with lock:
            state["active"] -= 1
        return "done"

    with ThreadPoolExecutor(max_workers=8) as callers:
        results = list(callers.map(engine.run, [job] * 12))

    assert results == ["done"] * 12
    assert state["peak"] == 2, f"capacity was exceeded: {state['peak']} calls at once"
    assert len(threads) == 2, f"the pool grew beyond its capacity: {sorted(threads)}"


def test_workers_start_on_first_use_not_at_import():
    """Importing the app (a CLI tool, a probe, the suite) must not spawn inference threads."""
    engine = make_engine(name="test-lazy-engine")
    assert not [t for t in threading.enumerate() if t.name.startswith(engine.name)]

    engine.run(lambda: None)
    assert len([t for t in threading.enumerate() if t.name.startswith(engine.name)]) == engine.capacity


# ---------------------------------------------------------------------------
# 2. the bounded queue, and what a caller is told
# ---------------------------------------------------------------------------
def test_a_full_queue_refuses_instead_of_stacking_and_then_recovers():
    engine = make_engine(capacity=1, queue_depth=1, wait_seconds=0.05)
    blocking = threading.Event()
    holders = release_after(engine, blocking)  # the single worker is now busy
    engine.submit(blocking.wait, 5)  # and the one waiting slot is full

    with pytest.raises(face_engine.FaceEngineBusy):
        engine.submit(blocking.wait, 5)

    assert engine.snapshot()["refused"] == 1
    assert engine.snapshot()["busy"] is True

    # A refusal is about the queue, not about the engine: the next caller is served once
    # there is room, and the refused work was never started.
    blocking.set()
    for holder in holders:
        holder.result(timeout=5)
    assert engine.run(lambda: "served") == "served"


def test_a_saturated_pool_answers_the_punch_with_503_and_retry_after(client, monkeypatch):
    """The contract at the gate: your photo is fine, the server is busy, try again."""
    engine = make_engine(capacity=1, queue_depth=1, wait_seconds=0.05)
    blocking = threading.Event()
    holders = release_after(engine, blocking)
    engine.submit(blocking.wait, 5)
    monkeypatch.setattr(face_engine, "ENGINE", engine)
    try:
        response = harness.clock_in(client, MOALLEM, headers=bearer(MOALLEM), image=jpeg_bytes())
        assert response.status_code == 503, response.text[:300]
        assert response.headers.get("Retry-After") == "5"
        detail = response.json()["detail"]
        assert detail["error_code"] == "face_check_busy"
        assert "try again" in detail["message"].lower()
        assert "red" not in detail["message"].lower(), "a busy server must not blame the photo"
    finally:
        blocking.set()
        for holder in holders:
            holder.result(timeout=5)


def test_a_refusal_does_not_leave_a_half_recorded_punch(client, monkeypatch):
    """Nothing is written for a punch that never ran: no session, no log, no notification."""
    engine = make_engine(capacity=1, queue_depth=1, wait_seconds=0.05)
    blocking = threading.Event()
    holders = release_after(engine, blocking)
    engine.submit(blocking.wait, 5)
    monkeypatch.setattr(face_engine, "ENGINE", engine)
    try:
        before = harness.db_scalar("SELECT COUNT(*) FROM attendance_logs")
        response = harness.clock_in(client, MOALLEM, headers=bearer(MOALLEM), image=jpeg_bytes())
        assert response.status_code == 503
        assert harness.db_scalar("SELECT COUNT(*) FROM attendance_logs") == before
        assert harness.db_scalar("SELECT COUNT(*) FROM active_sessions WHERE worker_id = ?", (MOALLEM,)) == 0
    finally:
        blocking.set()
        for holder in holders:
            holder.result(timeout=5)


# ---------------------------------------------------------------------------
# 3. failures
# ---------------------------------------------------------------------------
def test_a_failing_job_does_not_poison_the_pool():
    engine = make_engine()

    def boom() -> None:
        raise ValueError("no face detected")

    with pytest.raises(ValueError, match="no face detected"):
        engine.run(boom)
    assert engine.snapshot()["failed"] == 1
    assert "ValueError" in engine.snapshot()["last_error"]

    assert engine.run(lambda: "still working") == "still working"
    assert engine.snapshot()["completed"] == 1


def test_a_model_that_raises_is_reported_to_the_caller_unchanged(monkeypatch):
    """The engine must not swallow or rewrap what the model said: callers map specific
    failures to specific answers (no face -> 400, unreadable -> 500)."""
    engine = make_engine()

    def exploding(image, **kwargs):
        raise RuntimeError("graph is broken")

    monkeypatch.setattr(face_engine, "_represent", exploding)
    with pytest.raises(RuntimeError, match="graph is broken"):
        engine.represent("some-image")


# ---------------------------------------------------------------------------
# 4. nesting
# ---------------------------------------------------------------------------
def test_a_job_cannot_submit_to_its_own_engine():
    """A job waiting for its own pool waits for the worker that is running it."""
    engine = make_engine(capacity=1, queue_depth=4)

    def nested() -> None:
        engine.submit(lambda: None)

    with pytest.raises(face_engine.FaceEngineNested):
        engine.run(nested)

    assert engine.run(lambda: "healthy") == "healthy"


# ---------------------------------------------------------------------------
# 5. the event loop
# ---------------------------------------------------------------------------
def test_awaiting_the_pool_does_not_block_the_loop():
    """The property three enrollment paths were missing: the loop stays free."""
    engine = make_engine(capacity=1, queue_depth=4, wait_seconds=1)

    async def scenario() -> float:
        slow = asyncio.create_task(engine.run_async(time.sleep, 0.5))
        started = time.perf_counter()
        await asyncio.sleep(0.15)  # a second coroutine, on the same loop, meanwhile
        elapsed = time.perf_counter() - started
        assert not slow.done(), (
            "the loop was blocked by the model call: the job finished while a second "
            "coroutine was supposed to be running beside it"
        )
        await slow
        return elapsed

    elapsed = asyncio.run(scenario())
    assert elapsed < 0.4, f"the loop itself was delayed by {elapsed:.2f}s"


def test_a_slow_inference_does_not_stall_another_request(client, monkeypatch):
    """At the gate: one punch being verified must not stop the next request being answered."""
    release = threading.Event()

    def slow_represent(image, **kwargs):
        release.wait(timeout=5)
        return [{"embedding": [0.01] * 8, "face_confidence": 0.99}]

    monkeypatch.setattr(face_engine, "_represent", slow_represent)
    outcome: dict = {}

    def punch() -> None:
        outcome["response"] = harness.clock_in(client, MOALLEM, headers=bearer(MOALLEM), image=jpeg_bytes())

    worker = threading.Thread(target=punch, daemon=True)
    worker.start()
    time.sleep(0.3)  # let the punch reach the (now blocked) model call
    try:
        started = time.perf_counter()
        status = client.get("/api/v1/status")
        elapsed = time.perf_counter() - started
        assert status.status_code == 200
        assert elapsed < 0.5, f"an unrelated request waited {elapsed:.2f}s behind an inference"
    finally:
        release.set()
        worker.join(timeout=5)


# ---------------------------------------------------------------------------
# 6. nothing bypasses the engine
# ---------------------------------------------------------------------------
def _deepface_references(path) -> list[str]:
    """Real references to the model library in a module - parsed, not grepped.

    Parsed because prose counts otherwise: ``liveness.py`` explains in a docstring that it
    runs before ``DeepFace.represent()``, and a text search would report the explanation as
    a bypass.
    """
    import ast

    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("deepface"):
            found.append(f"import at line {node.lineno}")
        elif isinstance(node, ast.Import) and any(
            alias.name == "deepface" for alias in node.names
        ):
            found.append(f"import at line {node.lineno}")
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "DeepFace"
        ):
            found.append(f"{node.func.value.id}.{node.func.attr} at line {node.lineno}")
    return found


def test_only_the_engine_calls_the_face_models():
    """A new endpoint must not reach for the model directly.

    A source check rather than a runtime one, because the failure it prevents is invisible at
    runtime: a direct call works perfectly and simply does not participate in the capacity
    policy - which is exactly how the last three call sites came to run unbounded, one of
    them on the event loop.
    """
    offenders = {
        path.name: _deepface_references(path)
        for path in sorted(harness.BACKEND_DIR.glob("*.py"))
        if path.name != "face_engine.py" and _deepface_references(path)
    }
    # ``main`` is allowed exactly one reference: the startup preload, which runs before the
    # server accepts a request (nothing to bound, nothing else on the machine yet).
    assert set(offenders) == {"main.py"}, offenders
    calls = [item for item in offenders["main.py"] if not item.startswith("import")]
    assert len(calls) == 1 and "build_model" in calls[0], offenders["main.py"]


# ---------------------------------------------------------------------------
# 7. what the operator can see
# ---------------------------------------------------------------------------
def test_stats_and_readiness_report_the_load():
    engine = make_engine(capacity=2, queue_depth=8)
    engine.run(lambda: None)
    engine.run(lambda: None)

    snapshot = engine.snapshot()
    assert snapshot["capacity"] == 2
    assert snapshot["queue_depth"] == 8
    assert snapshot["completed"] == 2
    assert snapshot["busy"] is False
    assert isinstance(snapshot["last_duration_ms"], float)

    check = readiness._check_face_engine({})
    assert check.name == "face_engine"
    assert check.tier == readiness.TIER_ADVISORY
    assert check.ok is True
    assert check.value["capacity"] >= 1


def test_readiness_reports_a_saturated_engine_without_failing_the_gate():
    """Being busy is a load condition, not a broken deployment: report it, never block."""
    engine = make_engine(capacity=1, queue_depth=1, wait_seconds=0.05)
    blocking = threading.Event()
    holders = release_after(engine, blocking)
    engine.submit(blocking.wait, 5)
    import face_engine as module

    original = module.ENGINE
    module.ENGINE = engine
    try:
        check = readiness._check_face_engine({})
        assert check.ok is False, "a full queue is worth showing an operator"
        assert check.tier == readiness.TIER_ADVISORY, "and not worth refusing to start for"
        assert "waiting" in check.detail
    finally:
        module.ENGINE = original
        blocking.set()
        for holder in holders:
            holder.result(timeout=5)


def test_shutdown_finishes_accepted_work_and_the_pool_starts_again():
    """A queued job is honoured across a graceful shutdown, and a later call is still served.

    The second half is the point of the first: this engine is a module-level object, and a
    process can bring the application up more than once (the suite does it for the session
    rotation test). A shutdown that could never be cleared would leave the *second* startup
    verifying every punch with no pool at all - a failure that looks like a healthy app
    until the moment it is saturated.
    """
    engine = make_engine(capacity=1, queue_depth=4, wait_seconds=1)
    results = [engine.submit(lambda value: value, n) for n in range(3)]
    engine.shutdown(timeout=5)

    assert [future.result(timeout=5) for future in results] == [0, 1, 2]
    assert not [t for t in threading.enumerate() if t.name.startswith(engine.name)]
    engine.shutdown()  # idempotent

    assert engine.run(lambda: "after a restart") == "after a restart"
    assert len([t for t in threading.enumerate() if t.name.startswith(engine.name)]) == 1


def test_shutdown_never_started_is_harmless():
    """A CLI tool or the suite may shut down an engine that never did any work."""
    make_engine(name="test-never-started").shutdown()


def test_the_shipped_defaults_are_the_measured_ones():
    """Two verifications at once, from the measurement in ``face_engine``'s docstring.

    Pinned because the number is a decision, not a detail: at four the host gains no
    throughput and doubles the latency a worker waits at the gate.
    """
    from config import settings

    assert settings.face_inference_concurrency == 2
    assert settings.face_inference_queue >= 32, "the queue is what absorbs the 04:00 arrival"
    assert settings.face_inference_wait_seconds >= 5
    assert os.environ.get("FACE_INFERENCE_CONCURRENCY") is None, "no test may leak this setting"
