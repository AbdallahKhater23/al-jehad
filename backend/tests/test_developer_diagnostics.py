"""The root tier's operational surface: the models, the database, the child, the devices.

``developer.py`` already had the runtime store, the alert hub, the audit stream, the refused
punches and the session lever. What this suite covers is the *diagnostic* half added beside
them, and every property below is one of the reasons a surface like this exists at all:

* **it is the root tier's, and only the root tier's.** The refusals are asserted from the
  outside (administrator, head administrator, worker, nobody) because a diagnostics route that
  a site administrator can reach is a route that hands out the deployment's internals - scores,
  schema, device salts - to the audience it was moved away from.
* **asking never has a side effect it does not admit to.** No endpoint here may spawn the model
  process, repair the schema, or create the shadow log: each of those is either the punch path's
  job, startup's decision, or the offline tool's. The one deliberate exception is stated in the
  payload (the WAL report is a passive checkpoint) and pinned here.
* **the mode the deployment runs in has one answer.** The runtime override moves what the punch
  path, enrolment, readiness and the status surfaces all see, because they all resolve it
  through ``liveness.mode()`` - and it cannot move without an audit row carrying the reason.
* **the derived numbers come from the modules that own them.** The anchor arithmetic is
  ``offline_sync.effective_timestamp``'s, the cutover gate is ``shadow.flip_ready``'s, and the
  band view is ``face_detector``'s own; this suite compares the endpoints' answers against those
  functions rather than against copies of their arithmetic.

    pytest backend/tests/test_developer_diagnostics.py -q
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from database import db
from harness import ADMIN, HEAD_ADMIN, MOALLEM, WORKER, bearer

import developer
import face_detector
import face_engine
import face_process
import liveness
import offline_sync
import security
from config import settings

TS = "%Y-%m-%d %H:%M:%S"
DEV_ID = developer.DEVELOPER_ID_DEFAULT
DEV_PASSWORD = "root-credential-for-the-diagnostics-suite-1"

#: Every route this suite adds. One list, for the same reason ``test_developer_role`` keeps one:
#: a route added to the module without its own refusal assertion is still covered by the loop.
GET_ROUTES = (
    "/api/v1/developer/ml/diagnostics",
    "/api/v1/developer/ml/shadow-summary",
    "/api/v1/developer/db/stats",
    "/api/v1/developer/db/integrity",
    "/api/v1/developer/engine/process-stats",
    "/api/v1/developer/offline/devices",
    "/api/v1/developer/offline/tamper-alerts",
)

POST_ROUTES = (
    "/api/v1/developer/ml/liveness-mode",
    "/api/v1/developer/db/wal-checkpoint",
    "/api/v1/developer/engine/restart-worker",
)


def _as_developer() -> dict[str, str]:
    """Seed the root account into this test's database and return a header for it."""
    developer.seed_developer_account(password=DEV_PASSWORD, actor="test:developer_diagnostics")
    return bearer(DEV_ID, role=security.DEVELOPER_ROLE)


def _rows(sql: str, params: tuple = ()) -> list[dict]:
    with db() as conn:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _audit_rows(action: str) -> list[dict]:
    return _rows("SELECT * FROM audit_log WHERE action = ? ORDER BY id", (action,))


def _plant_device(
    device_id: str = "dev-alpha",
    *,
    worker_id: str = WORKER,
    salt: str = "0123456789abcdef0123456789abcdef",
    revoked_at: str | None = None,
) -> None:
    with db(write=True) as conn:
        conn.execute(
            "INSERT INTO worker_devices (device_id, worker_id, key_salt, key_epoch, created_at, "
            "last_seen_at, revoked_at, last_anchor_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                device_id,
                worker_id,
                salt,
                3,
                datetime.now().strftime(TS),
                datetime.now().strftime(TS),
                revoked_at,
                datetime.now().strftime(TS),
            ),
        )


def _plant_anchor(device_id: str, *, consumed: bool = False, hours_ago: int = 1) -> None:
    with db(write=True) as conn:
        conn.execute(
            "INSERT INTO device_anchors (anchor_id, device_id, worker_id, server_time, issued_at, "
            "issued_ip, consumed_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                f"anchor-{device_id}-{hours_ago}-{consumed}",
                device_id,
                WORKER,
                (datetime.now() - timedelta(hours=hours_ago)).strftime(TS),
                (datetime.now() - timedelta(hours=hours_ago)).strftime(TS),
                "203.0.113.9",
                "punch-1" if consumed else None,
            ),
        )


