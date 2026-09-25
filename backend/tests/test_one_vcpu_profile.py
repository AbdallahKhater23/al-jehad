"""The one-vCPU profile: what the benchmark approved, pinned where it lives.

WHY THIS EXISTS
---------------
Every change in this file is one line long and none of them is self-evidently load-bearing,
which is exactly why they need a test rather than a paragraph. Each was chosen against a
measurement on a 1 vCPU / 512 MB instance, and each undoes itself quietly if somebody
"tidies" it:

* **the ONNX CPU arena stays enabled.** Disabling it is the obvious-looking thing to do on a
  512 MB host, and the benchmark rejected it: with the arena off every intermediate buffer
  goes back to the system allocator and is asked for again on the next run, so the process
  pays in allocator churn rather than in peak - and peak is what the ceiling is about.
* **one thread per session, sequential execution.** The engine runs two face calls at a
  time, so two intra-op threads per session would put four compute threads on one core: no
  extra throughput, and per-thread scratch the host cannot spare.
* **OpenCV pinned to one thread** wherever it is used, because ``cv2``'s pool is
  process-wide and competes with the ONNX session for the same core.
* **``gc.freeze()`` after the warmups**, so the detector and embedding allocations are not
  re-scanned by every subsequent collection.
* **the uvicorn bounds** the container profile was measured with, and no ``workers=``: a
  second process would double the ~290 MiB floor rather than share it.
* **no SciPy on the punch path**, where it cost ~35 MB of RSS to run one dot product.

The source checks are parsed rather than grepped, so a sentence explaining one of these is
not mistaken for the thing itself.
"""

from __future__ import annotations

import ast
from pathlib import Path

import harness

BACKEND_DIR = harness.BACKEND_DIR


def _source(name: str) -> str:
    return (BACKEND_DIR / name).read_text(encoding="utf-8")


def _calls_in(source: str, name: str) -> list[ast.Call]:
    """Every call made from inside the function ``name``, in source order.

    All of them, not only the attribute calls: ``gc.freeze()`` is an attribute call and
    ``cosine(...)`` is a plain name, and a helper that quietly dropped one kind is how a
    source check passes against code that does not exist.
    """
    tree = ast.parse(source)
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    )
    calls = [node for node in ast.walk(function) if isinstance(node, ast.Call)]
    return sorted(calls, key=lambda node: node.lineno)


# --------------------------------------------------------------------------- #
# the decision that was rejected
# --------------------------------------------------------------------------- #
def test_the_cpu_memory_arena_is_enabled_explicitly_in_both_session_builders():
    """Rejected once, held rejected - and written down where the next reader will look.

    ``face_onnx`` is the engine a punch runs on; ``facenet_ort`` is the shadow scorer and the
    tools' session. Both are pinned, because the tempting change is to one of them and the
    temptation arrives with a memory graph.
    """
    for name in ("face_onnx.py", "facenet_ort.py"):
        source = _source(name)
        assert "enable_cpu_mem_arena = True" in source, name
        assert "enable_cpu_mem_arena = False" not in source, name


# --------------------------------------------------------------------------- #
# threads, on a box that has one
# --------------------------------------------------------------------------- #
def test_the_live_engine_asks_for_one_thread_per_session():
    import face_onnx

    assert face_onnx.DEFAULT_INTRA_OP_THREADS == 1
    assert face_onnx.DEFAULT_INTER_OP_THREADS == 1

    engine = face_onnx.OnnxFaceNetEngine(load_now=False)
    assert engine._intra_op_threads == 1, "the default a punch gets must be the pinned one"

    source = _source("face_onnx.py")
    assert "options.intra_op_num_threads = int(self._intra_op_threads)" in source
    assert "options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL" in source


def test_the_shadow_scorer_asks_for_one_thread_per_session_and_keeps_warming_up():
    """``facenet_ort`` is not on the punch path, and shares its deployment - so it is pinned.

    The warmup is the other half: the arena is sized on the first ``run()``, and the graph is
    91 MB, so without a warm pass the first worker of the day waits for the allocation.
    """
    source = _source("facenet_ort.py")

    assert "options.intra_op_num_threads = int(intra_threads or 1)" in source
    assert "options.inter_op_num_threads = 1" in source
    assert "self.warm(" in source or "warmup:" in source, "the warm pass went away"
    assert "def warm(" in source


