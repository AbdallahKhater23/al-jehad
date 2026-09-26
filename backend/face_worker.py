"""The process that owns the face models.

This is the other end of ``face_process``: a child interpreter that imports ``face_onnx``,
``face_detector`` and ``liveness``, opens their models, and then answers a small, closed set
of operations over a length-prefixed pickled pipe on stdin/stdout. Why a separate process at
all - what it buys, what it costs, and what it deliberately does not carry - is argued in
``face_process``'s module docstring; this file is the far side of it.

FOUR THINGS ABOUT THE PROTOCOL
------------------------------
1. **Only model calls cross.** The ops are ``represent``, ``detect``, ``liveness_check`` and
   ``liveness_reset``, plus ``ping``/``warm``/``status``/``shutdown`` for lifecycle. Every
   decision - the queue, the capacity, the one-subject rule, the cosine, the thresholds,
   liveness's mode policy, the refusal sentences - stays in the API process. An RPC that
   carried a *job* would drag the application in here, and then this process would need a
   database to answer a question about a photograph.
2. **The ops call the application's own functions.** ``represent`` is
   ``face_engine._represent``, not a second implementation of it. That is the only reason
   the two modes can be trusted to agree: there is one ``_represent``, one crop, one
   threshold, and this process merely happens to be where it runs. It also means the child
   must *not* forward - see rule 4.
3. **stdout is the channel, so nothing else may print on it.** A model library that prints
   a banner to stdout would be read as a frame header and would desynchronise every reply
   that followed. So the binary stream is captured first and the *text* ``sys.stdout`` is
   pointed at stderr before any model is imported.
4. **The flag must be off here, and that is refused rather than assumed.** With
   ``FACE_ENGINE_PROCESS`` set, ``face_engine._represent`` forwards to a child - so a child
   that inherited it would spawn a grandchild, forward to it, and hang until the deadline.
   ``face_process`` always clears the variable for the child; this module exits rather than
   serve if that ever stops being true, because the failure mode is a hang rather than an
   error and a hang inside a punch is an outage.

WHAT IT DOES NOT DO
-------------------
It does not bound itself. There is no cap on how much memory one embedding may allocate, no
timeout on an op (the *parent* holds the deadline and kills this process when it passes), and
no second worker: one op runs at a time, which is the honest cost of this prototype and is
discussed in ``face_process``. A cgroup around this process - which is what
``tools/capacity_test.py`` already knows how to measure - is the way to bound it properly.

Run by hand to see it work (it speaks the pipe, so the prompt is the protocol; Ctrl-C or EOF
ends it):

    python backend/face_worker.py
"""

from __future__ import annotations

import logging
import os
import pickle
import sys
import time
import traceback
from typing import Any

#: The wire format, defined once, in the module that owns the client side of it.
from face_process import FRAME_HEADER

log = logging.getLogger("face_worker")

#: Every operation this process will answer. Named rather than open-ended: an op is a
#: function this process is willing to run, and a protocol that accepted arbitrary names is
#: a remote code execution surface wearing a pipe.
OPS = (
    "ping",
    "warm",
    "status",
    "represent",
    "detect",
    "liveness_check",
    "liveness_reset",
    "shutdown",
)


class _Shutdown(Exception):
    """Asked to exit; answered first, then honoured."""


