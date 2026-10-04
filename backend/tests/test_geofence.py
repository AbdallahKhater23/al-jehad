"""The visual geofence: the cached fence, the clamped Haversine, and the fail-fast punch.

WHAT THIS SUITE HOLDS
---------------------
The feature has three claims that are worth more than the code that makes them, and each is
pinned here rather than asserted in a docstring:

1. **the punch decides from memory.** ``evaluate()`` reads ``geofence.CACHE`` and nothing
   else - no query, no disk, no model. The test proves it by *removing the database* under a
   running application and showing the refusal still comes back correct and fast. A punch
   that quietly opened a connection would fail there, and the latency budget
   (``< 1 ms``, the requirement) is measured rather than described.
2. **an out-of-fence fix is refused before anything is written.** The 403 is asserted, and
   so is the absence of a row: a refusal that still filed a punch is not a refusal.
3. **the legacy endpoint and the modern one are the same fence.** They write one row, one
   cache entry and one answer shape, so an old microservice and the console cannot hold two
   different boundaries.

The arithmetic itself is pinned against known distances (the equator degree, a meridian
degree, the antipode) and against the *domain* cases that used to be a ``ValueError``: a
zero-distance fix and a pair of antipodal points both have to come back as numbers.
"""

from __future__ import annotations

import math
import sqlite3
import time

import pytest

import geofence
import harness
from harness import ADMIN, HEAD_ADMIN, WORKER, bearer, db_scalar

pytestmark = pytest.mark.regression

#: A fix ~50 m north of the default fence centre: comfortably inside the default 100 m.
INSIDE = {"latitude": geofence.DEFAULT_LATITUDE + 0.00045, "longitude": geofence.DEFAULT_LONGITUDE}
#: London: inside no fence this deployment could plausibly have.
OUTSIDE = {"latitude": 51.5074, "longitude": -0.1278}

PUNCH = "/api/v1/attendance/punch"
MODERN = "/api/v1/geofence"
LEGACY = "/api/v1/legacy/set-geofence"


def _punch(client, *, user_id: str = WORKER, headers=None, **fix):
    body = {"user_id": user_id, **fix}
    return client.post(PUNCH, headers=headers or bearer(user_id), json=body)


def _punches() -> int:
    return int(db_scalar("SELECT COUNT(*) FROM attendance_punches") or 0)


# ---------------------------------------------------------------------------
# 1. the arithmetic
# ---------------------------------------------------------------------------
def test_a_degree_of_latitude_is_about_111_km():
    """The one distance everybody can check: a degree of meridian, at the equator."""
    metres = geofence.haversine_meters(0.0, 0.0, 1.0, 0.0)
    assert metres == pytest.approx(111_194.9, rel=1e-4), metres


def test_a_degree_of_longitude_shrinks_with_latitude():
    """A degree of longitude is a degree of *arc* scaled by ``cos(latitude)``."""
    equator = geofence.haversine_meters(0.0, 0.0, 0.0, 1.0)
    at_60 = geofence.haversine_meters(60.0, 0.0, 60.0, 1.0)
    assert equator == pytest.approx(111_194.9, rel=1e-4), equator
    assert at_60 == pytest.approx(equator * math.cos(math.radians(60.0)), rel=1e-3), at_60


def test_the_same_point_is_zero_metres_and_not_a_domain_error():
    """``a`` is exactly 0 here, and the clamp is what keeps ``sqrt`` out of negative numbers."""
    assert geofence.haversine_meters(30.05, 31.23, 30.05, 31.23) == 0.0


def test_antipodal_points_are_half_the_planet_and_not_a_value_error():
    """``a`` rounds to 1 at the antipode; ``sqrt(1 - a)`` would be ``sqrt(-0.0)`` without the clamp.

    This is the failure the requirement names: a float-precision domain error on ``asin`` /
    ``atan2``. A pair of antipodal fixes is the extreme case, and the answer has to be
    ``pi * R`` rather than a 500.
    """
    metres = geofence.haversine_meters(0.0, 0.0, 0.0, 180.0)
    assert metres == pytest.approx(math.pi * geofence.EARTH_RADIUS_METERS, rel=1e-6), metres


