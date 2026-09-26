# Runbook / design: adopting a smaller detection pass (a crop change) without refusing the workforce

**Status:** design. Nothing here is implemented or executed. It is the migration that a
640-class pass would *require*, written down so the decision to switch one on can be priced
before it is made.

**The problem in one sentence.** `FACE_DETECTOR_INPUT_SIZE` moves the detector's working
resolution, which moves the **crop** every stored template was embedded from; a template is the
embedding of a crop, so switching the pass invalidates every template and the band derived for
the old crop. `docs/FACE_VERIFICATION_MEMORY_REPORT.md` §2.5 measured that: the cap spends
**5–33× the band's entire 0.0036 headroom**, so it is not a detector swap, it is a data
migration. This page is that migration.

Two rails already exist and the design leans on both: `face_detector.capped_pipeline()` names
the crop (`yunet-2023mar-640x2`), and `band_for`/`readiness._check_face_match_band` refuse to
score or to boot without a measured band for that name. What is missing is provenance at the
**crop** level (not just the name), a way to re-derive the band behind a **fingerprint**, and a
transition that keeps a site serving while people are re-photographed.

---

## 0. First, try not to need any of this

The migration is the expensive path. Before writing a single line of it, ask whether a cap
exists whose drift already fits inside the headroom — in which case there is **no** re-enrolment
and **no** band change, only a smaller memory floor.

* `tools/detector_resolution_ab.py` currently sweeps `cap640x1`, `cap640x2` and `cap480x2`. A
  cap is a *number*, not a family, and the memory curve is linear in area
  (`tools/yunet_memory.py`: 320/480/640/960/1280 = +8.4/+15.6/+24.0/+51.9/+86.9 MiB resident).
  A mid cap — 960 or 1024 — is still **−35 to −45 MiB** against native and has **not** been
  measured for crop drift. **Add an `--input-size` sweep to the A/B tool before anything else.**
* Decide by the tool's own rule: adopt the smallest cap whose worst drift is **< 0.0036** and
  whose lost-subject count is zero. If one exists, stop here — install it, keep the band, done.
* Only if *every* useful cap exceeds the headroom do you run the migration below.

An even better structural option, if the sweep says no cap is safe: make the capped pass
**scale-normalised** — one coarse pass localises the face, a second capped pass runs on a
face-centred window whose scale is normalised by the coarse box, so landmark precision no longer
depends on the frame's dimensions. That would preserve the crop (and the templates) at 640, and
is a detector change rather than a data migration. It is out of scope here but it is the option
a "no safe cap" result should fund first.

---

## 1. The fingerprint: what a template was *made from*, not what it was *called*

Today a template records `pipeline` (a name) and `model`. A name is a claim; a **fingerprint**
is the thing itself. `corpus.detector_fingerprint` already produces exactly the right object and
`corpus.same_crop` already compares the right fields:

| field | why it is in the identity |
|---|---|
| `model_fingerprint` | sha256 of the model **file** — a re-export with the same name must not be scored against old templates |
| `input_size` | the working resolution, i.e. the crop |
| `square` | padded canvas or aspect-matched |
| `tiles` | how many windows, so the worst window's scale |
| `overlap` | only when `tiles > 1` |
| `kind` / `pipeline` | **ignored** by `same_crop` — two names for one graph is a labelling difference |

**Design.** Give every stored template a `crop` field — the `detector_fingerprint` of the pass
that produced it — written beside `pipeline`/`model` in `biometrics.write_reference` and read by
`read_reference` (absent on old files ⇒ `None` ⇒ needs migration). Then:

* `biometrics.stale_reason` gains a crop check that is **stricter than the name check**:
  `corpus.same_crop(reference.crop, live_crop_fingerprint())` must be true. The name check stays
  (it is what the band table is keyed by), but a name match with a different digest is now a
  refusal rather than a silent mismatch. This closes the "same-named re-export" hole the band
  runbook only closes by convention.
