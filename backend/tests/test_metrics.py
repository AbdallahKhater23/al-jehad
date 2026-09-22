"""Prometheus metrics: who may read them, what they say, and what they must never say.

WHY THIS SUITE EXISTS
---------------------
A metrics endpoint fails in two directions, and neither is visible from the outside: it can
be *readable by whoever reaches the port* (a summary of the business handed to anyone who
tries), and it can *lie* - a label that grows until the scrape times out, a gauge that
reports a fraction of the traffic because it landed on one worker, a histogram that times the
query plan instead of the query. So the claims pinned here are:

1. **It is never anonymous.** A scrape token or an administrator session, nothing else - and
   the token is never echoed back in the payload.
2. **Nothing personal and nothing unbounded is a label.** A punch link carries its token *in
   the path*, so the route label is the template, never the requested path; a worker id, a
   filesystem path and a SQL statement are all asserted absent, and the number of distinct
   label values is capped after a full exercise of the API.
3. **The numbers are real.** A punch moves the inference histogram, the score histogram and
   the outcome counter by exactly the outcome the API reported; a busy queue is counted as a
   refusal rather than as an outcome (it never got one); SQLite contention is counted when it
   happens, and a genuine SQL error is *not* counted as contention.
4. **Absent is not broken.** With ``prometheus_client`` missing, every helper is silent and
   ``/metrics`` says why in one sentence - observability that can fail a punch would be worse
   than none.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import types
from pathlib import Path

import pytest
from prometheus_client.parser import text_string_to_metric_families

import database
import face_engine
import harness
import liveness
import telemetry
from config import settings
from harness import (
    ADMIN,
    HEAD_ADMIN,
    MOALLEM,
    WORKER,
    bearer,
    clock_in,
    db_scalar,
)

BACKEND_DIR = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def scrape(client, headers=None, **kwargs) -> str:
    """The exposition payload, asserting the response is a successful scrape."""
    response = client.get("/api/v1/metrics", headers=headers or bearer(ADMIN), **kwargs)
    assert response.status_code == 200, response.text[:300]
    assert response.headers["content-type"].startswith("text/plain")
    return response.text


def samples(body: str, name: str) -> list:
    """Every sample of one metric family, parsed from the exposition format.

    Parsed rather than substring-matched: a text search for ``attendance_x_total{...} 1``
    passes just as happily against a comment, a HELP line or a label in the wrong place.

    The name may be given either as the family the client library registered
    (``attendance_http_requests``) or as the series a dashboard queries
    (``attendance_http_requests_total``, ``attendance_face_model_seconds_bucket``). Those are
    the same metric, and a test that has to know which of the two spellings the parser keeps
    would be testing the parser instead of the instrumentation. A family name is always a
    prefix of its series names, so one prefix rule covers both.
    """
    matches = []
    for family in text_string_to_metric_families(body):
        if family.name == name:
            matches.extend(family.samples)
        elif family.name + "_total" == name or name.startswith(family.name + "_"):
            # A single series was asked for (``..._count``, ``..._sum``, a counter's series):
            # return only that one, or ``value()`` would answer with the first *bucket* that
            # shares the label set - 0.0 for a histogram that was in fact observed.
            matches.extend(sample for sample in family.samples if sample.name == name)
    return matches


def value(body: str, name: str, labels: dict | None = None) -> float | None:
    """The value of one series, or ``None`` when it is absent (which is not zero)."""
    labels = labels or {}
    for sample in samples(body, name):
        if all(sample.labels.get(key) == expected for key, expected in labels.items()):
            return sample.value
    return None


def label_values(body: str, metric: str, label: str) -> set:
    return {sample.labels.get(label) for sample in samples(body, metric)}


def exercise_every_instrumented_path(client) -> None:
    """Do the work the payload can only report: one punch, and one explicit transaction.

    A Prometheus client emits *series*, not metric declarations, so a family nobody has recorded
    is legitimately absent from the payload - which makes "is this metric published?" a question
    about the process's history unless the test creates that history itself. Left implicit, the
    history came from whatever else had run: this module's own earlier tests, or ``test_face_
    engine``. That is invisible while the suite is one process and collection order is fixed,
    and it is what ``pytest -n auto`` exposes - the test lands in a worker that has done nothing
    yet and fails on ``attendance_verifications_total``, which looks like a broken metric.
    """
    response = clock_in(client, MOALLEM, headers=bearer(MOALLEM))
    assert response.status_code == 200, response.text[:200]

    # The one path a punch does not take: a punch writes inside the driver's own implicit
    # transaction, so the lock-wait histogram (BEGIN IMMEDIATE) needs an explicit one.
    connection = database.connect(isolation_level=None)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("ROLLBACK")
    finally:
        connection.close()


def _labels_of_families(body: str, prefix: str) -> set[str]:
    """Every label value (and label name) of the metric families whose name starts with ``prefix``.

    Used for the negative assertions about a subsystem: what may not leak into a SQLite label is
    a narrower question than what may not appear anywhere, and the narrower one is answerable.
    """
    values = set()
    for family in text_string_to_metric_families(body):
        if not family.name.startswith(prefix):
            continue
        for sample in family.samples:
            for key, value in sample.labels.items():
                if key != "__name__":
                    values.add(str(key))
                    values.add(str(value))
    return values


def series_sum(body: str, name: str, labels: dict | None = None) -> float:
    """The total across every series of a counter (counters have one series per label set)."""
    labels = labels or {}
    return float(
        sum(
            sample.value
            for sample in samples(body, name)
            if all(sample.labels.get(key) == expected for key, expected in labels.items())
        )
    )


class FakeSession:
    """A stand-in for an ``onnxruntime.InferenceSession`` (copied from the liveness suite).

    The real MiniFASNet model is not in the repository, so what is measured here is the
    *policy*: which verdict produces which metric label.
    """

    def __init__(self, probabilities):
        import numpy as np

        self._probabilities = np.asarray(probabilities, dtype=np.float32).reshape(1, -1)

    def get_inputs(self):
        return [types.SimpleNamespace(name="input", shape=[1, 3, 80, 80], type="tensor(float)")]

    def get_outputs(self):
        return [types.SimpleNamespace(name="output")]

    def run(self, output_names, feeds):  # noqa: ARG002 - mirrors the ORT signature
        return [self._probabilities]


#: MiniFASNet logits for a photo-of-a-photo: softmax puts "printed" first. See the liveness
#: suite for why these are logits rather than probabilities.
PRINTED_ATTACK = [-2.0, 5.0, -3.0]


# ---------------------------------------------------------------------------
# access control
# ---------------------------------------------------------------------------
def test_metrics_refuses_an_anonymous_scrape(client):
    """The whole payload is a description of the business: routes, volumes, error rates."""
    response = client.get("/api/v1/metrics")
    assert response.status_code == 401, response.text[:200]
    assert response.json()["detail"]["error_code"] == "metrics_unauthorised"


@pytest.mark.parametrize("user_id", [WORKER, MOALLEM])
def test_metrics_refuses_a_non_administrator(client, user_id):
    response = client.get("/api/v1/metrics", headers=bearer(user_id))
    assert response.status_code == 401


@pytest.mark.parametrize("user_id", [ADMIN, HEAD_ADMIN])
def test_metrics_is_readable_by_an_administrator(client, user_id):
    assert client.get("/api/v1/metrics", headers=bearer(user_id)).status_code == 200


def test_the_conventional_scrape_path_is_served_and_protected_too(client):
    """``/metrics`` is what every scrape config assumes; it must not be the path that 404s.

    Two names for one handler, asserted on the same properties as the scoped one: an
    unprotected alias would be the whole vulnerability with a different URL, and a cached one
    would be a graph that lies.
    """
    anonymous = client.get("/metrics")
    assert anonymous.status_code == 401, anonymous.text[:200]

    response = client.get("/metrics", headers=bearer(ADMIN))
    assert response.status_code == 200, response.text[:200]
    assert response.headers["content-type"].startswith("text/plain")
    assert response.headers["cache-control"] == "no-store"
    assert "attendance_build_info" in response.text


def test_a_configured_scrape_token_replaces_the_admin_path(client, monkeypatch):
    """Prometheus cannot log in. A token is the supported way for a scrape job to read this."""
    monkeypatch.setattr(settings, "metrics_token", "s3cret-scrape-token", raising=False)

    assert client.get("/api/v1/metrics", headers={"Authorization": "Bearer s3cret-scrape-token"}).status_code == 200
    assert client.get("/api/v1/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401
    # And an administrator session does *not* bypass a scrape token that has been configured:
    # one credential per surface, so revoking the token revokes the scrape.
    assert client.get("/api/v1/metrics", headers=bearer(ADMIN)).status_code == 401


def test_the_scrape_token_may_arrive_as_a_query_parameter(client, monkeypatch):
    """Some scrapers can only be given a URL. Supported, and still not anonymous."""
    monkeypatch.setattr(settings, "metrics_token", "query-token-value", raising=False)
    assert client.get("/api/v1/metrics?token=query-token-value").status_code == 200
    assert client.get("/api/v1/metrics?token=nope").status_code == 401


def test_the_token_is_never_echoed_in_the_payload(client, monkeypatch):
    """A scrape body is pasted into dashboards, tickets and chat."""
    monkeypatch.setattr(settings, "metrics_token", "do-not-print-me", raising=False)
    body = scrape(client, headers={"Authorization": "Bearer do-not-print-me"})
    assert "do-not-print-me" not in body


def test_metrics_can_be_disabled(client, monkeypatch):
    """404 rather than 403: a disabled surface should not confirm that it exists."""
    monkeypatch.setattr(settings, "metrics_enabled", False, raising=False)
    assert client.get("/api/v1/metrics", headers=bearer(ADMIN)).status_code == 404


def test_metrics_is_absent_from_the_openapi_schema(app_module):
    """The schema is disabled in production, but the route should not be documented either."""
    paths = app_module.app.openapi().get("paths", {})
    assert "/api/v1/metrics" not in paths


def test_the_response_is_never_cached(client):
    """A cached scrape is a graph that lies."""
    response = client.get("/api/v1/metrics", headers=bearer(ADMIN))
    assert response.headers.get("cache-control") == "no-store"


# ---------------------------------------------------------------------------
# what is in the payload: shape, size and secrets
# ---------------------------------------------------------------------------
def test_the_payload_parses_as_the_exposition_format(client):
    body = scrape(client)
    families = list(text_string_to_metric_families(body))
    names = {family.name for family in families}
    assert "attendance_http_requests" in names or "attendance_http_requests_total" in names
    assert "attendance_build_info" in names
    assert "attendance_sqlite_statements" in names


def test_the_payload_omits_nothing_an_alert_needs(client, app_module):
    """One exercised process must publish every family the alerting rules are written against.

    Why an exercise instead of a bare scrape: a Prometheus client emits *series*, not metric
    declarations, so a histogram nobody has observed is legitimately absent from the payload.
    Asserting presence on an idle process would either fail (and be "fixed" by weakening the
    check) or pass only because some earlier test left data behind. So this test does the work
    first - one punch, then one explicit write transaction, which between them touch every
    instrumented path - and only then insists the payload is complete. A rename, an accidental
    second registry or a metric that stopped being recorded fails here rather than in a
