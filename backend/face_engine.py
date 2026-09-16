"""Face verification runs here, and nowhere else - with a capacity of its own.

WHY THIS EXISTS
---------------
Every punch is a VGG-Face embedding plus an MTCNN detection, and those are the most
expensive things this application does: roughly half a second of CPU each on the deployment
host, with TensorFlow allocating inside a graph the whole process shares. Until this module
existed that work ran wherever the caller happened to be:

* on anyio's shared thread pool (40 threads) from ``/attendance/verify`` and
  ``/q/{token}``, so forty simultaneous punches meant forty simultaneous TensorFlow calls;
* **on the event loop** from the punch's liveness check and from three enrollment paths (the
  console's create-account, the registration link and the self-service capture), so one
  administrator uploading a photo stalled *every* request in that worker for the length of
  an inference - including the gate, where a worker was waiting to clock in.

There was no bound on how much ran at once, no queue, no timeout, no way to see how loaded
the thing was, and no policy for what to answer when it was full.

WHAT IT DOES
------------
One queue, a fixed number of worker threads, and the only functions in the codebase that
call the face models. A caller submits work and gets a result; when the queue is full it is
told (``FaceEngineBusy``) instead of queuing behind nothing in particular.

The capacity is measured, not guessed. Eight verifications on the deployment host (CPU
only, TensorFlow using every core):

    concurrency 1 -> 4.21 s total, 0.53 s per call, 1.90 verifications/s
    concurrency 2 -> 3.13 s total, 0.78 s per call, 2.55 verifications/s
    concurrency 4 -> 3.13 s total, 1.54 s per call, 2.56 verifications/s

The third and fourth concurrent inference add **no throughput at all** and double the time
a worker waits at the gate, so the default is two. What absorbs a site arriving at 04:00 is
the *queue*, not the concurrency: a punch costs one or two slots (its liveness check and its
verification), and beyond the queue's depth a request is refused with 503 + ``Retry-After``
- which the client can retry, and which the offline punch queue exists to cover - rather
than being stacked up invisibly until the process runs out of memory.

TWO RULES WORTH KNOWING
-----------------------
1. **A job must not submit to its own engine.** The workers are the pool: a job that waits
   for another job waits for a worker that is waiting for it. ``submit`` refuses that at
   runtime rather than deadlocking, and job bodies call the ``*_direct`` helpers, which do
   the model call without going through the queue.
2. **This does not isolate the models from the API process.** A crash inside native
   TensorFlow (an out-of-memory kill, a corrupt image reaching a half-installed model) still
   takes the API process with it, and a saturated queue still competes for the same CPU. Real
   isolation means a separate model server - Triton, TorchServe - behind a network call;
   ``FaceEngine`` is the seam that makes that a second implementation rather than a rewrite.
   Until then this bounds the damage, and ``stats()`` makes it visible.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
import traceback
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any, Callable, NamedTuple

import telemetry
from config import settings

log = logging.getLogger(__name__)

#: The detector and model every path in this application uses. Named once so a punch and an
#: enrollment cannot drift onto different models and stop matching each other.
FACE_MODEL = "VGG-Face"
FACE_DETECTOR = "mtcnn"


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------
class FaceEngineError(RuntimeError):
    """Base class for everything this engine refuses to do."""


class FaceEngineBusy(FaceEngineError):
    """Nothing was run: the queue stayed full for the whole wait.

    Not a failure of the request - a fact about the server, which is why every caller turns
    it into 503 (try again) rather than 400 (your photo is wrong) or 500 (we are broken).
    """


class FaceEngineNested(FaceEngineError):
    """A job tried to submit to the same engine it is running on.

    That would wait for a worker that is waiting for the job, so it is refused loudly: a
    deadlock inside an attendance punch is an outage, and it would present as a hang rather
    than as an error anybody could read.
    """


class FaceEngineUnavailable(FaceEngineError):
    """The pool could not be brought up at all (no capacity, a worker that will not start).

    Distinct from ``FaceEngineBusy``: busy means "try again shortly", this means the engine
    itself is not there - a 500 rather than a 503.
    """


class NoFaceDetected(ValueError):
    """The model ran and found no single usable face. Callers map this to a 4xx."""


# ---------------------------------------------------------------------------
# the model calls: the only place DeepFace is called for verification work
# ---------------------------------------------------------------------------
def _represent(image, *, detector_backend: str = FACE_DETECTOR, enforce_detection: bool = True):
    """VGG-Face embeddings for one image. Takes a BGR array or a path.

    Imported inside the function so that importing this module does not pull TensorFlow
    into a process that only wants the admin routes - and so the test harness's DeepFace
    stub is what runs, exactly as it is for the rest of the suite.

    Timed here rather than at the queue boundary because this is the number the capacity
    decision was made from: the pool's job time includes the wait and the result handling,
    and the promise being watched is "one embedding plus one MTCNN detection costs about
    half a second". A failure is timed too - a model that raises instantly is not fast, it is
    broken, and it would otherwise look like excellent latency on a graph.
    """
    from deepface import DeepFace

    started = time.perf_counter()
    error: BaseException | None = None
    try:
        return DeepFace.represent(
            img_path=image,
            model_name=FACE_MODEL,
            detector_backend=detector_backend,
            enforce_detection=enforce_detection,
        )
    except BaseException as exc:  # noqa: BLE001 - recorded, then handed to the caller unchanged
        error = exc
        raise
    finally:
        telemetry.observe_model_call(
            operation="represent",
            seconds=time.perf_counter() - started,
            model=FACE_MODEL,
            detector=detector_backend,
            error=error,
        )


def _detect(image, *, detector_backend: str = FACE_DETECTOR, enforce_detection: bool = False):
    """Faces in one image, without embedding them (the cheap half of the API).

    Separate operation label from ``represent`` on purpose: the detector is the part that
    scales with the number of faces in the frame, and an operator tuning the gate needs to
    see the two costs apart before deciding whether to change the detector.
    """
    from deepface import DeepFace

    started = time.perf_counter()
    error: BaseException | None = None
    try:
        return DeepFace.extract_faces(
            img_path=image, detector_backend=detector_backend, enforce_detection=enforce_detection
        )
    except BaseException as exc:  # noqa: BLE001
        error = exc
        raise
    finally:
        telemetry.observe_model_call(
            operation="detect",
            seconds=time.perf_counter() - started,
            model="mtcnn",
            detector=detector_backend,
            error=error,
        )


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------
@dataclass
class Stats:
    """What the engine has done, for readiness and for an operator under load."""

    capacity: int
    queue_depth: int
    queued: int = 0
    in_flight: int = 0
    submitted: int = 0
    completed: int = 0
    failed: int = 0
    refused: int = 0
    last_duration_ms: float | None = None
    slowest_ms: float | None = None
    last_error: str | None = field(default=None)

    @property
    def busy(self) -> bool:
        """True when there is no room left at all: the next caller is refused."""
        return self.queued >= max(1, self.queue_depth)

    def as_dict(self) -> dict[str, Any]:
        return {
            "capacity": self.capacity,
            "queue_depth": self.queue_depth,
            "queued": self.queued,
            "in_flight": self.in_flight,
            "submitted": self.submitted,
            "completed": self.completed,
            "failed": self.failed,
            "refused": self.refused,
            "last_duration_ms": self.last_duration_ms,
            "slowest_ms": self.slowest_ms,
            "busy": self.busy,
            "last_error": self.last_error,
        }


class _Job(NamedTuple):
    fn: Callable
    args: tuple
    kwargs: dict
    future: Future
    #: When ``submit`` accepted it. The difference between this and the worker picking it up
    #: is the wait a caller spent queueing, which is the number that moves first when a site
    #: arrives at 04:00 - before the per-call latency does.
    enqueued_at: float


def _job_name(fn: Callable) -> str:
    """A bounded label for a queued callable.

    The set of things submitted to this pool is fixed (the comparison, the two model calls,
    liveness, the enrollment embeddings), so a function's own name is a low-cardinality label
    and is exactly what an operator wants to read. Anything without a name - a lambda, a
    partial - reports as ``anonymous`` rather than inventing a series per call site.
    """
    name = getattr(fn, "__name__", "")
    if not name or name.startswith("<"):
        return "anonymous"
    return name.lstrip("_")[:48] or "anonymous"


#: Set while a thread is running a job, per engine, so ``submit`` can refuse nesting.
_local = threading.local()


# ---------------------------------------------------------------------------
# the engine
# ---------------------------------------------------------------------------
class FaceEngine:
    """A bounded pool for face-model work.

    Threads start on first use rather than at import: importing the application (a CLI
    tool, the test suite, a readiness probe) must not spawn inference workers.
    """

    def __init__(
        self,
        *,
        capacity: int | None = None,
        queue_depth: int | None = None,
        wait_seconds: float | None = None,
        name: str = "face-engine",
    ) -> None:
        self.capacity = max(1, int(capacity if capacity is not None else settings.face_inference_concurrency))
        self.queue_depth = max(0, int(queue_depth if queue_depth is not None else settings.face_inference_queue))
        self.wait_seconds = float(
            wait_seconds if wait_seconds is not None else settings.face_inference_wait_seconds
        )
        self.name = name
        self.stats = Stats(capacity=self.capacity, queue_depth=self.queue_depth)
        # ``queue_depth`` bounds the *waiting* jobs, and that is exactly what the queue
        # holds: a worker takes its job out of the queue before running it, so the jobs
        # being verified are the threads, not the queue. (Which also means a depth of 0
        # cannot mean "no queue": a submission must be able to enqueue work for an idle
        # worker, so the smallest real depth is 1.)
        self._slots: queue.Queue[_Job | None] = queue.Queue(maxsize=max(1, self.queue_depth))
        self._threads: list[threading.Thread] = []
        self._start_lock = threading.Lock()
        self._counters_lock = threading.Lock()
        self._closed = False

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> "FaceEngine":
        """Start the workers, once. Safe to call from several threads.

        After a ``shutdown`` this starts them again rather than staying dead, because the
        engine is a module-level object and a process can bring the application up more than
        once: the suite does it twice (a second ``TestClient`` for the session-rotation
        test), and an embedded app can do it too. A shutdown flag that could never be
        cleared would mean the *second* startup silently ran every punch without a pool -
        a failure that looks like a working app right up until it is saturated.
        """
        with self._start_lock:
            if self._threads:
                return self
            if self.capacity <= 0:  # pragma: no cover - __init__ clamps to >= 1
                raise FaceEngineUnavailable("the face engine has no capacity")
            self._closed = False
            for index in range(self.capacity):
                thread = threading.Thread(
                    target=self._work, name=f"{self.name}-{index}", daemon=True
                )
                thread.start()
                self._threads.append(thread)
            log.info(
                "face engine started: %s worker(s), %s queued, %.1fs wait",
                self.capacity,
                self.queue_depth,
                self.wait_seconds,
            )
        return self

    def shutdown(self, timeout: float = 5.0) -> None:
        """Stop the workers once they have finished what is already queued.

        A sentinel per worker goes behind the waiting jobs, so a job that was accepted is
        still run. Idempotent, safe when no worker ever started, and *not* permanent: the
        next submission starts the pool again (see ``start``).
        """
        with self._start_lock:
            if self._closed:
                return
            self._closed = True
            for _ in self._threads:
                try:
                    self._slots.put_nowait(None)
                except queue.Full:  # pragma: no cover - only when the queue is stuffed
                    break
            threads, self._threads = self._threads, []
        for thread in threads:
            thread.join(timeout=timeout)

    def _work(self) -> None:
        while True:
            job = self._slots.get()
            if job is None:
                self._slots.task_done()
                return
            _local.engine = self
            started = time.perf_counter()
            job_name = _job_name(job.fn)
            telemetry.observe_queue_wait(job=job_name, seconds=started - job.enqueued_at)
            try:
                # ``queued`` is read from the queue itself (a worker takes its job *out*
                # before running it, so the queue holds exactly the waiting jobs);
                # ``in_flight`` has to be counted, because nothing else knows it.
                with self._counters_lock:
                    self.stats.in_flight += 1
                try:
                    result = job.fn(*job.args, **job.kwargs)
                except BaseException as exc:  # noqa: BLE001 - handed to the caller, never lost
                    self._record(job, started, error=exc)
                    telemetry.observe_engine_job(
                        job=job_name, outcome="error", seconds=time.perf_counter() - started
                    )
                    job.future.set_exception(exc)
                else:
                    self._record(job, started)
                    telemetry.observe_engine_job(
                        job=job_name, outcome="ok", seconds=time.perf_counter() - started
                    )
                    job.future.set_result(result)
            finally:
                _local.engine = None
                with self._counters_lock:
                    self.stats.in_flight = max(0, self.stats.in_flight - 1)
                self._slots.task_done()

    def _record(self, job: _Job, started: float, error: BaseException | None = None) -> None:
        duration_ms = (time.perf_counter() - started) * 1000.0
        with self._counters_lock:
            if error is None:
                self.stats.completed += 1
            else:
                self.stats.failed += 1
                self.stats.last_error = f"{type(error).__name__}: {error}"
            self.stats.last_duration_ms = round(duration_ms, 1)
            slowest = self.stats.slowest_ms or 0.0
            self.stats.slowest_ms = round(max(slowest, duration_ms), 1)

    # -- submitting work ---------------------------------------------------
    def submit(self, fn: Callable, *args, **kwargs) -> Future:
        """Queue ``fn(*args, **kwargs)`` and return its future.

        Raises ``FaceEngineBusy`` when the queue is full for ``wait_seconds``, and
        ``FaceEngineNested`` when called from a job of this engine (see the class docs).
        """
        if getattr(_local, "engine", None) is self:
            telemetry.count_engine_refusal("nested")
            raise FaceEngineNested(
                f"{getattr(fn, '__name__', fn)!r} was submitted from a {self.name} worker: a job "
                "cannot wait for its own pool. Call the model directly (``represent_direct`` / "
                "``detect_direct``) inside a job body."
            )
        self.start()
        job = _Job(fn, args, kwargs, Future(), time.perf_counter())
        try:
            self._slots.put(job, timeout=self.wait_seconds)
        except queue.Full:
            with self._counters_lock:
                self.stats.refused += 1
            telemetry.count_engine_refusal("busy")
            raise FaceEngineBusy(
                f"the {self.name} queue is full ({self.stats.queued} waiting, {self.capacity} running)"
            ) from None
        with self._counters_lock:
            self.stats.submitted += 1
            self.stats.queued = self._slots.qsize()
        return job.future

    def run(self, fn: Callable, *args, **kwargs):
        """Run on the pool and wait. For callers that are already on their own thread."""
        return self.submit(fn, *args, **kwargs).result()

    async def run_async(self, fn: Callable, *args, **kwargs):
        """Run on the pool and await it, without occupying an event-loop thread.

        The admission wait happens on a helper thread: a caller waiting for room must not
        block the loop it is about to answer a request on.
        """
        future = await asyncio.to_thread(self.submit, fn, *args, **kwargs)
        return await asyncio.wrap_future(future)

    # -- the model operations ---------------------------------------------
    def represent(self, image, **kwargs):
        """VGG-Face embeddings, on the pool."""
        return self.run(_represent, image, **kwargs)

    async def represent_async(self, image, **kwargs):
        """VGG-Face embeddings, on the pool, awaited."""
        return await self.run_async(_represent, image, **kwargs)

    def detect(self, image, **kwargs):
        """Faces in an image, on the pool."""
        return self.run(_detect, image, **kwargs)

    async def detect_async(self, image, **kwargs):
        """Faces in an image, on the pool, awaited."""
        return await self.run_async(_detect, image, **kwargs)

    # For the body of a job that already holds a slot. See rule 1 in the module docstring.
    # Written as calls through the module globals rather than aliases of the functions, so
    # that patching ``face_engine._represent`` (tests do) moves every path at once - an
    # alias would quietly keep calling the original and a stub would look like a pass.
    @staticmethod
    def represent_direct(image, **kwargs):
        """The VGG-Face call, without going through the queue.

        For the body of a job that already holds a slot: submitting again would wait for
        the worker running it. See rule 1 in the module docstring.
        """
        return _represent(image, **kwargs)

    @staticmethod
    def detect_direct(image, **kwargs):
        """The detector call, without going through the queue."""
        return _detect(image, **kwargs)

    # -- reporting ---------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        """A consistent-enough view of the counters, for readiness."""
        with self._counters_lock:
            self.stats.queued = self._slots.qsize()
            return {**self.stats.as_dict(), "name": self.name, "wait_seconds": self.wait_seconds}


#: The application's engine: one queue for the whole process, which is the point.
ENGINE = FaceEngine()


def stats() -> dict[str, Any]:
    """The module-level snapshot, for readiness and for operators."""
    return ENGINE.snapshot()


def busy_http_exception(exc: FaceEngineError | None = None):
    """The 503 every saturated endpoint answers with.

    Defined here rather than at each endpoint so the worker reads the same sentence wherever
    the queue is full, and so ``Retry-After`` is not forgotten at one of them. The message
    says what to do - the photo is fine, the server is busy - because a worker at the gate
    who is told "we could not confirm that this is you" for a busy server would go and get
    their photo re-enrolled.
    """
    from fastapi import HTTPException

    return HTTPException(
        status_code=503,
        detail={
            "error_code": "face_check_busy",
            "message": (
                "The server is verifying a lot of faces right now. Try again in a few "
                "seconds - your photo and your account are fine."
            ),
            "retry_after_seconds": 5,
            **({"queue": str(exc)} if exc is not None else {}),
        },
        headers={"Retry-After": "5"},
    )


def describe_failure(exc: BaseException) -> str:
    """A short, safe description of a model failure for a log line (never for a worker)."""
    return f"{type(exc).__name__}: {exc}".strip() or type(exc).__name__


def log_failure(where: str, exc: BaseException) -> None:
    """Log a model failure with its traceback, once, where it happened."""
    log.error("%s failed: %s\n%s", where, describe_failure(exc), "".join(traceback.format_exception(exc)))
