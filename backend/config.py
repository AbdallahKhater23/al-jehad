"""Configuration, loaded once from the environment (and an optional .env).

Design rules
------------
1. **No secret has a default.** ``SECRET_KEY`` is mandatory and must be at least
   32 characters; whitespace counts as missing. A deployment with no signing key
   either refuses every request or signs tokens anybody who read the repository
   can forge, so failing loudly at import is the only safe behaviour.
2. **The database path is absolute and deterministic.** The original code used
   ``sqlite3.connect("times.db")``, i.e. relative to the current directory, which
   is exactly how a stale ``backend/times.db`` came to exist alongside the live
   one. One absolute path removes that class of accident.
3. **Nothing here reaches the network or the database** - this module is imported
   by tools and tests as well as the app.

Generate a key:  python -m config --write-env
"""

from __future__ import annotations

import datetime as _dt
import os
import secrets
import sys
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel

BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BACKEND_DIR.parent
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"

MIN_SECRET_LENGTH = 32

#: Values that look like a placeholder rather than a generated key.
WEAK_SECRETS = frozenset(
    {
        "changeme", "change-me", "secret", "secretkey", "secret-key", "password",
        "test", "testing", "dev", "development", "production", "admin", "default",
        "yoursecretkey", "your-secret-key", "supersecret", "letmein",
    }
)


class ConfigError(RuntimeError):
    """Raised when the deployment is not safely configurable."""


