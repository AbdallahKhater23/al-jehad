# Face verification memory — investigation report

**Status:** adopted. The recommended change (§3) is applied to `backend/uploads.py`, and its
tests are in `backend/tests/test_face_frame_chain.py` (§6). Everything else in this report is
investigation, and the options it rejects were not applied.

**Question asked:** are there more ways to cut memory with no tradeoffs, and can the *peak
during a face verification* be made smaller?

**Short answer:** yes, one — and it is a real ~12.5 MB per verification at the top of the
accepted photo range. The model-side knobs are already at their optimum (measured, and
deliberately pinned by `tests/test_one_vcpu_profile.py`); the remaining memory is *frame
buffers*, not the model.

---

## 1. Verdict table

| # | Idea | Measured effect | Tradeoff | Verdict |
|---|------|-----------------|----------|---------|
| 1 | Don't make `exif_transpose`'s full-resolution copy when the photo is already upright | **−12.5 MB** peak per frame at 2048×1536; scales with frame size | none found — output byte-identical | **Recommend** |
| 2 | Skip the redundant `convert("RGB")` when the image is already RGB | **0 MB** peak | none, but no gain | Reject (no benefit) |
| 3 | Return the decoded image itself (skip *both* copies, transfer ownership) | −12.5 MB — same as #1 | changes a documented ownership invariant for no extra gain | Reject |
| 4 | ONNX CPU arena: `arena_extend_strategy=kSameAsRequested` | **0 MB** | none, but no gain | Reject (not honoured for CPU) |
| 5 | ONNX CPU arena: `enable_cpu_mem_arena=False` | **0 MB** peak | allocator churn | Reject; also pinned by an existing test |
| 6 | `np.asarray` instead of `np.array` for the liveness frame | **0 MB** — `asarray` copies too | none, but it makes the array read-only | **Reject (measured)** |
| 7 | Run face models in a separate process | removes ~150–250 MB floor from the API process | real: IPC latency + ops complexity | Investigate (architectural) |

---

## 2. Where the memory actually goes

There are two distinct costs, and they behave completely differently.

### 2.1 The model is a one-time floor, not a per-punch cost

Measured on this development box with the real graph (`models/facenet128.onnx`, 91 MB) and
the live session settings (`intra_op=1`, `inter_op=1`, `ORT_SEQUENTIAL`,
`enable_cpu_mem_arena=True`):

```
base working set (interpreter + numpy + ORT)       49 MB
after building the session                       153 MB
peak working set                                 250 MB
after a further 400 inferences                   250 MB   <- unchanged
```

The peak is reached during **session construction / first run** (graph optimization and
arena sizing). Four hundred subsequent inferences add **nothing** to the peak. So:

> Steady-state per-punch model memory is ~0. The model's cost is paid once at boot and is
> a floor, not a per-request cost.

Consequence: any change aimed at "memory during face verification" has to target the
**frame buffers**, which is where the per-request memory actually is.

### 2.2 The per-verification transient is frame buffers

For one accepted upload, `main.judge_punch_frame` → `uploads.face_frame` decodes the photo
and resizes it once. Measured peak while doing that for a 2.37 MB / 3.1 MP JPEG at the
default 1280 px ceiling, one fresh process per run, imports equalized:

| variant | peak above baseline |
|---|---|
| `baseline` (current code) | **+38.1, +38.1, +38.3 MB** |
| `convert_only` (skip the redundant convert) | +38.1, +38.1 MB |
| **`skip_exif_copy`** (the proposal) | **+25.6, +25.7 MB** |
| `no_copy` (skip both, transfer ownership) | +25.5, +25.7 MB |
| `via_uploads` (proposal, as applied) | +25.5, +25.7, +25.8 MB |

**Result: +38.2 MB → +25.7 MB, i.e. −12.5 MB (−33%) per verification frame**, repeatable to
±0.2 MB across fresh processes.

### 2.3 The saving is one full-resolution buffer, and it scales

With the decoder warmed first (so decode cost is excluded), the delta tracks the frame size
exactly as "one fewer W×H×3 buffer" predicts:

| upload | one RGB buffer | baseline peak | proposal peak | saving |
|---|---|---|---|---|
| 1024×768 | 2.36 MB | +6.13 MB | +2.78 MB | 3.35 MB |
| 1600×1200 | 5.76 MB | +11.82 MB | +4.24 MB | 7.58 MB |
| 2048×1536 | 9.44 MB | +18.99 MB | +6.55 MB | **12.44 MB** |

The warm-run delta at 2048×1536 (12.44 MB) matches the cold-run delta (12.5 MB) — the same
buffer, isolated two different ways. The saving is monotonically proportional to frame
area, which is what a per-copy saving must look like, and it confirms the mechanism:
`ImageOps.exif_transpose` allocates a **second** full-resolution buffer even when there is
nothing to rotate (orientation absent or 1), and that allocation lands while the decoder's
own buffers are still live — so it, not the later `convert`, is what sets the peak.

### 2.4 Why concurrency multiplies this

`face_inference_concurrency = 2`: at most two frames are decoded at once, and the frame
decode happens *inside* the engine worker (`judge_punch_frame` is handed a path, not
bytes). So the gate's peak under load drops by roughly 2 × 12.5 MB ≈ **25 MB**, and the
saving applies to every path that builds a face frame — punch, quick link, offline sync,
registration link, console enrollment, bulk import — because they all go through
`uploads.face_frame`.

### 2.5 The detector: another one-time floor, sized by the largest frame seen

`face_detector.detect_and_align` was measured on its own, with the model loaded, in a fresh
process each time (peak and working set in bytes from the OS):

| call | frame | WS delta | peak delta |
|---|---|---|---|
| 1st ever | 1280×960 | **+85.9 MB** | **+104 MB** |
| 2nd | 1280×960 | +0.02 MB | +0.66 MB |
| 3rd–6th | 1280×960 | ≈0 | ≈0 |

**There is no per-punch leak** — calls 2 through 6 are flat, and call 6 is marginally *below*
call 5 — and the input shape production actually uses is not a factor: the negative-stride
BGR view `rgb_array[:, :, ::-1]` behaves identically to a contiguous array in steady state.
(An earlier probe suggested a ~107 MB cost on *every* strided call; that was a bug in the
probe — its warm-up guard did not match the mode name, so it measured a cold call twice.)

The allocation is sized by the **largest frame the detector has ever been shown**, and it
never shrinks:

| frame | megapixels | working set | delta |
|---|---|---|---|
| (baseline) | — | 58.8 MB | — |
| 320×320 | 0.10 | 66.3 MB | +7.5 MB |
| 640×480 | 0.31 | 81.1 MB | +14.8 MB |
| 960×1280 | 1.23 | 144.8 MB | +63.7 MB |
| 640×480 | 0.31 | 146.3 MB | +1.4 MB |
| 1280×960 | 1.23 | 145.6 MB | −0.6 MB |
| 320×320 | 0.10 | 146.2 MB | +0.6 MB |

Roughly **70–75 bytes per input pixel** — about 24× the frame's own 3 bytes/pixel — committed
once at the largest size and reused from then on. At the 1280 px punch ceiling that is a
fixed **~86 MB per process**, on top of the embedding graph's floor.

**How much of it is avoidable:**

