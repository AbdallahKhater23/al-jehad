# Runbook: making faces reach, and moving the embedder without a cutover

Three defects sit between this deployment and a face check that works at two metres, and they act
on **two different things**. Fixing the wrong one first is the standard mistake, so this runbook
states the split before it states any command:

| what you are trying to move | what moves it | phase |
| --- | --- | --- |
| **coverage** - can the detector even localise the face? | detector input size, tiling, and a single-resample alignment | 1 |
| **separation** - given a crop, can a threshold decide? | the input contract (channel order, value range), host-side L2, and a recalibrated band | 2 |
| the **tier** - is there room for an approve line and a review line? | the measured `floor/ceiling` ratio against `IMPOSTOR_MARGIN^2` = 1.82x | 3 |

Widening the detector will not widen a 1.08x window, and fixing the contract will not let a person
at 2 m be detected. They are separate experiments with separate evidence, and the third phase is a
*migration instrument* rather than a fix - the 128-to-512 change is cheap and should be made while
the first two are being measured, under a shadow that cannot affect a punch.

The band itself has its own runbook: [deriving the face-match band](RUNBOOK_FACE_BAND.md). This one
is about the pipeline that feeds it.

## What is already in the tree

| module | what it owns |
| --- | --- |
| `backend/corpus.py` | the labelled corpus: capture, label, export, statistics, erasure, and the live punch hook |
| `backend/tools/corpus_admin.py` | the operator command for it (not `corpus.py`: every tool here puts its own directory on `sys.path`, and a tool called `corpus.py` would shadow the store for the punch path) |
| `backend/detector_640.py` | resolution-correct localisation: an invertible `Letterbox`, YuNet at 640 (square or aspect-matched), an optional `tiles x tiles` pass with greedy NMS merging, and a `ScrfdDetector` for extreme range (reads both SCRFD export conventions, decodes on a square canvas) |
| `backend/face_align.py` | one-resample alignment: a closed-form Umeyama similarity transform from native-frame landmarks straight onto the 160x160 template, plus the four input `Contract`s |
| `backend/facenet_ort.py` | the ORT session (IOBinding, thread tuning, warmup) and **host-side L2** on the raw output |
| `backend/calibration.py` | the band rule, its identity-cluster bootstrap, and the refusal when no line can separate the distributions |
| `backend/shadow.py` | `ShadowScorer` - the enforced encoder plus a shadow, paired logging in SQLite, and the cutover gate |
| `backend/tools/contract_ab.py` | the contract experiment: all four permutations on one shared crop set |

None of these is wired into the punch path. They are parallel, drop-in modules on purpose: the
rollout below is an A/B, and an A/B needs both sides to be runnable.

## Phase 0 - a corpus that can carry a measurement

Neither of the next two phases can be run on synthetic frames, and a corpus assembled from whatever was
lying around is worse than none: a band derived from it is authoritative-looking and unmeasured. So the
first artefact of this rollout is a corpus of **real captures, with the crop that was measured on them**.

Three ways in, one pipeline. The commands below are all thin fronts for `corpus_ingest.py` - the
importer, the camera puller and the live punch hook run the *same* detector, the *same* quality judgment
and the *same* store, so there is no second definition of "a usable capture" to drift:

```bash
# (a) a live gate, sampled: detect on the stored copy, judge quality, file under the identity
venv/Scripts/python.exe tools/corpus_admin.py ingest --camera rtsp://10.0.0.9/gate \
    --camera-name gate-north --identity W-1042 --consent "deployment notice 2026-08" \
    --actor "R. Ops" --limit 60 --every 25 --coverage --expected-face-px 40

# (b) enrolment: bind a session's frames to a worker id that exists in the roster
venv/Scripts/python.exe tools/corpus_admin.py enroll --identity W-1042 \
    --source ./session-2026-09-22 --check-roster \
    --consent "signed form 2026-09-22" --actor "R. Ops"

# (c) a controlled sitting: the folder is the material, and the gate is permissive
venv/Scripts/python.exe tools/corpus_admin.py add --source ./lab-2026-09 \
    --consent "staff calibration session, signed form 2026-09-22" --actor "R. Ops" \
    --input-size 640 --tiles 2

venv/Scripts/python.exe tools/corpus_admin.py label --all-unlabelled --identity "W-1042"
venv/Scripts/python.exe tools/corpus_admin.py stats
```