@pytest.mark.parametrize(
    "latitude,longitude",
    [(0.0, 0.0), (91.0, 0.0), (0.0, 181.0), (float("nan"), 0.0), (float("inf"), 0.0)],
)
def test_implausible_fixes_are_not_wgs84(latitude, longitude):
    assert geofence.within_wgs84(latitude, longitude) is False


def test_a_real_fix_is_wgs84():
    assert geofence.within_wgs84(geofence.DEFAULT_LATITUDE, geofence.DEFAULT_LONGITUDE) is True
    # A latitude of 0 with a real longitude is a real place; only the *pair* of zeros is
    # the mock-location reading.
    assert geofence.within_wgs84(0.0, 31.23) is True


# ---------------------------------------------------------------------------
# 2. the cache: cold boot, and the punch that never reads the database
# ---------------------------------------------------------------------------
def test_a_cold_boot_gets_the_documented_default_fence(app_module):
    """No row in ``geofence_settings`` is a supported state, not an uninitialised one."""
    assert db_scalar("SELECT COUNT(*) FROM geofence_settings") == 0
    fence = geofence.refresh_cache()
    assert (fence.latitude, fence.longitude) == (
        geofence.DEFAULT_LATITUDE,
        geofence.DEFAULT_LONGITUDE,
    )
    assert fence.radius_meters == geofence.DEFAULT_RADIUS_METERS
    assert fence.source == "default"


def test_the_punch_decides_with_no_database_at_all(client, app_module, monkeypatch):
    """The zero-latency claim, proved by taking the database away.

    ``evaluate`` is the whole decision, and it must not touch SQLite. The test points the
    application at a path that does not exist and asserts the refusal is still correct -
    which a punch path that opened a connection for the fence, the accuracy policy or
    anything else could not survive.
    """
    from pathlib import Path

    geofence.refresh_cache()
    monkeypatch.setattr(
        "database.DB_PATH", Path("Z:/there-is-no-database-here/times.db"), raising=False
    )
    verdict = geofence.evaluate(OUTSIDE["latitude"], OUTSIDE["longitude"])
    assert verdict.approved is False
    assert verdict.reason == "outside_geofence"
    assert verdict.distance_meters > 1000


def test_the_decision_is_under_a_millisecond():
    """The budget the requirement names, measured over many calls rather than one.

    The threshold is deliberately generous for a test host (a shared CI machine can be
    slow): the claim being pinned is *no I/O on this path*, and a single Haversine is
    hundreds of nanoseconds. A path that opened a connection, read a file or logged a row
    would be two to four orders of magnitude above this line, which is the difference the
    test is for.
    """
    geofence.refresh_cache()
    started = time.perf_counter()
    for _ in range(1000):
        geofence.evaluate(INSIDE["latitude"], INSIDE["longitude"], 5.0)
    average_ms = (time.perf_counter() - started) * 1000.0 / 1000.0
    assert average_ms < 1.0, f"the in-memory decision averaged {average_ms:.3f} ms"


def test_an_edit_moves_the_cache_and_the_database_together(client):
    """The write the requirement asks for: SQLite *and* the cache, in the same request."""
    response = client.post(
        MODERN,
        headers=bearer(ADMIN),
        json={"latitude": 29.98, "longitude": 31.75, "radius_meters": 250.0},
    )
    assert response.status_code == 200, response.text
    assert response.json()["geofence"]["radius_meters"] == 250.0

    # The cache the next punch reads.
    assert geofence.CACHE.fence.latitude == pytest.approx(29.98)
    assert geofence.CACHE.fence.radius_meters == pytest.approx(250.0)
    # And the durable copy.
    assert db_scalar("SELECT latitude FROM geofence_settings ORDER BY id DESC LIMIT 1") == pytest.approx(29.98)
    assert db_scalar("SELECT COUNT(*) FROM geofence_settings") == 1


def test_the_newest_row_is_the_active_fence(app_module):
    """The table keeps the history; ``ORDER BY id DESC`` is what makes one row active."""
    with app_module.db(write=True) as conn:
        for radius in (100.0, 200.0, 300.0):
            conn.execute(
                "INSERT INTO geofence_settings (latitude, longitude, radius_meters, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (30.05, 31.23, radius, "2026-10-03 09:00:00"),
            )
    fence = geofence.refresh_cache()
    assert fence.radius_meters == pytest.approx(300.0)
    assert fence.source == "database"


