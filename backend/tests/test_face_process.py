"""The prototype that moves the face models into a child process.

WHY THIS EXISTS
---------------
``face_process`` and ``face_worker`` are the seam ``face_engine`` names in its own
docstring: the models stop sharing a fate with the API process, so an ONNX Runtime failure
or an OOM kill ends a child that the next punch restarts instead of ending the process that
serves the gate. The claim is architectural, so it is pinned in three layers:

1. **The transport.** Against a stub worker that speaks the same framing: a reply crosses, a
   failure comes back as the failure it was, a child that dies mid-request is reported and
   replaced, a child that stops answering is killed rather than waited for, and an unreadable
   frame ends the child instead of desynchronising it.
2. **The wiring.** With a stand-in transport, the engine/liveness/preload forward exactly the
   *model calls* - and nothing else. The decision logic (the gate, the one-subject rule, the
   thresholds) must still run in the API process, which is what makes this an isolation of
   models rather than a second copy of the application.
3. **The guard that keeps the seam honest.** A child that could forward would spawn a
   grandchild and hang, so the flag is cleared for every spawned child and the worker refuses
   to run with it set.

No test here opens a real model: the stub is the transport's peer, and the wiring tests use a
stand-in client. ``tools/face_process_memory.py`` is where the real numbers are taken.
"""

from __future__ import annotations

import ast
import os
import pickle
import subprocess
import sys
import time
from pathlib import Path

import pytest

import config
import face_engine
import face_onnx
import face_process
import face_worker
import harness
import liveness

BACKEND_DIR = harness.BACKEND_DIR

#: The other end of the protocol, in miniature: it frames messages exactly the way
#: ``face_process`` does (it imports the same ``FRAME_HEADER``), and everything it can be
#: asked to do is a way for the transport to be wrong.
_STUB = '''
import os, pickle, sys, time

from face_process import FRAME_HEADER


def _read_exactly(stream, count):
    chunks = []
    while count > 0:
        chunk = stream.read(count)
        if not chunk:
            return None
        chunks.append(chunk)
        count -= len(chunk)
    return b"".join(chunks)


def main():
    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
    while True:
        header = _read_exactly(stdin, FRAME_HEADER.size)
        if header is None:
            return 0
        (size,) = FRAME_HEADER.unpack(header)
        body = _read_exactly(stdin, size)
        if body is None:
            return 0
        message = pickle.loads(body)
        op = message["op"]
        if op == "die":
            os._exit(7)
        if op == "sleep":
            time.sleep(float(message["kwargs"].get("seconds", 30)))
        if op == "garbage":
            # A frame the parent cannot unpickle. It cannot be resynchronised, so the child
            # ends rather than answering - which is the behaviour under test.
            stdout.write(b"\\x00\\x00\\x00\\x0bnot-a-pickle")
            stdout.flush()
            return 1
        if op == "boom":
            # A failure the child catches and frames: the op was wrong, not the transport.
            payload = pickle.dumps(
                {
                    "id": message["id"],
                    "ok": False,
                    "error": {
                        "type": "ValueError",
                        "message": "no face detected in the photo",
                        "traceback": "",
                    },
                    "extra": {},
                    "seconds": 0.001,
                }
            )
            stdout.write(FRAME_HEADER.pack(len(payload)) + payload)
            stdout.flush()
            continue
        result = {"pid": os.getpid(), "op": op, "echo": message["args"]}
        payload = pickle.dumps(
            {"id": message["id"], "ok": True, "result": result, "extra": {}, "seconds": 0.001}
        )
        stdout.write(FRAME_HEADER.pack(len(payload)) + payload)
        stdout.flush()


raise SystemExit(main())
'''


@pytest.fixture()
def stub(tmp_path: Path):
    """A transport pointed at the stub worker, stopped on the way out."""
    script = tmp_path / "stub_worker.py"
    script.write_text(_STUB, encoding="utf-8")
    clients: list[face_process.FaceProcess] = []

    def build(**overrides) -> face_process.FaceProcess:
        client = face_process.FaceProcess(
            handler=script,
            extra_env={"PYTHONPATH": str(BACKEND_DIR)},
            deadline_seconds=overrides.pop("deadline_seconds", 20.0),
            start_seconds=overrides.pop("start_seconds", 30.0),
            **overrides,
        )
        clients.append(client)
        return client

    yield build
    for client in clients:
        client.stop()


# ---------------------------------------------------------------------------
# 1. the transport
# ---------------------------------------------------------------------------
def test_a_request_and_its_answer_cross_the_pipe(stub):
    client = stub()
    answer = client.call("ping", "one", 2)
    assert answer["op"] == "ping"
    assert answer["echo"] == ("one", 2)
    assert client.stats()["calls"] == 1
    assert client.stats()["alive"] is True


def test_the_worker_pid_is_learned_from_the_child_itself(stub):
    """Not the pid we spawned: on Windows a venv's ``python.exe`` is a launcher stub.

    Killing the wrong one leaves a 200 MiB child behind, so the pid the child reports is
    what the forced path terminates. On Linux the two are the same process.
    """
    client = stub()
    client.call("ping")
    assert client.worker_pid is not None
    assert client.stats()["worker_pid"] == client.worker_pid


