# Runbook: deriving the face-match band

**Symptom.** The deployment refuses to serve. `GET /admin/readiness` reports one **fatal**
check, and every request that needs a verdict is refused:

```
face_match_band  fatal  ok=false
no face-match thresholds have been derived for pipeline 'yunet-2023mar' with model 'Facenet';
measure the new crop and add its band before scoring anything
```

This is not a bug and not a missing environment variable. It is the one face check that
**cannot** degrade: a distance (0.31) has no meaning without a line, and the line only means
something for the crop *and* the embedding model that produced both vectors. A build that can
crop and embed but has never measured a line has exactly two wrong answers available -
approve against numbers measured for a different crop, or refuse everybody - so it refuses to
open the port instead.

## Why there is no default band

There is no fallback line, and there is not going to be one. The table used to hold two bands
measured for VGG-Face (4096-dim, DeepFace preprocessing). This build embeds FaceNet-128 through
ONNX Runtime from a 112x112 YuNet-aligned crop, which is a different vector space; the two
preprocessing contracts alone differ by a cosine of ~0.48 on the same photograph, which is
larger than the approve line those bands were built around. Reusing them would apply thresholds
nobody measured to vectors nobody measured them for.

So the table holds **only measured lines**. Today that is one band - `yunet-2023mar` / `Facenet`,
the crop and model this build really runs - and the last section below has what it is and where
it came from. A crop with no entry is never scored against another crop's numbers: `band_for`
raises, and the gate refuses to serve, which is the symptom this page opens with.

The band is keyed by `(pipeline, model)` with the keys **written out** as literals, so bumping a
pipeline name makes the table answer with nothing rather than with the previous crop's lines.
The derivation tool takes the live pipeline's name and no other for the same reason - it embeds
through the live detector, so any other label would be one crop's numbers behind another crop's
name, which is exactly the inheritance above.

## Forcing the deployment up does not fix a punch

`STARTUP_OVERRIDE_REASON` (see `RUNBOOK_STARTUP_OVERRIDE.md`) gets the process listening, and
that is all it does. The verdict path resolves the band per punch
(`face_detector.band_for(biometrics.current_pipeline())`), so with no band every punch fails
with `UnknownPipelineError` - today as an unhandled 500 in the punch route. Use the override
to inspect the deployment, never to run a site on it.

## 1. Build a corpus that can support a line

Identity is a directory: one folder per person, every image in it a capture of that person.

```
corpus/
  ahmed/    1.jpg  2.jpg  3.jpg
  bilal/    1.jpg  2.jpg
  carol/    1.jpg  2.jpg
```

A flat folder also works when the identity is in the filename before a trailing number
(`ahmed_1.jpg`, `bilal-2.jpg`). Subdirectories win when both are present.

An image is **usable** only if the application's own detector finds **exactly one face** in
it, and that face is at least **160 px** on its shorter side (the engine upscales its crop to
160 px, and a smaller face would be embedded as interpolation). A group photo, a screenshot
with a face in the corner, and a 120 px profile picture are all discarded, with the reason
printed - that is not the tool being fussy, it is the operating condition being enforced.

What the corpus has to contain:

| | minimum | comfortable |
|---|---|---|
| usable captures per identity | 2 | 5+ |
| identities | 3 | 20+ |
| different-people pairs | 100 (`--min-impostor-pairs`) | 300 |

**The captures must resemble the gate, not a photo album.** The genuine ceiling is the highest
distance between two captures *of one person*; the more a corpus mixes years, cameras,
lighting and pose, the higher that ceiling climbs, and the narrower the window gets. Enrollment
photo vs. gate selfie of the same worker under the same light is the pair the lines have to
separate. Public-figure portraits are a poor calibration set for exactly that reason.

## 2. Measure

```bash
cd backend
venv/Scripts/python.exe tools/derive_facenet_band.py --corpus ./corpus          # prints a MatchBand(...)
venv/Scripts/python.exe tools/derive_facenet_band.py --corpus ./corpus --json band.json
```

Flags that matter:

* `--pipeline KEY` names the key the band is emitted under, and it must be the pipeline this
  build really runs (`face_detector.active_pipeline()`: YuNet model file present =
  `yunet-2023mar`, absent = the `mtcnn` fallback). It does **not** choose the crop - the tool
  embeds through the live detector either way - so any other key is refused rather than emitted.
  A band labelled `mtcnn` from a host running YuNet would be the live crop's numbers under the
  fallback's name, and nothing downstream could tell. To measure the fallback, run the tool on a
  host that is *on* the fallback, and install what it prints there.