# ---------------------------------------------------------------------------
# 3. the punch pipeline
# ---------------------------------------------------------------------------
def test_a_fix_inside_the_fence_is_approved_and_recorded(client):
    response = _punch(client, **INSIDE, accuracy_meters=8.0)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "approved"
    assert body["distance_meters"] < 100.0
    assert body["radius_meters"] == geofence.DEFAULT_RADIUS_METERS
    assert body["accuracy_degraded"] is False
    assert body["verification_ms"] < 1.0
    assert _punches() == 1
    assert db_scalar("SELECT status FROM attendance_punches") == "approved"


def test_a_fix_outside_the_fence_is_refused_with_403_and_writes_nothing(client):
    response = _punch(client, **OUTSIDE)
    assert response.status_code == 403, response.text
    body = response.json()
    assert body["status"] == "rejected"
    assert body["reason"] == "outside_geofence"
    assert body["distance_meters"] > 1_000_000
    assert _punches() == 0, "a refusal that filed a punch is not a refusal"


def test_an_unusable_accuracy_is_a_400_rather_than_a_403(client):
    """The two refusals mean different things and are answered differently.

    "Your GPS cannot support this reading" is the phone's problem and is fixable (step
    outside, wait for a better fix). "You are not at work" is a fact about the person. A
    worker is owed the distinction.
    """
    response = _punch(client, **INSIDE, accuracy_meters=geofence.MAX_ACCURACY_METERS + 1)
    assert response.status_code == 400, response.text
    body = response.json()
    assert body["reason"] == "accuracy_degraded_beyond_usable"
    assert body["accuracy_degraded"] is True
    assert _punches() == 0


def test_the_accuracy_ceiling_itself_is_accepted(client):
    """The boundary is inclusive: exactly 50 m is usable, 50.0001 m is not.

    Measured from the fence centre, so the fix's own uncertainty does not reach the boundary
    and the answer is an unqualified approval - the ceiling is what is being tested here, not
    the drift flag.
    """
    response = _punch(
        client,
        latitude=geofence.DEFAULT_LATITUDE,
        longitude=geofence.DEFAULT_LONGITUDE,
        accuracy_meters=geofence.MAX_ACCURACY_METERS,
    )
    assert response.status_code == 200, response.text
    assert response.json()["accuracy_degraded"] is False


def test_drift_at_the_boundary_is_approved_and_flagged(client):
    """GPS drift: the fix is inside, its uncertainty reaches the line, so it is *flagged*.

    Refusing here would refuse a worker standing in the right place with an honest phone.
    Approving silently would record a reading that may be on the other side of the boundary.
    So the punch is recorded and the flag travels with it, to the client and to the ledger.
    """
    radius = geofence.DEFAULT_RADIUS_METERS
    # A fix 90 m out with a ±20 m fix: inside the 100 m fence, and 90 + 20 > 100.
    latitude = geofence.DEFAULT_LATITUDE + 90.0 / geofence.EARTH_RADIUS_METERS * (180.0 / math.pi)
    response = _punch(client, latitude=latitude, longitude=geofence.DEFAULT_LONGITUDE, accuracy_meters=20.0)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["accuracy_degraded"] is True
    assert body["distance_meters"] < radius
    assert db_scalar("SELECT accuracy_degraded FROM attendance_punches") == 1


def test_the_drift_alert_is_filed_once_a_day(client):
    """A worker at the boundary all morning leaves one notice, not forty."""
    latitude = geofence.DEFAULT_LATITUDE + 90.0 / geofence.EARTH_RADIUS_METERS * (180.0 / math.pi)
    for _ in range(3):
        response = _punch(client, latitude=latitude, longitude=geofence.DEFAULT_LONGITUDE, accuracy_meters=20.0)
        assert response.status_code == 200, response.text
    notices = int(db_scalar(
        "SELECT COUNT(*) FROM admin_notifications WHERE kind = 'geofence_accuracy_drift'"
    ) or 0)
    assert notices == 1, notices
    assert _punches() == 3


def test_a_mock_location_reading_is_a_400(client):
    """``(0, 0)`` is the mock provider's output: invalid input, not "outside the fence"."""
    response = _punch(client, latitude=0.0, longitude=0.0)
    assert response.status_code == 400, response.text
    assert _punches() == 0