def _self_rss() -> int | None:
    """This process's resident set, where the platform accounts for one.

    Read on Linux (the deployment) and left ``None`` where it is not available: the parent
    can measure this process from outside, and a made-up number here would be worse than no
    number.
    """
    try:
        with open("/proc/self/status", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


_MODELS: tuple[Any, Any, Any, Any] | None = None


def _models():
    """The three model modules, imported on first use and cached.

    Cached in a module global rather than imported at the top so ``--help``-shaped uses and
    the refusal above cost nothing; every op that needs a model needs all three eventually
    anyway.
    """
    global _MODELS
    if _MODELS is None:
        import face_detector
        import face_engine
        import face_onnx
        import liveness

        _MODELS = (face_engine, face_onnx, face_detector, liveness)
    return _MODELS


def _describe(load: bool) -> dict:
    """What this process holds. ``load`` decides whether describing it also opens it."""
    face_engine, face_onnx, face_detector, liveness = _models()
    if load:
        embedding = face_onnx.load_now()
    else:
        embedding = face_onnx.get_engine().describe()
    return {
        "embedding": embedding,
        "detector": face_detector.describe(),
        "liveness": liveness.status(),
        "pid": os.getpid(),
        "rss_bytes": _self_rss(),
    }


def _dispatch(op: str, args: tuple, kwargs: dict) -> tuple[Any, dict]:
    """Run one op. Returns ``(result, extra)``, where ``extra`` labels the model call.

    ``extra`` exists so the *parent* can record the model-call telemetry: this process's own
    Prometheus counters are in another process and are never scraped, so a timing recorded
    here would be recorded where nobody reads it.
    """
    if op == "ping":
        return {
            "worker": "face-worker",
            "pid": os.getpid(),
            "ops": list(OPS),
            "python": sys.version.split()[0],
        }, {}
    if op == "warm":
        return _describe(load=True), {}
    if op == "status":
        return _describe(load=False), {}
    if op == "shutdown":
        raise _Shutdown()

    face_engine, face_onnx, face_detector, liveness = _models()
    if op == "represent":
        return face_engine._represent(*args, **kwargs), {
            "operation": "represent",
            "model": face_onnx.MODEL_NAME,
            "detector": face_detector.active_detector(),
        }
    if op == "detect":
        return face_engine._detect(*args, **kwargs), {
            "operation": "detect",
            "model": face_detector.active_detector(),
            "detector": face_detector.active_detector(),
        }
    if op == "liveness_check":
        return liveness.check_liveness(*args, **kwargs), {"operation": "liveness"}
    if op == "liveness_reset":
        liveness.reset_session()
        return None, {}
    raise KeyError(f"unknown face worker op: {op!r}")


def _answer(message: dict) -> tuple[dict, bool]:
    """One request in, one reply out. Never raises: a failure is an answer too."""
    request_id = message.get("id")
    op = str(message.get("op", ""))
    args = tuple(message.get("args") or ())
    kwargs = dict(message.get("kwargs") or {})
    started = time.perf_counter()
    try:
        result, extra = _dispatch(op, args, kwargs)
    except _Shutdown:
        return {"id": request_id, "ok": True, "result": None, "extra": {}, "seconds": 0.0}, True
    except BaseException as exc:  # noqa: BLE001 - described to the caller, never raised over the wire
        # Logged with its traceback *here*, where the model is: this is the process whose
        # stack says which line of ONNX Runtime or OpenCV objected, and the parent's log
        # cannot show it.
        log.error("face worker op %r failed: %s", op, exc)
        log.debug("%s", traceback.format_exc())
        return {
            "id": request_id,
            "ok": False,
            "error": {
                "type": type(exc).__name__,
                "message": str(exc)[:2000],
                "traceback": traceback.format_exc()[-4000:],
            },
            "extra": {},
            "seconds": time.perf_counter() - started,
        }, False
    return {
        "id": request_id,
        "ok": True,
        "result": result,
        "extra": extra,
        "seconds": time.perf_counter() - started,
    }, False


def _read_exactly(stream, count: int) -> bytes | None:
    """``count`` bytes, or ``None`` when the pipe ended. Same rule as the parent's reader."""
    chunks: list[bytes] = []
    remaining = count
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def serve(stdin, stdout, *, stop_after: int | None = None) -> int:
    """Answer requests until stdin ends, the pipe breaks, or ``shutdown`` arrives."""
    served = 0
    while stop_after is None or served < stop_after:
        header = _read_exactly(stdin, FRAME_HEADER.size)
        if header is None:
            log.info("the API process closed the pipe; exiting")
            return 0
        (size,) = FRAME_HEADER.unpack(header)
        body = _read_exactly(stdin, size)
        if body is None:
            return 0
        try:
            message = pickle.loads(body)
        except Exception:  # noqa: BLE001 - an unreadable frame desynchronises the stream
            # Exiting rather than answering: the parent cannot match a reply to a request it
            # cannot identify, and its own reader ends on this exit and fails what is in
            # flight. A desynchronised stream cannot be resynchronised, so this is the end.
            log.error("unreadable request frame; exiting\n%s", traceback.format_exc())
            return 1
        reply, stop = _answer(message)
        payload = pickle.dumps(reply, protocol=pickle.HIGHEST_PROTOCOL)
        stdout.write(FRAME_HEADER.pack(len(payload)) + payload)
        try:
            stdout.flush()
        except (BrokenPipeError, OSError):
            log.info("the API process went away while replying; exiting")
            return 0
        served += 1
        if stop:
            log.info("shutdown requested; exiting after %s request(s)", served)
            return 0
    return 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="[face-worker %(levelname)s] %(message)s",
    )
    # Rule 4 in the module docstring: a child that forwards would spawn a grandchild and
    # hang. Refused at startup, before any model is imported, so the mistake is a one-line
    # exit rather than a punch that never answers.
    if os.environ.get("FACE_ENGINE_PROCESS", "").strip().lower() in {"1", "true", "yes", "on"}:
        print(
            "face_worker refuses to run with FACE_ENGINE_PROCESS set: this process runs the "
            "models, and forwarding from here would recurse into another child and hang.",
            file=sys.stderr,
        )
        return 2
    # Rule 3: capture the binary channel first, then send the *text* stdout somewhere else,
    # so a print() in any imported model library cannot corrupt the protocol.
    stdout = sys.stdout.buffer
    sys.stdout = sys.stderr
    log.info("face worker up (pid %s, python %s)", os.getpid(), sys.version.split()[0])
    return serve(sys.stdin.buffer, stdout)


if __name__ == "__main__":
    raise SystemExit(main())
