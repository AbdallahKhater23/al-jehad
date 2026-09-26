"""The face models, in a process of their own.

WHY THIS EXISTS
---------------
``face_engine`` ends its own docstring with the honest limit of what it is:

    This does not isolate the models from the API process. A crash inside native ONNX
    Runtime (an out-of-memory kill, a corrupt image reaching a half-installed model)
    still takes the API process with it [...] ``FaceEngine`` is the seam that makes that
    a second implementation rather than a rewrite.

This is that second implementation, and it is a **prototype** - off unless
``FACE_ENGINE_PROCESS`` is set (see the flag's own comment in ``config.py``).

The measured problem is two different things that both live in the API process today:

* **A floor.** Opening the 87 MiB FaceNet graph and then running YuNet once costs the API
  process a one-time, never-returned ~150 MiB, and the detector's arena is sized by the
  *largest frame ever seen* (~86 MiB at the 1280 px ceiling, measured in
  ``docs/FACE_VERIFICATION_MEMORY_REPORT.md`` section 2.5). None of that is per-punch, and
  none of it can be given back. On a 512 MiB instance with swap off it is simply gone.
* **A shared fate.** An ONNX Runtime allocation failure, a corrupt model, or the kernel's
  own OOM killer ends the *API* process - so the thing that dies takes the gate, the
  admin console and every non-face endpoint with it, over a punch that could have been
  retried.

Moving the models out fixes the second exactly and the first partially: the API process
stops importing the graph, stops building a session, and stops running a detector, so its
resident set is the web stack plus whatever frame is in flight. It does not make the
*host's* total smaller - there are now two interpreters and the model process's ~200 MiB is
real. That trade is the whole point, and it is why the flag defaults to off: this buys
survivability and a smaller *critical* process, not a smaller machine.

WHAT CROSSES, AND WHAT DOES NOT
-------------------------------
Only the model calls cross. ``face_engine._represent`` and ``face_engine._detect`` forward
when the flag is on, and so does ``liveness.check_liveness`` - which is why ``liveness.inspect``'s
mode policy, the one-subject rule, the cosine, the thresholds and every refusal sentence still
run in the API process, where they always did. The queue and the capacity policy never moved
either: it is the *parent* pool that admits work, and a child only ever runs one call it was
given. That is deliberate: an RPC
boundary that carried a *job* would drag the application into the model process, and the
model process would then need the database, the settings and the whole app to answer a
question about a photograph.

So the wire protocol is a small, closed set of ops (see ``face_worker.OPS``) and this module
is only the transport: spawn, frame, dispatch, time out, notice a death, restart.

HOW A DEATH IS HANDLED
----------------------
* A child that exits - cleanly, by signal, or by the kernel's OOM killer - ends the read
  loop (EOF on its stdout). Every request still in flight fails with
  ``FaceProcessUnavailable``, the client marks itself dead, and the **next** call spawns a
  fresh child. One punch pays for a crash; the next one does not wait for an operator.
* A child that stops answering is *not* trusted to finish: the call that hit its deadline
  kills the child and raises ``FaceProcessTimeout``. Killing rather than ignoring, because
  replies are matched to requests by id in order, so a child that is one answer behind
  would silently apply the wrong reply to the wrong photograph.
* Neither of those may reach a caller as a bare ``FaceProcessError``: ``face_engine`` maps
  both to ``FaceEngineUnavailable``, which every endpoint already answers as a server
  error rather than as "your photo is wrong".

WHAT IS *NOT* SOLVED HERE
-------------------------
* **The child still holds ~2x the model floor** if it is restarted repeatedly - no, it does
  not; a fresh interpreter per restart is the point of ``subprocess``. What is unsolved is
  that the *unbound* part is unchanged: an embedding of a hostile image can still exhaust
  the child's memory, and the child has no memory limit of its own (a cgroup would be the
  way to bound it, which is what ``tools/capacity_test.py`` measures).
* **Capacity is now one model process, one inference at a time.** ``face_engine``'s default
  concurrency of two submits two jobs, and both now queue behind a single child. On 1 vCPU
  the measured difference between one and two concurrent inferences was small (2.55 vs
  1.90 verifications/s, see ``face_engine``) but it is not zero, and the honest fix - N
  children, or a real model server - is not in this prototype.
* **Telemetry is recorded by the caller, not the child.** The child's own Prometheus
  counters are in another process and are never scraped; the parent records the model call
  from the timings the reply carries. Job-level metrics are unchanged because the queue
  never moved.

SECURITY
--------
The channel carries ``pickle``, so it must never be reachable by anything but this parent's
own child: the parent reads exactly one stdout, from a process it spawned itself. A reply
is a *value*, never a callable, and an error from the child is re-raised as a string
description rather than unpickled as an exception object.
"""