* **Per punch: none, and there is nothing to avoid.** ~0.9 MB, flat.
* **The floor: only by showing the detector smaller frames.** The cost is linear in frame
  area, so the 1280 px ceiling (≈86 MB) against a 640 px ceiling (≈22 MB) is ~64 MB — but
  that is exactly the small-face regime `detect_raw` reads at native size to serve, so it is
  an accuracy tradeoff, not a free win (see `face_detector.MIN_SUBJECT_PX` and the framing
  coach's working-distance band).
* **The first-call transient is free to move.** Nothing warms the detector at startup today:
  `settings.face_model_preload` calls `face_onnx.load_now()` (the embedding graph only) and
  `readiness` calls `face_detector.describe()`, which loads the 232 KB model but runs no
  detection. So the first worker of the day pays the ~104 MB peak spike *and* a ~141 ms
  detection instead of ~116 ms. One synthetic detection at the ceiling size during startup
  would move both to boot, where nothing is waiting. It does not reduce total memory — it
  relocates the spike away from a request.

---

## 3. Recommended change

One branch in `uploads.decode_photo`: only call `exif_transpose` when the EXIF orientation
says the pixels actually need rotating. When they don't, `load()` the source and let the
existing `convert("RGB")` produce the copy the caller receives — so **the returned frame is
still a `convert` copy** and the documented descriptor-ownership invariant is untouched.

```diff
--- backend/uploads.py
+++ backend/uploads.py
@@ -113,6 +113,11 @@
 #: be swept out from under it.
 SPOOL_STALE_SECONDS = 3600.0
 
+#: The EXIF tag that says how a photo is rotated relative to the sensor - value 1 (or its
+#: absence) means "already upright". Named because it is the one fact that decides whether a
+#: decode has to transpose the pixels at all; see ``decode_photo``.
+ORIENTATION_TAG = 0x0112
+
 #: Pixel ceiling for a decoded photo. 40 MP is far above any phone camera (a 108 MP
 #: sensor still produces a 12 MP default JPEG) and far below the point where a
 #: decompression bomb is worth attempting.
@@ -659,8 +664,19 @@
         if refusal is not None:
             raise refusal
         # ``exif_transpose`` is what makes a portrait phone photo landscape-correct; a
-        # face reference stored at the wrong rotation rejects its owner forever.
-        upright = ImageOps.exif_transpose(image)
+        # face reference stored at the wrong rotation rejects its owner forever. It is only
+        # *asked* when there is something to rotate, though: it returns a full-resolution copy
+        # either way, and that copy is allocated while libjpeg's own decode buffers are still
+        # live, which is what makes it the peak. Measured on a 3.1 MP gate upload at the
+        # default 1280 px ceiling, skipping it when the orientation says the pixels are
+        # already upright takes the transient from ~38 MB to ~26 MB per frame - on the host
+        # with 512 MB to spend, that is the memory. The copy the caller receives is still the
+        # ``convert`` below, so nothing downstream reads this descriptor (see the ``finally``).
+        if image.getexif().get(ORIENTATION_TAG, 1) in (None, 1):
+            image.load()
+            upright = image
+        else:
+            upright = ImageOps.exif_transpose(image)
         if keep_alpha and has_alpha(upright):
             return upright.convert("RGBA")
         return upright.convert("RGB")
```

A copy of this patch is saved at `/tmp/face_frame_memory.patch` (working-tree paths), and
the patched file at `/tmp/x_patched.py`, for reference.

### Why it is safe

* **Output is byte-identical.** Compared against a reconstruction of the current path over
  10 encodings — JPEG with EXIF orientation absent/1/3/6/8, PNG, WebP, greyscale JPEG,
  palette PNG, CMYK JPEG — mode, size and every pixel are identical.
* **The descriptor invariant holds.** The caller still receives a `convert` copy, and the
  existing `finally: image.close()` is unchanged. `load()` on the upright path only makes
  the source's pixels resident sooner.
* **The refusal paths are untouched.** The declared-size refusals are raised before any of
  this, and still close the source handle (pinned by
  `test_the_boundary_is_answered_from_the_header_without_decoding_anything`).
* **A malformed EXIF still fails the same way.** `getexif()` is on the same code path
  `exif_transpose` already used, so a tag that cannot be read still lands in the same
  `ERR_UNREADABLE` refusal rather than a 500.

### Why the alternative (idea #3) was dropped

An earlier prototype also skipped the `convert` copy and handed the caller the opened image
itself. It measured the **same** −12.5 MB (the `convert` copy is free at peak because the
decode buffers are already released by then), while changing the documented
"the returned frame is a copy" invariant and complicating the `finally`. It earns nothing
and costs clarity, so it is not proposed.

---

## 4. Rejected / investigated, with evidence

### 4.1 ONNX Runtime memory knobs — no effect on CPU

Measured with the real graph, 400 inferences per configuration, fresh process each:

| config | peak working set |
|---|---|
| default (arena on) | 250.4 MB |
| arena on + `session.arena_extend_strategy=kSameAsRequested` | 252.4 MB |
| arena off (`enable_cpu_mem_arena=False`) | 250.4 MB |

All within noise. `arena_extend_strategy` is a CUDA/TensorRT provider option; the accepted
config entry is not honoured by the CPU EP. Disabling the arena does not lower peak either,
which corroborates the existing note in `face_onnx.py`. Both are additionally pinned by
`tests/test_one_vcpu_profile.py` (`enable_cpu_mem_arena = True` must appear in both session
builders). **Do not touch these.**

### 4.2 The `convert("RGB")` copy — measurably free at peak

`convert_only` measured +38.1 MB against the baseline's +38.2 MB. Dropping it is a genuine
waste-of-work removal, but it buys no peak because by then the decoder's buffers are gone.

### 4.3 The shadow scorer is a second 91 MB graph

`facenet_ort.FaceNetORT` builds its own session for the shadow rollout
(`models/facenet512.onnx`). It is only constructed when shadow rollout is active, so it is
not a default cost — but if shadow rollout is ever enabled on the 512 MB instance, that is
a second model floor, not a per-request cost.

---

## 5. Still open (ranked, with tradeoffs)

**Measured and rejected — the liveness array copy (`main.py:1236`).** `judge_punch_frame`
builds `rgb_array = np.array(image)` (a full-resolution copy) and hands the liveness model
that array. `np.asarray` looks like the zero-copy swap and is **not** one: for an RGB frame it
goes through Pillow's `tobytes()` and comes back with an immutable `bytes` *base*, so it
retains the same buffer. Measured on a 1280×960 frame, fresh process per run, frame build
warmed first:

| variant | retained | peak delta | array writable | `base` |
|---|---|---|---|---|
| `np.array(image)` | +3.61, +3.91, +3.85 MB | 0.00 MB | yes | `None` |
| `np.asarray(image)` | +3.66, +3.63, +3.96 MB | 0.00 MB | **no** | `bytes` |

The saving is not real, and the reason is structural rather than a Pillow preference:
interleaved RGB is 3 bytes per pixel, numpy has no dtype with a 3-byte itemsize, so the
buffer cannot be expressed as an array without a copy. Switching would change nothing about
memory while turning the array read-only — any future write to `rgb_array`, or to the
`[:, :, ::-1]` view the detector is handed, would fail at runtime instead. The existing
`np.array` plus reversed-stride view is already **one** full-resolution array per
verification, and that is the floor while the detector needs full-resolution pixels as a
contiguous array.

The adjacent step was checked too, since it is the other place a copy could hide:
`liveness.preprocess` on the same frame retains only the **0.08 MB** 80×80 float batch
(peak delta +0.16–0.22 MB), so `Image.fromarray(...).resize()` is not materialising a second
full-resolution image. Nothing to save there either.

What remains open:

1. **Warm the detector at startup.** §2.5: nothing runs a detection before the first punch, so
   the first worker pays a ~104 MB transient spike and ~25 ms of extra latency that a boot-time
   warm-up would move off the request. One synthetic detection at the ceiling size, gated on
   the existing `settings.face_model_preload`, is the whole change. It relocates the spike
   rather than shrinking it — a site with no punches yet would hold ~86 MB it does not need.
2. **Separate the models into their own process/service.** The only way to remove the
   150–250 MB floor from the API process and to turn an ONNX OOM kill from "the whole API
   dies" into "one punch fails". `face_engine` is explicitly the seam for this. Real
   tradeoff: IPC latency and another moving part.
3. **Return freed pages to the OS after the 04:00 burst** (`malloc_trim` / allocator
   tuning). Post-burst RSS stays high because the allocator keeps arenas; trimming after a
   quiet period returns them. Platform-specific and costs a little CPU — not "free".
4. **`punch_frames.store_frame` copies the full frame** to make a 256 px thumbnail. Small
   (~5 MB) and the full frame has to stay alive for `maybe_capture_punch` regardless.

---

## 6. Test plan for the recommended change

Already run against the patch while it was temporarily applied, then reverted:

* **268 tests pass** across `test_face_frame_chain`, `test_punch_spool`, `test_branding`,
  `test_quick_links`, `test_biometric_identity`, `test_offline_selfie_scoring`,
  `test_account_creation`, `test_admin_self_enrollment`, `test_walk_up_registration`,
  `test_walk_up_registration_readiness`, `test_face_engine`, `test_corpus_ingest`.
* **Baseline is green** (69 passed on `test_face_frame_chain`, `test_punch_spool`,
  `test_branding`), so the comparison is meaningful.
* **Byte-for-byte equivalence** across the 10 encodings listed in §3.
* **Descriptor invariant probe**: for upright, rotated (orientation 6), orientation-1 and
  an oversized (refused) upload, the frame holds no file handle and the upload can be
  unlinked immediately afterwards — the property `spool_photo`/`discard` depend on on
  Windows.

**The tests that were missing, now added** (section 5 of `tests/test_face_frame_chain.py`):

1. `test_an_upright_photo_is_not_transposed` — counts the `exif_transpose` call, because the
   extra buffer leaves no trace in the output. **Verified to fail when the fix is reverted**
   and to pass with it, so the guard is real rather than decorative.
2. `test_an_exif_rotated_photo_still_comes_back_upright` — orientation 6 must still be
   rotated (the answer is the *smaller* frame), so the fast path cannot skip a rotation that
   is actually needed.
3. `test_a_decoded_frame_leaves_no_handle_on_the_upload` — the success-path half of the
   descriptor property, which the spool's `discard()` depends on; the existing header-only
   test covers only the refusal path.
4. `test_an_upright_decode_does_not_pay_for_a_transpose_buffer` — **the peak, in bytes.**
   Each measurement runs in a fresh interpreter, because peak resident memory is a
   process-lifetime high-water mark and would otherwise make the second run look free. The
   assertion is a comparison rather than an absolute figure: a *rotated* photo of the same
   pixel dimensions is the calibration, since its transpose is required and differs from the
   upright case by nothing except the extra full-resolution buffer (measured upright
   **+7.1 MB** vs rotated **+19.5 MB**, ±0.3 MB). A third run — two 2048×1536 buffers
   allocated with nothing else happening — establishes that the allocator *can* see such a
   buffer, so an insensitive machine skips while a sensitive one that finds no difference
   **fails**. Verified: with the fix reverted the test fails (upright 19,456,000 B vs rotated
   19,832,832 B, inside the quarter-buffer margin), and passes with it.

That last one is deliberately broader than the `exif_transpose` guard: it does not need to
know *which* operation allocated the buffer, so a future `.copy()`, a second `np.array`, or a
second decode anywhere in the chain is caught even though the frame stays byte-identical.

---

## 7. Limits of this measurement

* The numbers above are **working set on this Windows development box**. The deltas are
  repeatable (±0.2 MB) and internally consistent, but absolute RSS on the Linux 1 vCPU /
  512 MB deployment will differ.
* **The Linux confirmation has not been taken yet.** There is no docker, podman or WSL on the
  Windows box these figures come from, so the deployment class host is still unmeasured. Two
  routes to it, both ready: dispatch the `frame-decode-memory` job in
  `.github/workflows/punch-memory.yml` (it runs `tools/frame_decode_memory.py` on
  `ubuntu-latest` and then again inside the built image at `--memory 512m --memory-swap 512m
  --cpus 1`, i.e. the deployment's jemalloc), or run the same tool verbatim on the host.
  `tools/capacity_test.py` remains the heavier hook for the ceiling itself, and the profile in
  `tests/test_one_vcpu_profile.py` for the runtime configuration.

---

## 8. Recommendation

Adopt idea #1 (the §3 patch) — **applied**: roughly **12.5 MB less peak per verification
frame**, ~25 MB at the gate with the current two-worker concurrency, byte-identical output,
no configuration change, and no change to any documented invariant. It touches one function
and adds three tests. Everything else measured here is either already optimal or not worth
its tradeoff.

Re-confirmed after applying, with the same probe: `via_uploads` (the patched
`face_frame`) peaks at **+25.6 MB** where the baseline measured **+38.2 MB**. Full run:
**281 tests pass** across the twelve frame/punch/enrollment suites plus
`test_one_vcpu_profile`.