def _plant_punch(
    punch_id: str,
    *,
    status: str,
    rejection_code: str | None,
    anchor_server_time: str | None = None,
    monotonic_offset_s: float | None = None,
    client_timestamp: str | None = None,
) -> None:
    now = datetime.now().strftime(TS)
    with db(write=True) as conn:
        conn.execute(
            "INSERT INTO punch_queue (client_punch_id, device_id, worker_id, action, "
            "client_timestamp, client_offset_s, signature, received_at, status, rejection_code, "
            "anchor_server_time, monotonic_offset_s) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                punch_id,
                "dev-alpha",
                WORKER,
                "Clock In",
                client_timestamp or now,
                60,
                "signature-not-verified-here",
                now,
                status,
                rejection_code,
                anchor_server_time,
                monotonic_offset_s,
            ),
        )


# ---------------------------------------------------------------------------
# it is the root tier's, and only the root tier's
# ---------------------------------------------------------------------------
#: A body the mode lever accepts, so a 422 from the model cannot be mistaken for a refusal.
PROBE = {"mode": "off", "reason": "probe"}


def test_no_business_role_and_no_stranger_can_reach_a_diagnostic(client, app_module):
    _as_developer()
    for user_id in (ADMIN, HEAD_ADMIN, MOALLEM, WORKER):
        for path in GET_ROUTES:
            refused = client.get(path, headers=bearer(user_id))
            assert refused.status_code == 403, f"{user_id} reached {path}: {refused.status_code}"
        for path in POST_ROUTES:
            refused = client.post(path, json=PROBE, headers=bearer(user_id))
            assert refused.status_code in (403, 405), f"{user_id} reached {path}: {refused.status_code}"
    for path in GET_ROUTES:
        assert client.get(path).status_code == 401, path
    for path in POST_ROUTES:
        assert client.post(path, json=PROBE).status_code in (401, 405), path


def test_the_slow_query_ring_answers_on_the_route_it_has_always_had(client, app_module):
    """One surface, one path: the database tier's ring buffer is the diagnostics route.

    The alternative - a second name for the same thing at ``/developer/db/slow-queries`` - would
    be two paths answering one question, and the day they disagree (one of them gained a filter)
    is the day an operator cannot tell which answer is the deployment's.
    """
    body = client.get("/api/v1/developer/diagnostics/slow-queries", headers=_as_developer()).json()
    assert "threshold_ms" in body and "recent" in body


# ---------------------------------------------------------------------------
# the ML panel
# ---------------------------------------------------------------------------
def test_the_ml_panel_reports_the_models_the_detector_and_the_bands(client, app_module):
    _as_developer()
    panel = client.get("/api/v1/developer/ml/diagnostics", headers=_as_developer())
    assert panel.status_code == 200, panel.text
    body = panel.json()

    # The bands are the module's own table, keyed the same way, and the active one is named
    # separately because that is the table a punch is actually decided by.
    assert set(body["bands"]) == set(face_detector.BANDS)
    assert body["detector"]["pipeline"] == face_detector.active_pipeline()
    assert body["detector"]["detector"] == face_detector.active_detector()
    assert body["detector"]["crop"]["min_subject_px"] == face_detector.MIN_SUBJECT_PX
    # A missing measurement is a *state*, reported beside the table rather than raised: on a
    # checkout whose live pipeline has no derived band (the fallback detector, here) the panel
    # still has to answer.
    assert body["active_band"] is not None or body["active_band_error"]
    assert (body["active_band"] is None) != (body["active_band_error"] is None)

    # The queue facts come from the engine itself, not from a counter kept here.
    assert body["engine"]["capacity"] == face_engine.stats()["capacity"]
    assert body["engine"]["busy"] == face_engine.stats()["busy"]
    # liveness reports through its own status, override included.
    assert body["liveness"]["mode"] == liveness.mode()
    assert body["liveness_override"] == developer.liveness_override()
    # And the vocabulary, because this is the second door onto the same policy: the console's
    # mode select is drawn from what the endpoint publishes rather than from three words spelled
    # out in the frontend, which would be a fourth copy of ``liveness.MODES``.
    assert body["liveness_modes"] == list(liveness.MODES)


