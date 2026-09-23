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

## Deriving the band from live traffic on a volume-backed deployment (Railway and friends)

Sections 1–3 measure on a laptop, from a corpus somebody assembled by hand. A deployed site
can do better: the punch path already knows how to file captures into a labelled corpus
(``corpus.maybe_capture_punch``), so the deployment collects its own calibration while it
runs — the gate selfies the lines actually have to separate. The corpus is opt-in and off by
default.

### 1. Turn capture on — for a stated period

Three environment variables on the service:

```bash
CALIBRATION_CAPTURE_ENABLED=true
CALIBRATION_CAPTURE_UNTIL=2026-10-01T00:00:00Z    # ISO-8601 UTC; empty = no end date
CALIBRATION_CORPUS_DIR=/data/calibration_corpus   # must be ON THE VOLUME
```

``CALIBRATION_CAPTURE_UNTIL`` is the end of the collection period. It is an **instant**, not a
duration, so a redeploy does not restart the clock — and a period with an end is the only shape a
biometric collection should have: "for a week" left as a calendar reminder is a deployment that
keeps collecting faces until somebody remembers. Capture stops by itself at that instant.

A value that cannot be parsed **stops** capture rather than being ignored. The other reading of an
unreadable deadline is "indefinitely", and that is not the reading to pick by accident for a store
of people's faces. Read the state any time from ``GET /api/v1/admin/corpus_consents``, which
answers ``capture_window``:

| ``state`` | meaning |
|---|---|
| ``off`` | the switch is off — nothing is captured |
| ``open`` | inside the window, with ``seconds_remaining`` |
| ``closed`` | the window has passed, **or** the deadline is unreadable |
| ``open_ended`` | on, with no end date — deliberate, and worth a second look |

``/data`` (or wherever the volume is mounted) because a corpus that lives on the container
filesystem is erased by the next deploy. Redeploy once with the variables set. From then on
punches are re-detected once on their downscaled copy and filed — **but only for a worker who
has opted in** (see the consent section below): the deployment switch states that this site
collects at all, and the per-worker opt-in states that this particular person agreed. Frames
from workers who never answered, or who withdrew, are not captured — whatever the switch says.

**approved** punches from a consenting worker carry their worker id as the label automatically
(that verdict established an identity), and **refused** or multi-face captures land under
``_unlabelled`` for a human to decide. A capture is never destroyed by a wrong verdict; it is
only left unlabelled. The consent basis is recorded on every capture as
``worker-granted: per-worker opt-in, audited in corpus_capture_consents`` — the older basis
(``deployment: CALIBRATION_CAPTURE_ENABLED with worker notice``) appears only on captures taken
before per-worker consent existed, so history reads as what it was.

### 1b. The per-worker consent (required for any capture at all)

A worker opts in — or out — in their own app, which records the decision in two places at once:

* ``corpus_capture_consents`` (append-only at the database level): one row per decision, with
  the direction, an optional note, the timestamp, the actor and the request's IP. The newest
  row per worker is the state; nothing in the table can be updated or deleted, so the history
  is evidence rather than a current value.
* ``audit_log``: the same decision as ``corpus_capture_consent``, with the actor and
  provenance the audit trail already carries.

The worker's way in is a card on their own **profile tab** ("Photos for face matching"), which
reads the state from this endpoint and writes it back through the same one. It is the only path
that has to exist: an operator cannot agree on a worker's behalf, and the card offers no control at
all until the server's answer has landed — a consent button whose write the page cannot prove it can
make is a button that records nothing.

The endpoints, both for the signed-in worker themselves:

```bash
# grant (the worker's own action in the app):
POST /api/v1/worker/me/corpus/consent   {"granted": true, "note": "optional context"}
# withdraw - same endpoint, no administrator in the loop, on purpose:
POST /api/v1/worker/me/corpus/consent   {"granted": false, "note": "changed my mind"}
# what they agreed to, and the full history of their own decisions:
GET  /api/v1/worker/me/corpus/consent
```

The operator's view — who has consented, and every decision written — is
``GET /api/v1/admin/corpus_consents``. The consented set is exactly the coverage a derivation
can plan around; there is no way to capture outside it.

**Withdrawal is about the future.** Captures already taken stay in the corpus until
``purge`` removes them (per identity, on request) or retention ages them out; a worker who
asks "delete my face" is answered by ``tools/corpus_admin.py purge --identity <id>``, and the
withdrawal record is what makes that request auditable. Consent and erasure are deliberately
different operations with different records.

### 1c. The frames a week of punches will actually add

A corpus built from *successful* punches cannot hold the small-face regime, and no switch changes
that. Two reasons, both now fixed:

* **The call site.** A worker far enough back that the detector cannot see them is refused at the
  face check, and that refusal used to happen *before* the capture hook — so the exact frame class
  the coverage question is about was discarded by control flow. The refusal path now calls in with
  the refusal reason recorded, which is what puts a distant frame in the corpus at all.
