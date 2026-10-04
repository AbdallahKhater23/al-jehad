# Idle RSS reduction — a zero-tradeoff review

**What this is.** An analysis of the container's **idle** resident set (zero active traffic)
for the Railway deployment (`al-jehad`, 1 vCPU, cgroup `memory.max` = 1,999,998,976 B ≈
1.86 GiB), with a hard rule from the brief: **no tradeoffs.** Nothing here may defer work
that the first punch pays for, reduce throughput under load, or weaken error handling,
health checks or data-integrity guards.

**Where the idle memory actually is.** Two processes hold essentially all of it:

| Process | Idle RSS | What it holds |
|---|---|---|
| `backend/face_worker.py` (the child, `FACE_ENGINE_PROCESS=1`) | **351.7 MB** | Python + numpy + OpenCV + the ONNX session + the detector working set |
| `uvicorn backend.main:app` (the API) | **171.5 MB** (Pss 139.6) | Python + FastAPI/pydantic + SQLite/JWT + numpy + **cv2 (unused)** |

The API process's Pss, attributed by mapped library (`/proc/3/smaps`):

```
total Pss 139.6 MiB
     105.4 MiB  [anon]                       # interpreter, FastAPI, pydantic, app code, SQLite
       6.6 MiB  cv2/cv2.abi3.so
       3.4 MiB  cryptography/.../_rust.abi3.so
       2.0 MiB  libpython3.12.so.1.0
       2.0 MiB  pydantic_core/_pydantic_core...
       1.7 MiB  numpy/_core/_multiarray_umath...
       1.2 MiB  opencv_python.libs/libQt5Widgets-...so   # ← Qt: the *non-headless* wheel
       1.2 MiB  opencv_python.libs/libopenblasp-r0-...so
       1.1 MiB  opencv_python.libs/libQt5Core-...so
       0.9 MiB  numpy.libs/libscipy_openblas64_-...so
       0.9 MiB  opencv_python.libs/libQt5Gui-...so
       0.8 MiB  opencv_python.libs/libavfilter-...so
```

Measured import cost inside the production container (fresh interpreter → import), which is
the honest way to size each removal:

| Stage | RSS | Delta |
|---|---|---|
| interpreter | 14.9 MB | — |
| `+ numpy` | 35.0 MB | **+20.1 MB** |
| `+ cv2` | 59.9 MB | **+24.9 MB** |
| `+ onnxruntime` | 76.1 MB | +16.2 MB |

**The one genuinely load-bearing observation:** the API process **maps `cv2`, `opencv_python`,
`libQt5*`, `libav*` and `libopenblas` — while `onnxruntime` is *not* mapped there.** The models
already live in the child (that is what `FACE_ENGINE_PROCESS=1` bought). The vision *libraries*
did not follow them out.

---

## 1. The prioritized matrix

Ordered by measured/estimated idle saving. Every row is zero-tradeoff under the brief's rules;
where a recommendation has a precondition (e.g. "1 vCPU only"), that precondition **is** the
reason it is free, and dropping it turns the row into a tradeoff.