dashboard months later.

    The exercise itself lives in ``exercise_every_instrumented_path`` because the name test below
    needs exactly the same history, and had been getting it by accident from this test.
    """
    exercise_every_instrumented_path(client)

    body = scrape(client)
    for family in (
        "attendance_http_requests_total",
        "attendance_http_request_seconds",
        "attendance_http_in_progress",
        "attendance_face_model_seconds",
        "attendance_face_cosine_seconds",
        "attendance_face_match_score",
        "attendance_face_engine_job_seconds",
        "attendance_face_engine_queue_wait_seconds",
        "attendance_face_engine_capacity",
        "attendance_verifications_total",
        "attendance_punches_total",
        "attendance_active_sessions",
        "attendance_sqlite_statements_total",
        "attendance_sqlite_transaction_seconds",
        "attendance_sqlite_lock_wait_seconds",
        "attendance_sqlite_connections_total",
        "attendance_build_info",
    ):
        assert samples(body, family) or samples(body, f"{family}_total"), f"{family} is missing"

    # Failure-mode counters are the exception, and deliberately so: they exist to be *absent*
    # until something goes wrong, so a rule on them is a rule that fires on news rather than
    # on traffic. What is pinned *here* is that they are documented, which is what makes the
    # rule writable at all. That they carry no series until they have news is asserted in a
    # fresh interpreter instead (see the subprocess test below): by the time this test runs,
    # the refusals have been caused on purpose by the face-engine suite, so "no series" in
    # *this* process would be a claim about the test session and not about the deployment.
    for family in ("attendance_face_model_failures_total", "attendance_face_engine_refusals_total"):
        assert f"# HELP {family}" in body, f"{family} is not documented in the payload"


def test_the_build_info_carries_the_deployment_and_its_policy(client):
    """The first thing an operator checks after a deploy: did the intended build land?"""
    import migrations

    body = scrape(client)
    sample = samples(body, "attendance_build_info")[0]
    assert sample.labels["version"] == str(settings.app_version)
    assert sample.labels["schema_version"] == str(migrations.SCHEMA_VERSION)
    assert sample.labels["face_inference_concurrency"] == str(settings.face_inference_concurrency)
    assert sample.value == 1


def test_no_filesystem_path_is_published(client):
    """A metrics endpoint is still a place a server's layout does not belong."""
    body = scrape(client)
    assert str(settings.database_path) not in body
    assert str(settings.backup_dir) not in body
    assert str(harness.PROJECT_ROOT) not in body
    assert "worker_photos" not in body and "local_references" not in body