The punch path can also contribute, and it is **off** unless a deployment turns it on
(`CALIBRATION_CAPTURE_ENABLED=1`); it never fails a punch and never takes one over.

The store is a directory, one partition per identity:

```
<corpus_root>/
  W-1042/                         identity is the directory, so an operator sees the corpus
    20260922T204113_9f3a2b1c.jpg    the frame, capped at CALIBRATION_CORPUS_MAX_PX
    20260922T204113_9f3a2b1c.json   crop, landmarks (stored-image pixels), detector, camera, quality
  _unlabelled/                    captures nobody has decided about yet - a state, not a gap
```

The name is `<timestamp>_<random>`: sortable, and it does not encode who the person is - a countable
filename for a face is a directory anybody can enumerate. `label` **moves** the pair between partitions
(copy, then remove, so a crash leaves a visible duplicate rather than an image with no provenance).

Six things about that store decide whether it is usable later, and each is enforced rather than
recommended:

1. **Only a verified capture carries a label.** A punch the band approved is a capture whose identity the
   system established; a flagged or refused one lands **unlabelled**, visible in `list --unlabelled` and
   in `stats`, for a human to decide. A corpus of assumed labels inherits the assumption into every
   number derived from it.
2. **The crop travels with the detector that made it** - model digest, input size, square, tiles,
   overlap. Both tools verify it and **refuse** a mismatch rather than re-detecting, because the
   alternative is a report carrying one configuration's name over another's pixels. `--landmarks detect`
   is how an operator says "I mean to measure a new crop on this corpus".
3. **Landmarks are in the stored image's coordinates**, and both face sizes are recorded (`face_px` and
   `native_face_px`), so a corpus capped at 1280 cannot quietly become a corpus of small faces.
4. **A basis is recorded per capture**, and erasure is the operator's explicit command - the corpus is
   *not* swept by `retention`, by design (a corpus is calibration material, not attendance data, so it
   has its own switch, its own reader and its own purge):

```bash
venv/Scripts/python.exe tools/corpus_admin.py purge --older-than-days 180   # previews
venv/Scripts/python.exe tools/corpus_admin.py purge --identity "W-1042" --apply
```

5. **Quality has two lines, not one.** *Discard* is where a capture is evidence of nothing - no face, or
   one so small or so far off-angle that the template it would produce is not a picture of anybody.
   *Flag* is where it stops being typical: kept, marked, and **left out of a measurement by default**.
   This is deliberate, because a coverage experiment needs exactly the frames a band must not be fitted
   to. `stats` and `export` report what they left behind, so a run of 200 images cannot quietly be 180:

```bash
venv/Scripts/python.exe tools/corpus_admin.py stats      # measured N of M; hard cases left out: K
venv/Scripts/python.exe tools/corpus_admin.py export --destination ./corpus-export
venv/Scripts/python.exe tools/corpus_admin.py export --destination ./corpus-edge --include-hard-cases
```

   Thresholds live in `corpus_ingest.GateConfig`, and every capture records the gate that judged it, so a
   stored corpus can be re-read under a different policy later. Roll is a real measurement; yaw and pitch
   are **stated proxies** measured against the alignment template's own pose - see the module docstring
   for exactly what they do and do not mean, because "severe pitch" is not a claim about degrees.

6. **Coverage is checked before anything is derived from the corpus.** The failure mode of building one is
   quietly collecting the staff who happened to walk close to the camera:

```bash
venv/Scripts/python.exe tools/corpus_admin.py ingest --source ./gate-dump --coverage --expected-face-px 40
#   coverage verdict: thin: fewer than 20 captures at or below the expected face size - the corpus is
#   mostly the easy regime, so a band derived from it will look better than the gate does
```

   The same sentence is why the detector's own reach is worth printing next to the corpus: "we have no
   small faces" and "our detector cannot see small faces" are different findings with the same symptom.