* **The reach.** The capture re-detects on its own stored copy, and it used to do so at the punch
  pipeline's own input size. The reach that refused the frame is therefore the reach that could not
  store it either, so only faces the punch had already found were ever filed. When the pipeline's
  pass yields no single usable face, the capture looks again at twice the reach
  (``corpus.WIDENED_REACH_INPUT_SIZE``) and files the specimen it finds there — with the provenance
  saying which pass found it (``input_size`` on the detector fingerprint, ``reach=widened`` in the
  note), because "the punch detector saw this" and "only the wide pass saw this" are different
  claims about the same corpus.

**For those frames to exist, workers have to punch from further back.** The framing coach accepts the
whole 0.7–1.5 m band and warns below about 1.6 m; a worker who stands well back and is refused is
now producing usable evidence rather than a lost frame. That is the point of the week: the corpus's
value is the small-face regime, and the only way it appears is that somebody stands in it.

Refused captures stay **unlabelled**. ``approved`` is the only verdict that established an identity;
a refusal is the absence of that claim, so naming the punching worker would be the corpus inventing
a label out of a successful login. Label one by hand only when the frame really shows that worker.

### 2. Collect, then label what a human must decide

Let the site run for a few days of normal punches — for a distance experiment, stand workers further
back than the coach asks for, so the refused frames exist at all. Then look at what gathered (run
these in the Railway shell, ``python`` is on the image's ``PATH``):

```bash
python backend/tools/corpus_admin.py stats                # what the corpus holds, in band terms
python backend/tools/corpus_admin.py list --unlabelled    # the captures nobody has decided about
python backend/tools/corpus_admin.py label --capture <id> --identity <worker_id>
```

Label a refused capture only when you can see from the frame (``punch_frames`` stores the same
punch as evidence) that it really is that worker. A label you are unsure about is the one the
unlabelled partition exists to hold. The derivation reads labelled identities only, so

### 2b. Re-run the coverage sweep on what the week collected

This is the measurement the week exists for: the same corpus, every detector configuration, no
storage and no second copy of anybody's face on disk.

```bash
python backend/tools/coverage_sweep.py --corpus-store --json /data/coverage-after.json
```

The report answers, per configuration (``yunet@320``, ``yunet@640``, ``yunet@640+2x2tiling``, and
SCRFD when a model is present): how many real faces each one **found**, how many it **discarded and
why** (``no_face``, ``many_faces``, or a gate reason such as ``quality:too_small``), what each cost
in milliseconds, and the **flip table** — the frames one configuration recovers that another loses,
which is the only honest way to price a detector change on this deployment's own traffic.

Read the two defaults deliberately: ``--exclude-hard-cases`` and ``--exclude-unlabelled`` are both
**off**, because the flagged frames are precisely the ones whose recoverability the sweep is about.
Turning them on answers a different question ("how does the pipeline do on frames it already likes")
and will make every configuration look equally good.

Compare against the baseline in ``docs/RUNBOOK_EMBEDDER_MIGRATION.md`` — the sweep run before the
week, on the frames that existed then — and treat a configuration as an upgrade candidate only if it
wins on *this* corpus, not on the published numbers.


uncertainty here costs coverage, never correctness.

### 3. Export and measure

```bash
python backend/tools/corpus_admin.py export --destination /tmp/corpus-export
python backend/tools/derive_facenet_band.py --corpus /tmp/corpus-export --json /tmp/band.json
```

The tool embeds through the deployment's own live pipeline, so what it prints is measured in
exactly the vector space the punches run in. It still refuses — exit code 2 — when the corpus
cannot support a line (too few identities, overlapping distributions); ``stats`` says which.
A window too narrow for two lines prints the one-line band and says so, like the current one.

### 4. Install it

The band is **code**, not configuration — there is no environment variable that injects one,
deliberately, because an unmeasured override is the failure this whole design exists to
prevent. So: copy the printed ``MatchBand(...)`` into ``face_detector.BANDS`` in the checkout
(replacing the provisional entry), run
``python -m pytest backend/tests/test_face_match_bands.py -q`` locally, commit and push. The
redeploy is the rollout. The evidence string travels with the band, so the next reader can
see which corpus produced it and when.

### 5. Housekeeping

The corpus is deliberately **not** swept by the retention timer (see ``corpus.py``). It is
the operator's store:

```bash
python backend/tools/corpus_admin.py purge --older-than-days 180   # dry run by default
python backend/tools/corpus_admin.py purge --identity <id> --apply # an erasure request
```

When the new band is installed and serving, turn ``CALIBRATION_CAPTURE_ENABLED`` back off —
the corpus keeps whatever it gathered, and a later re-measurement starts from it rather than
from nothing.

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