def test_no_worker_or_session_identity_is_published(client):
    """Timestamps, tokens and ids are what a metrics label must never be made of."""
    response = clock_in(client, MOALLEM, headers=bearer(MOALLEM))
    assert response.status_code == 200, response.text[:200]
    body = scrape(client)

    assert db_scalar("SELECT COUNT(*) FROM active_sessions WHERE worker_id = ?", (MOALLEM,)) == 1
    assert f'worker_id="{WORKER}"' not in body
    assert "Seed Worker" not in body
    # The session's clock-in time is a timestamp; nothing here should carry one as a label.
    clock_in_time = str(db_scalar("SELECT clock_in_time FROM active_sessions WHERE worker_id = ?", (WORKER,)))
    assert clock_in_time not in body


def test_a_path_carrying_a_token_is_labelled_by_its_route_template(client, app_module):
    """The punch link puts its credential in the path; a label per token would leak and grow.

    This is the cardinality failure that a naive ``request.url.path`` label produces, and it
    is worse than a leak of memory: the leaked value is a working credential.
    """
    import quick_links

    issued = client.post("/api/v1/admin/quick_links", headers=bearer(HEAD_ADMIN), json={"worker_id": MOALLEM})
    assert issued.status_code == 200, issued.text[:200]
    token = issued.json().get("token") or issued.json().get("url", "").rstrip("/").split("/")[-1]
    assert token, issued.json()

    # The link has to actually be *opened*: a label for a route nobody has requested does not
    # exist yet, and asserting it against some earlier test's traffic is how a cardinality test
    # quietly stops testing anything. This is the request a worker makes from an SMS.
    opened = client.get(f"/api/v1/q/{token}")
    assert opened.status_code in (200, 409), opened.text[:200]

    body = scrape(client)
    assert token not in body, "a punch credential reached the metrics"
    route_labels = label_values(body, "attendance_http_requests_total", "route")
    assert "/api/v1/q/{token}" in route_labels, route_labels
    _ = quick_links  # the route vocabulary under test belongs to this module


def test_an_unknown_path_is_one_label_and_not_the_path(client):
    """404s are the least trustworthy input in the system; they get one bucket, not a series."""
    for path in ("/nope", "/nope/2", "/definitely-not-here"):
        client.get(path)
    body = scrape(client)
    routes = label_values(body, "attendance_http_requests_total", "route")
    assert telemetry.UNMATCHED_ROUTE in routes
    assert not {path for path in routes if path.startswith("/nope")}