`stats` is the gate before a measurement, and it answers the questions that decide whether a run is
worth starting: how many **different-people pairs** exist (a floor needs at least 100), how much of the
corpus is in the small-face regime the failures are at, and whether more than one crop configuration is
mixed in. Then `export` writes the folder contract the two tools read, and the same crops come back out:

```bash
venv/Scripts/python.exe tools/corpus_admin.py export --destination ./corpus-export
venv/Scripts/python.exe tools/contract_ab.py --corpus ./corpus-export --landmarks stored
```

**What this does not solve.** The corpus is a biometric store: it needs a stated basis, a retention
period, and a per-identity erasure when a person asks - `purge --identity` is that last one, and it is
an operator's obligation rather than an automated one. The runbook that says so is this paragraph; the
mechanism that makes it possible is the command above.

## Phase 1 - coverage

**The arithmetic first**, because it decides the configuration. A face is found when its
appearance *inside a detector window* clears the anchor floor (~16 px is a marginal cell, ~32 px
gives usable landmarks). Measured with this module's own geometry:

| frame | pass | window scale | smallest native face at a 16 px floor |
| --- | --- | --- | --- |
| 1280x720 | 1 tile | 0.500 | 32 px |
| 1280x720 | 2x2 grid | 0.909 (worst window) | 17.6 px |
| 1920x1080 | 1 tile | 0.333 | 48 px |
| 1920x1080 | 2x2 grid | 0.606 (worst window) | 26 px |

A 320x320 detector - the shape this defect was diagnosed from - scales 60 native pixels to 15, which
is below stride. Note the second row: a 2x2 grid multiplies subject scale by **1.82, not 2**, because
each window is letterboxed into the same canvas. Any reach figure computed as `scale x tiles` is
about 10 % optimistic, in the direction that leaves the person at the far end of the room out.

```python
detect = detector_640.build_detector(
    "yunet", backend / "models" / "face_detection_yunet_2023mar.onnx",
    input_size=640, tiles=2, overlap=0.2,        # the policy is bound here, not at the call site
)
faces = detect(frame)                            # native coordinates, letterbox already inverted
crop = face_align.align_to_size(frame, faces[0].landmarks, size=160)
```

Two decisions worth stating rather than discovering:

* **Aspect-matched beats square where the runtime allows it.** `square=False` sends 640x360 for a
  16:9 frame; `square=True` pads to 640x640 and spends 44 % of the input on padding. Choose
  `square=True` only when a fixed-shape engine (a TensorRT profile) requires it.
* **Cost is linear in windows.** Measured on this box with YuNet at 640, one 1280x720 frame:
  22 ms at `tiles=1`, 44 ms at `tiles=2` (4 windows), 154 ms at `tiles=4` (16 windows). Tile only the
  frames that need it - a gate camera whose subjects stand close does not.**The detector is not the whole of coverage.** A crop upscaled from 112x112 to the graph's 160x160
input carries interpolation blur that no threshold absorbs, which is why Phase 2 aligns *directly*
onto the 160 template: one resample, ever.

### Measure the coverage on your own frames

The arithmetic above is generic; your gate is not. Before choosing a configuration, run the sweep:
one folder of real frames through **four** configurations — 320, 640, 640+2x2 tiling, and **SCRFD
at 640** — with every miss named by its reason (`no_face`, `many_faces`, a quality discard) and a
flip table showing exactly which frames each step recovers. Read-only — it stores nothing and needs
no consent, because deciding to collect is the decision it informs.

The first three hold the network fixed (YuNet) and vary input size and tiling, which is the dial
this deployment controls. The fourth is the detector *comparison*: same corpus, same frames, same
score threshold, a different network. It is there because the published detection-rate tables for
SCRFD were measured on somebody else's images — whether its stride-8 and -16 heads find more of
*our* workers, at *our* gate exposures, is a measurement and not a citation. It is compared at 640
untiled so the step changes one thing: tiling SCRFD as well would measure two changes and answer
neither question.

