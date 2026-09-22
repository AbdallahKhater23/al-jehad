# Face models

Two quite different artefacts live behind this directory, and they are treated differently
on purpose.

## 1. Face detector — YuNet (committed, 232 KB)

```
backend/models/face_detection_yunet_2023mar.onnx
```

**This one is in the repository**, and `.gitignore` carries an explicit exception for it.
The `*.onnx` rule below exists for large weights; 232 KB is not that, and the difference it
makes is the difference between a 10 ms and a 308 ms detection on *every punch* — so it
ships with the code rather than behind a deploy step that can be forgotten. Measured at the
application's own 640x640 working size, MTCNN 308 ms against YuNet 10 ms, taking the whole
verification call from 537 ms to 209 ms (see `face_detector.py`).

It is OpenCV's own model, downloaded from the OpenCV Zoo:

```
https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx
sha256  8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4
```

`FACE_DETECTOR_MODEL_PATH` relocates it; `FACE_DETECTOR_MODEL_SHA256` makes the server
refuse an unexpected file (the pattern `LIVENESS_MODEL_SHA256` already uses). Both are
reported by `readiness`'s `face_detector` check and by `/api/v1/status/detail`.

**When it is absent** the application still works: verification falls back to DeepFace's
previous detector, thirty times slower on detection and reported as such. Templates written
while the fallback is live are labelled with the fallback pipeline, so they are not later
mistaken for YuNet templates (see `face_detector.PIPELINE`).

**Replacing this file is not a drop-in.** The crop is the contract: swapping the detector -
or its alignment, or the model that embeds the crop - moves every stored template and every
distance, so all three have to move together. Bump `face_detector.PIPELINE`, expect every
enrolled account to be reported for re-enrollment (`/api/v1/admin/enroll/needs_reenrollment`),
and **re-derive the decision lines**: the band that turns a distance into approved / review /
refused is keyed by `(pipeline, model)` in `face_detector.BANDS`, and a build that can run a
pipeline without one fails the startup gate rather than scoring against numbers measured for
another crop. The derivation rule, the measured boundaries it currently rests on and the
method for re-measuring are in `backend/face_detector.py` (the `MatchBand` section) and in the
README's *Where a face match is decided*.

## 2. Liveness model — MiniFASNet (not committed)

Passive anti-spoofing expects a MiniFASNet export here:

```
backend/models/minifasnet.onnx
```

`LIVENESS_MODEL_PATH` overrides the location. The `*.onnx` files are git-ignored: a binary
of that size belongs in release assets or object storage, not in the repository.

### Where the file comes from

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
pip install onnxruntime        # required for the liveness check to run at all
pip install qrcode             # optional: renders the enrollment QR code
pip install openpyxl           # optional: XLSX report export
```

The face detector needs neither: it runs on `cv2.FaceDetectorYN`, which is part of the
OpenCV build the application already requires.

See `requirements-optional.txt`.

## Behaviour when the liveness model is absent

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
curl -s localhost:8000/api/v1/status/detail -H "Authorization: Bearer <admin token>" | jq .face_detector
```

`mode`, `available`, `model_fingerprint` and any load error are all reported there. Pin
the artifact with `LIVENESS_MODEL_SHA256` if you want the server to refuse an
unexpected file.

## Changing the detector invalidates every template

Both of the models above are only half of a template's meaning: the template is an
embedding of the **crop** the detector chose, so swapping the detector changes the crop
(measured IoU 0.814 between MTCNN's box and YuNet's - about 19% different) and every stored
template has to be taken again.

That is why a template records its pipeline and why a mismatch is refused rather than
scored. `biometrics.PIPELINE` is the version, `biometrics.stale_references()` is the
worklist, and a deployment sees the count in the `face_detector` readiness check. The repair
is the enrollment flow that already exists.
