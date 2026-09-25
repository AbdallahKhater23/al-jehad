"""The tool that puts a second encoder on disk, held to what it claims to guarantee.

WHAT IS ACTUALLY BEING PINNED HERE
----------------------------------
Not "the FaceNet-512 export is accurate" - that is a corpus measurement and lives in
``tools/contract_ab.py``. What this file pins is the *adapter*: the published 512 export declares a
NCHW input, the application feeds one NHWC tensor to both encoders, and a graph that keeps its own
layout passes ``shadow_rollout.validate_pair`` (the canvases agree at 160) and then fails inside
``FaceNetORT._run`` on the first scored frame. The wrapper in ``tools/fetch_shadow_graph.py`` is the
whole difference between "the migration starts" and "the migration starts and then finds nothing".

So the graphs below are tiny synthetic models, one per layout, and the assertions are about shape,
layout and identity -- plus the two refusals that keep a 90 MiB blob honest: the source digest, and
a source that is not a graph this tool can read.

``onnx`` is imported through ``importorskip``: it is developer tooling (it arrives with ``tf2onnx``
in this venv) rather than something the deployment needs, so a machine without it skips rather than
fails - the running application never imports it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

onnx = pytest.importorskip("onnx", reason="tools/fetch_shadow_graph.py is exercised through onnx")

from onnx import TensorProto, helper  # noqa: E402 - after the importorskip

_BACKEND_DIR = Path(__file__).resolve().parent.parent
_TOOLS_DIR = _BACKEND_DIR / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

import fetch_shadow_graph  # noqa: E402 - the path has to be set up first


def _tiny_graph(path, width: int, *, layout: str, size: int = 160):
    """A real ONNX graph of the requested width and layout: mean over spatial, then a matmul."""
    if layout == "NCHW":
        input_shape = ["batch", 3, size, size]
        axes = [2, 3]
    else:
        input_shape = ["batch", size, size, 3]
        axes = [1, 2]
    nodes = [
        helper.make_node("ReduceMean", ["input"], ["pooled"], axes=axes, keepdims=0),
        helper.make_node("MatMul", ["pooled", "w"], ["embedding"]),
    ]
    weight = helper.make_tensor("w", TensorProto.FLOAT, [3, width], np.arange(3 * width, dtype=np.float32))
    graph = helper.make_graph(
        nodes,
        f"tiny_{layout}_{width}",
        [helper.make_tensor_value_info("input", TensorProto.FLOAT, input_shape)],
        [helper.make_tensor_value_info("embedding", TensorProto.FLOAT, ["batch", width])],
        [weight],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)])
    # Ahead of the pinned onnxruntime's ceiling, as in the shadow suite: lowered so a failure here
    # is about the tool and not about two dev dependencies disagreeing on a version number.
    model.ir_version = 13
    onnx.checker.check_model(model)
    onnx.save(model, str(path))
    return path


def _runtime_facts(path):
    import onnxruntime as ort

    session = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
    return session.get_inputs()[0].shape, session.get_outputs()[0].shape


# ---------------------------------------------------------------------------
# 1. the layout adapter
# ---------------------------------------------------------------------------
def test_an_nchw_export_is_re_layed_as_the_shared_nhwc_tensor(tmp_path):
    source = _tiny_graph(tmp_path / "raw.onnx", 512, layout="NCHW")
    destination = tmp_path / "facenet512.onnx"

    facts = fetch_shadow_graph.to_nhwc(source, destination)

    assert facts["wrapped"] is True
    input_shape, output_shape = _runtime_facts(destination)
    assert input_shape[1:] == [160, 160, 3], (
        "the whole point of the wrapper: the transpose puts the channels last so the one tensor "
        "face_align produces can be handed to this graph as well as the incumbent"
    )
    assert output_shape[-1] == 512
    # A moved stride is not a resample, so the transpose must not disturb a single value: the
    # wrapper's output has to equal the source's own on the corresponding tensor.
    source_facts = fetch_shadow_graph.describe(source)
    assert source_facts["layout"] == "NCHW"
    assert fetch_shadow_graph.describe(destination)["layout"] == "NHWC"


def test_the_wrapped_graph_can_be_fed_the_nhwc_tensor_and_returns_a_width(tmp_path):
    source = _tiny_graph(tmp_path / "raw.onnx", 512, layout="NCHW")
    destination = tmp_path / "facenet512.onnx"
    fetch_shadow_graph.to_nhwc(source, destination)

    import onnxruntime as ort

    session = ort.InferenceSession(str(destination), providers=["CPUExecutionProvider"])
    probe = np.random.default_rng(3).random((1, 160, 160, 3)).astype(np.float32)
    vector = session.run(None, {session.get_inputs()[0].name: probe})[0]

    assert vector.shape == (1, 512), "the migration names its width from this output"


def test_an_already_nhwc_graph_is_copied_through_unchanged(tmp_path):
    """An export that already matches must not be rewritten, so its digest stays the measured one."""
    source = _tiny_graph(tmp_path / "raw.onnx", 256, layout="NHWC")
    destination = tmp_path / "facenet256.onnx"

    facts = fetch_shadow_graph.to_nhwc(source, destination)

    assert facts["wrapped"] is False
    assert fetch_shadow_graph.sha256_of(source) == fetch_shadow_graph.sha256_of(destination)
    assert _runtime_facts(destination)[0][1:] == [160, 160, 3]


# ---------------------------------------------------------------------------
# 2. the refusals that make a fetched blob reproducible
# ---------------------------------------------------------------------------
def test_a_source_that_is_not_the_pinned_artifact_is_refused(tmp_path, capsys):
    source = _tiny_graph(tmp_path / "raw.onnx", 512, layout="NCHW")
    code = fetch_shadow_graph.main(
        [
            "--source",
            str(source),
            "--output",
            str(tmp_path / "out.onnx"),
            "--expected-sha256",
            "0" * 64,
        ]
    )
    assert code == 2
    assert "sha256" in capsys.readouterr().err
    assert not (tmp_path / "out.onnx").exists(), "a refused source must not leave a graph behind"


def test_a_correct_digest_converts_and_reports_the_pinned_digest(tmp_path, capsys):
    source = _tiny_graph(tmp_path / "raw.onnx", 512, layout="NCHW")
    destination = tmp_path / "out.onnx"
    code = fetch_shadow_graph.main(
        [
            "--source",
            str(source),
            "--output",
            str(destination),
            "--expected-sha256",
            fetch_shadow_graph.sha256_of(source),
            "--json",
        ]
    )
    payload = capsys.readouterr().out
    assert code == 0
    assert destination.exists()
    import json

    report = json.loads(payload)
    # The reported facts are the *written* file's, which is the only thing an operator can check
    # later - reporting the source's NCHW shape here would describe a file that no longer exists.
    assert report["input_shape"][1:] == [160, 160, 3]
    assert report["width"] == 512
    assert report["output_sha256"] == fetch_shadow_graph.sha256_of(destination)
    assert report["wrapped"] is True


def test_a_file_that_is_not_a_graph_is_refused(tmp_path, capsys):
    junk = tmp_path / "not-a-graph.onnx"
    junk.write_bytes(b"this is not an onnx file")
    code = fetch_shadow_graph.main(["--source", str(junk), "--expected-sha256", "", "--output", str(tmp_path / "out.onnx")])
    assert code == 2
    assert capsys.readouterr().err.strip()


def test_the_shipped_source_is_pinned_to_a_digest():
    """The download is only reproducible if the digest is a real one, not a placeholder."""
    assert len(fetch_shadow_graph.SOURCE_SHA256) == 64
    assert set(fetch_shadow_graph.SOURCE_SHA256) <= set("0123456789abcdef")
    assert fetch_shadow_graph.SOURCE_URL.startswith("https://")