SCRFD needs its own ONNX file, which does not ship with this repository's git history. Put
`scrfd_2.5g_bnkps.onnx` **or** `scrfd_10g_bnkps.onnx` beside the YuNet model (`backend/models/`),
or point `--scrfd-model` at it, or set `SCRFD_MODEL_PATH`:

```bash
# the 2.5G head, 3.3 MB - the one the CPU-only deployment should measure first
curl -L -o backend/models/scrfd_2.5g_bnkps.onnx \
  https://huggingface.co/MonsterMMORPG/files1/resolve/main/scrfd_2.5g_bnkps.onnx

python backend/tools/coverage_sweep.py --corpus /data/sweep-frames --json /tmp/sweep.json
```

The two names are searched in that order (2.5G first: a tenth of the size, faster on CPU, and the
reach difference between the two heads is on the order of a few pixels). Either file is measured
under the same spec, and the report's `models` block always names the file that produced the
numbers — so a sweep can never be read as a comparison of a file it did not run. Whichever you
download, check it loads before trusting a report: the two head *conventions* and the canvas
geometry are documented in the `ScrfdDetector` docstring, and a file that parses neither way is
refused by name at construction rather than returning an empty detection list that reads as "no
face in frame".

**SCRFD is decoded on a square canvas, and no other shape will do.** Its published exports lay
their heads out as `(input/stride)**2` cells — 12800, 3200 and 800 at input 640 — and the 10G
file declares those shapes *statically*. Feed it an aspect-fitted canvas (640x360 for a 16:9 frame,
what the ordinary YuNet path produces) and the real grids come out 7200, 1840 and 480, which no
single rounding rule explains: boxes would land in the wrong place with no error to notice. The
frame is therefore letterboxed into a square canvas with padding, the subject keeps its full scale,
and the decoder refuses any other canvas by name.

If that file is absent the run **refuses** rather than quietly printing three lines: a report that
lists three configurations and says nothing about the fourth reads as "the other network was
measured and lost", which is the assumption the fourth configuration exists to replace. To sweep
without it, say so — `--skip-scrfd` — and the report records `scrfd@640: NOT MEASURED` in the
terminal output and in the JSON's `skipped_configs`, so nobody mistakes the artifact for a
comparison.

Read the output in this order: the `found` share per configuration (the headline), the
`misses_by_reason` (a `many_faces` count under tiling usually means background faces entering the
tiled windows — narrow the camera's view or raise the score threshold, it is not a detector-size
verdict), then the flip rows (`320 -> 640: +N recovered` is the upgrade's direct yield on *your*
frames).

### Whose face counts

A `many_faces` count is a statement about a *rule*, not about the detector, and the two questions it
conflates matter differently depending on where the camera points:

* **would the punch path keep this frame?** A clock-in needs one subject — two faces of near-equal
  size is an ambiguity the gate refuses rather than guesses at, so the bystander is correctly a
  `many_faces` loss. This is the default, and it is the rule the deployment actually enforces.
* **how far does this detector reach?** A passer-by does not make the detector worse. Under the
  single-subject rule, though, **tiling is charged for every extra person its own wider field of
  view pulls in** — so the configuration most able to reach a distant face reports the *lowest*
  coverage, and the report says "tiling made things worse" when what happened is that more people
  were in shot.

`--multi-subject` switches to the second question: a frame counts as found when **any** detection
clears the gate. Two things keep it honest. It relaxes the *count* only — every detection still has
to clear the same gate (face size, pose, score), so a distant speck cannot make a frame count as
covered — and the rule is recorded in the report header and the JSON's `subject` field, because a
found-share without its rule beside it is a number that could mean either thing. The shipped gate is
untouched by it.

Run both on the same folder whenever the corpus has bystanders in it. The strict run says what the
pipeline would discard; the multi-subject run says what the detector could see; the gap between them
is the bystander cost of that configuration, which is the number to know before pointing a tiled
detector at a public doorway. Each configuration's line carries the raw count
(`multi-face frames: N (M counted by the any-face rule)`) so the gap is visible without subtracting
shares by hand.