from __future__ import annotations

import atexit
import logging
import os
import pickle
import signal
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config import settings

log = logging.getLogger(__name__)

BACKEND_DIR = Path(__file__).resolve().parent

#: The child. A file rather than ``-m`` so its own directory lands on ``sys.path`` without
#: anything having to arrange it - the Ubuntu image, a Windows checkout and the test suite
#: all spawn it the same way.
WORKER = BACKEND_DIR / "face_worker.py"

#: A 4-byte big-endian length, then the pickled message. Length-prefixed rather than a
#: delimiter because the payload is binary and contains any byte value at all. Public
#: because ``face_worker`` imports it: two copies of a wire format are two wire formats.
FRAME_HEADER = struct.Struct("!I")

#: The environment the child is given, on top of this process's own. ``FACE_ENGINE_PROCESS``
#: is forced back to 0: the child is the process that runs the models, and if it inherited
#: the flag it would forward the call straight back to a process of its own - the recursion
#: this module exists to avoid, and one that would present as a hang.
_CHILD_ENV = {
    "FACE_ENGINE_PROCESS": "0",
    "PYTHONUNBUFFERED": "1",
}

#: Ops the child answers that need no model at all. Named here so a caller can ask a healthy
#: question ("are you there?") without loading anything.
PING = "ping"
WARM = "warm"
STATUS = "status"
SHUTDOWN = "shutdown"


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------
class FaceProcessError(RuntimeError):
    """Base class for everything this transport refuses to do."""


class FaceProcessUnavailable(FaceProcessError):
    """The child could not be started, or it died while a request was in flight."""


class FaceProcessTimeout(FaceProcessError):
    """The child did not answer within the deadline, so it was killed."""


class FaceProcessRemoteError(FaceProcessError):
    """The child answered, and the answer was a failure.

    The child's exception is *described* rather than transported: ``type`` and ``message``
    come back as strings, and the traceback is logged where it happened.
    """

    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(f"{error_type}: {message}")
        self.error_type = error_type
        self.message = message


def reply_error(reply: dict) -> FaceProcessRemoteError:
    """The exception a failed reply stands for."""
    error = reply.get("error") or {}
    return FaceProcessRemoteError(
        str(error.get("type", "Error")), str(error.get("message", ""))
    )


# ---------------------------------------------------------------------------
# the transport
# ---------------------------------------------------------------------------
@dataclass
class _Pending:
    """One request waiting for its answer."""

    event: threading.Event = field(default_factory=threading.Event)
    reply: dict | None = None


def _read_exactly(stream, count: int) -> bytes | None:
    """``count`` bytes, or ``None`` if the pipe ended first.

    A pipe read returns what is available, so a 4-byte header routinely arrives in pieces.
    """
    chunks: list[bytes] = []
    remaining = count
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