| # | Mechanism | Est. idle saving | Why it has **zero** active-execution tradeoff | Verification |
|---|---|---|---|---|
| 1 | **Keep cv2 out of the API process.** cv2 is mapped in `uvicorn` (most likely via a startup probe or a transitive module-level import — the exact trigger still to be confirmed), but all detection & alignment already run in the child (`face_detector.warm()` already *declines* when `_models_run_elsewhere()`). Make the parent's boot graph never `import cv2`; if a startup self-test probes the detector, probe the **child**. | **~25 MB** measured (`+cv2` = +24.9 MB), of which ~14 MB is file-backed | The parent performs **no vision work**: it runs numpy-only band math (`band_for`, `subject_detections`) and delegates every detect/embed/liveness op to the child. Cold start is unchanged — the child warms the detector exactly as it does now. Under load the parent's task is I/O + SQLite, so removing an unused import cannot change throughput. | `grep -c cv2 /proc/$(pgrep -f 'uvicorn backend.main')/maps` → **0**; `cat /sys/fs/cgroup/memory.current` before/after a redeploy |
| 2 | **`opencv-python` → `opencv-python-headless`.** The non-headless wheel drags **Qt5 + FFmpeg + libGL** in (`libQt5Widgets/Core/Gui`, `libavfilter/codec`). Headless drops them, and `libgl1` + `libglib2.0-0` stop being needed in the image at all. | **~5–15 MB** file-backed Pss per process that imports cv2, plus image size / pull time | The app uses only `objdetect`/`imgproc`/`imgcodecs`/`calib3d` (`FaceDetectorYN.create`, `resize`, `warpAffine`, `cvtColor`, `copyMakeBorder`, `estimateAffinePartial2D`, `imread`). It never calls `highgui`/`imshow` or video capture — the only things headless removes. Same kernels, same speed. | `python -c "import cv2; print(cv2.FaceDetectorYN)"` after the swap; `grep -c 'libQt5' /proc/<pid>/maps` → 0. **Confirm `FaceDetectorYN` ships in the headless wheel before pinning it.** |
| 3 | **jemalloc `narenas:1`** (and `thp:never`) in the 1 vCPU profile's `MALLOC_CONF`. jemalloc's default is `4 × nproc` arenas; on one core nothing allocates in parallel, so the extra arenas only add per-arena metadata and retained free pages. | **~2–6 MB** (metadata + per-arena muzzy/dirty retained) | On a **1 vCPU** instance there is no allocator parallelism to lose — the ~4 arenas cannot be used concurrently. This is why it is free *only* on this profile; on a multi-core host it would be a throughput tradeoff, so it belongs in the image's 1 vCPU ENV block, not the code default. `thp:never` avoids RSS inflation from huge pages. | `MALLOC_CONF` in the container env; jemalloc `stats.arenas` / `stats.retained`, or `cat /sys/fs/cgroup/memory.current` |
| 4 | **Pin the BLAS/OpenMP thread counts to 1**: `OPENBLAS_NUM_THREADS=1`, `OMP_NUM_THREADS=1`, `MKL_NUM_THREADS=1`, `NUMEXPR_NUM_THREADS=1`. `libopenblas` (both numpy's and OpenCV's) is mapped at boot and sizes a thread pool at import. | **~1–3 MB** + removes per-thread scratch | On 1 vCPU a second BLAS thread buys no FLOPs and only contends — the ORT config (`intra=inter=1`, sequential) already assumes a single core. Pinning makes the assumption explicit and prevents oversubscription on any host where cgroup detection is off. | `python -c "import numpy; print(numpy.__config__)"`; `grep -i threads` in container env |
| 5 | **`cv2.setNumThreads(1)`** — **already implemented** (`detector_640.py:59`, `face_detector.py:381`). | 0 (already) | — | `grep -n setNumThreads backend/detector_640.py` |
| 6 | **`gc.collect()` + `gc.freeze()` after boot** — **already implemented** (`main.py:7003–7004`). The frozen generation is never scanned again. | 0 (already) | Keeps steady-state GC work off the request path. | `grep -n "gc.freeze" backend/main.py` |
| 7 | **OR T arena / threads / sequential execution** — **already implemented and deliberately justified** (`face_onnx.py:362–381`: `enable_cpu_mem_arena = True`, `intra=inter=1`, `ORT_SEQUENTIAL`; `facenet_ort.py:218–222`). | 0 (already) | — | `grep -n enable_cpu_mem_arena backend/face_onnx.py` |
| 8 | **jemalloc decay tuning + slim base** — **already implemented** (`MALLOC_CONF=…,dirty_decay_ms:1000,muzzy_decay_ms:0`; `python:3.12-slim`; `--no-install-recommends`). | 0 (already) | — | `grep MALLOC_CONF Dockerfile` |