```bash
python backend/tools/coverage_sweep.py --corpus /data/sweep-frames --multi-subject \
    --json /tmp/sweep-reach.json
``` The last flip row is the only one marked `[changes detector: yunet -> scrfd]`: that row is
the family comparison, and the three before it are tuning steps — do not read a tiling gain as
evidence about SCRFD. The report also names the model *files* each family ran with (`models`), and
each configuration's spec carries the file's digest, so "SCRFD found 3 more" stays answerable next
to the weights it was measured with. The full JSON keeps one entry per frame per configuration, so
"which frames flipped" is answerable months later.

Two things about the flip rows worth knowing before reading them:

* **frames are paired by position, not by name.** A corpus is routinely laid out one folder per
  identity (`angela-merkel/01.jpg`, `barack-obama/01.jpg`), where a dozen frames share the name
  `01.jpg`; joining on that name collapsed them and under-reported every flip. The report counts
  the repeats and says so (`note: N frame names repeat across this corpus`), and each row prints
  the file rather than the basename.
* **one score threshold, applied to both families.** `--min-score` (default 0.6) is the gate's
  threshold and the comparison is only fair because both networks answer to it — but the two score
  scales are not calibrated to each other (SCRFD's own default is 0.5). A sensitivity run at 0.5
  is worth ten seconds when a family's number looks like a threshold artefact.

First run on this deployment's own 18 stored punch frames (2026-09): 320 found 18/18, 640 found
18/18, 640+tiling found 3/18 — tiling's wider field of view pulled background people into the
windows, and the single-subject rule reported the ambiguity. On frames this close-in, tiling buys
nothing and costs 6.5x the detector time; the sweep is how you find that out before deploying it.

Read that 3/18 as the *rule* it is, then re-run it with `--multi-subject` before concluding
anything about tiling: if the 15 losses were `many_faces` they were bystanders, and the same
frames may show tiling reaching faces the untiled windows miss. A bystander corpus makes the effect
blunt — on the 48 public-figure portraits in `temp/faces` (2026-09), every one of tiling's 24
losses was a `many_faces` it had itself introduced, and under `--multi-subject` all three
configurations found 48/48: the whole apparent "tiling halves coverage" result was a counting rule,
not a detector. Those portraits are a sanity corpus, not a gate — the point is only that on a
crowded frame the strict count measures the crowd.

#### Which frames to sweep

The corpus that decides the detector question is the deployment's own stored punch frames, on the
Railway volume (`/data/punch_frames`; copy or `tar` a sample out if you would rather sweep it
elsewhere). They are **not** in any checkout, and one trap is worth naming: the repository's own
`punch_frames/` directory holds the *test harness's* output — 240x240 flat placeholders of
`(128, 132, 136)` written by the suite's synthetic punches, with 396 of them byte-identical — so
sweeping it measures nothing but the placeholder. (It looks like data: 1511 files, real JPEGs, real
names. Check the mean and the content hashes before trusting a folder as a corpus.)

Two ways in, and the second is the one to prefer on the deployment:

```bash
# 1. a folder of frames
python backend/tools/coverage_sweep.py --corpus /data/punch_frames --json /tmp/sweep-gate.json

# 2. the calibration corpus, in place - no export step, and no second copy of faces on disk
python backend/tools/coverage_sweep.py --corpus-store --json /tmp/sweep-corpus.json

# inside the service, where the volume is:
railway run python backend/tools/coverage_sweep.py --corpus-store --json /tmp/sweep-corpus.json
```

`--corpus-store` reads the calibration store directly (`<identity>/<capture>.jpg` plus its sidecar
*is* the folder contract, so an export works — but an export is a duplicate of a biometric corpus,
and the sweep is read-only by design). It includes the **hard cases and the unlabelled captures by
default**, which is the opposite of the export's defaults and deliberately so: a band must not be
fitted to flagged frames, but a coverage experiment is about exactly those frames. `--exclude-hard-cases`
and `--exclude-unlabelled` narrow it. With no `--corpus` and no captures, the run **refuses** with
the corpus root and the two variables that switch capture on, rather than printing four configurations
that measured nothing.

Until that run exists, the numbers below are a comparison of the two networks on *photographs* —
useful for validating the plumbing and the decode, not for choosing a detector.

#### First four-configuration run (2026-09, 48 real photographs, `temp/faces`)

Run after the SCRFD file was added, so all four lines are measured rather than three measured and
one assumed. Strict rule, `--min-score 0.6`, times are detector milliseconds for the whole corpus:

| configuration | found | misses | detect time |
|---|---|---|---|
| `yunet@320` | 38/48 (79.2 %) | 10 `many_faces` | 0.9 s |
| `yunet@640` | 37/48 (77.1 %) | 11 `many_faces` | 2.7 s |
| `yunet@640+2x2tiling` | 24/48 (50.0 %) | 24 `many_faces` | 8.1 s |
| **`scrfd@640`** | **38/48 (79.2 %)** | 8 `many_faces`, 2 `quality:extreme_pitch` | 3.1 s |

On this corpus SCRFD is the only configuration that recovers frames the others lose: the
`yunet@640+2x2tiling -> scrfd@640` row reads **+16 recovered, −2 lost (net +14)**, for about the
cost of a single YuNet pass at 640 (64 ms/frame against tiling's 170 ms). Under `--multi-subject`
SCRFD is the one configuration with a genuine miss — 46/48, and both losses are
`quality:extreme_pitch`, i.e. the *gate* refusing the pose rather than the detector failing to find
the face.

Three honest caveats before this is read as a migration decision:

1. **These are portraits, not gate frames.** The corpus that decides this is the deployment's own
   punch frames; see the command below. Public-figure photographs are clean, front-on and
   well-lit — exactly the conditions where a detector comparison looks flattest.
2. **`many_faces` counts are a property of the corpus**, not the network: portraits have crowds in
   them, and one-subject frames lose to bystanders under the strict rule.
3. **A 0.5 threshold run is nearly identical** (37/37/23/36): the ranking is stable, and the
   threshold mostly moves bystander counts, not detector reach.




### The report that keeps itself current

The four-configuration sweep answers "does SCRFD reach more of *our* faces" for the day it is run.
The cameras do not hold still - a site is added, a lamp is replaced, a worker starts parking at the
far end of the yard - so `backend/coverage_report.py` runs the same sweep on a cadence and tells the
administrators when the answer changes. It is the standing version of the command above, not a
second measurement with its own rules: it imports the tool, so the four configurations, the gate and
the verdict are the same object.

**When it measures.** The corpus is the gate's own frame folder (`PUNCH_FRAMES_DIR`, on the volume:
`/data/punch_frames`). One tick every `STANDING_SWEEP_INTERVAL_SECONDS` (default daily) asks two
questions, and needs both:

* has the folder grown by `STANDING_SWEEP_MIN_NEW_FRAMES` frames (default 25) since the last
  measurement?
* has a whole cadence passed since it?

A folder that *shrank* re-baselines instead of blocking the trigger forever - retention erases punch
frames at 30 days by default, and a count that only ever went up would turn a legitimate deletion
into a permanently stuck report. Each measurement reads the newest `STANDING_SWEEP_SAMPLE` frames
(default 200): that cap is the entire cost control, and it is why the report is a sample of the
corpus rather than all of it. The report records both numbers, so a found-share is never read
without the corpus it came from.

```bash
# what it is doing right now, without waiting for the timer
railway run python backend/coverage_report.py            # one tick: decides and maybe measures
railway run python backend/coverage_report.py --force    # measure now, whatever the trigger says
railway run python backend/coverage_report.py --force --sample 500 --json /tmp/sweep.json
```

**What it writes, and where.** `STANDING_SWEEP_DIR` (put it on the volume:
`/data/coverage_reports`) holds `latest.json` (the last verdict plus its full per-frame evidence),
`run-<timestamp>.json` (one snapshot per measurement, newest `STANDING_SWEEP_KEEP` kept) and
`state.json` (the corpus baseline the trigger reads). Nothing here is a database row: the report is
derived evidence, and losing it costs one cadence.

**When it is telling you something.** The verdict is SCRFD against the *best* YuNet configuration
of that run, expressed as a margin **in frames**, because a share difference of 0.4% is not a
migration argument. `STANDING_SWEEP_MIN_MARGIN_FRAMES` (default 1) sets the step that counts as an
overtake; a tie is reported as a tie and is not one.

* **SCRFD becomes ahead** - an administrator notification (`coverage_report`, warning). This is the
  cue to re-read Phase 2 and Phase 3 below rather than to change anything on the spot: it says the
  second network finds more of the frames this deployment actually stores.
* **SCRFD stops being ahead** - the same notification, informational. Both are the same finding:
  the cameras or the corpus moved.
* **A run where SCRFD was not measured** raises nothing, and the readiness check below fails. An
  absent model file must never read as "SCRFD was measured and lost".

Read it live at `GET /api/v1/admin/coverage_report` (administrators only), or the whole evidence at
`latest.json`. The readiness check `coverage_report` is advisory and never fatal: it fails when new
frames are waiting past a cadence plus the startup delay ("the report is not keeping up"), and when
the last measurement has no SCRFD line. It stays quiet for a deployment that has not measured yet,
because the first run deliberately waits past the model preload.

**Running it from cron instead.** `STANDING_SWEEP_ENABLED=0` turns the in-process timer off and
`python -m coverage_report` does one tick from a scheduler - the same entry point, and the readiness
check says the schedule is now somebody else's job rather than reporting a dead timer.

**One honest limit.** On a small corpus a two-frame margin is noise. Below roughly a hundred
sampled frames, read the margin as "asked again, no answer yet" and let the corpus grow before
treating a crossing as a finding; the report prints the sample size beside every share for exactly
this reason.


## Phase 2 - the contract, measured rather than assumed

Nothing in a `.onnx` file says `RGB` or `BGR`, and nothing says `[0, 1]` or `[-1, 1]`. A graph fed
the wrong convention returns a vector of the right shape in the right range, so the defect has no
symptom but a closed separation window. Measure it:

```bash
venv/Scripts/python.exe tools/contract_ab.py --corpus ./calibration --json contracts.json
# a corpus captured by the live pipeline, measured on its own stored crops, with no detection at all:
venv/Scripts/python.exe tools/contract_ab.py --corpus ./corpus-export --landmarks stored \
    --input-size 320 --overlap 0.0
```

It scores **all four permutations on one shared crop set** (detection and alignment run once per
image; only the tensor mapping changes), reports each arm's `floor/ceiling` ratio with its bootstrap
interval, and prints the shift of every arm against the incumbent - a near-constant offset plus a
rank agreement near 1 means the whole separation change is one global input transform, not a
per-identity effect.

Read the output in this order:

1. **the guard rail on the shipped band.** `flip_ready`-style gating aside, the *review tier is
   possible only above 1.82x*. If the best arm's interval lower bound is under it, no review line
   exists and the band is one line - honestly declared, not hidden.
2. **the winner's interval, not its point estimate.** A floor is a sample minimum, so it is an
   estimate of the lower tail taken from above; at a few hundred pairs the raw minimum is optimistic.
3. **the norms line.** If it reports an embedding norm near 5.8 instead of 1.0, host-side L2 is not
   in force and *every* cosine in the report is wrong. `facenet_ort` normalizes; a home-grown engine
   may not.

Then install the winning contract's band - `derive_facenet_band.py` and
[the band runbook](RUNBOOK_FACE_BAND.md) - keyed by `(pipeline, model_id, contract_id)`, so a band
cannot be read by a pipeline it was not measured for.

## Phase 3 - the embedder swap, under a shadow

The width is the *cheap* change: +2.63 MiB and an unmeasurable latency delta on this graph. Run it
as a shadow for as long as it takes to backfill the gallery:

```python
scorer = shadow.ShadowScorer(
    enforced_128, shadow_512,                 # different widths; the constructor refuses a copy
    db_path="shadow.sqlite",
    enforced_gallery=live_gallery,            # width 128, its own calibrated band
    shadow_gallery=shadow_gallery_128_to_512, # width 512, the same band *recalibrated*
)
embedding, verdict, _ = scorer.score(tensor)  # verdict is ALWAYS the enforced one
```

Three properties the module enforces rather than documents:

* **the shadow cannot change a decision, and cannot fail a punch.** Every shadow failure is caught,
  stamped `shadow_error`, and dropped;
* **no comparison across widths.** A gallery carries its width and refuses a probe of another, and
  refuses to store an un-normalized template - a raw 5.8-norm template matched against a unit probe
  reads as distance 0.83, i.e. everybody refused, with no error anywhere;
* **the gate is a measurement.** Flip only when the shadow gallery covers the workforce, the paired
  sample count is meaningful, the shadow's error rate is near zero, the encoders are not in universal
  agreement (suspicious: same graph twice), and the shadow is not **more permissive** than the
  incumbent on identical traffic. A migration that widens acceptance is an access-control weakening
  wearing an upgrade's clothes.

```bash
# coverage, paired comparison, and the gate's verdict in one object
python -c "import shadow; print(shadow.dump_report(shadow.rollout_report(scorer, total_workers=N, rollout=cfg)))"
```

### Beginning it, offline

`backend/shadow_rollout.py` and `tools/shadow_migration.py` are the wiring for the above, and they
run **outside the request path** - not by convention but by construction, so a shadow that fails, or
that is simply slow, cannot touch a punch. Four commands:

```bash
python backend/tools/shadow_migration.py status          # can it start, and with which graphs?
python backend/tools/shadow_migration.py backfill \
    --shadow-approve 0.42 --evidence "measured 2026-09-24"
python backend/tools/shadow_migration.py score --limit 500 \
    --shadow-approve 0.42 --evidence "measured 2026-09-24"
python backend/tools/shadow_migration.py report --shadow-approve 0.42
```

Three things about it are worth knowing before it is run.

* **The graph is a drop-in.** Nothing ships a 512-D export, so `status` reporting `not fetched` is
the normal state rather than a fault: put a FaceNet graph at `backend/models/facenet512.onnx`, or
point `FACENET_SHADOW_MODEL_PATH` at one, and the backfill can run. `facenet_ort` reads the width
out of the graph, so a 1792-D export is the same migration.
* **The evidence from stored frames is weaker than live traffic, in one direction.** Those frames
are overwhelmingly punches the incumbent already approved, so both encoders agree on most of them
and `verdict_agreement` reads near 1.0 - which the gate already treats as suspicious. Coverage,
volume, shadow health and permissiveness are all still meaningful; a claim that the shadow is *more
accurate* is not, and the report says which source its probes came from.
* **`--shadow-approve` has no default, deliberately.** A line measured in a 128-D space is not a
line in a 512-D one - not even the same units - so the shadow needs its own measured line, and a
tool that supplied a plausible default would be handing out an unmeasured access-control decision.
The bands stay keyed by `(pipeline, model_id, contract_id)` as the contract runbook requires, and
the tool refuses a cached gallery built by a different graph rather than scoring against it.

Rollback is a configuration change, not a data migration: the enforced encoder and the band table
are read from configuration, both bands stay installed, and `shadow_scores` is instrumentation that
can be dropped without touching a shift. That table is also the audit of what the old band decided
during any window the new one was live, which is the question asked after a rollback.

## What this runbook cannot tell you

It cannot tell you the winner of Phase 2 on *your* cameras: that is a corpus measurement, and the
four arms differ only in the tensor mapping, so a corpus where no arm clears 1.82x is telling you
about the crop or the detector instead. It cannot tell you whether the gallery backfill will finish
in an hour or a week - that is gate traffic, and `rollout_report` measures it. And it cannot make a
one-line band into a two-tier one: no configuration turns a 1.08x window into 1.82x, and a review
line placed inside an overlap region sends real workers to review every day.