def _env_str(name: str, default: str | None = None) -> str | None:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip()
    return value if value else default


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = _env_str(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = _env_str(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_list(name: str, default: list[str] | None = None) -> list[str]:
    raw = _env_str(name)
    if raw is None:
        return list(default or [])
    return [item.strip() for item in raw.split(",") if item.strip()]


def _env_path(name: str, default: Path) -> Path:
    """Resolve a path against the project root so it never depends on the cwd."""
    raw = _env_str(name)
    candidate = Path(raw).expanduser() if raw else Path(default)
    if not candidate.is_absolute():
        candidate = (PROJECT_ROOT / candidate).resolve()
    return candidate


#: The push services a browser's own ``PushManager`` hands out endpoints for, and the only
#: hosts this server will ever POST a notification to.
#:
#: An **allowlist** rather than a denylist, and that is the whole control. The endpoint in a
#: subscription is a URL a worker supplies and the server later fetches, so a private address,
#: a loopback port or a cloud metadata host is something a worker can simply type. Refusing
#: private ranges alone would leave every *public* host reachable - the attacker's own server,
#: a third party's, an open redirect on any domain - so "not internal" is not the property
#: wanted here. The set of hosts a real browser can produce is small and known, so it is
#: enumerated instead, and everything else is refused at the door.
#:
#: Add to it with ``PUSH_ENDPOINT_HOSTS`` (comma-separated); a deployment running its own
#: push service is the reason that exists. A host matches itself or a *subdomain* of itself
#: only - ``notify.windows.com`` matches ``xyz.notify.windows.com`` and never
#: ``notify.windows.com.evil.test``, which is the shape a suffix check gets wrong.
PUSH_ENDPOINT_HOSTS: tuple[str, ...] = (
    "fcm.googleapis.com",  # Chrome, Edge, Opera (Firebase Cloud Messaging)
    "updates.push.services.mozilla.com",  # Firefox (autopush)
    "push.services.mozilla.com",  # older Firefox endpoints still in the wild
    "web.push.apple.com",  # Safari, macOS and iOS 16.4+
    "push.apple.com",  # older Safari endpoints
    "notify.windows.com",  # Edge / WNS: <region>.notify.windows.com
)


class Settings(BaseModel):
    secret_key: str
    jwt_algorithm: str = "HS256"
    jwt_ttl_hours: float = 12.0          # deliberately longer than the 11h shift cap
    jwt_leeway_seconds: int = 30
    database_path: Path
    backup_dir: Path
    backup_max_age_hours: int = 24
    #: The four directories the application keeps its own files in: biometric templates and
    #: reference selfies, enrollment photos, punch evidence frames, and quick-link punch
    #: selfies. Each is read from the environment for the same reason ``database_path`` is - a
    #: child process inherits the environment but not this process's in-memory state, so a path
    #: that only exists inside the running server is a path a script, a report or a second
    #: worker gets wrong, silently, by writing somewhere nobody looks. Each defaults to the
    #: directory inside the project the application has always used, so a deployment that sets
    #: none of them behaves exactly as before.
    local_refs_dir: Path
    worker_photos_dir: Path
    punch_frames_dir: Path
    quick_link_photos_dir: Path
    #: Where a walk-up registration's photo waits for review (see ``registrations``). Its own
    #: tree rather than the punch spool: a spool file means nothing after its request, and the
    #: startup sweep deletes one an hour old - a registration photo has to survive until an
    #: administrator has looked at it, which is days.
    registration_photos_dir: Path
    #: The *fifth* tree, and the only one that is not application data: the labelled calibration
    #: corpus (see ``corpus``). It is read from the environment for the same reason the other four
    #: are - a child inherits the environment, not this process's state - and it is deliberately its
    #: own directory rather than a corner of ``punch_frames``: a corpus has a different retention
    #: rule, a different reader, and a different reason to exist, and mixing the two would make
    #: "delete the evidence" and "delete the calibration" the same command.
    calibration_corpus_dir: Path
    #: The pre-hardening single origin list. It still works - ``netguard`` merges it into
    #: ``cors_worker_origins`` - because a deployment that had it set was allowing exactly
    #: the worker class, and renaming it out from under them would silently close their
    #: console. New deployments should use the two lists below.
    allowed_origins: list[str] = []

    # -- network hardening (see ``netguard``) -------------------------------
    #  Who may call this API from a browser, and from where.
    #
    #  Two origin classes, because they are two trust levels: a *worker* origin is a phone
    #  that has been handed a session; an *admin* origin is a managed console. An admin
    #  origin may read the whole API, a worker origin may read everything except
    #  ``/admin/*``. Both accept exact origins and one host wildcard
    #  (``https://*.example.com`` - subdomains, not the bare domain: list both if both are
    #  needed). ``*`` is accepted for worker origins only.
    cors_worker_origins: list[str] = []
    cors_admin_origins: list[str] = []
    #: Empty means the middleware's default (GET, POST, OPTIONS / the headers this client
    #: actually sends, including ``ngrok-skip-browser-warning``).
    cors_allowed_methods: list[str] = []
    cors_allowed_headers: list[str] = []
    #: ``Access-Control-Allow-Credentials``. The app authenticates with a bearer token, so
    #: this is only needed for a console that wants to send cookies; it is dropped
    #: automatically when a worker origin list contains ``*`` (the browser forbids the pair).
    cors_allow_credentials: bool = True
    cors_max_age_seconds: int = 600
    #: CIDRs or bare addresses permitted to reach administrator routes. **Empty means no
    #: gate** - the routes are still token-guarded, but any address may attempt them.
    admin_ip_allowlist: list[str] = []
    admin_allowlist_paths: list[str] = ["/admin", "/api/v1/admin"]
    #: Which peers may set ``X-Forwarded-For`` / ``X-Forwarded-Proto``. Loopback by default,
    #: matching what ``serve.py`` hands uvicorn. A proxy on another host must be listed here,
    #: or the real client address is unknown and the allowlist above checks the proxy instead.
    trusted_proxies: list[str] = ["127.0.0.1/32", "::1/128"]
    security_headers_enabled: bool = True
    #: Sent only over TLS (or when a *trusted* proxy reports ``X-Forwarded-Proto: https``).
    #: 0 disables the header entirely for a deployment that terminates TLS somewhere it
    #: cannot see.
    hsts_max_age_seconds: int = 15552000
    #: Document / API policies. Unset uses ``netguard.CSP_HTML`` and ``netguard.CSP_API``.
    #: The bundled policy already refuses inline ``<script>`` elements and names no
    #: third-party origin: the frontend's styles are component classes in ``style.css``, not
    #: utilities compiled in the browser. Set ``CSP_HTML`` to add the sources a customised
    #: page needs, and hashes for any inline block it reintroduces.
    csp_api: str | None = None
    csp_html: str | None = None

    enable_api_docs: bool = False
    face_model_preload: bool = True
    bootstrap_admin_password: str | None = None
    min_password_length: int = 8
    login_rate_limit: str = "10/minute"
    attendance_rate_limit: str = "15/minute"
    readiness_rate_limit: str = "60/minute"
    notification_retention_days: int = 180
    schema_guard_mode: str = "enforce_repair"   # enforce_repair | detect_only
    startup_override_reason: str | None = None
    startup_override_until: str | None = None
    app_version: str = "phase02"

    # -- passive liveness (MiniFASNet ONNX) ---------------------------------
    #  off      : skip the check entirely (tests, emergency)
    #  advisory : record the verdict and notify, never block  <- default, because a
    #             mis-calibrated threshold must not stop a site recording attendance
    #  enforce  : reject a presentation attack before DeepFace runs
    liveness_mode: str = "advisory"
    liveness_model_path: Path = Path("backend/models/minifasnet.onnx")
    liveness_model_sha256: str | None = None
    liveness_input_size: int = 80
    liveness_accept_threshold: float = 0.70
    liveness_reject_threshold: float = 0.55
    #: In ``enforce`` mode, is a missing runtime/model fatal (fail closed, the safe
    #: default) or a warning that lets the punch through?
    liveness_allow_unavailable: bool = False

    # -- face detection (YuNet ONNX, see ``face_detector``) -----------------
    #  Detection was ~70% of a verification: measured 308 ms with MTCNN against 10 ms with
    #  YuNet at the application's own 640x640 working size. The model is 232 KB and ships
    #  in-tree; the override and the pin exist for the same reason the liveness ones do -
    #  an operator who keeps the artifact elsewhere, or wants the server to refuse an
    #  unexpected one. Absent, verification falls back to the previous detector.
    face_detector_model_path: Path = Path("backend/models/face_detection_yunet_2023mar.onnx")
    face_detector_model_sha256: str | None = None

    # -- face embedding (FaceNet-128 ONNX, see ``face_onnx``) ----------------
    #  The recognition half of verification, and the one that decides what a template *is*.
    #  It was VGG-Face: 4096 floats from a 553 MiB TensorFlow graph at ~250 ms and ~2.3 GiB
    #  per worker. The same weights compiled to ONNX measure ~13 ms and ~151 MiB, and the
    #  session costs ~0.2 s to build instead of ~2.7 s - so this is the knob that decides
    #  what a punch costs and how many workers fit on a host.
    #
    #  The path and the pin exist for the same reason the detector's do: an operator who
    #  keeps the artifact outside the tree, or wants the server to refuse an unexpected one.
    #  The graph is 87 MiB and gitignored (see ``backend/models/README.md``), so a host that
    #  has not fetched it must fail loudly at the first embed rather than silently serve.
    facenet_model_path: Path = Path("backend/models/facenet128.onnx")
    facenet_model_sha256: str | None = None

    # -- the FaceNet-512 shadow encoder (see ``shadow_rollout``) ------------
    #  The migration instrument from docs/RUNBOOK_EMBEDDER_MIGRATION.md phase 3: a second
    #  encoder fed the *same* crop, whose opinion is logged and can never change a punch.
    #
    #  Off by default, and it has to be, for two reasons. The graph is a ~90 MiB artifact
    #  that does not ship (``backend/models/README.md``: large weights are fetched per host),
    #  so a deployment that has not fetched it is the normal state rather than a misconfigured
    #  one - and ``shadow_rollout`` reports the absence instead of failing anything. And the
    #  second session costs real memory on a small container, which is a decision an operator
    #  makes rather than one they inherit.
    #
    #  The path is a *drop-in*: put the graph there (or point FACENET_SHADOW_MODEL_PATH at it)
    #  and the migration can start. Nothing else needs to change, because ``facenet_ort`` reads
    #  the embedding width out of the graph rather than assuming it.
    facenet_shadow_model_path: Path = Path("backend/models/facenet512.onnx")
    facenet_shadow_model_sha256: str | None = None
    #  The input contract the shadow graph is fed. Stated as configuration rather than assumed,
    #  because ``shadow_rollout`` feeds one tensor to both encoders - and a graph fed a tensor
    #  it did not ask for returns a plausible vector in the right range while recognising
    #  nobody. It is refused when it differs from the incumbent's; see that module.
    facenet_shadow_contract: str = "bgr:0_1"
    #  The paired log (``shadow_scores``). Its own SQLite file, deliberately: it is migration
    #  instrumentation with a defined end, and it must be droppable without touching a shift.
    #  Empty means "beside the database", which is the volume in a deployment.
    shadow_log_path: str | None = None

    # -- face verification capacity (see ``face_engine``) -------------------
    #  Every punch and every enrollment is an embedding plus a detection, which together are
    #  still the most expensive thing this app does. These bound how much of it can run at
    #  once, and what a caller is told when there is no room.
    #
    #  ``concurrency`` is measured, not guessed. Eight verifications on a CPU-only host:
    #  1 -> 1.90/s, 2 -> 2.55/s, 4 -> 2.56/s. The third and fourth concurrent inference add
    #  no throughput and double the latency a worker waits at the gate, so 2 is the default.
    #  Raise it only if a measurement on YOUR hardware says otherwise - and if the models
    #  ever move out of this process into a model server, this is the number that goes
    #  away (see ``face_engine``).
    face_inference_concurrency: int = 2
    #: How many verifications may wait for a slot. A punch costs one slot for its liveness
    #: check and one for its verification, so 64 is roughly "a whole site arriving at
    #: 04:00" with room to spare. Past it a request is refused with 503 + Retry-After
    #: instead of being stacked up until the process runs out of memory.
    face_inference_queue: int = 64
    #: How long a caller waits for room before being refused. Longer than one inference and
    #: shorter than a phone's patience; together with the queue depth, this is what absorbs a
    #: burst instead of shedding it.
    face_inference_wait_seconds: float = 20.0
    #  ``face_engine_process`` moves the model calls into a child process (see
    #  ``face_process`` and ``face_worker``). **Off by default, and it is a prototype.**
    #
    #  What it buys: the API process stops importing the 87 MiB FaceNet graph, stops
    #  building an ONNX session and stops running a detector, so an ONNX Runtime allocation
    #  failure, a corrupt model or the kernel's OOM killer ends the *child* - which the next
    #  punch restarts - instead of ending the process that serves the gate, the console and
    #  every other endpoint. What it costs: a second interpreter, every frame copied over a
    #  pipe, one inference at a time (see ``face_process``) and a slightly *larger* host
    #  total, because the models now live in a process of their own. This is a decision about
    #  survivability, not about the size of the machine, which is why it is a flag.
    face_engine_process: bool = False
    #: How long one model call may take before the child is treated as hung. It is killed
    #: rather than waited for (replies are matched by id, so a late answer would be applied
    #: to the wrong photograph); generous, because a cold graph load is inside this window.
    face_engine_process_timeout_seconds: float = 60.0
    #: How long the child has to answer the first request - the preload, in practice, which
    #: is where importing onnxruntime and opening 87 MiB of graph actually happens.
    face_engine_process_start_seconds: float = 120.0
    #: The interpreter to spawn. ``None`` means this process's own ``sys.executable``, which
    #: is what keeps the child inside the deployment's venv (or the image's Python).
    face_engine_process_python: str | None = None
    #: How long a child status answer is reused (``/api/v1/status`` and readiness call it).
    face_engine_process_status_seconds: float = 5.0

    # -- rapid enrollment ---------------------------------------------------
    enrollment_token_ttl_hours: int = 72
    enrollment_token_max_uses: int = 1
    #: inherit | off | advisory | enforce. ``inherit`` means enrollment liveness is
    #: never laxer than the attendance policy; set ``enforce`` once the model is
    #: deployed, because a bad reference template is permanent.
    enrollment_liveness_mode: str = "inherit"
    enrollment_base_url: str | None = None
    enrollment_rate_limit: str = "20/minute"

    # -- quick clock links --------------------------------------------------
    #  A link the administrator sends the worker: one tap clocks them in, the next one
    #  clocks them out, with no password. It is a credential, so it expires, it can be
    #  revoked, and every use is logged with the selfie that was taken.
    #
    #  A month by default, because a link is issued to a person rather than to a moment:
    #  a link that expires overnight would be re-issued every morning, and the point of
    #  the feature is that the worker keeps it. ``max_uses`` 0 means "as many taps as the
    #  shift needs" - a single-use link could clock somebody in and never out.
    quick_link_ttl_hours: int = 24 * 30
    quick_link_max_uses: int = 0
    #  The punch endpoint takes a photo, so it is limited like the attendance endpoints
    #  rather than like the enrollment ones: a worker taps a link a handful of times a
    #  day, and anything far above that is a script.
    quick_link_rate_limit: str = "20/minute"
    bulk_enroll_max_rows: int = 500
    bulk_enroll_max_zip_mb: int = 64

    # -- walk-up registration (see ``registrations``) -----------------------
    #  One persistent public link anybody can submit to, and an administrator's decision
    #  before any account exists. Closed by default, on the same reasoning as the
    #  calibration switch below: a public endpoint that collects a face is not something a
    #  deployment should *discover* it is running, and this switch is also the kill switch
    #  for a link that has been forwarded too far.
    registration_enabled: bool = False
    #  How many requests may sit waiting for a decision at once. Enforced inside the same
    #  write that inserts one - a cap read before the insert is only advice.
    registration_pending_cap: int = 200
    #  The link is public and static, so the limit is per IP and deliberately tight: an
    #  honest applicant submits once, and a shift-full of workers at one gate shares an IP.
    registration_rate_limit: str = "20/minute"
    #  How long a *crashed* submission's photo, or a photo with no request behind it, waits
    #  before the startup sweep removes it. Never touches a live pending request.
    registration_photo_stale_hours: int = 24 * 7

    # -- calibration corpus (see ``corpus``) ---------------------------------
    #  Capturing faces for measurement is a separate decision from storing punch evidence, so it is
    #  its own switch and it defaults to **off**: a deployment that turned it on without meaning to
    #  is a biometric store nobody asked for. When it is on, every face is stored with the detector
    #  that produced its crop, under a retention period an operator purges by hand.
    calibration_capture_enabled: bool = False
    #  When non-empty, capture stops at this instant: ISO-8601, UTC, e.g.
    #  ``2026-10-01T00:00:00Z``. Empty means no window (capture runs while the switch is on).
    #  A collection period is a decision with an end, and an operator who says "for a week"
    #  has already named one: leaving that in a calendar reminder means the deployment keeps
    #  collecting faces until somebody remembers, which is the exact failure the switch's own
    #  comment warns about. Expressed as an instant rather than a duration so a restart does
    #  not restart the clock.
    #
    #  Malformed values **stop** capture rather than being ignored: the stricter answer to
    #  "when does this end?" is the safe one when the answer is unreadable, because the other
    #  reading is "indefinitely", applied to a biometric store.
    calibration_capture_until: str = ""
    #  Long edge of a stored frame. 1280 keeps a 1920x1080 gate frame's subject at two thirds of its
    #  native size; 0 keeps the original, which is what an experiment about the small-face regime
    #  wants - because downscaling *moves a corpus into* that regime.
    calibration_corpus_max_px: int = 1280
    #  The age at which a capture is presumptively past its purpose. Not enforced by a timer (the
    #  corpus is not swept by ``retention``), so this is the number an operator passes to
    #  ``python -m tools.corpus purge --older-than-days`` - and it is here rather than in the runbook
    #  so that the policy and the command cannot disagree.
    calibration_corpus_retention_days: int = 180

    # -- offline punch sync -------------------------------------------------
    offline_signature_required: bool = True
    offline_punch_max_age_hours: int = 72
    offline_punch_clock_skew_seconds: int = 300
    offline_batch_max: int = 50
    offline_duplicate_window_seconds: int = 90

    # -- uploads ------------------------------------------------------------
    #  One ceiling for every photo this app accepts: the clock-in selfie, an admin's
    #  enrollment photo, a self-service capture, the photo sent with a registration
    #  link, and every photo inside a bulk ZIP. 5 MB is comfortably above a phone
    #  camera JPEG (2-4 MB) and far below anything an incident report needs to be.
    upload_max_photo_bytes: int = 5 * 1024 * 1024
    #  Long edge of the frame the face pipeline sees - on a punch (clock-in/out, quick
    #  link, offline sync) *and* on every enrollment path, because the template and the
    #  live frame are compared to each other and ``uploads.face_frame`` makes both with
    #  this one number (see that function for why one chain matters more than the value).
    #  640 used to be the number; the working-distance band (0.7-1.5 m, see the framing
    #  coach) puts a face at 53-113 px in a 640-wide frame, and the alignment template
    #  wants ~112 - so beyond ~0.7 m the crop was being upscaled before it was embedded,
    #  and every extra metre cost match score. 1280 keeps the crop at native scale through
    #  ~1.5 m. Latency scales with frame area, not linearly: YuNet on a 1280x960 frame
    #  costs ~55 ms against ~15 ms at 640, and the embedding itself is size-independent
    #  (always a 112x112 crop).
    #
    #  Enrollment used to cap itself at 800, which was worse than it looked: the template
    #  is the *reference* side of every future comparison, so it was the one frame that
    #  could least afford a smaller face than the punch it would be matched against.
    face_frame_max_px: int = 1280

    # -- the ingestion boundary (how big a photo may be before it is refused) --------
    #  ``face_frame_max_px`` above is a *ceiling on the frame a model sees*; these two
    #  are a ceiling on what may be uploaded at all, and they exist because the two are
    #  not the same question.
    #
    #  A frame built from a large photo is not merely slower, it is *different*: the
    #  decode hint that keeps a 12 MP JPEG from becoming a 36 MB bitmap makes libjpeg
    #  resample on a power-of-two grid, so a 4032x3024 photo arrives at the detector as
    #  ~1008 px and a 1280x960 one as 1280 - two different resamplings of the same face,
    #  which the pipeline then measures against a decision margin of ~0.0036. The drift
    #  was measured at up to ~0.0088 on the live pipeline: larger than the margin it is
    #  being compared with, so a punch can cross the line because of the *size of the
    #  photo* rather than the face in it.
    #
    #  Bounding the input bounds that drift, and (with the hint gone) leaves one decode
    #  path for every accepted upload. 4 MP / 2048 px: above the ~1.2 MP a browser canvas
    #  capture produces, below the 12 MP a phone's own camera app produces - so an upload
    #  from the file picker has to be downscaled by the client first, and the refusal says
    #  so. Both are checked; a 3000x1200 photo fails the edge rule while sitting under the
    #  pixel limit, and a 2048x2048 one fails the pixel rule while sitting on the edge.
    face_frame_max_pixels: int = 4_000_000
    face_frame_max_edge_px: int = 2048

    # -- worker notes (the written channel between a worker and the admin) ---
    #  A note is cheap to write and expensive to ignore, so the two controls are a
    #  per-worker cap on *open* notes (db-backed, unaffected by a shared tunnel IP)
    #  and a coarse rate limit on the write endpoints (spam/disk backstop).
    notes_rate_limit: str = "30/minute"
    notes_max_open_per_worker: int = 20
    notes_max_subject_chars: int = 120
    notes_max_body_chars: int = 2000

    # -- Prometheus metrics (see ``telemetry``) -----------------------------
    #  The scrape endpoint is never anonymous. Either a scrape token is configured and a
    #  scraper presents it, or an administrator session is required - which keeps the
    #  endpoint usable for an operator testing by hand without leaving it world-readable on
    #  whatever port this app is reachable on.
    metrics_enabled: bool = True
    #: The token a Prometheus scrape job sends as ``Authorization: Bearer <token>``. Generated
    #: like any other secret: ``python -c "import secrets;print(secrets.token_urlsafe(32))"``.
    #: Left unset, ``/metrics`` falls back to requiring an admin JWT (see the endpoint).
    metrics_token: str | None = None

    # -- worker notifications (Web Push) -------------------------------------
    #  The worker's own channel: a row in ``worker_notifications`` per event, and a Web Push
    #  delivery so it reaches a phone whose app is closed. Push is the one capability here
    #  that needs something this repository cannot ship - a VAPID key pair and the optional
    #  ``pywebpush`` encryption dependency - so it degrades the way liveness and XLSX export
    #  do: the inbox always works, ``GET /worker/me/push`` says whether delivery is possible
    #  and why not, and ``readiness`` raises it as an advisory. Nothing silently pretends to
    #  have notified anybody.
    #
    #  Generate a pair with ``python -m push --generate-keys`` (prints both halves, one line
    #  each). The public half is served to the browser, which is the only place it belongs;
    #  the private half must never be in the frontend, in a log, or in the repository.
    push_enabled: bool = True
    vapid_public_key: str | None = None
    vapid_private_key: str | None = None
    #: Contact for the push service operator: a ``mailto:`` or ``https:`` URL, per RFC 8292.
    #: Push services use it if a subscription starts misbehaving, and some refuse a request
    #: without one.
    vapid_subject: str = "mailto:admin@example.invalid"
    #: How stale a notification may be and still be worth pushing. A phone that was offline
    #: for a day must not be told it crossed the overtime line yesterday as though it were
    #: now - the inbox is where history belongs.
    push_max_age_minutes: int = 15
    #: Attempts per notification before the server stops trying. A push service that answers
    #: 500 for a device that will never accept another message must not be retried for ever.
    push_attempt_limit: int = 3
    #: The hosts a subscription endpoint may point at - see ``PUSH_ENDPOINT_HOSTS``. This is
    #: the SSRF control: without it a worker registers ``https://169.254.169.254/...`` and the
    #: server fetches it on the next notification, from inside the network, with no credential
    #: of the worker's involved.
    push_endpoint_hosts: tuple[str, ...] = PUSH_ENDPOINT_HOSTS

    # -- overtime watcher ---------------------------------------------------
    #  One timer, two rules: the automatic close ends a day at ``regular_hours`` paid, and
    #  the crossing alert fires at ``overtime_notify_hours``. Both run in the same pass, and
    #  the close deletes the session it closed - so a shift it has ended can never be
    #  observed crossing the alert line. Which rule acts therefore decides whether a crossing
    #  is reported at all, and that decision is made in one place
    #  (``shift_hours.day_end_rules``):
    #
    #    * alert below the paid day  -> the alert fires while the shift is open, then the
    #      close ends the standard day at ``regular_hours``;
    #    * alert above the paid day  -> the close **stands down** (the shipped 8.1/8.0 pair),
    #      so the shift runs on, the crossing is reported at the threshold, and the hours
    #      past the paid day are clocked out into overtime review. Nothing is auto-closed at
    #      8 h; set the overtime line strictly below the paid day to get the close back;
    #    * alert on the paid day     -> nothing to observe, so the close acts and the scans
    #      report that the alert cannot fire.
    #
    #  The verdict is logged at startup, published to the console at
    #  ``GET /api/v1/admin/shift_rules`` (as ``day_end``) and raised by
    #  ``GET /api/v1/readiness`` as ``overtime_alert_reachable`` and
    #  ``overtime_close_deferred``.
    #
    #  Turning this off stops *both* rules: nothing closes a forgotten shift and nothing
    #  alerts about one.
    overtime_watcher_enabled: bool = True
    overtime_watcher_interval_seconds: int = 60

    # -- data retention and erasure (see ``retention``) ---------------------
    #  Every period below is in days, and **0 always means keep forever** - the same value
    #  in every knob, so an operator never has to remember which one spells "off"
    #  differently. The period that is not here is the one that must not exist: there is no
    #  setting to delete ``attendance_logs``, because hours worked are pay records and a
    #  retention policy that removes them is a wage dispute with a timestamp.
    #
    #  The defaults are the ones an auditor expects to see: a month of raw punch selfies,
    #  a year of administrative audit, and biometric residue gone a week after it stops
    #  being someone's. Deactivation already deletes a face immediately; a week is the net
    #  under that, not a grace period for keeping it.
    retention_enabled: bool = True
    #: How often the sweeper runs. Six hours: the periods above are measured in days, so
    #: anything faster only costs writes, and anything slower makes a failure take longer to
    #: notice than to matter. The timer is a thread in this process; a deployment that would
    #: rather schedule it externally calls ``python -m retention --apply`` from cron and sets
    #: ``RETENTION_ENABLED=0`` here.
    retention_interval_seconds: int = 6 * 3600
    #: The first sweep after boot is delayed, deliberately: a boot already competes with the
    #: model preload, the schema guard and the first shift's punches for the same CPU and the
    #: same SQLite write lock, and retention is not urgent by hours.
    retention_initial_delay_seconds: int = 300
    #: Run without deleting anything, forever. Present because some operators want to watch a
    #: report for a while before trusting it, and because a staging deployment should exercise
    #: the same code path it will run in production.
    retention_dry_run: bool = False
    #: Raw punch selfies, one per tap of a clock link. The largest accumulation of faces
    #: here, and the one belonging to people who may never have been enrolled at all.
    retention_punch_photo_days: int = 30
    #: Downscaled punch frames, the review-card evidence stored with every punch (see
    #: ``punch_frames.py``). Same window as the selfies they were derived from: evidence has
    #: no reason to outlive the photograph it was cropped from.
    retention_punch_frame_days: int = 30
    #: Biometric residue: templates and selfies for accounts that are no longer active, files
    #: with no owning account, quarantined legacy names, and half-written staging files.
    retention_biometric_days: int = 7
    #: Administrative audit rows - the answer to "who changed this, and when".
    retention_audit_days: int = 365
    #: Raw offline punches once they have a record in ``attendance_logs`` (or were refused).
    #: They carry coordinates to phone accuracy, so they are location history.
    retention_punch_queue_days: int = 90
    #: Offline replay anchors: anti-replay metadata, useful only until the sync window closes.
    retention_anchor_days: int = 7
    #: How many items one sweep removes. The one knob where 0 does not mean "keep it" but "no
    #: cap": the sweep holds SQLite's single write lock while it works, and a first run over a
    #: large backlog would otherwise make a punch wait on the lock and fail. A backlog is
    #: reported as ``deferred`` and finishes over the next few sweeps.
    retention_max_items_per_sweep: int = 20_000
    #: Who a sweep is attributed to in the audit event, when it was not the timer. An operator
    #: running ``--apply`` by hand should set this: "the system deleted 1 400 audit rows" and
    #: "A. Engineer deleted 1 400 audit rows" are different sentences in an investigation.
    retention_actor: str | None = None

    # -- the standing detector-coverage report (see ``coverage_report``) ----
    #  ``tools/coverage_sweep.py`` measures which faces the detectors find, on this
    #  deployment's own frames, and is the evidence for or against moving to SCRFD. Run by
    #  hand it answers for the month somebody sampled; the timer below re-answers it when the
    #  gate's frame folder grows, and flags the run where SCRFD overtakes YuNet. The cost is
    #  real - a detector pass over a sample of frames on the box that serves punches - which is
    #  why the sample is capped, the cadence is daily, and *none* of it runs before the models
    #  have preloaded and the first shift's punches have gone through.
    standing_sweep_enabled: bool = True
    standing_sweep_interval_seconds: int = 24 * 3600
    #: The first check after boot waits past the preload for the same reason retention's does:
    #: measuring is CPU work on the box every punch shares.
    standing_sweep_initial_delay_seconds: int = 600
    #: Where the gate's own frames are. This is *not* the calibration corpus: these are the
    #: frames a punch actually stored, so the report measures the deployment rather than a
    #: curated sample of it, and it needs no capture flag to have content.
    standing_sweep_corpus_dir: Path = Path("punch_frames")
    #: Where snapshots and the trigger state live. Point this at the volume on a platform
    #: (``STANDING_SWEEP_DIR=/data/coverage_reports``) if the reports are wanted across
    #: deploys; the default is beside the app, which is ephemeral - a report is regenerated,
    #: so losing it costs one cadence.
    standing_sweep_dir: Path = Path("coverage_reports")
    #: How many of the newest frames one measurement reads. This is the entire cost control:
    #: four configurations over 200 frames is about 90 seconds of CPU on a bare server, and
    #: the report says how many frames the folder actually holds beside the sample.
    standing_sweep_sample: int = 200
    #: New frames, not frames: the finding is a *change* in what the cameras show, and a
    #: handful of new frames cannot move a found-share by more than noise.
    standing_sweep_min_new_frames: int = 25
    #: The margin, in frames, that counts as SCRFD overtaking YuNet. One is the smallest
    #: honest step - a tie is reported as a tie - and a deployment that only wants to hear
    #: about a decisive move raises it.
    standing_sweep_min_margin_frames: int = 1
    #: Run snapshots to keep. They are the evidence behind a flag, so the newest few are kept
    #: and the older ones are pruned; 0 keeps every run.
    standing_sweep_keep: int = 5
    #: The face-detector gate the sweep applies. Left at the punch path's own threshold by
    #: default, because a measurement taken under a different gate is not evidence about the
    #: gate - raise or lower it only to ask a question about the threshold itself.
    standing_sweep_min_score: float = 0.6
    #: The counting rule: ``exactly_one`` (the punch path's - a second face is a refusal) or
    #: ``any`` (a face is found if any detection clears the gate). The default is the rule the
    #: deployment enforces; ``any`` is the one that answers how far a detector reaches, and the
    #: value travels into the report either way.
    standing_sweep_subject: str = "exactly_one"

    @property
    def startup_override_active(self) -> bool:
        """True only for a *reasoned* and possibly time-limited override.

        A bare boolean is deliberately not accepted: an unattributed escape hatch
        is indistinguishable from a permanent bypass, and the reason lands in the
        audit trail. An override past its expiry is ignored so a forgotten .env
        line cannot disable the gate forever.
        """
        if not self.startup_override_reason:
            return False
        if not self.startup_override_until:
            return True
        try:
            until = _dt.datetime.fromisoformat(self.startup_override_until)
        except ValueError:
            return False
        if until.tzinfo is not None:
            now = _dt.datetime.now(until.tzinfo)
        else:
            now = _dt.datetime.now()
        return now < until

    @property
    def secret_key_fingerprint(self) -> str:
        import hashlib

        return hashlib.sha256(self.secret_key.encode("utf-8")).hexdigest()[:8]

    def describe(self) -> dict:
        return {
            "database_path": str(self.database_path),
            "backup_dir": str(self.backup_dir),
            # The file trees, because "where did it put the photo" is the first question when
            # an enrollment or a punch says it worked and nothing appears in the expected place.
            "local_refs_dir": str(self.local_refs_dir),
            "worker_photos_dir": str(self.worker_photos_dir),
            "punch_frames_dir": str(self.punch_frames_dir),
            "quick_link_photos_dir": str(self.quick_link_photos_dir),
            "registration_photos_dir": str(self.registration_photos_dir),
            "registration_enabled": self.registration_enabled,
            "registration_pending_cap": self.registration_pending_cap,
            "calibration_corpus_dir": str(self.calibration_corpus_dir),
            "calibration_capture_enabled": self.calibration_capture_enabled,
            "calibration_corpus_max_px": self.calibration_corpus_max_px,
            "calibration_corpus_retention_days": self.calibration_corpus_retention_days,
            "jwt_algorithm": self.jwt_algorithm,
            "jwt_ttl_hours": self.jwt_ttl_hours,
            "enable_api_docs": self.enable_api_docs,
            "allowed_origins": self.allowed_origins,
            # Printed as the two lists an operator actually sets, plus the two that decide
            # whether the admin gate and HSTS are doing anything at all.
            "cors_worker_origins": self.cors_worker_origins,
            "cors_admin_origins": self.cors_admin_origins,
            "cors_allow_credentials": self.cors_allow_credentials,
            "admin_ip_allowlist": self.admin_ip_allowlist,
            "admin_allowlist_paths": self.admin_allowlist_paths,
            "admin_gate_enabled": bool(self.admin_ip_allowlist),
            "trusted_proxies": self.trusted_proxies,
            "security_headers": self.security_headers_enabled,
            "hsts_max_age_seconds": self.hsts_max_age_seconds,
            "schema_guard_mode": self.schema_guard_mode,
            "app_version": self.app_version,
            "liveness_mode": self.liveness_mode,
            "liveness_model_path": str(self.liveness_model_path),
            # Printed because it is a decision an operator made (or accepted), and the first
            # question asked of a slow gate is what this process is allowing itself to run.
            "facenet_model_path": str(self.facenet_model_path),
            # The shadow encoder is reported for the same reason, and one more: whether this
            # path exists is what decides whether a 128-to-512 migration has begun at all.
            "facenet_shadow_model_path": str(self.facenet_shadow_model_path),
            "facenet_shadow_contract": self.facenet_shadow_contract,
            # Printed because it is a decision an operator made (or accepted), and the first
            # question asked of a slow gate is what this process is allowing itself to run.
            "face_inference_concurrency": self.face_inference_concurrency,
            "face_inference_queue": self.face_inference_queue,
            "face_inference_wait_seconds": self.face_inference_wait_seconds,
            # Printed because it changes where the memory goes, and an operator looking at
            # two processes has to be told that this was a decision rather than a leak.
            "face_engine_process": self.face_engine_process,
            # Printed because a retention policy nobody can read is a policy nobody follows,
            # and because the numbers are the first thing an auditor asks to see.
            "metrics_enabled": self.metrics_enabled,
            # Reported as a boolean, never the token itself: this output is printed, pasted
            # into tickets and captured in deployment logs.
            "metrics_require_token": bool(self.metrics_token),
            "retention_enabled": self.retention_enabled,
            "retention_dry_run": self.retention_dry_run,
            "retention_days": {
                "punch_photo": self.retention_punch_photo_days,
                "punch_frame": self.retention_punch_frame_days,
                "biometric": self.retention_biometric_days,
                "audit": self.retention_audit_days,
                "notification": self.notification_retention_days,
                "punch_queue": self.retention_punch_queue_days,
                "anchor": self.retention_anchor_days,
            },
        }


def _validate_secret(raw: str | None) -> str:
    value = (raw or "").strip()
    if not value:
        raise ConfigError(
            "SECRET_KEY is not set. Tokens cannot be signed without it.\n"
            "  Fix:  python -m config --write-env        (writes .env with a generated key)\n"
            "  Or:   set SECRET_KEY to at least 32 random characters."
        )
    if len(value) < MIN_SECRET_LENGTH:
        raise ConfigError(
            f"SECRET_KEY is too short ({len(value)} chars); at least {MIN_SECRET_LENGTH} are required "
            "because it signs authentication tokens.\n"
            "  Fix:  python -m config --write-env"
        )
    if value.lower() in WEAK_SECRETS or len(set(value)) <= 3:
        raise ConfigError(
            "SECRET_KEY looks like a placeholder rather than a generated key, so tokens would be "
            "forgeable by anyone who guesses it.\n"
            "  Fix:  python -m config --write-env"
        )
    return value


def build_settings(*, env_file: Path | None = None) -> Settings:
    """Load .env (without overriding real environment variables) and validate."""
    candidate = Path(env_file or _env_str("ENV_FILE") or DEFAULT_ENV_FILE)
    if candidate.exists():
        load_dotenv(candidate, override=False)

    raw_db = _env_str("DATABASE_PATH")
    database_path = Path(raw_db).expanduser() if raw_db else (PROJECT_ROOT / "times.db")
    if not database_path.is_absolute():
        database_path = (PROJECT_ROOT / database_path).resolve()

    raw_backup_dir = _env_str("BACKUP_DIR")
    backup_dir = Path(raw_backup_dir).expanduser() if raw_backup_dir else (PROJECT_ROOT / "backups")
    if not backup_dir.is_absolute():
        backup_dir = (PROJECT_ROOT / backup_dir).resolve()

    mode = (_env_str("SCHEMA_GUARD_MODE", "enforce_repair") or "enforce_repair").lower()
    if mode not in {"enforce_repair", "detect_only"}:
        mode = "enforce_repair"

    liveness_mode = (_env_str("LIVENESS_MODE", "advisory") or "advisory").lower()
    if liveness_mode not in {"off", "advisory", "enforce"}:
        liveness_mode = "advisory"

    enrollment_liveness_mode = (_env_str("ENROLLMENT_LIVENESS_MODE", "inherit") or "inherit").lower()
    if enrollment_liveness_mode not in {"inherit", "off", "advisory", "enforce"}:
        enrollment_liveness_mode = "inherit"

    return Settings(
        secret_key=_validate_secret(_env_str("SECRET_KEY")),
        jwt_algorithm=_env_str("JWT_ALGORITHM", "HS256") or "HS256",
        jwt_ttl_hours=_env_float("JWT_TTL_HOURS", 12.0),
        jwt_leeway_seconds=_env_int("JWT_LEEWAY_SECONDS", 30),
        database_path=database_path,
        backup_dir=backup_dir,
        backup_max_age_hours=_env_int("BACKUP_MAX_AGE_HOURS", 24),
        # Read through ``_env_path`` so a relative value is resolved against the project root
        # rather than the cwd, the way ``DATABASE_PATH`` is.
        local_refs_dir=_env_path("LOCAL_REFS_DIR", PROJECT_ROOT / "local_references"),
        worker_photos_dir=_env_path("WORKER_PHOTOS_DIR", PROJECT_ROOT / "worker_photos"),
        punch_frames_dir=_env_path("PUNCH_FRAMES_DIR", PROJECT_ROOT / "punch_frames"),
        quick_link_photos_dir=_env_path("QUICK_LINK_PHOTOS_DIR", PROJECT_ROOT / "quick_link_photos"),
        registration_photos_dir=_env_path("REGISTRATION_PHOTOS_DIR", PROJECT_ROOT / "registration_photos"),
        calibration_corpus_dir=_env_path("CALIBRATION_CORPUS_DIR", PROJECT_ROOT / "calibration_corpus"),
        allowed_origins=_env_list("ALLOWED_ORIGINS"),
        cors_worker_origins=_env_list("CORS_WORKER_ORIGINS"),
        cors_admin_origins=_env_list("CORS_ADMIN_ORIGINS"),
        cors_allowed_methods=[item.upper() for item in _env_list("CORS_ALLOWED_METHODS")],
        cors_allowed_headers=_env_list("CORS_ALLOWED_HEADERS"),
        cors_allow_credentials=_env_flag("CORS_ALLOW_CREDENTIALS", True),
        cors_max_age_seconds=_env_int("CORS_MAX_AGE_SECONDS", 600),
        admin_ip_allowlist=_env_list("ADMIN_IP_ALLOWLIST"),
        admin_allowlist_paths=_env_list("ADMIN_ALLOWLIST_PATHS", ["/admin", "/api/v1/admin"]),
        trusted_proxies=_env_list("TRUSTED_PROXIES", ["127.0.0.1/32", "::1/128"]),
        security_headers_enabled=_env_flag("SECURITY_HEADERS", True),
        hsts_max_age_seconds=_env_int("HSTS_MAX_AGE_SECONDS", 15552000),
        csp_api=_env_str("CSP_API"),
        csp_html=_env_str("CSP_HTML"),
        enable_api_docs=_env_flag("ENABLE_API_DOCS", False),
        face_model_preload=_env_flag("FACE_MODEL_PRELOAD", True),
        bootstrap_admin_password=_env_str("BOOTSTRAP_ADMIN_PASSWORD"),
        min_password_length=_env_int("MIN_PASSWORD_LENGTH", 8),
        login_rate_limit=_env_str("LOGIN_RATE_LIMIT", "10/minute") or "10/minute",
        attendance_rate_limit=_env_str("ATTENDANCE_RATE_LIMIT", "15/minute") or "15/minute",
        readiness_rate_limit=_env_str("READINESS_RATE_LIMIT", "60/minute") or "60/minute",
        notification_retention_days=_env_int("NOTIFICATION_RETENTION_DAYS", 180),
        schema_guard_mode=mode,
        startup_override_reason=_env_str("STARTUP_OVERRIDE_REASON"),
        startup_override_until=_env_str("STARTUP_OVERRIDE_UNTIL"),
        liveness_mode=liveness_mode,
        liveness_model_path=_env_path("LIVENESS_MODEL_PATH", PROJECT_ROOT / "backend/models/minifasnet.onnx"),
        liveness_model_sha256=_env_str("LIVENESS_MODEL_SHA256"),
        face_detector_model_path=_env_path(
            "FACE_DETECTOR_MODEL_PATH",
            PROJECT_ROOT / "backend/models/face_detection_yunet_2023mar.onnx",
        ),
        face_detector_model_sha256=_env_str("FACE_DETECTOR_MODEL_SHA256"),
        facenet_model_path=_env_path(
            "FACENET_MODEL_PATH", PROJECT_ROOT / "backend/models/facenet128.onnx"
        ),
        facenet_model_sha256=_env_str("FACENET_MODEL_SHA256"),
        facenet_shadow_model_path=_env_path(
            "FACENET_SHADOW_MODEL_PATH", PROJECT_ROOT / "backend/models/facenet512.onnx"
        ),
        facenet_shadow_model_sha256=_env_str("FACENET_SHADOW_MODEL_SHA256"),
        facenet_shadow_contract=_env_str("FACENET_SHADOW_CONTRACT") or "bgr:0_1",
        shadow_log_path=_env_str("SHADOW_LOG_PATH"),
        liveness_input_size=_env_int("LIVENESS_INPUT_SIZE", 80),
        liveness_accept_threshold=_env_float("LIVENESS_ACCEPT_THRESHOLD", 0.70),
        liveness_reject_threshold=_env_float("LIVENESS_REJECT_THRESHOLD", 0.55),
        liveness_allow_unavailable=_env_flag("LIVENESS_ALLOW_UNAVAILABLE", False),
        enrollment_token_ttl_hours=_env_int("ENROLLMENT_TOKEN_TTL_HOURS", 72),
        enrollment_token_max_uses=_env_int("ENROLLMENT_TOKEN_MAX_USES", 1),
        enrollment_liveness_mode=enrollment_liveness_mode,
        enrollment_base_url=_env_str("ENROLLMENT_BASE_URL"),
        enrollment_rate_limit=_env_str("ENROLLMENT_RATE_LIMIT", "20/minute") or "20/minute",
        face_inference_concurrency=_env_int("FACE_INFERENCE_CONCURRENCY", 2),
        face_inference_queue=_env_int("FACE_INFERENCE_QUEUE", 64),
        face_inference_wait_seconds=_env_float("FACE_INFERENCE_WAIT_SECONDS", 20.0),
        face_engine_process=_env_flag("FACE_ENGINE_PROCESS", False),
        face_engine_process_timeout_seconds=_env_float(
            "FACE_ENGINE_PROCESS_TIMEOUT_SECONDS", 60.0
        ),
        face_engine_process_start_seconds=_env_float("FACE_ENGINE_PROCESS_START_SECONDS", 120.0),
        face_engine_process_python=_env_str("FACE_ENGINE_PROCESS_PYTHON"),
        face_engine_process_status_seconds=_env_float(
            "FACE_ENGINE_PROCESS_STATUS_SECONDS", 5.0
        ),
        bulk_enroll_max_rows=_env_int("BULK_ENROLL_MAX_ROWS", 500),
        bulk_enroll_max_zip_mb=_env_int("BULK_ENROLL_MAX_ZIP_MB", 64),
        quick_link_ttl_hours=_env_int("QUICK_LINK_TTL_HOURS", 24 * 30),
        quick_link_max_uses=_env_int("QUICK_LINK_MAX_USES", 0),
        quick_link_rate_limit=_env_str("QUICK_LINK_RATE_LIMIT", "20/minute") or "20/minute",
        offline_signature_required=_env_flag("OFFLINE_SIGNATURE_REQUIRED", True),
        offline_punch_max_age_hours=_env_int("OFFLINE_PUNCH_MAX_AGE_HOURS", 72),
        offline_punch_clock_skew_seconds=_env_int("OFFLINE_PUNCH_CLOCK_SKEW_SECONDS", 300),
        offline_batch_max=_env_int("OFFLINE_BATCH_MAX", 50),
        offline_duplicate_window_seconds=_env_int("OFFLINE_DUPLICATE_WINDOW_SECONDS", 90),
        upload_max_photo_bytes=_env_int("UPLOAD_MAX_PHOTO_BYTES", 5 * 1024 * 1024),
        face_frame_max_px=_env_int(
            # ``PUNCH_SELFIE_MAX_PX`` is what this was called when it only sized punches. It
            # is still read as the fallback so a deployment that set it keeps the value it
            # chose instead of silently reverting to the default.
            "FACE_FRAME_MAX_PX",
            _env_int("PUNCH_SELFIE_MAX_PX", 1280),
        ),
        face_frame_max_pixels=_env_int("FACE_FRAME_MAX_PIXELS", 4_000_000),
        face_frame_max_edge_px=_env_int("FACE_FRAME_MAX_EDGE_PX", 2048),
        notes_rate_limit=_env_str("NOTES_RATE_LIMIT", "30/minute") or "30/minute",
        notes_max_open_per_worker=_env_int("NOTES_MAX_OPEN_PER_WORKER", 20),
        notes_max_subject_chars=_env_int("NOTES_MAX_SUBJECT_CHARS", 120),
        notes_max_body_chars=_env_int("NOTES_MAX_BODY_CHARS", 2000),
        metrics_enabled=_env_flag("METRICS_ENABLED", True),
        metrics_token=_env_str("METRICS_TOKEN"),
        push_enabled=_env_flag("PUSH_ENABLED", True),
        calibration_capture_enabled=_env_flag("CALIBRATION_CAPTURE_ENABLED", False),
        registration_enabled=_env_flag("REGISTRATION_ENABLED", False),
        registration_pending_cap=_env_int("REGISTRATION_PENDING_CAP", 200),
        registration_rate_limit=_env_str("REGISTRATION_RATE_LIMIT", "20/minute") or "20/minute",
        registration_photo_stale_hours=_env_int("REGISTRATION_PHOTO_STALE_HOURS", 24 * 7),
        calibration_capture_until=_env_str("CALIBRATION_CAPTURE_UNTIL") or "",
        calibration_corpus_max_px=_env_int("CALIBRATION_CORPUS_MAX_PX", 1280),
        calibration_corpus_retention_days=_env_int("CALIBRATION_CORPUS_RETENTION_DAYS", 180),
        vapid_public_key=_env_str("VAPID_PUBLIC_KEY"),
        vapid_private_key=_env_str("VAPID_PRIVATE_KEY"),
        vapid_subject=_env_str("VAPID_SUBJECT", "mailto:admin@example.invalid") or "mailto:admin@example.invalid",
        push_max_age_minutes=_env_int("PUSH_MAX_AGE_MINUTES", 15),
        push_attempt_limit=_env_int("PUSH_ATTEMPT_LIMIT", 3),
        push_endpoint_hosts=tuple(
            host.lower().strip(".") for host in (_env_list("PUSH_ENDPOINT_HOSTS") or PUSH_ENDPOINT_HOSTS)
        ),
        overtime_watcher_enabled=_env_flag("OVERTIME_WATCHER_ENABLED", True),
        overtime_watcher_interval_seconds=_env_int("OVERTIME_WATCHER_INTERVAL_SECONDS", 60),
        retention_enabled=_env_flag("RETENTION_ENABLED", True),
        retention_interval_seconds=_env_int("RETENTION_INTERVAL_SECONDS", 6 * 3600),
        retention_initial_delay_seconds=_env_int("RETENTION_INITIAL_DELAY_SECONDS", 300),
        retention_dry_run=_env_flag("RETENTION_DRY_RUN", False),
        retention_punch_photo_days=_env_int("RETENTION_PUNCH_PHOTO_DAYS", 30),
        retention_punch_frame_days=_env_int("RETENTION_PUNCH_FRAME_DAYS", 30),
        retention_biometric_days=_env_int("RETENTION_BIOMETRIC_DAYS", 7),
        retention_audit_days=_env_int("RETENTION_AUDIT_DAYS", 365),
        retention_punch_queue_days=_env_int("RETENTION_PUNCH_QUEUE_DAYS", 90),
        retention_anchor_days=_env_int("RETENTION_ANCHOR_DAYS", 7),
        retention_max_items_per_sweep=_env_int("RETENTION_MAX_ITEMS_PER_SWEEP", 20_000),
        retention_actor=_env_str("RETENTION_ACTOR"),
        #  The standing coverage report. The interval has a floor of five minutes - a timer that
        #  measured every few seconds would be a load test on the gate that carries it - and the
        #  sample cap counts frames, not time, because that is what the cost is proportional to.
        standing_sweep_enabled=_env_flag("STANDING_SWEEP_ENABLED", True),
        standing_sweep_interval_seconds=max(
            300, _env_int("STANDING_SWEEP_INTERVAL_SECONDS", 24 * 3600)
        ),
        standing_sweep_initial_delay_seconds=max(
            0, _env_int("STANDING_SWEEP_INITIAL_DELAY_SECONDS", 600)
        ),
        standing_sweep_corpus_dir=_env_path(
            "STANDING_SWEEP_CORPUS_DIR",
            _env_path("PUNCH_FRAMES_DIR", PROJECT_ROOT / "punch_frames"),
        ),
        standing_sweep_dir=_env_path("STANDING_SWEEP_DIR", PROJECT_ROOT / "coverage_reports"),
        standing_sweep_sample=max(0, _env_int("STANDING_SWEEP_SAMPLE", 200)),
        standing_sweep_min_new_frames=max(0, _env_int("STANDING_SWEEP_MIN_NEW_FRAMES", 25)),
        standing_sweep_min_margin_frames=max(
            0, _env_int("STANDING_SWEEP_MIN_MARGIN_FRAMES", 1)
        ),
        standing_sweep_keep=max(0, _env_int("STANDING_SWEEP_KEEP", 5)),
        standing_sweep_min_score=_env_float("STANDING_SWEEP_MIN_SCORE", 0.6),
        standing_sweep_subject=_env_str("STANDING_SWEEP_SUBJECT", "exactly_one") or "exactly_one",
    )


#: Import-time singleton. A misconfigured deployment must not start at all...
#:
#: ...except when this file is itself the program being run. ``python -m config
#: --write-env`` exists precisely for a deployment that has no key yet, so requiring a
#: valid key at import would make the documented fix impossible to execute - which is
#: exactly what happened: the command died with "SECRET_KEY is not set" while trying to
#: print the message telling the operator to run it. ``main()`` validates explicitly and
#: reports failures through its own error handling.
if __name__ == "__main__":
    settings = None  # type: ignore[assignment]
else:
    settings = build_settings()


def write_env_file(path: Path | None = None, *, overwrite: bool = False) -> Path:
    """Create a .env with a generated SECRET_KEY (operator convenience)."""
    target = Path(path or DEFAULT_ENV_FILE)
    if target.exists() and not overwrite:
        raise ConfigError(f"{target} already exists; pass --overwrite to replace it")
    target.write_text(
        "# Site Attendance configuration. NEVER commit this file.\n"
        f"SECRET_KEY={secrets.token_urlsafe(48)}\n"
        "DATABASE_PATH=\n"
        "# The four directories this app writes its own files into: biometric templates and\n"
        "# reference selfies, enrollment photos, punch evidence frames, and quick-link punch\n"
        "# selfies. Empty means the project's own directories. Set them when something else owns\n"
        "# the disk - a mounted volume, a different service account - and remember that they are\n"
        "# read by *child processes too*: any script that imports this app writes where these\n"
        "# point, not where the server's copy happens to be:\n"
        "LOCAL_REFS_DIR=\n"
        "WORKER_PHOTOS_DIR=\n"
        "PUNCH_FRAMES_DIR=\n"
        "QUICK_LINK_PHOTOS_DIR=\n"
        "# Origins allowed to call this API from a browser. Two classes, because they are two\n"
        "# trust levels: a worker origin may read everything except /admin/*, an admin origin\n"
        "# may read all of it. Comma-separated; 'https://*.example.com' matches subdomains.\n"
        "#\n"
        "# Empty is the correct answer for the Cloudflare deployment, not an omission: the Worker\n"
        "# serves the frontend and proxies /api, /static, /enroll and /q to this service, so the\n"
        "# browser only ever sees one origin and there is nothing to allow (deploy/cloudflare/\n"
        "# worker.mjs). A page on one of these hosts calls *that* host - the frontend resolves its\n"
        "# API base as the page's own origin. Name origins here only for a client that really is\n"
        "# on another origin, e.g. the console, or the diagnostics fallback that points a browser\n"
        "# straight at the backend ('https://<worker>.workers.dev,https://<service>.up.railway.app').\n"
        "# Never '*' for the admin class, and never a wildcard as a way to avoid naming a pair:\n"
        "# it grants every origin on every path, /admin/* included.\n"
        "ALLOWED_ORIGINS=\n"
        "CORS_WORKER_ORIGINS=\n"
        "CORS_ADMIN_ORIGINS=\n"
        "# Addresses permitted to reach administrator routes (empty = no network gate):\n"
        "ADMIN_IP_ALLOWLIST=\n"
        "# Which peers may set X-Forwarded-For. Loopback by default; list a proxy on another\n"
        "# host here, or the allowlist above ends up checking the proxy's address.\n"
        "TRUSTED_PROXIES=127.0.0.1/32,::1/128\n"
        "ENABLE_API_DOCS=0\n"
        "JWT_TTL_HOURS=12\n"
        "SCHEMA_GUARD_MODE=enforce_repair\n"
        "BACKUP_MAX_AGE_HOURS=24\n"
        "# Emergency only - both lines are required, and the reason is audited. Which faults\n"
        "# this may cover, and how to get back to a healthy deployment, is\n"
        "# docs/RUNBOOK_STARTUP_OVERRIDE.md; a start with no reason is refused, and two\n"
        "# checks (secret_key_configured, database_reachable) can never be overridden:\n"
        "# STARTUP_OVERRIDE_REASON=\n"
        "# STARTUP_OVERRIDE_UNTIL=2026-01-01T06:00:00+02:00\n"
        "# Web Push: the notice that reaches a worker whose app is closed. Generate a pair with\n"
        "# `cd backend && python -m push --generate-keys` (needs the optional pywebpush) and\n"
        "# read docs/RUNBOOK_WORKER_PUSH.md. The private half is a secret: never commit it.\n"
        "# Without a pair the worker's inbox still records every event; only the buzz is missing.\n"
        "# VAPID_PUBLIC_KEY=\n"
        "# VAPID_PRIVATE_KEY=\n"
        "# The contact address a push service can reach you at (a mailto: or https: URL):\n"
        "# VAPID_SUBJECT=mailto:admin@example.invalid\n"
        "# Set PUSH_ENABLED=0 to stop all ringing without touching the stored subscriptions:\n"
        "# PUSH_ENABLED=1\n"
        "# Data retention, in days. 0 means keep forever. Read the README before raising these:\n"
        "RETENTION_PUNCH_PHOTO_DAYS=30\n"
        "RETENTION_BIOMETRIC_DAYS=7\n"
        "RETENTION_AUDIT_DAYS=365\n"
        "# Set RETENTION_DRY_RUN=1 to report without deleting while you evaluate it:\n"
        "RETENTION_DRY_RUN=0\n"
        "# The standing detector-coverage report: re-measures which detector settings find this\n"
        "# deployment's own punch frames, and notifies when SCRFD overtakes YuNet. It reads\n"
        "# PUNCH_FRAMES_DIR by default, samples the newest STANDING_SWEEP_SAMPLE frames once a\n"
        "# day, and only measures when at least STANDING_SWEEP_MIN_NEW_FRAMES have landed.\n"
        "# Read docs/RUNBOOK_EMBEDDER_MIGRATION.md first; STANDING_SWEEP_ENABLED=0 turns it off,\n"
        "# and the same run is available by hand as `python -m coverage_report` (`--force`\n"
        "# measures regardless of the trigger):\n"
        "STANDING_SWEEP_ENABLED=1\n"
        "# STANDING_SWEEP_DIR=/data/coverage_reports\n"
        "# STANDING_SWEEP_INTERVAL_SECONDS=86400\n"
        "# STANDING_SWEEP_SAMPLE=200\n"
        "# STANDING_SWEEP_MIN_NEW_FRAMES=25\n"
        "# STANDING_SWEEP_MIN_MARGIN_FRAMES=1\n"
        "# Prometheus scrape token for GET /metrics. Leave empty to require an admin JWT:\n"
        "METRICS_TOKEN=\n",
        encoding="utf-8",
    )
    try:
        os.chmod(target, 0o600)
    except OSError:  # pragma: no cover - cosmetic on NTFS
        pass
    return target


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--write-env" in argv:
        try:
            path = write_env_file(overwrite="--overwrite" in argv)
        except ConfigError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"Wrote {path}\nRestart the server to use it.")
        return 0
    try:
        current = build_settings()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"Configuration OK (secret fingerprint {current.secret_key_fingerprint})")
    for key, value in current.describe().items():
        print(f"  {key}: {value}")
    print(f"  startup_override_active: {current.startup_override_active}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