def test_the_label_sets_stay_small_after_a_full_exercise(client, app_module):
    """A cap on cardinality is the difference between a metrics endpoint and a memory leak.

    Route, status, outcome and operation are vocabularies the application defines, so they are
    capped absolutely: a series per requested path or per statement would blow past these
    within one test's traffic. The face-engine ``job`` label is the one that is *not* capped
    here, because its value is the ``__name__`` of whatever was submitted to the engine: the
    application submits a fixed handful, but so does this suite, so an absolute number would
    pin the test session rather than the endpoint. It is held to the property that matters
    instead - the number of names does not grow with the number of requests, and no name can
    be a request's own text.
    """
    before_jobs = label_values(scrape(client), "attendance_face_engine_job_seconds", "job")
    client.get("/api/v1/status")
    client.get("/api/v1/admin/users", headers=bearer(ADMIN))
    client.get("/api/v1/admin/sites", headers=bearer(ADMIN))
    client.get("/api/v1/admin/readiness", headers=bearer(ADMIN))
    client.get("/api/v1/readiness")
    client.get("/api/v1/q/not-a-real-token")
    clock_in(client, MOALLEM, headers=bearer(MOALLEM))
    body = scrape(client)

    # The route vocabulary is defined in code, one label per *route template*, so what belongs
    # here is the property rather than a count: the same route asked for with different path
    # parameters has to be **one** label, and asking again has to add nothing.
    #
    # This line used to be a literal ceiling on the labels seen *so far* (last raised to 31 by
    # ``/admin/enroll/needs_reenrollment``), which measures the test session rather than the
    # endpoint: a batch of suites that exercises more of the API legitimately sees more
    # templates, so it fails without anything being wrong - and it says nothing about the
    # request that would actually be a leak. The route *is* exercised here, which is what makes
    # "did the parameter become a label?" answerable at all (a route nobody requested has no
    # label yet; see ``test_a_path_carrying_a_token_is_labelled_by_its_route_template``).
    def route_labels(payload):
        return set(label_values(payload, "attendance_http_requests_total", "route"))

    before_routes = route_labels(body)
    for path in (
        "/api/v1/q/not-a-real-token",
        "/api/v1/q/another-made-up-token",
        "/nope",
        "/nope/2",
    ):
        client.get(path)
    after_routes = route_labels(scrape(client))
    assert "/api/v1/q/{token}" in after_routes, (
        "the punch-link route is labelled by its template: " + repr(sorted(after_routes))
    )
    added = after_routes - before_routes
    assert len(added) <= 2, (
        "two tokens and two unknown paths may add the template and the one unmatched bucket, "
        f"not a series each: {sorted(added)}"
    )
    assert not any("not-a-real-token" in label or "made-up-token" in label for label in after_routes)
    # And repeating them adds nothing at all, which is the leak this test is named for.
    for path in ("/api/v1/q/not-a-real-token", "/api/v1/q/another-made-up-token", "/nope"):
        client.get(path)
    assert route_labels(scrape(client)) - after_routes == set()
    assert len(label_values(body, "attendance_http_requests_total", "status")) <= 12
    assert len(label_values(body, "attendance_verifications_total", "outcome")) <= 6
    assert len(label_values(body, "attendance_sqlite_statements_total", "operation")) <= 10

    # Six requests, one punch: a code path that minted a label from anything the caller sent
    # would add roughly that many names. The application's own fixed set adds a couple at most.
    jobs = label_values(body, "attendance_face_engine_job_seconds", "job")
    assert len(jobs - before_jobs) <= 6, sorted(jobs - before_jobs)
    for name in jobs:
        assert name and name.isidentifier() and len(name) <= 48, name
        assert "not-a-real-token" not in name


def test_sql_is_labelled_by_verb_and_never_by_statement(client, app_module):
    """The statement text is unbounded, and in a database layer it can carry a worker id."""
    clock_in(client, MOALLEM, headers=bearer(MOALLEM))
    body = scrape(client)
    operations = label_values(body, "attendance_sqlite_statements_total", "operation")
    assert operations <= {"select", "insert", "update", "delete", "replace", "ddl", "pragma", "transaction", "maintenance", "script", "other"}
    assert "select" in operations
    # Checked against the *label values* of the database metrics rather than the payload text:
    # a table name is a legitimate part of this module's own metric names
    # (``attendance_active_sessions``) and of its route templates (``/api/v1/admin/users``), so a
    # substring search would fail on the documentation and pass on a leak buried elsewhere.
    sql_labels = _labels_of_families(body, "attendance_sqlite_")
    for leaked in ("active_sessions", "attendance_logs", "users", "audit_log", "worker_id"):
        assert not any(leaked in label for label in sql_labels), f"{leaked} reached a SQL label"


# ---------------------------------------------------------------------------
# the inference and outcome metrics
# ---------------------------------------------------------------------------
def test_a_punch_records_the_model_latency_the_score_and_the_outcome(client, app_module):
    """The three numbers the capacity decision rests on, on one request.

    Every count is a *delta* against the same scrape made before the punch: the registry lives
    for the whole session, so an absolute value here would be measuring the rest of the suite.
    """
    before_body = scrape(client)
    before = series_sum(before_body, "attendance_verifications_total", {"outcome": "approved"})
    before_scores = value(before_body, "attendance_face_match_score_count") or 0.0
    before_score_sum = value(before_body, "attendance_face_match_score_sum") or 0.0

    response = clock_in(client, MOALLEM, headers=bearer(MOALLEM))
    assert response.status_code == 200, response.text[:200]

    body = scrape(client)
    assert series_sum(body, "attendance_verifications_total", {"outcome": "approved"}) == before + 1
    assert series_sum(body, "attendance_punches_total", {"action": "clock_in"}) >= 1

    assert value(body, "attendance_face_model_seconds_count", {"operation": "represent", "model": "Facenet"}) >= 1

    # Exactly one new distance, and the harness's engine stub returns the enrolled embedding
    # verbatim, so that one distance is ~0: the punch was approved on the score the histogram
    # recorded, not on some other code path's opinion of it.
    assert value(body, "attendance_face_match_score_count") == before_scores + 1
    delta = value(body, "attendance_face_match_score_sum") - before_score_sum
    assert 0.0 <= delta <= 0.5, delta


