# Liveness model (MiniFASNet)

Passive anti-spoofing expects a MiniFASNet export here:

```
backend/models/minifasnet.onnx
```

`LIVENESS_MODEL_PATH` overrides the location. The `*.onnx` files are git-ignored: a
binary of that size belongs in release assets or object storage, not in the repository.

## Where the file comes from

Any MiniFASNet (Silent-Face-Anti-Spoofing `MiniFASNetV2` / `V3`, 80x80 crop) exported
to ONNX works. The contract the code assumes:

* **input** `float32` NCHW, `1x3x80x80`, RGB, values in `[0, 1]`
  (`LIVENESS_INPUT_SIZE` changes the spatial size only);
* **output** `[live, print_attack, replay_attack]` **logits** (the softmax is applied by
  `liveness.py`), or two logits `[live, spoof]`, or a single genuine-probability/logit.

If a model was trained on OpenCV crops it expects BGR, and the symptom is a model that
rejects everything - swap the channel order in `liveness.preprocess`.

## Dependencies

```bash
pip install onnxruntime        # required for the check to run at all
pip install qrcode             # optional: renders the enrollment QR code
pip install openpyxl           # optional: XLSX report export
```

See `requirements-optional.txt`.

## Behaviour when the model is absent

The check reports `available=false` and the API keeps working. It is **not** treated as
"genuine":

| `LIVENESS_MODE` | No model |
| --- | --- |
| `off` | check skipped |
| `advisory` (default) | recorded as `unavailable`, punch proceeds, administrator notified |
| `enforce` | punch refused with `liveness_unavailable`, and the startup gate reports a **fatal** misconfiguration |

Verify a deployment with:

```bash
curl -s localhost:8000/api/v1/status/detail -H "Authorization: Bearer <admin token>" | jq .liveness
```

`mode`, `available`, `model_fingerprint` and any load error are all reported there. Pin
the artifact with `LIVENESS_MODEL_SHA256` if you want the server to refuse an
unexpected file.
