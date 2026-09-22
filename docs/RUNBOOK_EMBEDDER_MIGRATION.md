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
| `backend/detector_640.py` | resolution-correct localisation: an invertible `Letterbox`, YuNet at 640 (square or aspect-matched), an optional `tiles x tiles` pass with greedy NMS merging, and a `ScrfdDetector` for extreme range |
| `backend/face_align.py` | one-resample alignment: a closed-form Umeyama similarity transform from native-frame landmarks straight onto the 160x160 template, plus the four input `Contract`s |
| `backend/facenet_ort.py` | the ORT session (IOBinding, thread tuning, warmup) and **host-side L2** on the raw output |
| `backend/calibration.py` | the band rule, its identity-cluster bootstrap, and the refusal when no line can separate the distributions |
| `backend/shadow.py` | `ShadowScorer` - the enforced encoder plus a shadow, paired logging in SQLite, and the cutover gate |
| `backend/tools/contract_ab.py` | the contract experiment: all four permutations on one shared crop set |

None of these is wired into the punch path. They are parallel, drop-in modules on purpose: the
rollout below is an A/B, and an A/B needs both sides to be runnable.

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
  frames that need it - a gate camera whose subjects stand close does not.

**The detector is not the whole of coverage.** A crop upscaled from 112x112 to the graph's 160x160
input carries interpolation blur that no threshold absorbs, which is why Phase 2 aligns *directly*
onto the 160 template: one resample, ever.

## Phase 2 - the contract, measured rather than assumed

Nothing in a `.onnx` file says `RGB` or `BGR`, and nothing says `[0, 1]` or `[-1, 1]`. A graph fed
the wrong convention returns a vector of the right shape in the right range, so the defect has no
symptom but a closed separation window. Measure it:

```bash
venv/Scripts/python.exe tools/contract_ab.py --corpus ./calibration --json contracts.json
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