def test_the_body_cannot_name_another_account(client):
    """The session is the identity; the payload field may only agree with it.

    A worker's own session, claiming to be the head administrator: refused, and nothing is
    recorded. This is the check that makes the ``user_id`` field in the contract a
    compatibility courtesy rather than a way to punch in for somebody else.
    """
    response = _punch(client, user_id=HEAD_ADMIN, headers=bearer(WORKER), **INSIDE)
    assert response.status_code == 403, response.text
    assert "own account" in response.json()["detail"]
    assert _punches() == 0


def test_a_punch_requires_a_session(client):
    response = client.post(PUNCH, json={"user_id": WORKER, **INSIDE})
    assert response.status_code in (401, 403), response.status_code
    assert _punches() == 0


def test_the_developer_account_does_not_check_in(client):
    """The root tier owns the deployment, not a rota - the same rule the punch path has.

    The session has to name its *own* account (the root one) for this to reach the refusal
    under test: claiming somebody else's id is a different refusal, asserted above.
    """
    import developer

    headers = harness.root_bearer()
    response = _punch(client, user_id=developer.DEVELOPER_ID_DEFAULT, headers=headers, **INSIDE)
    assert response.status_code == 403, response.text
    assert "developer" in response.json()["detail"].lower()
    assert _punches() == 0


def test_a_refusal_is_answered_before_the_face_check_would_run(client, face):
    """The ordering the requirement asks for, seen from outside: no model was consulted.

    ``face.FACE_MODE`` is set to a *mismatch*, which is what a punch that reached the face
    check would be refused for. The refusal that comes back is the geofence's, and the face
    engine was never asked - so the expensive half of the pipeline is not reached by a fix
    that is not at the site.
    """
    face.FACE_MODE = "mismatch"
    response = _punch(client, **OUTSIDE)
    assert response.status_code == 403
    assert response.json()["reason"] == "outside_geofence"
    assert _punches() == 0