* `--min-impostor-pairs N` sets the floor on different-people pairs (default 100).
* `--min-face-pixels N` sets the usable-face floor (default: the engine's 160 px input size).

The tool refuses - exit code 2, nothing emitted - when the corpus cannot support a line, and
says which of these it is:

* **no same-person pairs** - one photograph per person cannot produce a genuine ceiling;
* **too few different-people pairs** - the floor would be an anecdote;
* **the distributions overlap** - the highest same-person distance is at or above the lowest
  different-people distance: no line separates them, so this is a statement about the corpus or
  the crop, and either way it needs a human;
* **the two decision lines cannot be ordered inside the window** - the window separates, but
  not far enough for the review band. The rule needs the impostor floor to be at least
  `IMPOSTOR_MARGIN²` = **1.82x** the genuine ceiling; below that, the review line lands at or
  below the approve line and no capture could ever be routed to a human. This one is **not** a
  refusal: the tool emits the one-line band the rule produces and says so (see below);
* **`--pipeline` naming a crop it did not measure** - the label is a claim about which detector
  produced the vectors, and this tool has exactly one detector.

## 3. Install it

Paste the `MatchBand(...)` the tool prints into `face_detector.BANDS`, under the pipeline name
it was measured for:

```python
BANDS: dict[str, MatchBand] = {
    "yunet-2023mar": MatchBand(
        model="Facenet",
        approve=0.52,
        review=0.60,
        genuine_ceiling=0.31,
        impostor_floor=0.67,
        evidence=("Measured in this build's own vector space on ... "),
    ),
}
```

Then, in this order:

1. `venv/Scripts/python.exe -m pytest tests/test_face_match_bands.py -q` - the suite
   re-derives every line from the boundaries it carries, so a hand-edited number, a line moved
   without its measurement, or a collapsed window fails here rather than at a gate.
2. **Re-enroll every account** if the crop or the model moved:
   `GET /api/v1/admin/enroll/needs_reenrollment` lists who, and their templates are stale
   until they are re-enrolled - a stale template is not scored at all.
3. Confirm: `GET /admin/readiness` reports `face_match_band` **ok**, with the derivation in its
   detail and `approve` / `review` / `genuine_ceiling` / `impostor_floor` in its value. An
   operator whose review queue moves wants those four numbers, not the verdict.

## What this checkout measured, and what it installed

On 2026-09-22 the tool was run here against `temp/faces` (12 public figures, 48 images, 21
usable after the single-face and 160 px rules, 10 identities):

| | measured |
|---|---|
| genuine (same person), 14 pairs | 0.142 - 0.496 |
| impostor (different people), 196 pairs | 0.538 - 1.291 |
| window | 1.08x (`0.538 / 0.496`) against the **1.82x** the rule needs |
| band | `approve = review = 0.50` - one line, through the clamp described below |

The corpus cannot support a **two-line** band, and the numbers say why: its same-person
captures differ nearly as much as its different-people captures separate. What the rule
produces from that window is a one-line band, and that is what `face_detector.BANDS` holds, so
this deployment serves. Two separate questions remain open, and they are different work:

* **the corpus** - public portraits are heterogeneous and mostly too small (27 of 48 images
  were skipped); a deployment corpus (gate selfies, one camera, one session, several captures
  per worker) is the calibration the tool is asking for;
* **the pipeline** - genuine pairs of the same person sitting as high as 0.50 is poor for a
  FaceNet embedding, so before trusting a wider corpus it is worth confirming the crop and the
  ONNX preprocessing contract (input size, colour order, value range) against the model file
  that is actually loaded. A model swap is not a drop-in: `face_detector.PIPELINE` and the band
  move with it.

### What the one-line band means here

A band with `approve == review` is a legal two-tier decision - accept at or below the line,
refuse above it - and it is the shape the rule produces from this window: 0.50 sits above the
0.496 ceiling and below the 0.538 floor. It means **no capture is ever routed to a human**: an
ambiguous punch is refused at the gate and the worker retries. Nothing else in the deployment
changes, and a site whose workers keep their enrolled look will rarely meet the line at all -
but an operator should not have to infer that from a review queue that never fills.

Three things make it a stated configuration rather than an accident, and all three are in
place:

1. `MatchBand.derived()` clamps the review line up to the approve line and says so in `basis()`
   ("no review window"), instead of returning an unordered pair that `classify` would read as
   an inverted band;
2. `tests/test_face_match_bands.py` holds both shapes: a two-line band is still required to
   route its window to a human, so the clamp cannot quietly swallow a band that has room for
   one, and the table a *deployment* loads is checked against the gate's requirement rather
   than only the fixture the suite scores with;
3. `readiness` reports the derivation in its detail, so the absent review tier is visible from
   `GET /admin/readiness` rather than inferred.

Replacing it with two lines is a re-measurement, not an edit: the corpus has to separate more
widely (the deployment corpus above is the calibration the tool is asking for), and the new
band arrives carrying the boundaries it came from.