def test_a_band_view_carries_the_derivation_and_not_only_the_lines(client, app_module):
    """A hand-edited line and a derived one must not look the same on the panel."""
    _as_developer()
    body = client.get("/api/v1/developer/ml/diagnostics", headers=_as_developer()).json()
    for name, view in body["bands"].items():
        band = face_detector.BANDS[name]
        assert view["approve"] == band.approve and view["review"] == band.review
        assert view["derived_approve"] == band.derived()[0]
        assert view["derived_review"] == band.derived()[1]
        assert view["basis"] == band.basis()
        assert view["one_line"] == (band.review <= band.approve)


def test_the_shadow_summary_says_what_it_could_not_evaluate(client, app_module, monkeypatch):
    """No paired log: ``started`` is false and the reason names the setting, not a zero count."""
    _as_developer()
    monkeypatch.setattr(settings, "shadow_log_path", None, raising=False)
    body = client.get("/api/v1/developer/ml/shadow-summary", headers=_as_developer()).json()
    assert body["started"] is False
    assert body["summary"] is None and body["flip_ready"] is None
    assert "SHADOW_LOG_PATH" in body["reason"]


def _paired_log(path, rows: tuple[tuple[float, float], ...]) -> None:
    """A shadow log with the schema and ``rows`` of ``(enforced, shadow)`` distances."""
    import sqlite3

    import shadow

    with sqlite3.connect(str(path)) as conn:
        conn.executescript(shadow.SCHEMA)
        for enforced, shad in rows:
            conn.execute(
                "INSERT INTO shadow_scores (created_at, enforced_model, enforced_width, "
                "enforced_dist, enforced_verdict, shadow_model, shadow_width, shadow_dist, "
                "shadow_verdict, outcome, enforced_ms, shadow_ms, total_ms) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'shadow_ok', 12.0, 9.0, 21.0)",
                (
                    datetime.now().strftime(TS),
                    "facenet128",
                    128,
                    enforced,
                    "approved",
                    "facenet512",
                    512,
                    shad,
                    "approved" if shad == 0.10 else "refused",
                ),
            )
        conn.commit()


def test_the_shadow_summary_reads_a_paired_log_and_never_creates_one(client, app_module, tmp_path, monkeypatch):
    """Reading the paired log is four SELECTs; a GET that *created* it would be a lie."""
    import shadow

    _as_developer()
    log = tmp_path / "shadow_scores.db"
    _paired_log(log, ((0.10, 0.10), (0.10, 0.90)))  # one agreement, one disagreement

    report = shadow.summary_of(log)
    assert report["events"] == 2 and report["paired_samples"] == 2
    assert report["error_rate"] == 0.0 and report["verdict_agreement"] == 0.5

    monkeypatch.setattr(settings, "shadow_log_path", str(log), raising=False)
    body = client.get(
        "/api/v1/developer/ml/shadow-summary?enforced_threshold=0.5&shadow_threshold=0.5",
        headers=_as_developer(),
    ).json()
    assert body["started"] is True and body["summary"]["events"] == 2
    assert body["paired"]["paired_samples"] == 2
    # No gallery named, so the gate is *not evaluated* rather than "not ready".
    assert body["flip_ready"] is None and "gallery" in body["reason"]

    # A path that does not exist is refused, and stays not existing: constructing a scorer
    # against it would have written the schema and produced the empty log this endpoint would
    # then have reported as a quiet zero.
    fresh = tmp_path / "never_written.db"
    monkeypatch.setattr(settings, "shadow_log_path", str(fresh), raising=False)
    missing = client.get("/api/v1/developer/ml/shadow-summary", headers=_as_developer()).json()
    assert missing["started"] is False
    with pytest.raises(shadow.ShadowError):
        shadow.summary_of(fresh)
    assert not fresh.exists(), "reading a paired log created one"


def test_the_workforce_the_coverage_is_measured_against_is_the_schema_s_own(app_module):
    """The shadow migration's own two queries, held to the live schema.

    They were written against ``users.user_id`` and ``users.is_active``, neither of which any
    migration has ever created - so the backfill and the coverage denominator raised
    ``no such column`` on a real database, and the cutover gate could not be run at all. The
    denominator is on this suite's path now (``/developer/ml/shadow-summary``), and a roster
    query that has never been executed is exactly the kind that rots back.
    """
    import shadow_rollout

    with db() as conn:
        ids = shadow_rollout.worker_ids(conn)
        total = shadow_rollout.total_workers(conn)
        expected = [
            str(row["id"])
            for row in conn.execute(
                "SELECT id, status FROM users WHERE role = 'worker' ORDER BY id"
            ).fetchall()
            if str(row["status"] or "active") == "active"
        ]
    assert ids == expected and total == len(expected)
    # The seeded worker is in it, and the administrators are not: the fraction is about the
    # workforce, and a gallery of everybody would report coverage of a population that never
    # punches.
    assert WORKER in ids
    assert ADMIN not in ids and HEAD_ADMIN not in ids