* The **adoption fingerprint** `F*` is the destination pass's fingerprint (e.g.
  `kind=yunet, input_size=640, tiles=2, overlap=0.2, model_fingerprint=<digest>`). It is what
  the migration is gated on and what it stamps.
* `pipeline` is *derived from* `F*` via `capped_pipeline()`, so the band key and the crop
  identity cannot disagree — one is a string the operator reads, the other is the truth.

**Provenance ledger.** A new additive migration adds one table, so the migration is idempotent
and resumable and readiness can report progress:

```sql
CREATE TABLE IF NOT EXISTS crop_migrations (
    fingerprint   TEXT PRIMARY KEY,   -- F*, the destination crop
    pipeline      TEXT NOT NULL,      -- capped_pipeline() for F*
    model         TEXT NOT NULL,      -- FACE_MODEL the band was derived with
    band_installed INTEGER NOT NULL DEFAULT 0,
    started_at    DATETIME,
    completed_at  DATETIME            -- set once no template is left on another crop
);
```

---

## 2. Re-derive the band first, behind the fingerprint

The band is the only thing that makes the new crop *scorable*, and it is produced by the tool
the band runbook already describes — `tools/derive_facenet_band.py` — run **on a host whose live
pass is already the new one** (`FACE_DETECTOR_INPUT_SIZE=640 FACE_DETECTOR_TILES=2`). It embeds
through the live detector, so what it measures *is* the new crop, and `--pipeline` must equal
`capped_pipeline()` or it refuses.

Two additions the migration needs from it:

1. **Record `F*` beside the band.** The tool should print the `detector_fingerprint` it ran
   under next to the `MatchBand(...)`, so the evidence string names the crop by identity and not
   only by `yunet-2023mar-640x2`.
2. **A calibration corpus captured through the new pass.** This is `RUNBOOK_EMBEDDER_MIGRATION.md`
   Phase 0 with `--input-size 640 --tiles 2`: gate selfies, one camera, several captures per
   worker, at the resolution the gate actually delivers. The corpus's own sidecar fingerprints
   are the audit that the band was measured on `F*`.

Then paste the `MatchBand(...)` into `face_detector.BANDS` under `capped_pipeline()`, and record
the row in `crop_migrations` with `band_installed=1`. A band for `F*` **must not** be installed
while `active_pipeline()` is still native unless the migration below also installs the
two-crop transition — otherwise the first worker enrolled on the new crop is refused, which is
the failure mode, not the fix.

---

## 3. Can a stored template be re-embedded server-side? The fidelity gate

The tempting migration is: for each account, read the stored reference selfie, run it through the
new pass, write a new template. **In general this is wrong, and the reason is the same invariance
the cap removes.**

* A punch frame is up to `settings.face_frame_max_px` (1280) and `face_frame` only ever shrinks.
  The stored selfie is that framed image *thumbnailed to `PHOTO_MAX_EDGE` = 800*
  (`biometrics.write_reference`). They are usually **different sizes**.
* The native pass reads a frame at its own size and `align` normalises the crop to 112×112 by
  the landmarks, so it is largely frame-size-independent — which is why templates survive being
  made from an 800 px upload and checked against a 1280 px punch today.
* The capped pass downscales the **whole frame** into 640, so the face's pixel size *in the
  letterboxed input* — and therefore landmark precision, and therefore where the 112×112 crop
  lands — is a function of the frame's dimensions. A 200 px face is 160 px in the 640 input from
  an 800 px selfie and 100 px from a 1280 px frame. **Different crop, different template.**

So re-embedding a selfie is only valid if the selfie is a frame the gate could have presented.
The migration tests that instead of assuming it:

**The fidelity gate**, per account, run offline by the migration tool while both passes are
still available:

1. `probe = embed(native_pass(stored_selfie))` — the pass that made the stored template.
2. require `cosine(probe, stored_template) <= ε_fidelity`, a *tight* ε at the JPEG-re-encode
   floor (it is the same photo, so the only difference is the store's own re-encode).
3. **PASS** ⇒ the selfie is the source frame at full resolution; re-embed it through the new
   pass and write the new template + `crop = F*`.
4. **FAIL** ⇒ the selfie is a downscale (or a rotated/cropped derivative); re-embedding it makes
   a *third* crop, so the account goes to the **human re-capture worklist**.

**Even a PASS is conditional, and the tool has to say so.** A re-embedded template's crop is
`F*` of an 800 px frame; a later 1280 px punch is `F*` of a 1280 px frame. They agree only if the
gate frames are the same size class as the selfie. So the preflight also reports the *gate-frame
size distribution* (`punch_frames` dimensions) beside the *selfie* sizes: if the gate routinely
resizes to 1280 and the selfies are 800, the server-side path is mostly **not** applicable and
the migration is, honestly, a re-capture campaign with a cheap subset.

> Expect the honest answer to be "most accounts need a new photo". That is not a failure of the
> design; it is the design telling the truth that a crop change invalidates templates, and no
> amount of GPU-free re-embedding changes which frame the template was measured on.

---

## 4. The transition: two crops, or a maintenance window — not a silent mixed state

The trap is that a probe is **always** the live pass. If the live pass is capped and a gallery
template is native, every comparison is cross-crop and meaningless — and *nothing errors*,
which is the exact failure the band rail exists to prevent. Mixed scoring is therefore only
safe if **the probe is made by the template's own crop**. Two ways to get that:

### 4a. Two-crop transition (recommended)

While `crop_migrations.completed_at` is null, the build holds **both** passes and **both** bands:

* `stale_reason` accepts a template whose crop is **either** the adopted `F*` **or** the retired
  native fingerprint, as long as that crop has a band (`band_for` still refuses the rest).
* `compare_faces_sync` embeds the live frame with the pass the **template** was made by — the
  gallery row names its crop, so the probe is chosen per comparison. New enrolments write `F*`
  (`current_pipeline()` returns the adopted name once the migration starts; the *enrolment*
  pass is the new one from day one of the window).
* Cost: a punch during the window may run **both passes** (~+95 MiB native + ~+34 MiB capped on
  the deployment profile). That is the price of not having an outage, it is bounded, and it ends
  when the last old template is replaced.
* `readiness` gains a check: `crop_migration` **warn** while `pending > 0`, carrying
  `migrated/total` and the oldest un-migrated enrolment; it becomes **fatal** past a configured
  deadline, so a worklist nobody is working is a deployment that refuses to keep running on a
  crop it cannot finish moving off.

### 4b. Maintenance-window cutover (simpler, an outage)

Set the cap and migrate in one deploy with readiness **fatal until `pending == 0`**: the site is
down for the length of the re-capture campaign. Acceptable only for a site small enough to
photograph everybody in a scheduled window. The tool is the same below; only the serving state
differs.

### Exit

When `pending == 0`, remove the **native** band from `BANDS` (or mark it retired). Removing it is
the point: a leftover native template then raises `UnknownPipelineError` instead of being
silently compared cross-crop, which is the same "nothing is scored without a measurement"
invariant the whole table rests on. Flip `FACE_DETECTOR_INPUT_SIZE`/`TILES` to `F*`'s settings
(redundant if they were already set for the window), set `completed_at`, and the two-crop code
path is dead configuration.

---

## 5. The migration tool (offline, resumable, idempotent)

`backend/tools/migrate_crop.py` — **offline**, against the database, the reference directory and
the models; it does not go through the serving app and it never runs in a request.

```bash
# 1. what would happen, and how much of the workforce it costs
python backend/tools/migrate_crop.py plan            # migratable / needs-photo / no-selfie
# 2. re-embed the migratable subset, stamping crop = F*
python backend/tools/migrate_crop.py apply --fingerprint <F*> --limit 500
# 3. the worklist for the enrolment paths (unchanged flows: /admin/enroll, enrollment links)
python backend/tools/migrate_crop.py worklist --json /tmp/needs-photo.json
```

Properties the tool must hold, matching how the rest of this project treats a biometric store:

* **Idempotent and resumable.** It re-checks each record's `crop` before touching it; a
  re-run after a crash re-does only what is left. `--limit` bounds a run so it can be stopped.
* **Write-then-rename.** It goes through `biometrics.write_reference` (temp name + `os.replace`),
  so a crash never leaves half a template. The retirement of the old file is the existing
  `quarantine_legacy`/replace path, unchanged.
* **Refuses without a band.** The tool checks `band_installed` for `F*` before writing anything:
  migrating templates for a crop that cannot be scored is how a working site is turned into a
  refusing one.
* **Refuses a mismatched `F*`.** If `--fingerprint` is not the live pass's fingerprint, it stops.
  (This is the "behind a fingerprint" requirement — the run is keyed to the crop it is migrating
  *to*, and a re-export or a changed tile count is a different migration.)
* **Reports, never guesses.** A selfie that fails the fidelity gate is a *worklist row*, not a
  silently worse template.
* **No second copy of a face.** It reads the selfie in place; it does not export, duplicate or
  retain crops. (The corpus rules in `RUNBOOK_EMBEDDER_MIGRATION.md` Phase 0 apply to any
  calibration material, and this tool produces none.)

---

## 6. Failure and rollback

| failure | behaviour |
|---|---|
| band for `F*` missing | tool refuses; readiness fatal (`face_match_band`), unchanged rail |
| selfie fails fidelity | account routed to the human worklist; its old template keeps scoring on the old crop during 4a |
| a punch during the window | probe built with the template's own crop; no cross-crop comparison ever |
| the deadline passes with a non-empty worklist | readiness bumps `crop_migration` to fatal — a site that cannot finish moving off a crop stops rather than mis-matches |
| wrong `F*` passed | tool refuses before writing |
| rollback before `pending == 0` | clear the settings; both bands and both passes are still installed, no data was lost |
| rollback after `pending == 0` | the native templates are gone (replaced or quarantined), so this is a **forward-only** point: do not remove the bands/table until the site is sure |

Rollback is clean *until* the old templates are retired, which is deliberate: the point of no
return is exactly the point where the old crop can no longer score anybody.

---

## 7. Test plan (mirrors the suites that already pin the rails)

* `tests/test_crop_migration.py` (new): the ledger is additive and idempotent; `plan` counts
  migratable/needs-photo/no-selfie on a fixture gallery; `apply` stamps `crop = F*` and is a
  no-op on a second run; it refuses on a missing band and on a mismatched `F*`; a fidelity fail
  becomes a worklist row and changes no file.
* `tests/test_face_detector.py` / `tests/test_biometric_identity.py`: `read_reference` parses the
  new `crop` field; a template with a *same-name, different-digest* crop is refused by
  `stale_reason`; a template matching `F*` passes.
* `tests/test_readiness_surface.py`: `crop_migration` is warn with a worklist, fatal past the
  deadline, quiet once `completed_at` is set.
* `tests/test_face_match_bands.py`: a band for `capped_pipeline()` and the native band coexist
  during the window; the native band's removal is what makes a leftover native template a hard
  refusal.
* The A/B tool's `--input-size` sweep (step 0) gets a contract test: a cap whose drift is inside
  the headroom is reported OK, one above it is FAIL.

## 8. What this design cannot do

* It cannot make a crop change free. If the sweep in §0 finds no safe cap, somebody photographs
  the workforce again — the design only makes that safe, resumable and honest.
* It cannot quantify the fidelity-gate pass rate without running it. The only way to know how
  many accounts are server-side migratable is `migrate_crop.py plan` on the real reference
  directory; state the number before promising a cutover date.
* It cannot make the two-crop window cheap: during 4a a punch can run both passes. Measure it on
  the deployment profile before enabling 4a on a 1 vCPU / 512 MB host, or choose 4b instead.