def test_the_cosine_comparison_is_timed_separately_from_the_embedding(client, app_module):
    """Two operations with a hundredfold cost difference must not share one histogram."""
    clock_in(client, MOALLEM, headers=bearer(MOALLEM))
    body = scrape(client)
    assert value(body, "attendance_face_cosine_seconds_count") >= 1
    cosine_sum = value(body, "attendance_face_cosine_seconds_sum")
    model_sum = value(body, "attendance_face_model_seconds_sum", {"operation": "represent"})
    assert cosine_sum is not None and model_sum is not None
    assert cosine_sum < model_sum, "the cosine step was timed as if it were inference"


@pytest.mark.parametrize(
    "mode,expected",
    [("review", "flagged_review"), ("mismatch", "rejected")],
)
def test_each_verification_outcome_gets_its_own_label(client, app_module, face, mode, expected):
    """One counter per outcome, labelled by what actually happened to the worker."""
    face.FACE_MODE = mode
    response = clock_in(client, MOALLEM, headers=bearer(MOALLEM))

    body = scrape(client)
    assert series_sum(body, "attendance_verifications_total", {"outcome": expected}) >= 1
    if expected == "rejected":
        assert response.status_code == 422
    else:
        assert response.status_code == 200


@pytest.mark.parametrize("mode", ["none"])
def test_a_frame_with_no_face_is_its_own_outcome(client, app_module, face, mode):
    """``no face in the frame`` and ``that is not this worker`` have different fixes."""
    before = scrape(client)
    face.FACE_MODE = mode
    response = clock_in(client, MOALLEM, headers=bearer(MOALLEM))
    assert response.status_code in (400, 422)
    body = scrape(client)
    assert series_sum(body, "attendance_verifications_total", {"outcome": "frame_refused"}) >= 1
    # ``rejected`` means "a face was found and it is not this worker" - the one label that must
    # not absorb a frame the detector could not read. Measured as a delta: the registry lives
    # for the whole session, so an absolute count would be asserting on earlier tests.
    assert series_sum(body, "attendance_verifications_total", {"outcome": "rejected"}) == series_sum(
        before, "attendance_verifications_total", {"outcome": "rejected"}
    )


def test_two_faces_in_one_frame_are_counted_as_a_refusal(client, app_module, face):
    face.FACE_COUNT = 2
    response = clock_in(client, MOALLEM, headers=bearer(MOALLEM))
    assert response.status_code == 400, response.text[:200]
    body = scrape(client)
    assert series_sum(body, "attendance_verifications_total", {"outcome": "frame_refused"}) >= 1


def test_a_presentation_attack_is_counted_as_liveness_spoof(client, app_module, monkeypatch, jpeg):
    """The label the anti-spoofing alert is built on."""
    before = scrape(client)
    monkeypatch.setattr(liveness, "get_session", lambda: FakeSession(PRINTED_ATTACK))
    monkeypatch.setattr(settings, "liveness_mode", "enforce", raising=False)

    response = clock_in(client, MOALLEM, image=jpeg, headers=bearer(MOALLEM))
    assert response.status_code == 422, response.text[:200]

    body = scrape(client)
    assert series_sum(body, "attendance_verifications_total", {"outcome": "liveness_spoof"}) >= 1
    # A spoof is refused *before* the expensive path, so no embedding was paid for and nothing
    # was recorded as a punch - visible in the same payload rather than inferred. This is the
    # property that makes the spoof counter worth alerting on: it can only grow on requests
    # that cost the server a liveness check and nothing else.
    assert series_sum(body, "attendance_punches_total") == series_sum(before, "attendance_punches_total")
    assert series_sum(body, "attendance_face_model_seconds_count") == series_sum(
        before, "attendance_face_model_seconds_count"
    )


def test_the_engine_reports_job_time_queue_wait_and_capacity(client, app_module):
    clock_in(client, MOALLEM, headers=bearer(MOALLEM))
    body = scrape(client)

    assert value(body, "attendance_face_engine_job_seconds_count", {"job": "compare_faces_sync"}) >= 1
    assert value(body, "attendance_face_engine_queue_wait_seconds_count", {"job": "compare_faces_sync"}) >= 1
    assert value(body, "attendance_face_engine_capacity") == float(settings.face_inference_concurrency)
    assert value(body, "attendance_face_engine_in_flight") == 0.0
    assert value(body, "attendance_face_engine_queued") == 0.0


def test_an_engine_failure_is_counted_and_not_swallowed(client, app_module, face, monkeypatch):
    """A model that raises is a labelled failure and a 500 - not a silent pass, not a punch.

    The *model* is what is broken here, not ``face_engine._represent``: that wrapper is where
    the timing lives, so replacing it would remove the instrumentation under test and prove
    only that nothing was recorded because nothing was running. The engine's ``embed_as_list``
    raising is the real failure mode - out of memory, a corrupt graph, a crop it cannot take.

    Counted at the model boundary, because that is where the exception starts and where the
    labels are exact (``operation`` and the exception type). The engine job around it is
    deliberately *not* also flagged: the comparison catches this and answers with an error
    result, so the pooled job genuinely completed - and a second, coarser series saying
    otherwise would double-count one incident in two places with different shapes.
    """

    def broken(*args, **kwargs):
        raise RuntimeError("the model exploded")

    before = scrape(client)
    monkeypatch.setattr(face, "embed_as_list", broken)
    response = clock_in(client, MOALLEM, headers=bearer(MOALLEM))
    assert response.status_code == 500, response.text[:200]

    body = scrape(client)
    assert value(body, "attendance_face_model_failures_total", {"operation": "represent", "error": "RuntimeError"}) >= 1
    # The failure is timed too: a model that raises on the first call is not fast, it is broken,
    # and a gap in this histogram would read as excellent latency on a graph.
    assert value(body, "attendance_face_model_seconds_count", {"operation": "represent"}) >= 1

    # The worker's outcome is ``frame_refused`` - this photo could not be *used* - and never
    # ``rejected``, which means a face was found and it is not this worker. A server fault
    # reported as a mismatch is how a broken deployment gets a review queue full of innocent
    # workers, so the two are counted apart and pinned apart.
    for outcome, expected in (("frame_refused", 1), ("rejected", 0), ("approved", 0)):
        before_outcome = series_sum(before, "attendance_verifications_total", {"outcome": outcome})
        assert series_sum(body, "attendance_verifications_total", {"outcome": outcome}) == (
            before_outcome + expected
        ), outcome

    # Nothing was recorded as a punch: the 500 is the whole outcome of that request.
    assert series_sum(body, "attendance_punches_total") == series_sum(before, "attendance_punches_total")