def test_a_failing_op_comes_back_as_the_failure_it_was(stub):
    client = stub()
    with pytest.raises(face_process.FaceProcessRemoteError) as caught:
        client.call("boom")
    assert caught.value.error_type == "ValueError"
    assert "no face detected" in caught.value.message
    # And the transport is still usable: one bad answer is not a dead child.
    assert client.call("ping")["op"] == "ping"


def test_a_child_that_dies_mid_request_is_reported_and_replaced(stub):
    client = stub()
    client.call("ping")
    with pytest.raises(face_process.FaceProcessUnavailable):
        client.call("die")
    assert client.alive is False
    # The next call spawns a fresh one rather than staying dead: one punch pays for a crash.
    assert client.call("ping")["op"] == "ping"
    assert client.stats()["starts"] == 2


def test_a_child_that_stops_answering_is_killed_rather_than_waited_for(stub):
    client = stub(deadline_seconds=1.0)
    client.call("ping")
    started = time.perf_counter()
    with pytest.raises(face_process.FaceProcessTimeout):
        client.call("sleep", seconds=60)
    assert time.perf_counter() - started < 30, "the deadline was not applied"
    assert client.alive is False
    assert client.stats()["timeouts"] == 1
    # Killed, not leaked: the next caller gets a working child, not the hung one's queue.
    assert client.call("ping")["op"] == "ping"
    assert client.stats()["starts"] == 2


def test_an_unreadable_frame_ends_the_child_rather_than_desynchronising_it(stub):
    client = stub()
    client.call("ping")
    with pytest.raises(face_process.FaceProcessUnavailable):
        client.call("garbage")
    assert client.alive is False
    assert client.call("ping")["op"] == "ping"


def test_unpicklable_arguments_are_refused_without_killing_the_child(stub):
    client = stub()
    client.call("ping")

    def unpicklable():
        raise RuntimeError

    with pytest.raises(face_process.FaceProcessError):
        client.call("ping", unpicklable)
    assert client.alive is True, "a caller's bad argument is not the child's problem"


def test_a_client_that_cannot_start_says_so_rather_than_hanging(tmp_path):
    client = face_process.FaceProcess(
        handler=tmp_path / "does_not_exist.py", deadline_seconds=1.0, start_seconds=1.0
    )
    with pytest.raises(face_process.FaceProcessUnavailable):
        client.call("ping")
    assert client.stats()["alive"] is False


# ---------------------------------------------------------------------------
# 2. the guard that keeps the seam from recursing
# ---------------------------------------------------------------------------
def test_every_spawned_child_has_forwarding_disabled(stub, monkeypatch):
    """A child that inherited the flag would forward to a grandchild and hang.

    A hang, not an error: the punch would sit on the deadline and then be killed, and the
    only symptom would be a gate that stopped verifying people.
    """
    monkeypatch.setenv("FACE_ENGINE_PROCESS", "1")
    client = stub()
    assert client.child_env()["FACE_ENGINE_PROCESS"] == "0"


def test_the_worker_refuses_to_run_with_forwarding_enabled():
    completed = subprocess.run(
        [sys.executable, str(BACKEND_DIR / "face_worker.py")],
        cwd=str(BACKEND_DIR),
        env={**os.environ, "FACE_ENGINE_PROCESS": "1"},
        input=b"",
        capture_output=True,
        timeout=60,
    )
    assert completed.returncode == 2
    assert b"refuses to run" in completed.stderr


def test_the_worker_runs_the_application_s_own_model_call():
    """There is one ``_represent``, and the child is merely where it runs.

    Parsed rather than grepped: the module docstrings explain this rule, and a text search
    would read the explanation as a second implementation.
    """
    tree = ast.parse((BACKEND_DIR / "face_worker.py").read_text(encoding="utf-8"))
    calls = [
        f"{node.value.id}.{node.attr}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in {"face_engine", "liveness"}
    ]
    assert "face_engine._represent" in calls
    assert "face_engine._detect" in calls
    assert "liveness.check_liveness" in calls
    # And nothing that re-implements one: the detector is reached only through the engine.
    assert "face_detector.detect_and_align" not in calls


# ---------------------------------------------------------------------------
# 3. the wiring: model calls cross, decisions do not
# ---------------------------------------------------------------------------
class StandIn:
    """A transport that answers from a table instead of a pipe."""

    def __init__(self, results=None, errors=None) -> None:
        self.results = dict(results or {})
        self.errors = dict(errors or {})
        self.calls: list[tuple] = []
        self.stopped = False
        self.warmed = False

    def call(self, op, *args, **kwargs):
        self.calls.append((op, args, kwargs))
        if op in self.errors:
            raise self.errors[op]
        return self.results.get(op)

    def reply_of(self, op, *args, **kwargs):
        self.calls.append((op, args, kwargs))
        if op in self.errors:
            raise self.errors[op]
        return {
            "ok": True,
            "result": self.results.get(op),
            "seconds": 0.002,
            "extra": {"operation": op, "model": "Facenet", "detector": "yunet"},
        }

    def status(self):
        self.calls.append(("status", (), {}))
        if "status" in self.errors:
            raise self.errors["status"]
        return self.results.get("status") or {}

    def warm(self):
        self.warmed = True
        self.calls.append(("warm", (), {}))
        return self.results.get("warm") or {}

    def stats(self):
        return {"enabled": True, "alive": True, "calls": len(self.calls)}

    def stop(self):
        self.stopped = True