**Realistic total available: ~30–50 MB of the API process's 171 MB idle RSS**, all of it from
items 1–2 (the parent's unused vision stack). That is the whole opportunity — see §3.

---

## 2. Verification commands

```bash
# ---- 1. What is mapped where (the core evidence) -----------------------------
# API process: expect onnxruntime ABSENT (good) and, after item 1, cv2 ABSENT too
APID=$(pgrep -f 'uvicorn backend.main')
grep -oE 'cv2|onnxruntime|libQt5[^ ]*|libav[^ ]*|libopenblas[^ ]*' /proc/$APID/maps | sort | uniq -c

# ---- 2. Per-library Pss attribution (what actually costs memory) -------------
# Sum Pss by mapping name — file-backed libraries vs [anon]
awk '/^[0-9a-f]+-/{n=$6} /^Pss:/{s[n]+=$2} END{for(k in s) printf "%.1f MiB  %s\n", s[k]/1024, k}' \
  /proc/$APID/smaps | sort -rn | head -15

# ---- 3. Import cost of each candidate removal (run inside the container) -----
python3 - <<'PY'
for stage, mod in (("interpreter", None), ("numpy", "numpy"), ("cv2", "cv2")):
    if mod:
        __import__(mod)
    rss = [l for l in open("/proc/self/status") if l.startswith("VmRSS:")][0].split()[1]
    print(f"{stage:12} {int(rss)/1024:.1f} MB")
PY

# ---- 4. Container idle working set (the number the brief is about) ----------
cat /sys/fs/cgroup/memory.current          # bytes; idle baseline was 554,737,664 (~529 MiB)

# ---- 5. jemalloc retention (item 3) -----------------------------------------
MALLOC_CONF=stats_print:true python3 -c "import numpy" 2>&1 | grep -A3 'arenas\['
```

---

## 3. What looks like a win but is **not** (rejected by the zero-tradeoff rule)

This is the part that matters most, because each of these *looks* like an obvious idle-RSS fix
and each one is a tradeoff the brief forbids:

| Tempting change | Why it is rejected |
|---|---|
| **Disable `options.enable_cpu_mem_arena`** | The most obvious-looking 512 MB-host change, and the repo's own benchmark already rejected it (`face_onnx.py:373–381`): with the arena off, every intermediate buffer returns to the system allocator and is re-requested on the next run — you pay in **allocator churn and worse peak**, and lose the flat allocation pattern a burst depends on. Peak is what the OOM ceiling is about. |
| **Lazy-load the model / defer the graph or detector** | Explicitly forbidden: this is exactly the cold-start cost the first punch would absorb. The repo already chose to *move* that spike to boot (`face_detector.warm()`), not to defer it. |
| **`PYTHONMALLOC=malloc`** | Routes Python's small-object allocations off pymalloc's fast path onto jemalloc. It can *lower retained RSS*, but it costs CPU on every small allocation — a throughput tradeoff. Reject. |
| **Defer pydantic models / build routers lazily** | Validation and the route table are reliability surfaces; deferring them trades correctness and latency for memory. Reject. |
| **Reduce logging / drop the startup self-test / skip health checks** | Explicitly excluded by the brief. |
| **`narenas:1` on a multi-core host** | Item 3 is free *because* the box is 1 vCPU. On more cores it removes allocator parallelism and becomes a throughput tradeoff — so it must live in the 1 vCPU image profile, never the code default. |
| **Remove `scikit-learn` / Flask / gunicorn from `requirements.txt`** | These are **image size**, not idle RSS, unless imported — and no runtime module imports them (checked). Removing them does **not** lower the resident set; do not count them as an RSS win. (It is still worth doing for build/pull time, but that is a different argument.) |
| **`malloc_trim(0)` / manual purge after startup** | With jemalloc's `muzzy_decay_ms:0` already in place the pages return promptly; a manual trim buys ≈0–2 MB and can add page-fault churn later. Not worth the risk. |

---

## 4. Why the ceiling on savings is low (and that is the honest headline)

The API process's idle Pss is **~105 MiB of `[anon]`** — the interpreter, FastAPI/Starlette,
pydantic validators, the SQLite and JWT stacks, and the application's own module graph. That is
**live, reachable data**, not slack. None of it can be released without deferring work the first
request needs (forbidden) or weakening a guard (forbidden). The file-backed libraries total only
~35 MiB, and cv2's share of that is ~14 MiB plus ~11 MB of heap it pulled in at import.

So the realistic, zero-tradeoff improvement is **~25 MB from the API process by making cv2
child-only** (item 1), plus **~5–15 MB of file-backed mappings** from the headless wheel
(item 2), plus a few MB from jemalloc/BLAS hygiene (items 3–4) — call it **~30–50 MB**, or
roughly **2% of the 1.86 GiB cap** and **~20–30% of the API process**. The child's 351.7 MB is
where the real memory is, and it is all working set — model graph, ONNX arena, detector pool —
that the zero-tradeoff rule protects by construction.

**Practical conclusion.** Idle RSS is not the deployment's problem: it idles at ~529 MiB of a
1.86 GiB cap, and the constraint-respecting ceiling on further reduction is tens of megabytes.
The lever that actually changes the memory picture at idle — not holding the detector working
set from boot — is *precisely* the cold-start tradeoff the brief rules out. Recommend doing
items 1–2 for the file-backed/import tidy-up and **not** spending further effort hunting idle
RSS; the binding constraint on this instance remains CPU (see
`docs/LOAD_TEST_REPORT_2026-10-04.md`).

---

## 5. Appendix — measuring it yourself

```bash
# inside the container
python3 - <<'PY'
import os
def rss():
    return int([l for l in open("/proc/self/status") if l.startswith("VmRSS:")][0].split()[1])
def libs(pid):
    import re, collections
    pss, cur, total = collections.defaultdict(float), "?", 0.0
    for line in open(f"/proc/{pid}/smaps"):
        if re.match(r"^[0-9a-f]+-", line):
            p = line.split(); cur = p[-1] if len(p) > 5 else "[anon]"
        elif line.startswith("Pss:"):
            v = float(line.split()[1]) / 1024; pss[cur] += v; total += v
    print(f"pid {pid}: total Pss {total:.1f} MiB")
    for n, mb in sorted(pss.items(), key=lambda x: -x[1])[:12]:
        print(f"  {mb:8.1f} MiB  {n}")
libs(os.getpid())                       # run after importing the module you are judging
PY
```

Measure every candidate by its **import delta on a throwaway interpreter** (§1, `+cv2` =
+24.9 MB) and its **file-backed Pss attribution** (§1 table) — not by eyeballing container
totals, where the kernel's page cache and socket buffers dominate and mask the process.