def test_a_caller_can_still_ask_for_more_threads():
    """The pin is a default, not a lockdown: a host with cores to spare is measured, not guessed."""
    import face_onnx

    engine = face_onnx.OnnxFaceNetEngine(load_now=False, intra_op_threads=4)
    assert engine._intra_op_threads == 4


# --------------------------------------------------------------------------- #
# OpenCV
# --------------------------------------------------------------------------- #
def test_opencv_is_pinned_to_one_thread_in_every_module_that_uses_it():
    """A process-wide setting, so it has to be set wherever a module could be the first user.

    ``face_onnx`` is deliberately checked too, even though the detector modules already pin
    it: a tool that imports only the embedder must not be the exception that fans a resize
    across four threads on a single core.
    """
    for name in ("detector_640.py", "face_detector.py", "face_onnx.py"):
        assert "cv2.setNumThreads(1)" in _source(name), name


# --------------------------------------------------------------------------- #
# garbage collection
# --------------------------------------------------------------------------- #
def test_the_warmup_allocations_are_collected_and_then_frozen():
    """``gc.collect(); gc.freeze()`` in the lifespan, in that order and no other.

    Freezing without collecting first would freeze the warmup's own garbage; collecting
    without freezing leaves several thousand long-lived objects to be re-scanned by every
    collection the punch path triggers.
    """
    calls = [
        call.func.attr
        for call in _calls_in(_source("main.py"), "lifespan")
        if isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "gc"
    ]

    assert "freeze" in calls and "collect" in calls, calls
    assert calls.index("collect") < calls.index("freeze"), calls


# --------------------------------------------------------------------------- #
# the http server's bounds
# --------------------------------------------------------------------------- #
def test_the_server_bounds_are_the_ones_the_profile_was_measured_with():
    tree = ast.parse(_source("serve.py"))
    # ``uvicorn.run`` specifically: ``serve.py`` also shells out, and the first ``.run(...)``
    # in the module is a subprocess - which has no ``limit_concurrency`` and would make this
    # test pass by finding nothing to assert about.
    run = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "run"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "uvicorn"
    )
    keywords = {keyword.arg: keyword.value for keyword in run.keywords}

    assert isinstance(keywords.get("limit_concurrency"), ast.Constant)
    assert keywords["limit_concurrency"].value == 64
    assert keywords["timeout_keep_alive"].value == 5
    assert "workers" not in keywords, "a second process doubles the floor instead of sharing it"


# --------------------------------------------------------------------------- #
# the import that was removed from the punch path
# --------------------------------------------------------------------------- #
def test_no_runtime_module_on_the_punch_path_imports_scipy():
    """~35 MB of RSS for one dot product, measured, on a host with 512 MB in total.

    Parsed rather than grepped: ``main.cosine``'s own docstring names SciPy to explain what it
    replaced, and a source scan that counted that sentence as an import would make the fix
    un-documentable.
    """
    offenders = {}
    for name in ("main.py", "face_onnx.py", "face_engine.py", "facenet_ort.py", "biometrics.py"):
        source = _source(name)
        imported = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        if "scipy" in imported:
            offenders[name] = "scipy"
    assert offenders == {}, offenders


def test_the_comparison_is_the_one_numpy_implementation_and_it_handles_the_edges():
    import main
    import numpy as np

    a = np.array([1.0, 0.0, 0.0])

    assert main.cosine(a, a) == 0.0
    assert main.cosine(a, np.array([0.0, 1.0, 0.0])) == 1.0
    # Magnitude-invariant, like the function it replaced: the stored vector and the probe are
    # both normalised in production, but the score must not depend on that.
    assert abs(main.cosine(a * 7.0, a) - 0.0) < 1e-12
    # No direction to compare: refused by the band classifier rather than raising, because a
    # refusal is a punch the worker can retry and an exception is an "Internal processing error".
    assert np.isnan(main.cosine(a, np.zeros(3)))
    assert np.isnan(main.cosine(np.zeros(3), np.zeros(3)))


def test_the_punch_path_scores_with_the_local_cosine():
    """``compare_faces_sync`` must call it: the constant is only useful if it is used."""
    source = _source("main.py")
    calls = [
        call.func.id
        for call in _calls_in(source, "compare_faces_sync")
        if isinstance(call.func, ast.Name) and call.func.id == "cosine"
    ]

    assert calls == ["cosine"], "compare_faces_sync no longer scores with main.cosine"