class FaceProcess:
    """A child process that owns the models, and the pipe to it.

    Thread-safe: the queue above it has several workers, and any of them may call in.
    """

    def __init__(
        self,
        *,
        python: str | None = None,
        handler: Path | str | None = None,
        deadline_seconds: float | None = None,
        start_seconds: float | None = None,
        name: str = "face-worker",
        extra_env: dict[str, str] | None = None,
    ) -> None:
        self.python = python or settings.face_engine_process_python or sys.executable
        self.handler = Path(handler or WORKER)
        self.deadline_seconds = float(
            deadline_seconds
            if deadline_seconds is not None
            else settings.face_engine_process_timeout_seconds
        )
        self.start_seconds = float(
            start_seconds
            if start_seconds is not None
            else settings.face_engine_process_start_seconds
        )
        self.name = name
        self.extra_env = dict(extra_env or {})

        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._pending: dict[int, _Pending] = {}
        self._ids = iter(range(1, 1 << 60))
        self._proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._generation = 0
        self._closing = False
        self._cached_status: tuple[float, dict] | None = None

        #: The interpreter that actually runs the models, learned from its own answers
        #: (``ping``/``status`` report ``os.getpid()``). It is *not* always ``proc.pid``: on
        #: Windows a virtualenv's ``python.exe`` is a launcher stub that re-execs the base
        #: interpreter as a child - which is why ``punch_saturation`` has the same note - so
        #: killing the pid we spawned can leave the 200 MiB process running. On the Linux
        #: deployment the two are the same process and this is simply a no-op.
        self.worker_pid: int | None = None
        self.started_at: float | None = None
        self.starts = 0
        self.calls = 0
        self.failures = 0
        self.timeouts = 0
        self.last_error: str | None = None
        self.last_call_ms: float | None = None
        self.stopped = False

    # -- lifecycle ---------------------------------------------------------
    def child_env(self) -> dict[str, str]:
        """The environment a child is spawned with (exposed so a test can pin it)."""
        env = dict(os.environ)
        env.update(self.extra_env)
        env.update(_CHILD_ENV)
        return env

    def start(self) -> "FaceProcess":
        """Spawn the child if it is not running. Idempotent and thread-safe."""
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return self
            self._spawn_locked()
        return self

    def _spawn_locked(self) -> None:
        self._closing = False
        self.stopped = False
        try:
            proc = subprocess.Popen(  # noqa: S603 - a fixed interpreter and a fixed script
                [self.python, str(self.handler)],
                cwd=str(BACKEND_DIR),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self.child_env(),
                close_fds=True,
            )
        except OSError as exc:
            self.last_error = f"could not start the model process: {exc}"
            raise FaceProcessUnavailable(self.last_error) from exc
        self._proc = proc
        self.worker_pid = None
        self._generation += 1
        self.starts += 1
        self.started_at = time.monotonic()
        generation = self._generation
        self._reader = threading.Thread(
            target=self._read_loop, args=(proc, generation), name=f"{self.name}-reader", daemon=True
        )
        self._reader.start()
        self._stderr_reader = threading.Thread(
            target=self._drain_stderr,
            args=(proc, generation),
            name=f"{self.name}-stderr",
            daemon=True,
        )
        self._stderr_reader.start()
        log.info("face model process started: %s (pid %s)", self.handler.name, proc.pid)

    @property
    def alive(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    @property
    def pid(self) -> int | None:
        proc = self._proc
        return proc.pid if proc is not None else None

    def stop(self, timeout: float = 5.0) -> None:
        """Ask the child to exit, then make sure it has. Safe to call more than once.

        The polite ``shutdown`` op first, with a short deadline: a child between jobs exits
        in milliseconds, and only a child that is mid-inference needs the signal.
        """
        with self._lock:
            proc, self._proc = self._proc, None
            self._closing = True
            self._generation += 1  # any straggling reader belongs to a previous child now
        if proc is None:
            return
        try:
            self._send_to(proc, next(self._ids), SHUTDOWN, (), {})
        except Exception:  # noqa: BLE001 - the child may already be gone; the signal is next
            pass
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:  # pragma: no cover - a kill that did not take
                log.warning("face model process %s did not die after SIGKILL", proc.pid)
        self.stopped = True
        self._fail_all("the face model process was stopped")

    def restart(self) -> "FaceProcess":
        """Stop whatever is running and spawn a fresh child on the next call."""
        self.stop()
        return self.start()

    # -- the read side -----------------------------------------------------
    def _read_loop(self, proc: subprocess.Popen, generation: int) -> None:
        stream = proc.stdout
        try:
            while True:
                header = _read_exactly(stream, FRAME_HEADER.size)
                if header is None:
                    break
                (size,) = FRAME_HEADER.unpack(header)
                body = _read_exactly(stream, size)
                if body is None:
                    break
                try:
                    message = pickle.loads(body)
                except Exception as exc:  # noqa: BLE001 - a corrupt frame ends the child
                    self.last_error = f"unreadable reply from the model process: {exc}"
                    log.error("%s", self.last_error)
                    break
                self._deliver(message)
        finally:
            self._reap(proc, generation)

    def _deliver(self, message: dict) -> None:
        result = message.get("result")
        if isinstance(result, dict) and isinstance(result.get("pid"), int):
            self.worker_pid = result["pid"]
        pending = None
        with self._lock:
            pending = self._pending.pop(int(message.get("id", 0)), None)
        if pending is None:
            # A reply to a request that already timed out and was abandoned (or a reply that
            # arrived after its child was replaced). Dropped rather than mismatched: applying
            # it to whatever is waiting now would score one photograph with another's answer.
            log.debug("dropping an unclaimed reply from the model process: %r", message.get("id"))
            return
        pending.reply = message
        pending.event.set()

    def _reap(self, proc: subprocess.Popen, generation: int) -> None:
        """The child's stdout ended: it exited, was killed, or the pipe broke."""
        with self._lock:
            if generation != self._generation or self._proc is not proc:
                return  # a later child owns this client now; this one is history
            self._proc = None
        code = proc.poll()
        if code is None:
            code = proc.wait(timeout=30)
        if not self._closing:
            self.last_error = f"the face model process exited (code {code})"
            log.warning("%s", self.last_error)
        self._fail_all(self.last_error or "the face model process exited")

    def _drain_stderr(self, proc: subprocess.Popen, generation: int) -> None:
        """Keep the child's stderr moving, or a chatty child blocks on a full pipe.

        Not routed to the parent's log level: it is the child's own log, prefixed so it can
        be told apart from the API's.
        """
        stream = proc.stderr
        if stream is None:  # pragma: no cover - PIPE was requested
            return
        try:
            for raw in iter(stream.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip()
                if line:
                    log.warning("[face-worker] %s", line)
        except Exception:  # noqa: BLE001 - the pipe closing under us is the normal end
            pass

    def _fail_all(self, reason: str) -> None:
        with self._lock:
            pending, self._pending = self._pending, {}
        if pending:
            log.warning("%s: failing %s request(s) in flight", reason, len(pending))
        for waiter in pending.values():
            waiter.reply = {"id": 0, "ok": False, "error": {"type": "FaceProcessUnavailable", "message": reason}}
            waiter.event.set()

    # -- the write side ----------------------------------------------------
    def call(self, op: str, *args, deadline: float | None = None, **kwargs) -> Any:
        """Run ``op`` in the child and return its result, or raise.

        Raises ``FaceProcessUnavailable`` when the child could not be reached (or died while
        answering), ``FaceProcessTimeout`` when it did not answer in time, and
        ``FaceProcessRemoteError`` when it answered with a failure - the same exception the
        op would have raised in this process.
        """
        return self.reply_of(op, *args, deadline=deadline, **kwargs).get("result")

    def reply_of(self, op: str, *args, deadline: float | None = None, **kwargs) -> dict:
        """``call``, but the whole reply - for the ops whose timings the caller reports."""
        self.start()
        with self._lock:
            proc = self._proc
            if proc is None or proc.poll() is not None:  # pragma: no cover - start() raced
                raise FaceProcessUnavailable("the face model process is not running")
            request_id = next(self._ids)
            waiter = _Pending()
            self._pending[request_id] = waiter
        timeout = float(deadline if deadline is not None else self.deadline_seconds)
        started = time.monotonic()
        try:
            self._send_to(proc, request_id, op, args, kwargs)
        except FaceProcessUnavailable:
            with self._lock:
                self._pending.pop(request_id, None)
            self.failures += 1
            raise
        answered = waiter.event.wait(timeout)
        self.calls += 1
        self.last_call_ms = round((time.monotonic() - started) * 1000.0, 1)
        if not answered:
            with self._lock:
                self._pending.pop(request_id, None)
            self.timeouts += 1
            self.last_error = f"the face model process did not answer {op!r} within {timeout:.0f}s"
            # Killed, not ignored: the child is still working on this request, and the reply
            # it produces next would be this one - which would then be matched to the next
            # caller's photograph.
            self._kill("no answer within the deadline")
            raise FaceProcessTimeout(self.last_error)
        assert waiter.reply is not None
        reply = waiter.reply
        if not reply.get("ok"):
            self.failures += 1
            error = reply_error(reply)
            if error.error_type == "FaceProcessUnavailable":
                raise FaceProcessUnavailable(error.message)
            raise error
        return reply

    def _send_to(self, proc: subprocess.Popen, request_id: int, op: str, args: tuple, kwargs: dict) -> None:
        message = {"id": request_id, "op": op, "args": args, "kwargs": kwargs}
        try:
            payload = pickle.dumps(message, protocol=pickle.HIGHEST_PROTOCOL)
        except Exception as exc:  # noqa: BLE001 - an unpicklable argument is a caller bug
            raise FaceProcessError(f"{op!r} could not be sent to the model process: {exc}") from exc
        frame = FRAME_HEADER.pack(len(payload)) + payload
        try:
            with self._write_lock:
                if proc.stdin is None:  # pragma: no cover
                    raise FaceProcessUnavailable("the model process has no stdin")
                proc.stdin.write(frame)
                proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            self.last_error = f"the model process went away while sending {op!r}: {exc}"
            log.warning("%s", self.last_error)
            raise FaceProcessUnavailable(self.last_error) from exc

    def _kill(self, reason: str) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
            self._generation += 1
        if proc is None:
            return
        log.warning("killing the face model process (%s): %s", proc.pid, reason)
        try:
            proc.kill()
        except Exception:  # noqa: BLE001 - already gone
            pass
        self._kill_worker_pid(proc.pid, reason)
        self._fail_all(reason)

    def _kill_worker_pid(self, spawned: int | None, reason: str) -> None:
        """Also terminate the interpreter the child said it was, when that is a different one.

        Best effort, and only in the forced path: a *graceful* stop asks the child to exit and
        the launcher follows it, so this exists for the child that has stopped answering to
        everyone but is still holding a session's worth of memory.
        """
        pid = self.worker_pid
        if pid is None or pid == spawned:
            return
        try:
            os.kill(pid, signal.SIGTERM)
            log.warning("also terminated the model interpreter (pid %s): %s", pid, reason)
        except Exception:  # noqa: BLE001 - already gone is the outcome we wanted
            pass

    # -- the ops -----------------------------------------------------------
    def ping(self) -> dict:
        return self.call(PING, deadline=min(30.0, self.start_seconds))

    def warm(self) -> dict:
        """Load every model in the child and describe what was loaded."""
        return self.call(WARM, deadline=self.start_seconds)

    def status(self, *, cache_seconds: float | None = None) -> dict:
        """The child's own status, briefly cached.

        Cached because this is what ``/api/v1/status`` and readiness call: an operator
        refreshing a page must not spawn an IPC round trip per request, and a model process
        does not change what it is holding between two of them.
        """
        cache = (
            settings.face_engine_process_status_seconds if cache_seconds is None else cache_seconds
        )
        now = time.monotonic()
        cached = self._cached_status
        if cached is not None and cache > 0 and now - cached[0] < cache:
            return cached[1]
        snapshot = self.call(STATUS)
        self._cached_status = (now, snapshot)
        return snapshot

    def stats(self) -> dict[str, Any]:
        """What this transport has done, for readiness and for an operator."""
        return {
            "name": self.name,
            "handler": self.handler.name,
            "alive": self.alive,
            "pid": self.pid,
            "worker_pid": self.worker_pid,
            "starts": self.starts,
            "calls": self.calls,
            "failures": self.failures,
            "timeouts": self.timeouts,
            "in_flight": len(self._pending),
            "last_call_ms": self.last_call_ms,
            "deadline_seconds": self.deadline_seconds,
            "last_error": self.last_error,
        }


# ---------------------------------------------------------------------------
# the one client the application uses
# ---------------------------------------------------------------------------
_CLIENT: FaceProcess | None = None
_CLIENT_LOCK = threading.Lock()


def enabled() -> bool:
    """Whether the application is configured to run the models in a child process."""
    return bool(settings.face_engine_process)


def client() -> FaceProcess:
    """The shared transport, built on first use.

    Built lazily like every other model-adjacent thing in this codebase: importing the
    application - a CLI command, a migration, the test suite - must not spawn a process.
    """
    global _CLIENT
    if _CLIENT is None:
        with _CLIENT_LOCK:
            if _CLIENT is None:
                _CLIENT = FaceProcess()
    return _CLIENT


def use(replacement: FaceProcess | None) -> None:
    """Replace the shared transport. For tests and for a tool that owns its own child."""
    global _CLIENT
    with _CLIENT_LOCK:
        _CLIENT = replacement


def current() -> FaceProcess | None:
    """The shared transport **without** building one (``None`` until something asks)."""
    return _CLIENT


def stats() -> dict[str, Any]:
    """The transport's counters, or a ``not enabled`` marker. Never spawns a child."""
    if not enabled():
        return {"enabled": False}
    existing = current()
    if existing is None:
        return {"enabled": True, "alive": False, "starts": 0, "note": "not started yet"}
    return {"enabled": True, **existing.stats()}


def shutdown() -> None:
    """Stop the shared child, if there is one. Safe to call more than once."""
    existing = current()
    if existing is not None:
        existing.stop()


def _stop_at_exit() -> None:  # pragma: no cover - exercised by the interpreter's exit
    shutdown()


atexit.register(_stop_at_exit)