def test_a_gallery_cache_carries_the_shadow_line_the_gate_needs(client, app_module, tmp_path, monkeypatch):
    """With the cache named, the gate runs; without it, the endpoint says what is missing.

    The line and the coverage both live in the cache the backfill wrote, so the difference
    between "not ready" and "not evaluated" is a question about a file - and answering it the
    other way round would report a verdict that no comparison supports.
    """
    log = tmp_path / "shadow_scores.db"
    cache = tmp_path / "shadow_gallery.json"
    cache.write_text(
        json.dumps(
            {
                "width": 512,
                "model_id": "0" * 16,
                "contract": "bgr:0_1",
                "band": {"model": "Facenet512", "approve": 0.31, "review": 0.31, "evidence": "measured"},
                "templates": {WORKER: [0.0] * 4},
            }
        ),
        encoding="utf-8",
    )
    _paired_log(log, ((0.10, 0.10),))
    monkeypatch.setattr(settings, "shadow_log_path", str(log), raising=False)
    _as_developer()
    # The enforced line is passed rather than read from the live band: which band is live is a
    # property of the checkout (a missing detector model moves it to the fallback pipeline), and
    # this test is about the cache, not about which pipeline the machine happens to run.
    without = client.get(
        "/api/v1/developer/ml/shadow-summary", params={"enforced_threshold": 0.5}, headers=_as_developer()
    ).json()
    assert without["started"] is True and without["paired"] is None
    assert "gallery" in without["reason"], without["reason"]

    # Passed as params rather than pasted into the URL: this is a Windows path, and a backslash
    # is not a legal character in a query string.
    with_cache = client.get(
        "/api/v1/developer/ml/shadow-summary",
        params={"enforced_threshold": 0.5, "gallery": str(cache)},
        headers=_as_developer(),
    ).json()
    assert with_cache["gallery"]["band"]["approve"] == 0.31
    assert with_cache["paired"]["shadow_threshold"] == 0.31
    assert with_cache["coverage"] is not None
    # The gate ran, and it said no - for a reason, which is the whole value of running it.
    assert with_cache["flip_ready"] is False
    assert with_cache["reason"]


# ---------------------------------------------------------------------------
# the liveness override: one answer, and an audit row to go with it
# ---------------------------------------------------------------------------
def test_the_override_moves_what_the_punch_path_and_readiness_see(client, app_module, monkeypatch):
    """The reason this is a *mode* rather than a caller's argument: readiness must agree."""
    _as_developer()
    monkeypatch.setattr(settings, "liveness_mode", "enforce", raising=False)
    monkeypatch.setattr(liveness, "get_session", lambda: None)

    # enforce with no evaluator is the fatal misconfiguration the startup gate exists for.
    assert liveness.readiness()["severity"] == "fatal"
    assert liveness.readiness()["ready"] is False

    # The punch path, before anything moves: a detected presentation attack is refused.
    spoof = liveness.LivenessResult(
        verdict=liveness.VERDICT_SPOOF,
        is_live=False,
        available=True,
        spoof_class=liveness.CLASS_PRINT,
        error_code=liveness.ERR_SPOOF,
    )
    assert liveness.gate(spoof).blocked is True

    moved = client.post(
        "/api/v1/developer/ml/liveness-mode",
        json={"mode": "advisory", "reason": "07:00 - the model is refusing honest workers"},
        headers=_as_developer(),
    )
    assert moved.status_code == 200, moved.text
    # ``previous`` is the mode that was *in force*, and ``None`` is the honest answer for a
    # deployment the setting alone was deciding: the store's default value is reported in the
    # audit row, where "what was it before" is the question being answered.
    assert moved.json()["mode"] == "advisory" and moved.json()["previous"] is None

    # ...and after it: the same verdict proceeds, flagged, through ``gate`` - which is the
    # function the punch path calls, with no argument changed at any call site.
    relaxed = liveness.gate(spoof)
    assert relaxed.allowed is True and relaxed.blocked is False

    assert liveness.mode() == "advisory"
    assert liveness.readiness()["ready"] is True
    assert liveness.readiness()["severity"] != "fatal"
    view = liveness.status()
    # Both halves are reported: "enforce, overridden to advisory" and "advisory, as configured"
    # call for different next steps, and one field folded together could not tell them apart.
    assert view["mode"] == "advisory"
    assert view["configured_mode"] == "enforce"
    assert view["override"] == "advisory"

    panel = client.get("/api/v1/developer/ml/diagnostics", headers=_as_developer()).json()
    assert panel["liveness_override"] == "advisory"
    assert panel["liveness"]["mode"] == "advisory"