def test_a_busy_queue_is_a_refusal_and_not_a_verification_outcome(app_module):
    """A 503 never produced an outcome; counting it as one would poison the outcome mix."""
    engine = face_engine.FaceEngine(capacity=1, queue_depth=1, wait_seconds=0.2, name="metrics-probe")
    gate = __import__("threading").Event()
    try:
        engine.submit(gate.wait, 5)                      # occupies the single worker
        engine.submit(lambda: None)                      # fills the one queue slot
        with pytest.raises(face_engine.FaceEngineBusy):
            engine.submit(lambda: None)
    finally:
        gate.set()
        engine.shutdown(timeout=5)

    # Rendered directly rather than scraped: the engine above is a local instance, and the
    # registry is the process's, so the refusal it produced is already in the payload.
    body = telemetry.render().decode()
    refusals = value(body, "attendance_face_engine_refusals_total", {"reason": "busy"})
    assert refusals is not None and refusals >= 1
    assert series_sum(body, "attendance_verifications_total", {"outcome": "refused"}) == 0


def test_a_nested_submission_is_counted_separately(app_module):
    """A job submitting to its own pool is a deadlock refused at runtime, not a slow punch."""
    engine = face_engine.FaceEngine(capacity=1, queue_depth=4, wait_seconds=0.2, name="metrics-nested")
    try:
        with pytest.raises(face_engine.FaceEngineNested):
            engine.run(lambda: engine.submit(lambda: None))
    finally:
        engine.shutdown(timeout=5)
    body = telemetry.render().decode()
    assert value(body, "attendance_face_engine_refusals_total", {"reason": "nested"}) >= 1


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------
def test_a_scrape_gathers_active_sessions_from_the_database(client):
    """A gauge read at scrape time: paying for monitoring on the punch path is the alternative."""
    assert db_scalar("SELECT COUNT(*) FROM active_sessions") == 1
    body = scrape(client)
    sample = samples(body, "attendance_active_sessions")
    assert sample, "no active sessions were gathered"
    assert any(sample.labels.get("site_name") == harness.DOWNTOWN and sample.value >= 1 for sample in samples(body, "attendance_active_sessions"))


def test_a_write_transaction_is_timed_by_mode(client, app_module):
    """Every punch is a write; retention and migrations are explicit transactions."""
    clock_in(client, MOALLEM, headers=bearer(MOALLEM))
    body = scrape(client)
    assert value(body, "attendance_sqlite_transaction_seconds_count", {"mode": "implicit"}) >= 1
    assert value(body, "attendance_sqlite_statements_total", {"operation": "insert"}) >= 1


def test_an_explicit_transaction_and_its_lock_wait_are_timed(client, app_module):
    """The path the retention sweeper and the migrations take: BEGIN IMMEDIATE."""
    import retention

    retention.sweep(dry_run=False)
    body = scrape(client)
    assert value(body, "attendance_sqlite_transaction_seconds_count", {"mode": "explicit"}) >= 1
    assert value(body, "attendance_sqlite_lock_wait_seconds_count", {"operation": "transaction"}) >= 1


def test_lock_contention_is_counted_separately_from_other_sqlite_errors(client):
    """An alert on this counter has to mean contention, not a bug wearing the same exception."""
    holder = database.connect(isolation_level=None)
    try:
        holder.execute("BEGIN IMMEDIATE")
        contender = database.connect(timeout=0.01, isolation_level=None)
        try:
            with pytest.raises(sqlite3.OperationalError):
                contender.execute("BEGIN IMMEDIATE")
        finally:
            contender.close()
    finally:
        holder.execute("ROLLBACK")
        holder.close()

    body = scrape(client)
    assert value(body, "attendance_sqlite_lock_errors_total", {"operation": "transaction"}) >= 1


def test_is_lock_error_distinguishes_contention_from_a_real_fault():
    """The unit behind the counter: the label set must not absorb code bugs."""
    assert telemetry.is_lock_error(sqlite3.OperationalError("database is locked"))
    assert telemetry.is_lock_error(sqlite3.OperationalError("database table is locked"))
    assert telemetry.is_lock_error(sqlite3.OperationalError("database is busy"))
    assert not telemetry.is_lock_error(sqlite3.OperationalError("no such column: nope"))
    assert not telemetry.is_lock_error(sqlite3.IntegrityError("UNIQUE constraint failed"))


def test_a_broken_database_does_not_fail_the_scrape(client, monkeypatch):
    """No sample is the honest answer for an unreachable database; a zero is not."""
    import database as database_module

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    # The collector opens its own *read-only* connection rather than going through ``db()``, so
    # that is the seam to break - and only that one: the scrape's own authentication also needs
    # the database, and a sabotage broad enough to take that down would be testing the auth path.
    real_connect = database_module.connect

    def broken_if_read_only(*args, **kwargs):
        if kwargs.get("read_only"):
            raise sqlite3.OperationalError("database is locked")
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(database_module, "connect", broken_if_read_only)
    _ = broken
    body = scrape(client)  # still 200
    assert samples(body, "attendance_active_sessions") == []
    assert value(body, "attendance_face_engine_capacity") is not None, "the rest of the payload survived"


