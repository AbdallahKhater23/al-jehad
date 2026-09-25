# syntax=docker/dockerfile:1
#
# Site Attendance — the image Railway builds and serves.
#
# WHY A DOCKERFILE (railway.json's builder is DOCKERFILE, not NIXPACKS)
# ---------------------------------------------------------------------
# Nixpacks guesses a build from the repository's shape; this file states it. The two
# guesses that matter here are the ones Nixpacks cannot see from file names alone:
#
#  * opencv-python (the YuNet face detector, ``backend/face_detector.py``) is the
#    non-headless wheel and links against libGL at import time. A slim image without
#    the two system packages below imports cv2 as "libGL.so.1: cannot open shared
#    object file" — which surfaces at *runtime*, after a green build, as an app that
#    crash-loops on every deploy.
#  * the state this app writes — the SQLite database and the four biometric/evidence
#    directories (``config.py`` reads them from the environment, the way
#    ``DATABASE_PATH`` works) — has to live under one mountable root, so a Railway
#    volume at /data keeps payroll data across deploys instead of losing every punch
#    to the next one.
#
# The interpreter tracks ``.python-version`` (3.12) and the manifest was frozen on
# 3.12; ``backend/tests/test_deployment_manifest.py`` keeps the pair honest.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# System libraries the runtime manifest cannot name:
#   libgl1, libglib2.0-0 — what ``import cv2`` dynamically loads (see above);
#   libjemalloc2         — the allocator this deployment runs on (see below);
#   curl                 — the container HEALTHCHECK probe (Railway uses its own
#                          healthcheckPath and ignores this one; it is for local runs).
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        libjemalloc2 \
        curl \
 && rm -rf /var/lib/apt/lists/*

# Allocator: jemalloc, not glibc's malloc.
#
# WHY THE ALLOCATOR IS THE MEMORY FIX
# -----------------------------------
# The workload is a spike, not a steady state: a shift arrives at once, so the process
# allocates several large, short-lived buffers together (a decoded frame per queued job,
# an ONNX arena, a PIL bitmap) and drops them all within a second. glibc's malloc keeps a
# large share of that freed memory on its arenas instead of returning it to the kernel, so
# RSS sits at the high-water mark of the busiest minute forever - the spike the box *just
# survived* becomes the level it then *runs at*, and it is the next spike that gets the
# OOM kill. jemalloc's decay settings below hand dirty and muzzy pages back promptly, so
# the resident set tracks live data rather than the peak it once had.
#
# LD_PRELOAD is an image ENV rather than something the entrypoint exports because
# docker-entrypoint.sh re-execs the server through ``setpriv``: an environment variable set
# in the image crosses that exec (setpriv preserves the environment unless --reset-env), so
# the allocator is in place for the process that actually serves traffic. ``muzzy_decay_ms:0``
# is what makes the reclaim prompt; ``background_thread`` moves it off the request path.
ENV LD_PRELOAD=/usr/lib/x86_64-linux-gnu/libjemalloc.so.2 \
    MALLOC_CONF=background_thread:true,dirty_decay_ms:1000,muzzy_decay_ms:0

# The 512 MB instance profile. These are not the defaults - the defaults are the measured
# single-worker values that ``backend/tests/test_face_engine.py`` pins - they are what this
# *deployment* overrides, and each one is a trade this instance has already lost:
#
#   FACE_INFERENCE_QUEUE=8    - at 64, every queued punch holds its decoded frame in memory
#                               while it waits (~10 MB a job, measured), so the queue alone
#                               could ask for ~640 MB against this app's ~290 MB floor. Eight
#                               deep is still far more arrival-burst than two workers drain,
#                               and a job that will not fit is answered 503 + Retry-After
#                               instead of being stacked up invisibly.
#   STANDING_SWEEP_ENABLED=0  - the standing coverage report runs *detectors* on a timer
#                               (a thread, not a task: it is blocking CPU work). On 1 vCPU
#                               that is the sweep competing with the gate for the only core,
#                               and it is a measurement tool, not something the site needs to
#                               clock in. ``python -m coverage_report --once`` is the
#                               cron-shaped way to keep the report current off-box.
ENV FACE_INFERENCE_QUEUE=8 \
    STANDING_SWEEP_ENABLED=0

# Dependencies first, so an application-only change does not re-download the wheels.
#
# TWO MANIFESTS, AND WHY THE SECOND ONE IS HERE
# --------------------------------------------
# requirements.txt is the runtime freeze: what the application imports to do its job.
# backend/requirements-optional.txt is the other kind of dependency - each entry is a feature
# that *degrades* when the package is missing, which is exactly why none of them is in the
# runtime freeze. But a deployment does not merely have to boot; it has to do what it says it
# does, and this one advertises worker push. The VAPID key pair that turns Web Push on is an
# environment setting an operator can supply in a minute, and the *package* is not - without
# ``pywebpush`` in this image, ``push.transport_available()`` answers "the optional pywebpush
# package is not installed" however correct VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY are, the
# startup self-test reports ``worker_push_delivery`` as a degraded check, and a phone that is
# closed never rings. Installing the list here is how the image opts in to the extras; a host
# that wants a leaner image deletes this line and loses the features, not the boot (that is
# what "degrades gracefully" is for).
#
# It also brings ``py-vapid``, which is what ``python -m push --generate-keys``
# (docs/RUNBOOK_WORKER_PUSH.md) generates the key pair with - so the same image that sends a
# push can produce the credentials for it.
COPY requirements.txt backend/requirements-optional.txt .python-version ./
RUN python -m pip install --no-cache-dir -r requirements.txt -r requirements-optional.txt

# The application: the backend module tree (including backend/models/, where the YuNet
# detector ships with the code and facenet128.onnx — 87 MB — must be present, because
# the startup gate refuses to serve without it), and the frontend it serves as static
# files.
COPY backend/ backend/
COPY frontend/ frontend/

# State root. The directories are created *here*, in the image, rather than left to the
# app: ``serve.py`` deliberately refuses to create a missing database directory (a
# silently-papered-over volume looks exactly like a mounted one until the redeploy that
# takes every punch with it), so the image owning the mount point is what makes a
# first boot without a volume work — and attaching a Railway volume at /data is what
# makes the *second* boot keep the first one's data.
#
# Run as a non-root user: the container needs write access to exactly two trees
# (/app for nothing at runtime, /data for all of it), not the filesystem.
RUN mkdir -p /data/worker_photos /data/local_references /data/punch_frames /data/quick_link_photos \
 && useradd --system --uid 10001 --create-home appuser \
 && chown -R appuser:appuser /app /data

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# The entrypoint starts as root ONLY to take ownership of the mounted volume back (a
# platform volume arrives root-owned, and the mount erases the ownership this image
# gave /data), then re-execs the server as appuser. The application never runs with
# privileges; see docker-entrypoint.sh. USER stays root here because chown needs it -
# dropping it at the image level is what produced the Permission denied on the first
# deploy, and dropping it inside the entrypoint is what fixes it without giving the
# app process root.
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]

# Where the app writes. ``config.py`` reads each of these from the environment by name;
# pointing them into /data is the whole persistence story. On Railway: mount a volume
# at /data and every deploy keeps its database, enrolled faces and evidence frames.
ENV DATABASE_PATH=/data/times.db \
    WORKER_PHOTOS_DIR=/data/worker_photos \
    LOCAL_REFS_DIR=/data/local_references \
    PUNCH_FRAMES_DIR=/data/punch_frames \
    QUICK_LINK_PHOTOS_DIR=/data/quick_link_photos

# serve.py reads PORT (Railway injects it) and --tunnel means plain HTTP on this side:
# the platform terminates TLS, and serving our self-signed certificate behind its proxy
# is what breaks GPS and the camera on a worker's phone while the dashboard looks fine.
EXPOSE 8000

# For ``docker run`` / compose, not Railway: the platform has its own
# ``healthcheckPath``. Same route, same contract - it must answer 200 without a session.
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT:-8000}/api/v1/status" || exit 1