def test_every_override_is_on_the_audit_trail_with_the_reason_that_was_given(client, app_module):
    _as_developer()
    changed = client.post(
        "/api/v1/developer/ml/liveness-mode",
        json={"mode": "enforce", "reason": "the model is installed; enforcing from here"},
        headers=_as_developer(),
    )
    assert changed.status_code == 200, changed.text

    rows = _audit_rows(developer.RUNTIME_CHANGE_ACTION)
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["actor_role"] == security.DEVELOPER_ROLE
    assert row["entity"] == "developer_config"
    assert row["entity_id"] == developer.LIVENESS_OVERRIDE_KEY
    # The default is recorded as its value rather than as a null: "there was no override" is a
    # fact about the before-state, and a null would read as "unknown".
    assert json.loads(row["before_json"]) == {"value": ""}
    after = json.loads(row["after_json"])
    assert after["value"] == "enforce"
    assert after["note"] == "the model is installed; enforcing from here"


def test_the_mode_lever_refuses_a_mode_it_does_not_have_and_a_change_with_no_reason(client, app_module):
    _as_developer()
    root = _as_developer()
    unknown = client.post(
        "/api/v1/developer/ml/liveness-mode", json={"mode": "paranoid", "reason": "why not"}, headers=root
    )
    assert unknown.status_code == 400, unknown.text
    assert "off, advisory, enforce" in unknown.json()["detail"]

    silent = client.post(
        "/api/v1/developer/ml/liveness-mode", json={"mode": "off", "reason": "   "}, headers=root
    )
    # The reason is not decoration: it is what the audit row is read for, so a lever that
    # accepted an empty one would let an enforcement downgrade into the trail unexplained.
    assert silent.status_code in (400, 422), silent.text
    assert developer.liveness_override() is None
    assert _audit_rows(developer.RUNTIME_CHANGE_ACTION) == []


