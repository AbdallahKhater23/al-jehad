"""Fetch the FaceNet-512 shadow graph and re-lay it as a drop-in for the incumbent's tensor.

WHY THIS EXISTS
---------------
``shadow_rollout`` refuses to start without a second encoder, and the second encoder is a ~90 MiB
artifact that does not ship in this repository (``backend/models/README.md``: large weights are
fetched per host). So "get one" is a real step of the migration rather than a footnote, and it has
two ways to go wrong that this tool exists to remove:

1. **The artifact has no provenance.** A 90 MiB float blob downloaded by hand is a file nobody can
   re-derive, and the cutover gate's whole argument rests on the shadow really being a different
   graph. The source URL *and the digest of the source file* are pinned here, so what a host ends
   up with is reproducible rather than remembered.

2. **The layout does not match.** This is the subtle one, and it is why a raw export is not a
   drop-in. Both encoders are handed the *same* tensor, and the application's tensor is NHWC
   ``(1, S, S, 3)`` - that is what ``face_align.to_tensor`` produces and what the incumbent
   ``facenet128.onnx`` declares. The published 512 export declares NCHW ``(1, 3, S, S)``, so the
   pair passes ``shadow_rollout.validate_pair`` (the canvases agree at 160) and then dies on the
   first scored frame inside ``FaceNetORT._run``, whose error names a shape rather than the mistake.
   The wrapper below inserts one ``Transpose`` in front of the graph and renames the input, so the
   file this writes is NHWC in and ``(N, width)`` out and the layout question never comes up again.

The transpose is a permute of the input tensor's strides, not a resampling: it moves no pixels, so
it cannot change a distance. It costs one contiguous copy of a 160x160x3 tensor per frame.

WHAT IT WRITES, AND WHAT TO DO WITH IT
--------------------------------------
It writes ``backend/models/facenet512.onnx`` by default and prints the digest of the result, for
``FACENET_SHADOW_MODEL_SHA256``. Pinning that setting is optional but is the same choice the
incumbent's graph offers: it makes the server refuse a file that is not the one that was measured,
which is what stops a re-fetch from silently becoming a re-baseline.

    python tools/fetch_shadow_graph.py --source <cached copy>      # convert something already on disk
    python tools/fetch_shadow_graph.py                             # fetch the pinned source, then convert

Pass ``--source`` to skip the network: a host that already has the raw export (or one that has no
egress and fetches it out of band) gets the identical result, because the same digest is checked
either way. Nothing here touches a database, a template or a frame.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
import urllib.request
from pathlib import Path

import onnx
from onnx import TensorProto, helper

#: The published FaceNet-512 export, and the digest of *that file* rather than of what this tool
#: writes - so a re-fetch is checked against the artifact it was derived from.
SOURCE_URL = "https://huggingface.co/haikalmumtaz/facenet-onnx/resolve/main/facenet.onnx"
SOURCE_SHA256 = "26206b0f5930fd8b368b0e3430e20e07c81af57e386cc6856654c18d0e88f8ab"

#: ``backend/models/facenet512.onnx`` - the path ``shadow_rollout.DEFAULT_SHADOW_GRAPH`` and
#: ``config.settings.facenet_shadow_model_path`` already point at, so no setting has to change.
DEFAULT_OUTPUT = Path("backend/models/facenet512.onnx")

#: The one-channel-order wrapper's input name. The incumbent's graph names its input ``input``, and
#: keeping that name on the *transposed* tensor leaves every node in the exported graph untouched.
WRAPPER_INPUT = "input_nhwc"


class FetchError(RuntimeError):
    """The artifact could not be obtained, or is not a graph this tool can wrap."""


def sha256_of(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def fetch(source: str, *, url: str = SOURCE_URL, expected: str = SOURCE_SHA256) -> Path:
    """Download the pinned export to ``source``, unless a matching copy is already there.

    The digest is checked before the file is used and again by the caller, because the two failures
    it separates matter: a truncated download is "fetch it again", and a *different* file is "the
    upstream artifact changed", and only the second one makes the recorded band meaningless.
    """
    target = Path(source)
    expected = (expected or "").lower()
    if target.exists():
        actual = sha256_of(target)
        if not expected or actual == expected:
            return target
        raise FetchError(
            f"{target} is not the pinned artifact: sha256 {actual} but expected {expected}. "
            "Delete it and fetch again if that was a partial download."
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    print(f"fetching {url}", file=sys.stderr)
    try:
        with urllib.request.urlopen(url, timeout=300) as response:  # noqa: S310 - a pinned https URL
            payload = response.read()
    except Exception as exc:  # noqa: BLE001 - one error type out of the tool
        raise FetchError(f"could not download {url}: {type(exc).__name__}: {exc}") from exc
    actual = hashlib.sha256(payload).hexdigest()
    if expected and actual != expected:
        raise FetchError(
            f"downloaded artifact is sha256 {actual} but the pinned source is {expected}; the "
            "upstream file changed, so the band measured against it no longer applies"
        )
    target.write_bytes(payload)
    return target


def describe(path: str | Path) -> dict[str, object]:
    """The input and output the graph *resolves to at runtime*, which is what the app will see.

    Read through ONNX Runtime rather than off the protobuf on purpose. The published export names
    its embedding dimension symbolically (``Divembedding_dim_1``), so the file itself does not
    state the width that ``FaceNetORT`` reads - the runtime resolves it to 512 during shape
    inference, and a tool that reported the protobuf would call a perfectly loadable graph "has no
    static embedding width". Describing it the way the engine describes it is the only answer that
    predicts whether the migration starts.
    """
    import onnxruntime as ort

    try:
        model = onnx.load(str(path))
    except Exception as exc:  # noqa: BLE001 - a file that is not a model is a refusal, not a crash
        raise FetchError(
            f"could not read {Path(path).name} as an ONNX model: {type(exc).__name__}: {exc}"
        ) from exc
    if len(model.graph.input) != 1:
        raise FetchError(f"expected one graph input, found {len(model.graph.input)}")
    try:
        session = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
    except Exception as exc:  # noqa: BLE001 - an unloadable graph is the tool's normal refusal
        raise FetchError(f"could not open {Path(path).name} with onnxruntime: {exc}") from exc
    input_shape = [dim if isinstance(dim, int) else str(dim) for dim in session.get_inputs()[0].shape]
    output_shape = [dim if isinstance(dim, int) else str(dim) for dim in session.get_outputs()[0].shape]
    if len(input_shape) != 4:
        raise FetchError(f"expected a 4-D input, got {input_shape}")
    if input_shape[1] == 3:
        layout = "NCHW"
        size = int(input_shape[2])
    elif input_shape[-1] == 3:
        layout = "NHWC"
        size = int(input_shape[1])
    else:
        raise FetchError(f"input {input_shape} is neither NHWC nor NCHW with 3 channels")
    if not isinstance(output_shape[-1], int):
        raise FetchError(
            f"output {output_shape} has no static embedding width; the migration cannot name a "
            "width for a graph that never resolves one"
        )
    return {
        "input_name": session.get_inputs()[0].name,
        "input_shape": input_shape,
        "layout": layout,
        "input_size": size,
        "output_shape": output_shape,
        "width": int(output_shape[-1]),
    }


def to_nhwc(source: str | Path, destination: str | Path, *, input_size: int | None = None) -> dict[str, object]:
    """Write ``source`` as a graph whose input is NHWC ``(N, S, S, 3)`` and output is ``(N, width)``.

    A specific ``input_size`` is used when the source declares its canvas dynamically; the published
    export does not, but a wrapper that hard-codes 160 into a graph that never said so would be
    inventing a fact, so the parameter exists to make that choice explicit rather than implicit.
    """
    facts = describe(source)
    if facts["layout"] == "NHWC":
        # Already the shared tensor's order: the file is copied through rather than rewritten, so
        # the digest an operator pins is the digest of the artifact that was measured.
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(Path(source).read_bytes())
        facts["wrapped"] = False
        return facts
    size = int(input_size or facts["input_size"])
    declared = facts["input_shape"][0]
    batch = declared if isinstance(declared, str) else int(declared or 1)
    model = onnx.load(str(source))
    graph = model.graph
    original_input = graph.input[0].name
    del graph.input[:]
    graph.input.extend(
        [
            helper.make_tensor_value_info(
                WRAPPER_INPUT, TensorProto.FLOAT, [batch, size, size, 3]
            )
        ]
    )
    # The transposed tensor keeps the exported graph's own input name, so not one node below it has
    # to be rewritten - which is also why this cannot silently invalidate an initializer.
    graph.node.insert(0, helper.make_node("Transpose", [WRAPPER_INPUT], [original_input], perm=[0, 3, 1, 2]))
    onnx.checker.check_model(model)
    Path(destination).parent.mkdir(parents=True, exist_ok=True)
    onnx.save(model, str(destination))
    facts.update({"wrapped": True, "input_size": size, "input_name": WRAPPER_INPUT})
    return facts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", help="a raw export already on disk (skips the download)")
    parser.add_argument("--url", default=SOURCE_URL, help="override the pinned download URL")
    parser.add_argument("--expected-sha256", default=SOURCE_SHA256, help="digest of the raw artifact")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="where to write the drop-in graph")
    parser.add_argument("--input-size", type=int, default=None, help="canvas, for a dynamic-graph source")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)

    try:
        if args.source:
            source = Path(args.source)
            actual = sha256_of(source).lower()
            if args.expected_sha256 and actual != args.expected_sha256.lower():
                raise FetchError(
                    f"{source} is sha256 {actual} but the pinned source is {args.expected_sha256}; "
                    "pass --expected-sha256 to convert a different export deliberately"
                )
            report = to_nhwc(source, args.output, input_size=args.input_size)
            source_name, source_sha = str(source), actual
            source_facts = report
        else:
            # The raw export is downloaded, wrapped and discarded: it is only ever an intermediate,
            # and keeping a second 90 MiB blob beside the drop-in graph would be the kind of file
            # nobody knows the owner of. Use --source to convert an out-of-band copy instead.
            with tempfile.TemporaryDirectory() as staging:
                source = fetch(str(Path(staging) / "facenet.onnx"), url=args.url, expected=args.expected_sha256)
                report = to_nhwc(source, args.output, input_size=args.input_size)
                source_name, source_sha = args.url, sha256_of(source)
                source_facts = report
        # Described from the file that was actually written, not from the facts the conversion
        # started with: the whole point of the wrapper is that those two differ by a layout.
        written = describe(args.output)
    except FetchError as exc:
        print(f"cannot build the shadow graph: {exc}", file=sys.stderr)
        return 2

    report = {
        "source": source_name,
        "source_sha256": source_sha,
        "source_layout": source_facts["layout"],
        "source_input_shape": source_facts["input_shape"],
        "source_output_shape": source_facts["output_shape"],
        "wrapped": bool(source_facts.get("wrapped")),
        "output": str(args.output),
        "output_sha256": sha256_of(args.output),
        "input_name": written["input_name"],
        "input_shape": written["input_shape"],
        "output_shape": written["output_shape"],
        "width": written["width"],
    }
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
        return 0
    lines = [
        f"source     : {report['source']} ({report['source_layout']}, {report['source_input_shape']})",
        f"source sha : {report['source_sha256']}",
        f"written    : {report['output']}",
        f"input      : {report['input_name']} {report['input_shape']}",
        f"output     : {report['output_shape']} ({report['width']}-D)",
        f"output sha : {report['output_sha256']}",
        "",
        "pin it with FACENET_SHADOW_MODEL_SHA256=" + str(report["output_sha256"]),
        "then check the migration can start: python tools/shadow_migration.py status",
    ]
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