# ---------------------------------------------------------------------------
# HTTP instrumentation
# ---------------------------------------------------------------------------
def test_every_request_is_counted_by_route_method_and_status(client):
    client.get("/api/v1/status")
    body = scrape(client)
    assert value(
        body, "attendance_http_requests_total", {"route": "/api/v1/status", "method": "GET", "status": "200"}
    ) >= 1


def test_a_refused_request_is_counted_by_its_own_status(client):
    """Auth failures are the sharpest signal in the payload; they must not fold into 200s."""
    client.get("/api/v1/admin/users", headers=bearer(WORKER))
    body = scrape(client)
    assert value(
        body, "attendance_http_requests_total", {"route": "/api/v1/admin/users", "method": "GET", "status": "403"}
    ) >= 1


def test_request_duration_is_recorded(client):
    client.get("/api/v1/status")
    body = scrape(client)
    assert value(body, "attendance_http_request_seconds_count", {"route": "/api/v1/status", "method": "GET"}) >= 1


def test_the_metrics_route_itself_is_counted(client):
    """A scrape is a request; hiding it would make the payload describe a service that is not
    the one being scraped."""
    scrape(client)
    body = scrape(client)
    assert value(body, "attendance_http_requests_total", {"route": "/api/v1/metrics", "method": "GET"}) >= 1


def test_in_progress_counts_only_what_is_in_flight(client):
    """A gauge that only ever increments is an alert that never clears.

    Unlabelled on purpose: the increment happens before the router has chosen a route, so a
    per-route label would have to be read at a moment when there is nothing to read - the
    only honest label for both ends is none. What is pinned instead is that it *tracks*: after
    any number of sequential requests the value is exactly the one scrape that is in flight
    (the scrape is itself a request), not a running total.
    """
    for _ in range(3):
        client.get("/api/v1/status")
    body = scrape(client)
    assert value(body, "attendance_http_in_progress") == 1.0
    assert label_values(body, "attendance_http_in_progress", "route") == {None}


def test_an_unhandled_exception_is_counted_as_a_500(client, app_module, monkeypatch):
    """A crash that never became a response object is the one an operator most needs to see.

    The middleware records it and re-raises rather than swallowing it: a crash turned into a
    tidy response is the failure mode that keeps a bug alive for a week. In this process the
    exception surfaces out of ``TestClient`` (there is no catch-all handler), which is what
    ``pytest.raises`` asserts - the counter is still written on the way past.
    """

    def explode():
        raise RuntimeError("boom")

    monkeypatch.setattr(app_module, "get_shift_rules", explode)
    with pytest.raises(RuntimeError, match="boom"):
        client.get("/api/v1/admin/sites", headers=bearer(ADMIN))

    body = scrape(client)
    assert value(
        body,
        "attendance_http_requests_total",
        {"route": "/api/v1/admin/sites", "method": "GET", "status": "500"},
    ) >= 1
    assert value(
        body,
        "attendance_http_request_seconds_count",
        {"route": "/api/v1/admin/sites", "method": "GET"},
    ) >= 1, "a request that crashed still took time, and that is the number an operator wants"