def test_the_generic_runtime_door_moves_the_same_key_and_audits_it_the_same_way(client, app_module):
    """Two doors, one key, one trail - and clearing it is a value like any other."""
    _as_developer()
    root = _as_developer()
    moved = client.patch(
        "/api/v1/developer/runtime/liveness_mode_override",
        json={"value": "off", "note": "the detector will not load; keeping the gate open"},
        headers=root,
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["value"] == "off" and moved.json()["previous"] == ""
    assert liveness.mode() == "off"
    assert json.loads(_audit_rows(developer.RUNTIME_CHANGE_ACTION)[-1]["after_json"])["value"] == "off"

    refused = client.patch(
        "/api/v1/developer/runtime/liveness_mode_override", json={"value": "quiet"}, headers=root
    )
    assert refused.status_code == 400, refused.text
    assert liveness.mode() == "off", "a refused value moved the mode"

    cleared = client.patch(
        "/api/v1/developer/runtime/liveness_mode_override", json={"value": ""}, headers=root
    )
    assert cleared.status_code == 200, cleared.text
    assert developer.liveness_override() is None
    # Cleared means the deployment's own setting decides again - not "a fourth mode" and not
    # "still off".
    assert liveness.mode() == (settings.liveness_mode or liveness.MODE_ADVISORY).lower()
    assert json.loads(_audit_rows(developer.RUNTIME_CHANGE_ACTION)[-1]["before_json"])["value"] == "off"


def test_the_audit_stream_counts_a_runtime_change_as_a_security_event(client, app_module):
    """The developer's own incident stream is filtered by action; this one belongs in it."""
    _as_developer()
    client.post(
        "/api/v1/developer/ml/liveness-mode",
        json={"mode": "off", "reason": "rescuing a site whose detector is stale"},
        headers=_as_developer(),
    )
    body = client.get("/api/v1/developer/audit", headers=_as_developer()).json()
    assert developer.RUNTIME_CHANGE_ACTION in body["security_actions"]
    assert developer.RUNTIME_CHANGE_ACTION in {row["action"] for row in body["events"]}


def test_the_two_mode_vocabularies_are_the_same_three_words():
    """``developer`` restates them to avoid a cycle; this is what stops the copies drifting."""
    assert developer.LIVENESS_MODES == liveness.MODES
    assert developer.LIVENESS_OVERRIDE_CHOICES == ("",) + liveness.MODES


# ---------------------------------------------------------------------------
# the database tier
# ---------------------------------------------------------------------------
def test_the_database_stats_reach_the_pragmas_and_the_journal(client, app_module):
    _as_developer()
    body = client.get("/api/v1/developer/db/stats", headers=_as_developer()).json()
    counters = body["counters"]
    for key in (
        "connections_opened",
        "read_only_connections",
        "statements",
        "slow_statements",
        "lock_waits",
        "lock_timeouts",
        "max_lock_wait_seconds",
        "busy_timeout_ms",
        "slow_statement_threshold_ms",
    ):
        assert key in counters, key
    assert body["pragmas"]["journal_mode"].lower() == "wal"
    assert body["pragmas"]["page_size"] > 0
    assert body["wal"]["pages_in_log"] >= 0
    assert body["wal"]["report_is_a_passive_checkpoint"] is True
    # The counters are one worker's, and the payload says so rather than letting an operator
    # read a single process's statement count as the deployment's.
    assert "per process" in body["scope"]
    # The checkpoint modes travel with the panel that offers them, for the reason the liveness
    # modes do: the console's select asks for exactly what ``db_wal_checkpoint`` accepts, so a
    # vocabulary edit lands in one place instead of two that can disagree with a 400.
    assert body["checkpoint_modes"] == list(developer.WAL_CHECKPOINT_MODES)
    assert body["checkpoint_modes"] == ["PASSIVE", "FULL", "RESTART", "TRUNCATE"]


def test_the_integrity_report_compares_the_schema_and_never_repairs_it(client, app_module):
    _as_developer()
    body = client.get("/api/v1/developer/db/integrity", headers=_as_developer()).json()
    import migrations

    assert body["schema"]["ready"] is True, body["schema"]
    assert body["schema"]["mode"] != "enforce_repair", "a GET repaired the schema"
    assert body["schema_version"]["current"] == migrations.SCHEMA_VERSION
    assert body["schema_version"]["expected"] == migrations.SCHEMA_VERSION
    assert body["schema_version"]["pending"] == []
    assert body["integrity"]["ok"] is True, body["integrity"]
    assert body["integrity"]["output"] == ["ok"]


def test_the_wal_checkpoint_runs_in_the_named_mode_and_audits_both_counts(client, app_module):
    _as_developer()
    root = _as_developer()
    body = client.post("/api/v1/developer/db/wal-checkpoint?mode=TRUNCATE", headers=root).json()
    assert body["status"] == "success" and body["mode"] == "TRUNCATE"
    assert body["busy"] == 0, "a checkpoint on an idle database reported a busy handler"
    assert isinstance(body["log_frames"], int) and isinstance(body["before_frames"], int)

    rows = _audit_rows("db_wal_checkpoint")
    assert len(rows) == 1
    assert rows[0]["actor_role"] == security.DEVELOPER_ROLE
    assert json.loads(rows[0]["after_json"])["mode"] == "TRUNCATE"
    assert "pages_in_log" in json.loads(rows[0]["before_json"])

    # The default is the mode that gives the disk space back; anything outside the vocabulary is
    # refused rather than passed to SQLite as text.
    default = client.post("/api/v1/developer/db/wal-checkpoint", headers=root).json()
    assert default["mode"] == "TRUNCATE"
    refused = client.post("/api/v1/developer/db/wal-checkpoint?mode=VACUUM", headers=root)
    assert refused.status_code == 400, refused.text
    assert len(_audit_rows("db_wal_checkpoint")) == 2, "a refused checkpoint was audited as if it ran"


# ---------------------------------------------------------------------------
# the engine child
# ---------------------------------------------------------------------------
def test_the_engine_panel_reports_in_process_inference_rather_than_inventing_a_child(client, app_module):
    _as_developer()
    body = client.get("/api/v1/developer/engine/process-stats", headers=_as_developer()).json()
    assert body["enabled"] is False
    assert body["message"] == "In-process inference active"
    # The queue is still reported - the models are in this process, and that is the load panel
    # an operator is looking at.
    assert "queue_depth" in body["engine"]


def test_a_diagnostics_call_never_spawns_the_model_process(client, app_module, monkeypatch):
    """The whole point of the flag is that this process does not hold the models.

    A monitoring route that built the child to measure it would put the 200 MiB back on the API
    process, on a schedule, unasked - so the panel is required to answer from the transport's
    own counters without ever starting one.
    """
    _as_developer()
    monkeypatch.setattr(settings, "face_engine_process", True, raising=False)
    monkeypatch.setattr(face_process, "_CLIENT", None, raising=False)
    body = client.get("/api/v1/developer/engine/process-stats", headers=_as_developer()).json()
    assert body["enabled"] is True and body["started"] is False
    assert face_process.current() is None, "the diagnostics panel started the model process"


class _FakeTransport:
    """A transport with the counters the panel reads, and no child process behind it."""

    def __init__(self, *, ping_error: Exception | None = None) -> None:
        self.ping_error = ping_error
        self.restarts = 0
        self.pids = [101, 202]

    def stats(self) -> dict:
        index = min(self.restarts, len(self.pids) - 1)
        return {
            "name": "face-worker",
            "handler": "face_worker.py",
            "alive": True,
            "pid": self.pids[index],
            "worker_pid": self.pids[index],
            "starts": index + 1,
            "calls": 4,
            "failures": 0,
            "timeouts": 0,
            "in_flight": 0,
            "last_call_ms": 12.5,
            "deadline_seconds": 30.0,
            "last_error": None,
        }

    def restart(self) -> None:
        self.restarts += 1

    def ping(self) -> dict:
        if self.ping_error is not None:
            raise self.ping_error
        return {"pid": 202, "python": "3.12.10"}


def test_the_restart_lever_replaces_the_child_once_and_records_both_pids(client, app_module, monkeypatch):
    _as_developer()
    monkeypatch.setattr(settings, "face_engine_process", True, raising=False)
    transport = _FakeTransport()
    monkeypatch.setattr(face_process, "client", lambda: transport)

    body = client.post("/api/v1/developer/engine/restart-worker", headers=_as_developer()).json()
    assert body["status"] == "success"
    assert transport.restarts == 1
    assert body["old_pid"] == 101 and body["new_pid"] == 202
    assert body["new_worker_pid"] == 202

    rows = _audit_rows("engine_worker_restart")
    assert len(rows) == 1
    assert json.loads(rows[0]["before_json"])["pid"] == 101
    assert json.loads(rows[0]["after_json"])["pid"] == 202


def test_a_child_that_does_not_answer_the_ping_is_reported_rather_than_raised(client, app_module, monkeypatch):
    """The restart happened; the answer says what was left unproven instead of 500-ing."""
    _as_developer()
    monkeypatch.setattr(settings, "face_engine_process", True, raising=False)
    transport = _FakeTransport(ping_error=face_process.FaceProcessTimeout("no answer"))
    monkeypatch.setattr(face_process, "client", lambda: transport)

    response = client.post("/api/v1/developer/engine/restart-worker", headers=_as_developer())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["new_pid"] == 202 and body["new_worker_pid"] is None
    assert "FaceProcessTimeout" in body["ping_error"]


def test_the_restart_lever_is_refused_when_there_is_no_separate_process(client, app_module, monkeypatch):
    _as_developer()
    monkeypatch.setattr(settings, "face_engine_process", False, raising=False)
    refused = client.post("/api/v1/developer/engine/restart-worker", headers=_as_developer())
    assert refused.status_code == 409, refused.text
    assert "FACE_ENGINE_PROCESS" in refused.json()["detail"]
    assert face_process.current() is None
    assert _audit_rows("engine_worker_restart") == []


# ---------------------------------------------------------------------------
# offline forensics
# ---------------------------------------------------------------------------
def test_the_device_ledger_masks_the_salt_and_counts_the_anchors(client, app_module):
    _as_developer()
    _plant_device(salt="0123456789abcdef")
    _plant_anchor("dev-alpha", consumed=True)
    _plant_anchor("dev-alpha", consumed=False)
    _plant_anchor("dev-alpha", consumed=False, hours_ago=24 * 10)  # outside the 72 h window

    body = client.get("/api/v1/developer/offline/devices", headers=_as_developer()).json()
    device = next(row for row in body["devices"] if row["device_id"] == "dev-alpha")
    assert device["key_salt"] == {"prefix": "012345", "length": 16}
    assert device["key_epoch"] == 3
    assert device["worker_name"], "the ledger cannot say whose device it is"
    assert body["window_hours"] == developer.ANCHOR_WINDOW_HOURS
    # The window is the endpoint's own, compared against the same arithmetic in SQL: the anchor
    # from ten hours into the past is outside three days and is not counted.
    expected = _rows(
        "SELECT COUNT(*) AS n FROM device_anchors WHERE issued_at >= ?", (body["since"],)
    )[0]["n"]
    assert body["anchors"]["issued"] == expected == 2
    assert body["anchors"]["consumed"] + body["anchors"]["unconsumed"] == expected
    assert body["anchors"]["unconsumed"] == 1
    # The mask is the point rather than a formatting choice: a ledger that shipped the salt
    # would be a second copy of the key material the offline protocol signs with.
    assert "0123456789abcdef" not in json.dumps(body)


def test_a_device_that_was_revoked_is_still_listed_with_its_marks(client, app_module):
    _as_developer()
    _plant_device("dev-gone", revoked_at=datetime.now().strftime(TS))
    body = client.get("/api/v1/developer/offline/devices", headers=_as_developer()).json()
    device = next(row for row in body["devices"] if row["device_id"] == "dev-gone")
    assert device["revoked_at"] is not None


def test_the_tamper_list_derives_the_punch_time_the_way_the_server_does(client, app_module):
    """The anchor arithmetic is ``offline_sync``'s, compared against the endpoint's answer."""
    _as_developer()
    anchor = (datetime.now() - timedelta(hours=2)).replace(microsecond=0)
    claimed = anchor + timedelta(seconds=400)  # the device's clock is 6m40s ahead
    _plant_punch(
        "punch-tampered",
        status="rejected",
        rejection_code="clock_tampered",
        anchor_server_time=anchor.strftime(TS),
        monotonic_offset_s=90.0,
        client_timestamp=claimed.strftime(TS),
    )
    _plant_punch("punch-skewed", status="rejected", rejection_code="replayed_nonce")
    _plant_punch("punch-departed", status="accepted", rejection_code=None)

    body = client.get("/api/v1/developer/offline/tamper-alerts", headers=_as_developer()).json()
    by_id = {row["client_punch_id"]: row for row in body["alerts"]}
    assert "punch-departed" not in by_id, "an accepted punch is not a tamper alert"

    expected = offline_sync.effective_timestamp(anchor.strftime(TS), 90.0)
    row = by_id["punch-tampered"]
    assert row["effective_time"] == expected.strftime(TS)
    assert row["skew_seconds"] == pytest.approx((claimed - expected).total_seconds(), abs=0.1)
    assert row["tamper"] is True
    assert row["anchor_server_time"] and row["monotonic_offset_s"] == 90.0

    # A rejection with no anchor behind it has no derived time - and is reported as a tamper
    # alert anyway, because the code is the reason the list exists.
    replay = by_id["punch-skewed"]
    assert replay["tamper"] is True
    assert replay["effective_time"] is None and replay["skew_seconds"] is None

    assert body["counts"] == {"in_window": 2, "tamper": 2, "with_an_anchor": 1}
    assert set(body["tamper_codes"]) == set(developer.TAMPER_CODES)


def test_the_tamper_list_filters_by_worker_and_keeps_a_rejected_row_with_another_code(client, app_module):
    _as_developer()
    _plant_punch("punch-flagged", status="rejected", rejection_code="low_confidence")
    body = client.get("/api/v1/developer/offline/tamper-alerts", headers=_as_developer()).json()
    row = next(item for item in body["alerts"] if item["client_punch_id"] == "punch-flagged")
    # Rejected for another reason: in the list (it is still a refusal from the queue) and marked
    # as *not* a tamper, which is the distinction the panel is read for.
    assert row["tamper"] is False

    elsewhere = client.get(
        f"/api/v1/developer/offline/tamper-alerts?worker_id=999999", headers=_as_developer()
    ).json()
    assert elsewhere["alerts"] == []
    assert elsewhere["counts"]["in_window"] == 0
