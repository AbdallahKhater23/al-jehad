"""Prometheus metrics: what this service is doing, in a form a scraper can read.

WHY THIS EXISTS
---------------
The two things most likely to take this application down are the two things that were
invisible. A punch costs half a second of VGG-Face inference plus an MTCNN detection
(``face_engine``), and every request that writes takes SQLite's single write lock - so the
questions an operator actually has are "how long is inference taking, and is the queue
backing up?" and "how long are we holding the write lock, and is anything timing out on
it?". Before this module the answers were a log line per failure and a ``stats()`` dict
that only a running process could see.

Three properties were designed for, and each one rules something out:

1. **A scrape must not change what it measures.** Nothing here queries the database or
   touches the filesystem per request: the counters are incremented where the event already
   happens, and the two values that *are* derived from state (active sessions on each site,
   the face engine's queue depth) are gathered at scrape time by a custom collector. That
   also means the expensive bits are paid by the scraper, not by the punch.
2. **A label must not be unbounded.** Labels are route *templates* (``/api/v1/q/{token}``),
   HTTP status codes, outcome names and SQL verbs - never a worker id, a site name that came
   from a request, a raw path, a query string, or a SQL statement. A metrics endpoint that
   leaks an id is a privacy incident; one that grows a label per token is a memory leak with
   a dashboard attached. Both are tested in ``tests/test_metrics.py`` rather than trusted.
3. **Monitoring must never be the outage.** Every function here is a no-op when
   ``prometheus_client`` is not installed, and raises nothing when it is: observability that
   can fail a punch has made the system worse, not better. The library is an optional extra
   (``requirements-optional.txt``) and ``/metrics`` says so plainly when it is absent.

WHAT IS DELIBERATELY NOT MEASURED
---------------------------------
**Per-statement SELECT latency.** The Python SQLite API hands back a cursor: timing
``execute()`` measures the query plan, while the rows are produced on the first ``fetch`` -
so a "query duration" histogram built that way would report microseconds for a slow scan and
happily mislead whoever is paged by it. What is measured instead is what actually blocks
this application: how long acquiring the write lock took (``sqlite_lock_wait_seconds``), how
long a transaction then held it (``sqlite_transaction_seconds``), how many statements ran by
verb, and how often SQLite refused with "database is locked"
(``sqlite_lock_errors_total``). Those are honest, and the retention sweeper's batch cap was
sized against exactly these numbers.

**Inference throughput as a rate.** A histogram plus ``rate()`` in PromQL answers that
without inventing a metric; see the README's queries.

MULTIPLE WORKERS
----------------
The registry is per-process, and ``serve.py`` runs a single uvicorn process, so a scrape
sees everything. A deployment that adds ``--workers N`` must set
``PROMETHEUS_MULTIPROC_DIR`` (a directory the workers can all write) and export
``prometheus_client.multiprocess``'s collector instead - without it a scrape lands on one
worker and silently reports a fraction of the traffic, which is worse than no metric because
an alert threshold tuned to it fires late. ``/api/v1/readiness`` reports which mode this
process is in.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from config import settings

log = logging.getLogger("attendance.telemetry")

try:  # optional extra: see the module docstring, rule 3
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        CollectorRegistry,
        Counter,
        Gauge,
        Histogram,
        generate_latest,
    )
    from prometheus_client.core import GaugeMetricFamily

    AVAILABLE = True
    IMPORT_ERROR: str | None = None
except Exception as exc:  # pragma: no cover - exercised by the suite's import probe
    AVAILABLE = False
    IMPORT_ERROR = f"{type(exc).__name__}: {exc}"
    CONTENT_TYPE_LATEST = "text/plain; charset=utf-8"

    class Counter:  # type: ignore[no-redef]
        """Stand-in so this module imports (and every call below is a no-op) without the library.

        The constructor takes anything, because these are built with the real library's
        signature at import: a stand-in that raised ``TypeError`` on ``Counter(name, doc,
        labels, registry=...)`` would not degrade, it would stop the application from starting -
        which is the failure mode this whole design exists to avoid.

        ``record`` is the one extension over a silent object. The application's *write* path
        works identically with and without the library - ``labels(...).inc()`` either counts
        into the registry or arrives here - but the *read* path cannot: the real child object
        carries the series' value, and this one has nowhere to put it. ``record`` receives the
        label values (as keyword arguments, exactly as ``labels()`` was called) so a receiver
        can key observations the way the real registry would - tests use a dict keyed like the
        counter's label values. Without it a test can only assert that the application did not
        crash, not that a request was refused *for a stated reason*.
        """

        #: Set by whoever needs the degraded-mode read path (the test suite). Kept on the
        #: class, not the instance, because ``labels()`` returns ``self`` - there is one
        #: object per metric, not one per series. Receives the label values of the series
        #: being incremented; may be ``None`` when nobody is reading.
        record: Any = None

        def __init__(self, *args, **kwargs):
            self._label_kwargs: dict = {}

        def labels(self, *args, **kwargs):
            if kwargs:
                self._label_kwargs = dict(kwargs)
            elif args:
                # Positional form: ``labels("value1", "value2")``. The label names live on
                # the real class; the stand-in does not have them, so the receiver sees only
                # the values. ``count_netguard_refusal`` uses keywords, which is the form the
                # degraded read path needs.
                self._label_kwargs = {"values": tuple(args)}
            return self

        def inc(self, *args, **kwargs):
            if Counter.record is not None:
                Counter.record(**self._label_kwargs)

    class Gauge(Counter):  # type: ignore[no-redef]
        def set(self, *args, **kwargs):
            pass

        def inc(self, *args, **kwargs):
            pass

        def dec(self, *args, **kwargs):
            pass

        def set_function(self, *args, **kwargs):
            pass

    class Histogram(Counter):  # type: ignore[no-redef]
        def observe(self, *args, **kwargs):
            pass

    class CollectorRegistry:  # type: ignore[no-redef]
        def __init__(self, *args, **kwargs):
            self._collectors: list = []

        def register(self, collector):
            self._collectors.append(collector)

    class GaugeMetricFamily:  # type: ignore[no-redef]
        def __init__(self, *args, **kwargs):
            self.samples: list = []

        def add_metric(self, *args, **kwargs):
            pass

    def generate_latest(*args, **kwargs):
        return b""


#: The registry a scrape reads. Deliberately our own rather than the process-wide default:
#: the default registry is imported by other libraries (and by a test process's own imports),
#: which is how a scrape ends up publishing somebody else's metrics under our names.
REGISTRY = CollectorRegistry() if AVAILABLE else None

#: The exposition format's content type (``text/plain; version=0.0.4``).
METRICS_CONTENT_TYPE = CONTENT_TYPE_LATEST

#: Histogram buckets, chosen from this application's measured behaviour rather than the
#: library defaults. The defaults top out at 10 s with most of their resolution near zero,
#: which is the wrong shape for work that legitimately takes 0.5-1.5 s per call.
INFERENCE_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.4, 0.6, 0.8, 1.0, 1.5, 2.0, 3.0, 5.0, 10.0)
#: Queue wait ends at ``FACE_INFERENCE_WAIT_SECONDS``: past that a caller is refused, so a
#: value near the top of this histogram *is* the saturation signal.
QUEUE_BUCKETS = (0.001, 0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0)
#: A cosine distance over 4096 floats is a dot product: microseconds, or something is wrong.
COSINE_BUCKETS = (0.00005, 0.0001, 0.00025, 0.0005, 0.001, 0.0025, 0.005, 0.01, 0.05, 0.1)
#: The score itself, on a **fixed** grid that brackets every decision line this application
#: has used or could use: 0.35/0.40/0.45/0.50/0.55/0.60 cover both pipelines' bands
#: (``face_detector.MatchBand``). Fixed on purpose. The bands are now derived per pipeline and
#: will move again at the next crop change, and this histogram is how that change is *seen* -
#: a grid that moved with the lines would silently rescale the very distribution being
#: watched, and the before/after comparison would be gone at the moment it mattered most.
#: A histogram, not a gauge: the *distribution* is what tells an operator the model or the
#: hardware changed, and the bucket boundaries make "how many landed in the review band"
#: answerable in one PromQL expression.
SCORE_BUCKETS = (0.0, 0.1, 0.2, 0.3, 0.35, 0.4, 0.45, 0.5, 0.55, 0.6, 0.7, 0.8, 1.0, 2.0)
#: SQLite work. Sub-millisecond is normal here; a busy_timeout failure is 5 s.
SQLITE_BUCKETS = (0.0001, 0.0005, 0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 2.5, 5.0, 10.0)
HTTP_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.5, 5.0, 10.0)


# ---------------------------------------------------------------------------
# the metric set
# ---------------------------------------------------------------------------
def _counter(name: str, doc: str, labels: tuple[str, ...] = ()) -> Counter:
    return Counter(name, doc, labels, registry=REGISTRY)


def _histogram(name: str, doc: str, labels: tuple[str, ...], buckets: tuple[float, ...]) -> Histogram:
    return Histogram(name, doc, labels, buckets=buckets, registry=REGISTRY)


def _gauge(name: str, doc: str, labels: tuple[str, ...] = (), **kwargs) -> Gauge:
    return Gauge(name, doc, labels, registry=REGISTRY, **kwargs)


# -- HTTP ------------------------------------------------------------------
HTTP_REQUESTS = _counter(
    "attendance_http_requests_total",
    "HTTP requests by route template, method and status code.",
    ("route", "method", "status"),
)
HTTP_SECONDS = _histogram(
    "attendance_http_request_seconds",
    "HTTP request duration by route template and method.",
    ("route", "method"),
    HTTP_BUCKETS,
)
HTTP_IN_PROGRESS = _gauge(
    "attendance_http_in_progress",
    "HTTP requests currently being served. Deliberately unlabelled - see ``instrument_app``.",
    (),
    multiprocess_mode="livesum",
)

# -- face verification (the expensive path) --------------------------------
MODEL_SECONDS = _histogram(
    "attendance_face_model_seconds",
    "One call into a face model, by operation, model and detector.",
    ("operation", "model", "detector"),
    INFERENCE_BUCKETS,
)
MODEL_FAILURES = _counter(
    "attendance_face_model_failures_total",
    "Face-model calls that raised, by operation and exception type.",
    ("operation", "error"),
)
COSINE_SECONDS = _histogram(
    "attendance_face_cosine_seconds",
    "Cosine distance computation between a stored embedding and a live one.",
    (),
    COSINE_BUCKETS,
)
MATCH_SCORE = _histogram(
    "attendance_face_match_score",
    "Cosine distance between the stored reference and the live capture, on a fixed grid that "
    "brackets both pipelines' decision lines. The lines themselves are derived per pipeline "
    "from measured genuine/impostor boundaries - see face_detector.MatchBand, and "
    "/api/v1/readiness for the band in force.",
    (),
    SCORE_BUCKETS,
)
ENGINE_JOB_SECONDS = _histogram(
    "attendance_face_engine_job_seconds",
    "Time a face-engine job took to run, by job name and outcome.",
    ("job", "outcome"),
    INFERENCE_BUCKETS,
)
ENGINE_QUEUE_SECONDS = _histogram(
    "attendance_face_engine_queue_wait_seconds",
    "How long a face-engine job waited for a worker before it started.",
    ("job",),
    QUEUE_BUCKETS,
)
ENGINE_REFUSALS = _counter(
    "attendance_face_engine_refusals_total",
    "Face-engine submissions refused, by reason (busy = the queue was full for the whole "
    "wait and the caller got 503; nested = a job tried to submit to its own pool).",
    ("reason",),
)
# In-flight, queued and capacity are *not* gauges pushed from the hot path: they are gathered
# at scrape time by ``_FaceEngineCollector`` below, which is also where the one value that has
# to be read (active sessions) lives. Registering a gauge here as well would collide with the
# collector and raise at import.

# -- attendance outcomes ---------------------------------------------------
VERIFICATIONS = _counter(
    "attendance_verifications_total",
    "Face verifications by outcome: approved, flagged_review, rejected, liveness_spoof, "
    "frame_refused (the photo could not be used at all). A request refused because the queue "
    "was full is not an outcome - it never got one; see attendance_face_engine_refusals_total.",
    ("outcome",),
)
PUNCHES = _counter(
    "attendance_punches_total",
    "Punches recorded, by action and resulting status.",
    ("action", "status"),
)
ACTIVE_SESSIONS_LABEL = "site_name"

# -- SQLite ----------------------------------------------------------------
SQLITE_STATEMENTS = _counter(
    "attendance_sqlite_statements_total",
    "SQL statements executed, by verb. The statement text is deliberately never a label.",
    ("operation",),
)
SQLITE_LOCK_WAIT = _histogram(
    "attendance_sqlite_lock_wait_seconds",
    "Time spent acquiring SQLite's write lock (BEGIN IMMEDIATE). This is the number that "
    "grows when a punch is waiting behind another writer.",
    ("operation",),
    SQLITE_BUCKETS,
)
SQLITE_TRANSACTION = _histogram(
    "attendance_sqlite_transaction_seconds",
    "How long a transaction was open, by mode: explicit (an application-issued BEGIN, as "
    "migrations and the retention sweeper use) or implicit (the driver's own, around a "
    "write). Includes the lock wait.",
    ("mode",),
    SQLITE_BUCKETS,
)
SQLITE_LOCK_ERRORS = _counter(
    "attendance_sqlite_lock_errors_total",
    "SQLite refused a statement because the database was locked or busy (after busy_timeout).",
    ("operation",),
)
SQLITE_CONNECTIONS = _counter(
    "attendance_sqlite_connections_total",
    "SQLite connections opened, by access mode.",
    ("access",),
)

# -- network policy (see ``netguard``) -------------------------------------
NETGUARD_REFUSALS = _counter(
    "attendance_netguard_refusals_total",
    "Requests refused by the network policy, by reason: ip_not_allowed (the client is outside "
    "ADMIN_IP_ALLOWLIST), proxy_not_trusted (X-Forwarded-For arrived from a peer that is not "
    "a trusted proxy, so the real client address is unknown), origin_not_allowed (a CORS "
    "preflight from an origin no policy names). Worth an alert: a step in this counter is "
    "either a misconfigured proxy or somebody probing the admin surface.",
    ("reason",),
)

# -- build information -----------------------------------------------------
BUILD_INFO = _gauge(
    "attendance_build_info",
    "Always 1; the labels carry the build and the policy it is running with. No paths: a "
    "metrics endpoint is still a place a filesystem layout does not belong.",
    ("version", "schema_version", "liveness_mode", "face_inference_concurrency"),
)

_build_info_published = False


def publish_build_info() -> None:
    """Publish the build and the policy it runs with, once.

    Called from ``instrument_app`` rather than at import: importing ``migrations`` from module
    level would drag the whole application graph (config, database, security) into the import
    of a metrics module, and a cycle is exactly how a module that must never break the app ends
    up breaking it.
    """
    global _build_info_published
    if not AVAILABLE or _build_info_published:
        return
    _build_info_published = True
    try:
        import migrations

        BUILD_INFO.labels(
            version=str(settings.app_version),
            schema_version=str(migrations.SCHEMA_VERSION),
            liveness_mode=str(settings.liveness_mode),
            face_inference_concurrency=str(settings.face_inference_concurrency),
        ).set(1)
    except Exception as exc:  # pragma: no cover - a label must never stop the app serving
        log.debug("build info not published: %s", exc)


# ---------------------------------------------------------------------------
# observation helpers (all no-ops without the library)
# ---------------------------------------------------------------------------
def observe_http(*, route: str, method: str, status: int, seconds: float) -> None:
    HTTP_REQUESTS.labels(route=route, method=method, status=str(status)).inc()
    HTTP_SECONDS.labels(route=route, method=method).observe(seconds)


def http_started() -> None:
    HTTP_IN_PROGRESS.inc()


def http_finished() -> None:
    HTTP_IN_PROGRESS.dec()


def observe_model_call(
    *, operation: str, seconds: float, model: str, detector: str, error: BaseException | None = None
) -> None:
    MODEL_SECONDS.labels(operation=operation, model=model, detector=detector or "").observe(seconds)
    if error is not None:
        MODEL_FAILURES.labels(operation=operation, error=type(error).__name__).inc()


def observe_cosine(seconds: float) -> None:
    COSINE_SECONDS.observe(seconds)


def observe_match_score(distance: float) -> None:
    MATCH_SCORE.observe(float(distance))


def observe_engine_job(*, job: str, outcome: str, seconds: float) -> None:
    ENGINE_JOB_SECONDS.labels(job=job, outcome=outcome).observe(seconds)


def observe_queue_wait(*, job: str, seconds: float) -> None:
    ENGINE_QUEUE_SECONDS.labels(job=job).observe(max(0.0, seconds))


def count_engine_refusal(reason: str) -> None:
    ENGINE_REFUSALS.labels(reason=reason).inc()


def observe_verification(outcome: str) -> None:
    VERIFICATIONS.labels(outcome=outcome).inc()


def observe_punch(*, action: str, status: str) -> None:
    PUNCHES.labels(action=action, status=status).inc()


def count_connection(*, read_only: bool) -> None:
    SQLITE_CONNECTIONS.labels(access="read_only" if read_only else "read_write").inc()


def count_netguard_refusal(*, reason: str) -> None:
    NETGUARD_REFUSALS.labels(reason=reason).inc()


def count_statement(operation: str) -> None:
    SQLITE_STATEMENTS.labels(operation=operation).inc()


def observe_lock_wait(*, operation: str, seconds: float) -> None:
    SQLITE_LOCK_WAIT.labels(operation=operation).observe(seconds)


def observe_transaction(*, mode: str, seconds: float) -> None:
    SQLITE_TRANSACTION.labels(mode=mode).observe(max(0.0, seconds))


def count_lock_error(*, operation: str) -> None:
    SQLITE_LOCK_ERRORS.labels(operation=operation).inc()


# ---------------------------------------------------------------------------
# derived values, gathered at scrape time
# ---------------------------------------------------------------------------
_SQL_VERBS = (
    ("select", "select"),
    ("insert", "insert"),
    ("update", "update"),
    ("delete", "delete"),
    ("replace", "replace"),
    ("create", "ddl"),
    ("drop", "ddl"),
    ("alter", "ddl"),
    ("pragma", "pragma"),
    ("vacuum", "maintenance"),
    ("analyze", "maintenance"),
    ("savepoint", "transaction"),
    ("release", "transaction"),
    ("rollback", "transaction"),
    ("commit", "transaction"),
    ("begin", "transaction"),
)
_LEADING_COMMENTS = re.compile(r"^(?:\s*--[^\n]*\n|\s*/\*.*?\*/|\s)+", re.S)


def sql_operation(statement: str) -> str:
    """The verb of a SQL statement, as a low-cardinality label. Never the statement itself.

    The text is attacker-influenced in the worst case and unbounded in the ordinary one - a
    label per value of ``worker_id`` is how a metrics endpoint turns into a memory leak - so
    only the verb crosses this boundary. ``other`` is the honest answer for anything
    unrecognised.
    """
    text = str(statement or "")
    # Fast path: a statement almost always starts with its verb, and a leading comment must
    # start with '-' or '/', so ``lstrip`` alone answers it and the regex - which exists only
    # for comments - is skipped. This is on the hot path: it runs once per SQL statement the
    # application executes, i.e. several times per request, from ``database``'s instrumented
    # connection. The two paths agree whenever the first non-space character is a letter.
    head = text.lstrip()
    if not (head and head[0].isalpha()):
        try:
            head = _LEADING_COMMENTS.sub("", text, count=1).lstrip()
        except Exception:  # pragma: no cover - defensive
            return "other"
    lowered = head[:32].lower()
    for prefix, label in _SQL_VERBS:
        if lowered.startswith(prefix):
            return label
    return "other"


def is_lock_error(exc: BaseException) -> bool:
    """Whether an exception is SQLite refusing for contention rather than for a real fault.

    Both phrases matter: ``database is locked`` is a writer against a writer (or a reader in
    rollback-journal mode), ``database table is locked`` is a table-level conflict. A genuine
    error - a syntax fault, a missing column - must not be counted here, or an alert on this
    counter would page somebody about a code bug with the wrong explanation.
    """
    text = str(exc).lower()
    return "locked" in text or "busy" in text


class _DatabaseCollector:
    """``attendance_active_sessions`` - the one value that has to be read at scrape time.

    Active sessions are a row count, and the alternative to gathering them here is a query on
    every punch, clock-out and review - i.e. paying for monitoring on the hot path. At scrape
    time the cost is one cheap indexed count per site every 15 seconds, and the value cannot
    drift from the table it describes.

    Reads through ``database`` (the application's own connection factory, so the metrics
    inherit ``busy_timeout`` and WAL) and *never fails the scrape*: a database that is
    unreachable produces no sample, which Prometheus renders as "no data" rather than as a
    zero. A zero here would look like "nobody is on site", which is the one wrong answer.
    """

    def collect(self):  # noqa: D102 - prometheus_client's interface
        metric = GaugeMetricFamily(
            "attendance_active_sessions",
            "Open shifts right now, by site.",
            labels=[ACTIVE_SESSIONS_LABEL],
        )
        connection = None
        try:
            import database

            # Read-only, and deliberately not ``database.db()``: a collector runs at scrape
            # time on the event loop, so it must not be able to write even by accident, and it
            # owns a single connection it closes itself rather than a transaction context.
            connection = database.connect(read_only=True)
            rows = connection.execute(
                "SELECT site_name, COUNT(*) AS open_shifts FROM active_sessions "
                "GROUP BY site_name ORDER BY site_name"
            ).fetchall()
            for row in rows:
                metric.add_metric([str(row["site_name"] or "")], int(row["open_shifts"] or 0))
        except Exception as exc:  # noqa: BLE001 - a scrape must not fail because we are broken
            log.debug("active sessions not gathered: %s", exc)
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:  # pragma: no cover - defensive
                    pass
        yield metric


class _FaceEngineCollector:
    """The engine's own view: in-flight, queued, and its lifetime counters.

    Gathered from ``face_engine.ENGINE.snapshot()`` rather than pushed on every inference,
    because the engine is a separate, deliberately dependency-free module: pushing would mean
    a metric call on the hot path of every punch *and* a gauge that is stale between
    submissions. Imported lazily so a process that only serves admin routes never pulls
    TensorFlow in for a scrape.
    """

    def collect(self):  # noqa: D102
        try:
            import face_engine

            snapshot = face_engine.ENGINE.snapshot()
        except Exception as exc:  # noqa: BLE001 - not installed, not started, does not matter
            log.debug("face engine not gathered: %s", exc)
            return
        in_flight = GaugeMetricFamily("attendance_face_engine_in_flight", "Inferences running right now.")
        in_flight.add_metric([], int(snapshot.get("in_flight", 0)))
        queued = GaugeMetricFamily("attendance_face_engine_queued", "Jobs waiting for a worker right now.")
        queued.add_metric([], int(snapshot.get("queued", 0)))
        capacity = GaugeMetricFamily(
            "attendance_face_engine_capacity",
            "Worker threads the engine will run at once (FACE_INFERENCE_CONCURRENCY).",
        )
        capacity.add_metric([], int(snapshot.get("capacity", 0)))
        yield in_flight
        yield queued
        yield capacity


_registered = False


def register_collectors() -> None:
    """Attach the scrape-time collectors, once. Idempotent and safe to call repeatedly."""
    global _registered
    if not AVAILABLE or _registered:
        return
    REGISTRY.register(_DatabaseCollector())
    REGISTRY.register(_FaceEngineCollector())
    _registered = True


register_collectors()


# ---------------------------------------------------------------------------
# HTTP instrumentation
# ---------------------------------------------------------------------------
#: Fallback label for a request that matched no route (a 404, a static asset under the
#: catch-all mount). One bucket for all of them on purpose: a label per requested path is
#: exactly the cardinality explosion this module is written to avoid, and paths that do not
#: exist are the *least* trustworthy input in the system.
UNMATCHED_ROUTE = "unmatched"


def route_template(request) -> str:
    """The route pattern for a request, never the requested path.

    A punch link carries its token *in the path* (``/api/v1/q/<token>``), so labelling by
    path would mint a new time series for every link ever issued - a slow memory leak that
    also puts a credential in a metrics label. Starlette's router resolves the matched route
    into ``scope["route"]`` *while routing*, so this is only meaningful once a call into the
    application has returned (or raised) - see ``instrument_app`` for why the route label is
    read on the way out rather than on the way in.
    """
    scope = request.scope
    route = scope.get("route")
    if route is None:
        # A mounted app (StaticFiles) reports no route; its ``scope["root_path"]`` is the mount
        # point, so the label says which mount without naming the asset.
        mount = scope.get("root_path")
        return f"{mount}/*" if mount else UNMATCHED_ROUTE

    # The route object carries only its own sub-router path (``/q/{token}``); the router prefix
    # lives on the "effective route context" FastAPI resolves onto the scope. Preferred because
    # it is the path the OpenAPI schema and the caller's own logs use - ``/api/v1/q/{token}``.
    # Read defensively (``.get`` plus ``getattr``) and fall back to the route's own path, which
    # is still a template: a private attribute that moves degrades the label, never the punch.
    fastapi_ns = scope.get("fastapi")
    if isinstance(fastapi_ns, dict):
        effective = getattr(fastapi_ns.get("effective_route_context"), "path", None)
        if effective:
            return str(effective)

    path = getattr(route, "path", None)
    if path:
        return str(path)
    return UNMATCHED_ROUTE


def instrument_app(app) -> bool:
    """Add the HTTP metrics middleware. Returns whether the library is present.

    Records every request that reaches the application, including the ones that raise: an
    unhandled exception is a 500 that never became a response object, and re-raising it is
    deliberate - swallowing an exception to record a metric would turn a crash into a silent
    success, which is the opposite of observability.

    WHY THE ROUTE LABEL IS READ ON THE WAY OUT
    ------------------------------------------
    A middleware added to the application wraps the *router*, so when it starts a request no
    route has been chosen yet: ``scope["route"]`` is empty and there is nothing to label with.
    Reading it after ``call_next`` returns is exact - it is the template Starlette actually
    matched, on the exception path too, because routing succeeded before the handler ran. The
    alternative, matching the request against ``app.router.routes`` ourselves, would have to
    re-walk FastAPI's own route composition (in 1.6 an included router is an opaque container
    rather than a flat list) and would disagree with the real router the day that changes - a
    wrong label in a dashboard is worse than a coarse one.

    So ``attendance_http_in_progress`` is the one unlabelled metric here: its increment
    happens before the route is known and its decrement after, and a gauge that incremented
    under one label and decremented under another would drift negative and never clear. The
    per-route breakdown of what is in flight, and how slow, is answered by
    ``attendance_http_requests_total`` and ``attendance_http_request_seconds`` a moment later.
    """
    publish_build_info()

    @app.middleware("http")
    async def attendance_metrics_middleware(request, call_next):  # noqa: ANN001 - Starlette's shape
        method = request.method
        started = time.perf_counter()
        http_started()
        try:
            response = await call_next(request)
        except Exception:
            observe_http(
                route=route_template(request),
                method=method,
                status=500,
                seconds=time.perf_counter() - started,
            )
            raise
        else:
            observe_http(
                route=route_template(request),
                method=method,
                status=int(response.status_code),
                seconds=time.perf_counter() - started,
            )
            return response
        finally:
            http_finished()

    return AVAILABLE


#: ``install`` reads better at a call site that is not a framework-aware module.
install = instrument_app


def render() -> bytes:
    """The exposition payload for one scrape."""
    return generate_latest(REGISTRY)


def describe() -> dict[str, Any]:
    """What this process is doing about metrics, for ``/api/v1/readiness``."""
    import os

    return {
        "available": AVAILABLE,
        "import_error": IMPORT_ERROR,
        "enabled": bool(settings.metrics_enabled),
        "require_token": bool(settings.metrics_token),
        "authenticated_by": "scrape token" if settings.metrics_token else "admin JWT only",
        "multiprocess_dir": os.environ.get("PROMETHEUS_MULTIPROC_DIR") or None,
        "single_process_registry": not os.environ.get("PROMETHEUS_MULTIPROC_DIR"),
    }