@pytest.fixture()
def remote(monkeypatch):
    """Process mode on, with the transport replaced. Off and unpatched on the way out."""
    stand_in = StandIn()

    def install(**results) -> StandIn:
        stand_in.results.update(results)
        monkeypatch.setattr(config.settings, "face_engine_process", True)
        face_process.use(stand_in)
        return stand_in

    yield install
    face_process.use(None)
    monkeypatch.setattr(config.settings, "face_engine_process", False)


def test_the_engine_forwards_the_embedding_call(remote):
    stand_in = remote(represent=[{"embedding": [0.5, 0.25], "face_confidence": 1.0}])
    result = face_engine.ENGINE.represent_direct("an-image")
    assert result == [{"embedding": [0.5, 0.25], "face_confidence": 1.0}]
    assert stand_in.calls[0][0] == "represent"


def test_the_engine_forwards_the_detection_call(remote):
    stand_in = remote(detect=[{"facial_area": {"x": 1}}])
    assert face_engine.ENGINE.detect_direct("an-image") == [{"facial_area": {"x": 1}}]
    assert stand_in.calls[0][0] == "detect"


def test_a_dead_model_process_is_a_server_fault_not_a_photo_problem(remote):
    """``FaceEngineUnavailable``, which every endpoint already answers as 500/503.

    The distinction is the only one a worker at a gate can act on: "your photo is wrong"
    sends them to re-enroll, "we could not check right now" does not.
    """
    stand_in = remote()
    stand_in.errors["represent"] = face_process.FaceProcessUnavailable("the process exited")
    with pytest.raises(face_engine.FaceEngineUnavailable):
        face_engine.ENGINE.represent_direct("an-image")


def test_the_model_calls_stay_local_when_the_flag_is_off(monkeypatch):
    face_process.use(None)
    monkeypatch.setattr(config.settings, "face_engine_process", False)
    monkeypatch.setattr(
        face_engine.face_detector, "available", lambda: (False, "no detector model")
    )
    with pytest.raises(face_engine.FaceEngineUnavailable):
        face_engine._detect("an-image")


def test_liveness_forwards_only_the_model_call(remote):
    decided = liveness.LivenessResult(verdict=liveness.VERDICT_LIVE, is_live=True, available=True)
    stand_in = remote(liveness_check=decided)
    assert liveness.check_liveness("a-frame") is decided
    assert stand_in.calls == [("liveness_check", ("a-frame",), {"size": None})]
    # ``gate`` is policy and stays here, so the mode is applied by this process.
    decision = liveness.inspect("a-frame", mode_override=liveness.MODE_ENFORCE)
    assert decision.allowed is True
    assert stand_in.calls[-1][0] == "liveness_check", "only the model call crossed"


def test_liveness_is_unavailable_rather_than_fatal_when_the_child_is_gone(remote):
    stand_in = remote()
    stand_in.errors["liveness_check"] = face_process.FaceProcessUnavailable("the process exited")
    result = liveness.check_liveness("a-frame")
    assert result.available is False
    assert result.verdict == liveness.VERDICT_UNAVAILABLE
    assert "could not answer" in result.detail
    # An advisory gate still lets the punch through - the documented behaviour for a missing
    # model in the default mode - so a restarted child is not an outage.
    assert liveness.inspect("a-frame").allowed is True


def test_liveness_status_is_answered_by_the_child(remote):
    stand_in = remote(status={"liveness": {"mode": "advisory", "available": True, "error": None}})
    assert liveness.status()["available"] is True
    assert stand_in.calls[0][0] == "status"
    assert liveness._SESSION is None, "the API process never built a session of its own"


def test_liveness_falls_back_to_the_truth_when_the_child_cannot_be_asked(remote, monkeypatch):
    stand_in = remote()
    stand_in.errors["status"] = face_process.FaceProcessUnavailable("not running")
    monkeypatch.setattr(liveness, "get_session", lambda: None)
    monkeypatch.setattr(liveness, "_SESSION_ERROR", "no model")
    report = liveness.status()
    assert report["available"] is False
    assert report["error"] == "no model"


def test_the_preload_asks_the_child_to_load_every_model(remote):
    stand_in = remote(warm={"embedding": {"model": "Facenet", "loaded": True}})
    assert face_onnx.load_now() == {"model": "Facenet", "loaded": True}
    assert stand_in.warmed is True


def test_the_engine_snapshot_and_shutdown_reach_the_model_process(remote):
    stand_in = remote()
    engine = face_engine.FaceEngine(capacity=1, queue_depth=1, wait_seconds=0.1, name="probe")
    assert engine.snapshot()["model_process"]["enabled"] is True
    engine.shutdown()
    assert stand_in.stopped is True