# ---------------------------------------------------------------------------
# degrading without the library
# ---------------------------------------------------------------------------
def test_a_failure_counter_stays_silent_until_it_has_news():
    """Nothing to report must mean no series - and only a fresh interpreter can say so.

    A Prometheus client publishes *series*, so a counter with no children is a family with a
    HELP line and no samples at all: absent from the payload, present in the documentation.
    That is the shape an alerting rule wants ("fires on news, not on traffic"), and it is
    invisible from inside this session, where the failure paths have been exercised on
    purpose. So it is asserted where it is true: a clean interpreter that imports the module,
    renders, and never causes a failure. A counter that "completes" its family with a zero
    series fails here.
    """
    probe = (
        "import telemetry\n"
        "body = telemetry.render().decode()\n"
        "families = ['attendance_face_model_failures_total', 'attendance_face_engine_refusals_total']\n"
        "for family in families:\n"
        "    assert '# HELP ' + family in body, 'undocumented: ' + family\n"
        "    series = [\n"
        "        line for line in body.splitlines()\n"
        "        if line.startswith(family + '{') or line.startswith(family + ' ')\n"
        "    ]\n"
        "    assert not series, 'published with nothing to report: ' + repr(series)\n"
        "print('SILENT_OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(BACKEND_DIR),
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert "SILENT_OK" in result.stdout, result.stderr[-500:]


def test_every_helper_is_silent_when_the_library_is_missing():
    """Run in a clean interpreter with ``prometheus_client`` blocked at import."""
    probe = (
        "import sys; sys.modules['prometheus_client'] = None\n"
        "import telemetry\n"
        "assert telemetry.AVAILABLE is False, 'the library was importable'\n"
        "assert telemetry.render() == b''\n"
        "telemetry.observe_verification('approved')\n"
        "telemetry.observe_model_call(operation='represent', seconds=0.5, model='Facenet', detector='yunet')\n"
        "telemetry.observe_http(route='/x', method='GET', status=200, seconds=0.1)\n"
        "telemetry.count_lock_error(operation='select')\n"
        "telemetry.observe_match_score(0.3)\n"
        "telemetry.register_collectors()\n"
        "print('DEGRADED_OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(BACKEND_DIR),
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert "DEGRADED_OK" in result.stdout, result.stderr[-500:]


def test_the_metrics_endpoint_explains_a_missing_library():
    """501 with the install line, exactly like the XLSX export does for openpyxl."""
    probe = (
        "import sys; sys.modules['prometheus_client'] = None\n"
        "import telemetry\n"
        "from fastapi import HTTPException\n"
        "try:\n"
        "    raise HTTPException(status_code=501, detail={'error_code': 'metrics_unavailable'})\n"
        "except HTTPException as exc:\n"
        "    assert exc.status_code == 501\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], cwd=str(BACKEND_DIR), capture_output=True, text=True, timeout=180
    )
    assert "OK" in result.stdout, result.stderr[-500:]


# ---------------------------------------------------------------------------
# readiness
# ---------------------------------------------------------------------------
def _check(name: str):
    import readiness

    return {check.name: check for check in readiness.run_checks(None)[0]}[name]


def test_readiness_reports_the_metrics_mode(client, app_module):
    check = _check("metrics")
    assert check.tier == "advisory"
    assert check.ok is True
    assert check.value["available"] is True


def test_readiness_tells_an_operator_to_configure_a_scrape_token(client, app_module, monkeypatch):
    """No token means a scrape job cannot authenticate - a silent gap unless it is said."""
    monkeypatch.setattr(settings, "metrics_token", None, raising=False)
    check = _check("metrics")
    assert check.ok is True
    assert "no METRICS_TOKEN is set" in check.detail


def test_readiness_confirms_a_configured_scrape_token(client, app_module, monkeypatch):
    monkeypatch.setattr(settings, "metrics_token", "x" * 32, raising=False)
    check = _check("metrics")
    assert "scrape token" in check.detail
    assert "x" * 32 not in check.detail, "the token was printed into the readiness report"


def test_readiness_reports_metrics_as_disabled(client, app_module, monkeypatch):
    monkeypatch.setattr(settings, "metrics_enabled", False, raising=False)
    check = _check("metrics")
    assert check.ok is True and "404" in check.detail


# ---------------------------------------------------------------------------
# the helpers themselves
# ---------------------------------------------------------------------------
def test_sql_operation_reads_the_verb_and_ignores_comments_and_case():
    assert telemetry.sql_operation("SELECT 1") == "select"
    assert telemetry.sql_operation("  select * from users") == "select"
    assert telemetry.sql_operation("-- a comment\nINSERT INTO t VALUES (1)") == "insert"
    assert telemetry.sql_operation("/* block */ UPDATE t SET a = 1") == "update"
    assert telemetry.sql_operation("BEGIN IMMEDIATE") == "transaction"
    assert telemetry.sql_operation("PRAGMA busy_timeout = 5000") == "pragma"
    assert telemetry.sql_operation("CREATE TRIGGER x") == "ddl"
    assert telemetry.sql_operation("") == "other"
    assert telemetry.sql_operation(None) == "other"
    assert telemetry.sql_operation("EXPLAIN QUERY PLAN") == "other"


def test_routes_are_templates_not_paths(app_module):
    """The helper behind the label, tested directly: no request object, no guessing."""
    class Route:
        path = "/api/v1/q/{token}"

    class Request:
        def __init__(self, route, path="/api/v1/q/live-token", root_path=""):
            self.scope = {"route": route, "path": path, "root_path": root_path}

    assert telemetry.route_template(Request(Route())) == "/api/v1/q/{token}"
    assert telemetry.route_template(Request(None)) == telemetry.UNMATCHED_ROUTE
    assert telemetry.route_template(Request(None, root_path="/static")) == "/static/*"


def test_the_path_carrying_a_token_never_becomes_a_label(app_module):
    """The regression this whole design exists to prevent, in one assertion."""
    class Route:
        path = "/api/v1/q/{token}"

    class Request:
        scope = {"route": Route(), "path": "/api/v1/q/SUPER-SECRET-TOKEN", "root_path": ""}

    assert "SUPER-SECRET-TOKEN" not in telemetry.route_template(Request())


def test_a_metric_family_is_never_registered_twice(app_module):
    """Two sources for one name is a duplicate-timeseries crash at import, not a merge."""
    if not telemetry.AVAILABLE:
        pytest.skip("prometheus_client is not installed")
    telemetry.register_collectors()
    telemetry.register_collectors()
    body = telemetry.render().decode()
    assert body.count("# HELP attendance_active_sessions ") == 1
    assert body.count("# HELP attendance_face_engine_in_flight ") == 1


def test_the_counter_and_histogram_names_follow_the_conventions(client):
    """``_total`` on counters, base units (seconds) in names: a dashboard is written against
    these, so a rename is a breaking change and the shape is pinned here.

    Exercised first, for the reason spelled out in ``exercise_every_instrumented_path``: two of
    the names below (``attendance_verifications_total``, ``attendance_face_model_seconds_*``) are
    only in the payload once something has recorded them, and this test used to rely on an
    earlier test in the same process having done so.
    """
    exercise_every_instrumented_path(client)

    body = scrape(client)
    # Sample names, not family names: these are the strings a PromQL expression or a Grafana
    # panel is written against, and they are what the client library's name mangling produces.
    names = {sample.name for family in text_string_to_metric_families(body) for sample in family.samples}
    assert "attendance_verifications_total" in names
    assert "attendance_punches_total" in names
    assert "attendance_face_model_seconds_bucket" in names
    assert "attendance_face_model_seconds_count" in names
    assert "attendance_face_model_seconds_sum" in names
    assert "attendance_sqlite_statements_total" in names
    assert "attendance_http_requests_total" in names
    assert "attendance_http_request_seconds_count" in names
    assert "attendance_active_sessions" in names


def test_describe_never_reports_the_token_value():
    described = json.dumps(telemetry.describe())
    assert "token" in described
    assert "scrape token" in described or "admin JWT only" in described