# ---------------------------------------------------------------------------
# 4. the two ways of setting the fence
# ---------------------------------------------------------------------------
def test_the_legacy_payload_is_normalised_into_the_modern_one(client):
    """Strings, integers and whitespace - the three shapes the old caller sends."""
    response = client.post(
        LEGACY,
        headers=bearer(HEAD_ADMIN),
        json={"lat": " 29.98 ", "lng": "31.75", "rad": 150},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["normalized"] is True
    assert body["geofence"]["latitude"] == pytest.approx(29.98)
    assert body["geofence"]["radius_meters"] == pytest.approx(150.0)
    # The answer speaks the legacy dialect too, with the values that were actually stored.
    assert body["legacy"] == {"lat": "29.98", "lng": "31.75", "rad": "150.0"}
    # And it is the *same* fence the modern read answers with.
    assert geofence.CACHE.fence.latitude == pytest.approx(29.98)
    assert client.get(MODERN, headers=bearer(ADMIN)).json()["geofence"]["radius_meters"] == pytest.approx(150.0)


def test_a_comma_decimal_is_read_as_a_decimal(client):
    """A locale-formatted number is what a template-rendered legacy request sends."""
    response = client.post(
        LEGACY, headers=bearer(ADMIN), json={"lat": "29,98", "lng": "31,75", "rad": "120"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["geofence"]["latitude"] == pytest.approx(29.98)


def test_the_legacy_radius_defaults_when_it_is_absent(client):
    response = client.post(LEGACY, headers=bearer(ADMIN), json={"lat": "29.98", "lng": "31.75"})
    assert response.status_code == 200, response.text
    assert response.json()["geofence"]["radius_meters"] == geofence.DEFAULT_RADIUS_METERS


def test_a_legacy_radius_that_is_absurd_is_clamped_and_says_so(client):
    """A bound is *clamped* where the clamp is a real policy, and the answer reports the stored value."""
    response = client.post(
        LEGACY, headers=bearer(ADMIN), json={"lat": "29.98", "lng": "31.75", "rad": "99999"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["geofence"]["radius_meters"] == geofence.MAX_RADIUS_METERS


def test_a_legacy_coordinate_outside_wgs84_is_refused_not_clamped(client):
    """A latitude of 999 is a typo, and clamping it would put a fence nobody surveyed."""
    response = client.post(
        LEGACY, headers=bearer(ADMIN), json={"lat": "999", "lng": "31.75", "rad": "100"}
    )
    assert response.status_code == 422, response.text
    assert geofence.CACHE.fence.latitude == geofence.DEFAULT_LATITUDE


def test_a_legacy_radius_of_zero_is_refused(client):
    """A fence of radius 0 admits nobody - a site nobody can clock into."""
    response = client.post(
        LEGACY, headers=bearer(ADMIN), json={"lat": "29.98", "lng": "31.75", "rad": "0"}
    )
    assert response.status_code == 422, response.text


@pytest.mark.parametrize(
    "payload",
    [
        {"lat": "not-a-number", "lng": "31.75", "rad": "100"},
        {"lat": "", "lng": "31.75", "rad": "100"},
        {"lat": "29.98", "lng": "31.75", "rad": "wide"},
    ],
)
def test_a_legacy_payload_that_is_not_numeric_is_a_422(client, payload):
    response = client.post(LEGACY, headers=bearer(ADMIN), json=payload)
    assert response.status_code == 422, response.text


def test_the_modern_payload_validates_its_ranges(client):
    for payload in (
        {"latitude": 91.0, "longitude": 31.23, "radius_meters": 100},
        {"latitude": 30.05, "longitude": 181.0, "radius_meters": 100},
        {"latitude": 30.05, "longitude": 31.23, "radius_meters": 0},
        {"latitude": 30.05, "longitude": 31.23, "radius_meters": -5},
    ):
        response = client.post(MODERN, headers=bearer(ADMIN), json=payload)
        assert response.status_code == 422, (payload, response.status_code)


def test_the_modern_endpoint_refuses_null_island(client):
    response = client.post(
        MODERN, headers=bearer(ADMIN), json={"latitude": 0.0, "longitude": 0.0, "radius_meters": 100}
    )
    assert response.status_code == 422, response.text


# ---------------------------------------------------------------------------
# 5. who may do what
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("endpoint", [MODERN, LEGACY])
def test_a_worker_cannot_move_the_fence(client, endpoint):
    payload = (
        {"latitude": 29.98, "longitude": 31.75, "radius_meters": 100}
        if endpoint == MODERN
        else {"lat": "29.98", "lng": "31.75", "rad": "100"}
    )
    response = client.post(endpoint, headers=bearer(WORKER), json=payload)
    assert response.status_code == 403, response.text
    assert geofence.CACHE.fence.latitude == geofence.DEFAULT_LATITUDE


def test_an_administrator_may_read_the_fence(client):
    response = client.get(MODERN, headers=bearer(ADMIN))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["geofence"]["radius_meters"] == geofence.DEFAULT_RADIUS_METERS
    assert body["slider"]["min_meters"] == geofence.SLIDER_MIN_RADIUS_METERS


def test_a_worker_cannot_read_the_fence(client):
    """The path is the administrator's, so this is a refusal rather than a smaller answer."""
    assert client.get(MODERN, headers=bearer(WORKER)).status_code == 403


def test_an_edit_is_audited_with_both_geometries(client):
    client.post(
        MODERN, headers=bearer(ADMIN), json={"latitude": 29.98, "longitude": 31.75, "radius_meters": 175.0}
    )
    row = sqlite3.connect(str(harness.DB_PATH)).execute(
        "SELECT before_json, after_json, actor_id FROM audit_log "
        "WHERE action = 'geofence_update' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row is not None, "the edit was not audited"
    assert row[2] == ADMIN
    assert "30.05" in row[0] and "175.0" in row[1]


# ---------------------------------------------------------------------------
# 6. the schema the feature owns, and the one it must not touch
# ---------------------------------------------------------------------------
def test_the_feature_does_not_touch_attendance_history(client, app_module):
    """The compatibility requirement, measured: the punch path writes its own two tables only.

    A clock-in that went through this endpoint must leave ``attendance_logs`` and
    ``active_sessions`` exactly as the fixture seeded them - the shift pipeline that owns
    hours is a different system, and this endpoint records a location verdict rather than a
    shift.
    """
    logs_before = db_scalar("SELECT COUNT(*) FROM attendance_logs")
    sessions_before = db_scalar("SELECT COUNT(*) FROM active_sessions")
    assert _punch(client, **INSIDE).status_code == 200
    assert db_scalar("SELECT COUNT(*) FROM attendance_logs") == logs_before
    assert db_scalar("SELECT COUNT(*) FROM active_sessions") == sessions_before
    assert _punches() == 1


def test_the_tables_are_what_the_requirements_name(app_module):
    """The two schemas, column by column - the deliverable's own contract."""
    conn = sqlite3.connect(str(harness.DB_PATH))
    try:
        fence_columns = {
            row[1]: row[2].upper()
            for row in conn.execute("PRAGMA table_info(geofence_settings)")
        }
        punch_columns = {
            row[1]: row[2].upper()
            for row in conn.execute("PRAGMA table_info(attendance_punches)")
        }
    finally:
        conn.close()

    assert set(fence_columns) >= {"id", "latitude", "longitude", "radius_meters", "updated_at"}
    assert fence_columns["latitude"] == "REAL"
    assert fence_columns["longitude"] == "REAL"
    assert fence_columns["radius_meters"] == "REAL"
    assert "TIMESTAMP" in fence_columns["updated_at"] or "DATETIME" in fence_columns["updated_at"]

    assert set(punch_columns) >= {
        "id", "user_id", "latitude", "longitude", "distance_meters",
        "accuracy_meters", "status", "timestamp",
    }
    assert punch_columns["user_id"] == "TEXT"
    for column in ("latitude", "longitude", "distance_meters", "accuracy_meters"):
        assert punch_columns[column] == "REAL", column
    assert punch_columns["status"] == "TEXT"


def test_the_migration_is_registered_and_the_version_is_current(app_module):
    import migrations

    assert migrations.SCHEMA_VERSION == 33
    names = {version: name for version, name, _ in migrations.MIGRATIONS}
    assert names[32] == "visual_geofence", "the migration this suite covers is still registered"
    assert names[33] == "worker_moallem_assignments"
    assert migrations.current_version(sqlite3.connect(str(harness.DB_PATH))) == 33


def test_the_punch_path_still_reads_the_construction_site_fence(client):
    """The *other* geofence - the site's - is untouched by this feature.

    ``main.site_at`` judges a punch against ``construction_sites``. This asserts the two
    coexist: the deployment fence moved by this endpoint does not move the site the shift
    pipeline resolves.
    """
    import main

    with main.db() as conn:
        site = main.site_at(conn, 30.05, 31.23)
    assert site is not None, "the seeded site's fence stopped matching its own coordinates"
    assert site["site_name"] == "Downtown Tower A"


# ---------------------------------------------------------------------------
# 7. the page and its policy
# ---------------------------------------------------------------------------
def test_the_editor_page_is_served_with_a_map_policy(client):
    """The one document allowed to load a map library, and it says so for itself."""
    response = client.get("/geofence")
    assert response.status_code == 200, response.text
    assert "text/html" in response.headers["content-type"]
    policy = response.headers["content-security-policy"]
    assert policy == __import__("netguard").CSP_HTML_MAPS
    assert "https://unpkg.com" in policy
    assert "https://tile.openstreetmap.org" in policy


def test_every_other_page_keeps_the_strict_policy(client, link_paths):
    """The exception is one page wide: the worker's punch screen must not inherit it."""
    import netguard

    for path in ("/", link_paths["quick"], link_paths["enroll"]):
        response = client.get(path)
        assert response.headers["content-security-policy"] == netguard.CSP_HTML, path
        assert "unpkg.com" not in response.headers["content-security-policy"], path


def test_the_page_names_its_own_assets_with_content_hashes(client):
    """The editor is served like every other document: revalidated, with stamped assets."""
    response = client.get("/geofence")
    assert response.headers["cache-control"] == "no-cache, must-revalidate"
    assert 'geofence.js?v=' in response.text
    assert 'geofence.css?v=' in response.text


def test_the_editor_script_and_stylesheet_are_served(client):
    for asset, kind in (("geofence.js", "javascript"), ("geofence.css", "css")):
        response = client.get(f"/{asset}")
        assert response.status_code == 200, asset
        assert kind in response.headers["content-type"], asset


def test_the_page_route_is_declared_a_page_not_a_public_endpoint():
    """``PAGE_ROUTES`` rather than ``PUBLIC_ROUTES``: a document, not an endpoint."""
    import readiness

    assert "GET /geofence" in readiness.PAGE_ROUTES
    assert "GET /geofence" not in readiness.PUBLIC_ROUTES
