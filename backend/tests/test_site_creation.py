"""Creating a site: the Maps-link resolver, the cached fence, and the punch that reads it.

WHAT THIS SUITE PARS
--------------------
Four claims, each of which is worth more than the code that makes it:

1. **the link parser is a pure function, and it reads the shapes Google actually produces.**
   ``@lat,lng`` from the address bar, ``?q=`` from a share sheet, ``!3d!4d`` from the encoded
   place blob, ``/search/`` from the mobile app, and a bare ``lat,lng`` pasted with no URL
   around it. Pinned against real URLs rather than against a paraphrase of them, because the
   whole point of the parser is that an administrator's clipboard is not a documented format.

2. **the resolver will only ever fetch a Maps host.** An outbound fetch of a URL a caller
   chose is the textbook server-side request forgery, and this one is worse than most
   because the URL is *meant* to be followed somewhere else. So the allowlist is asserted
   from both sides: every Maps host is accepted, and ``maps.google.com.evil.test`` - the
   shape a suffix check gets wrong - is refused.

3. **the create endpoint writes the row and the cache together**, so the punch that follows
   is measured against the boundary that was just drawn rather than against the one this
   worker loaded at startup. That is the whole reason the endpoint exists instead of the
   console posting to ``/admin/sites/add``.

4. **a punch that names a site decides from memory, and only from memory.** The test removes
   the database from under a running application and shows the verdict still comes back
   correct - which is the requirement's "0 DB queries" stated as something a test can fail.

It also pins the two refusals the requirement asks for: a link that is dead or carries no
coordinates, and a coordinate pair outside WGS84 (including the ``(0,0)`` mock reading).
"""

from __future__ import annotations

import sqlite3

import pytest

import geofence
import harness
import sites
from harness import ADMIN, WORKER, bearer, db_scalar

pytestmark = pytest.mark.regression

RESOLVE = "/api/v1/resolve-maps-link"
CREATE = "/api/v1/sites"
CACHE = "/api/v1/sites"
PUNCH = "/api/v1/attendance/punch"

#: A real pair - Amman, Jordan - used as "somewhere this deployment has no site".
FAR = (24.7136, 46.6753)


# ---------------------------------------------------------------------------
# 1. the parser, which is pure
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "link, expected",
    [
        # The browser's own address bar, with the zoom suffix.
        ("https://www.google.com/maps/@29.351234,47.984712,17z", (29.351234, 47.984712)),
        # A share sheet's query string.
        ("https://www.google.com/maps?q=29.351234,47.984712", (29.351234, 47.984712)),
        # The same with a space after the comma, which is what a share produces.
        ("https://maps.google.com/?q=29.351234, 47.984712", (29.351234, 47.984712)),
        # The encoded place blob.
        (
            "https://www.google.com/maps/place/Tower/data=!4m5!3m4!1s0x0:0x0!8m2!3d29.351234!4d47.984712",
            (29.351234, 47.984712),
        ),
        # The mobile app's path shape.
        ("https://www.google.com/maps/search/29.351234,47.984712", (29.351234, 47.984712)),
        # A bare pair, pasted with no URL around it at all.
        ("29.351234,47.984712", (29.351234, 47.984712)),
        ("  29.351234 , 47.984712  ", (29.351234, 47.984712)),
        # Southern and western hemispheres: the sign is part of the answer.
        ("https://www.google.com/maps/@-33.868820,151.209290,15z", (-33.86882, 151.20929)),
        ("https://www.google.com/maps?q=-1.2921,36.8219", (-1.2921, 36.8219)),
    ],
)
def test_the_parser_reads_every_shape_a_maps_url_comes_in(link, expected):
    assert sites.coordinates_from_text(link) == pytest.approx(expected)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Tower B - phase 2",
        "https://www.google.com/maps",          # a maps URL with no place in it
    ],
)
def test_the_parser_answers_nothing_rather_than_guessing(text):
    """``None`` is the answer that makes the caller ask the server, so it has to be reachable.

    The last case is the one worth stating: a coordinate pair on somebody else's host is not
    this parser's business. It is still *readable* - the sweep that finds a pair in a page
    body has no host to check, and the allowlist is what decides whether that page may be
    fetched at all - so the parse returns the numbers and ``host_is_allowed`` refuses the
    link. Both halves are asserted below; this one only pins that a Maps URL with no place in
    it answers nothing.
    """
    assert sites.coordinates_from_text(text) is None


def test_a_pair_inside_a_url_is_still_a_pair():
    """The loose sweep is last, so it only ever answers when nothing more specific did."""
    found = sites.coordinates_from_text("https://www.google.com/maps/place/x/29.351234,47.984712,17z")
    assert found == pytest.approx((29.351234, 47.984712))


def test_the_mock_location_reading_is_not_a_coordinate():
    """``(0,0)`` is refused by the API, so the parser must not offer it as an answer."""
    assert sites.coordinates_from_text("0,0") is None
    assert sites.coordinates_from_text("https://www.google.com/maps?q=0,0") is None


def test_the_shortened_hosts_are_recognised_as_shortened():
    assert sites.is_shortened("https://maps.app.goo.gl/abc123") is True
    assert sites.is_shortened("https://goo.gl/maps/abc123") is True
    # A full URL is *not* short: its coordinates are in the text and no request is needed.
    assert sites.is_shortened("https://www.google.com/maps/@29.35,47.98,17z") is False


# ---------------------------------------------------------------------------
# 2. the allowlist, which is the SSRF control
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "link",
    [
        "https://maps.app.goo.gl/abc123",
        "https://goo.gl/maps/abc123",
        "https://www.google.com/maps/@29.35,47.98,17z",
        "https://maps.google.com/?q=29.35,47.98",
        "https://maps.google.co.uk/maps?q=51.5,-0.12",
        "https://lh3.googleusercontent.com/some/place",
    ],
)
def test_a_maps_host_may_be_fetched(link):
    assert sites.host_is_allowed(link) is True


@pytest.mark.parametrize(
    "link",
    [
        "http://169.254.169.254/latest/meta-data/",     # cloud metadata
        "http://localhost:8000/api/v1/status",
        "http://127.0.0.1/",
        "file:///etc/passwd",
        "gopher://example.test/",
        # The shape a suffix check gets wrong: this host *ends with* google.com as a string.
        "https://maps.google.com.evil.test/maps?q=1,2",
        "https://evil.test/maps.app.goo.gl",
        "https://google.com.evil.test/",
    ],
)
def test_a_host_that_is_not_google_maps_may_not_be_fetched(link):
    assert sites.host_is_allowed(link) is False


def test_a_link_that_is_not_a_url_is_refused_with_a_sentence(client):
    response = client.post(RESOLVE, headers=bearer(ADMIN), json={"url": "Tower B, block 4"})
    assert response.status_code == 400, response.text
    assert "not a Maps link" in response.json()["detail"]


def test_an_internal_host_is_refused_before_any_connection(client):
    """The refusal is a 400 from the allowlist, not a 502 from a transport that tried."""
    response = client.post(
        RESOLVE, headers=bearer(ADMIN), json={"url": "http://169.254.169.254/latest/meta-data/"}
    )
    assert response.status_code == 400, response.text
    assert "Google Maps links" in response.json()["detail"]


def test_no_outbound_call_was_made_by_any_of_those_refusals(outbound):
    """The allowlist's whole job: a refused link never reaches the network."""
    before = len(outbound.calls)
    for link in ("http://169.254.169.254/", "file:///etc/passwd", "Tower B"):
        client = None  # the calls below are direct, so no client is needed
        with pytest.raises(Exception):
            sites.resolve_maps_link(link)
    assert len(outbound.calls) == before, (
        "a refused link opened a connection: the allowlist is checked after the fetch, or "
        "not at all"
    )


def test_a_full_link_with_coordinates_in_it_makes_no_request_at_all(client, outbound):
    """Most pastes cost nothing: the numbers are already in the URL."""
    before = len(outbound.calls)
    response = client.post(
        RESOLVE,
        headers=bearer(ADMIN),
        json={"url": "https://www.google.com/maps/@29.351234,47.984712,17z"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["latitude"] == pytest.approx(29.351234)
    assert body["longitude"] == pytest.approx(47.984712)
    assert body["resolved"] is False, "a link whose coordinates were in it is not 'resolved'"
    assert len(outbound.calls) == before, (
        f"a link that needed no fetch made one: {outbound.urls()[before:]}"
    )


def test_a_shortened_link_is_followed_through_the_guard(client, outbound):
    """The one case the server exists for, driven through the outbound recorder.

    The recorder answers every request with a Twilio-shaped stub, so the *coordinates* cannot
    come from it - what is asserted is that the request went out, to the host the allowlist
    permits, and that the failure it then produces is the readable 422 rather than a 500.
    """
    before = len(outbound.calls)
    response = client.post(RESOLVE, headers=bearer(ADMIN), json={"url": "https://maps.app.goo.gl/abc123"})
    assert len(outbound.calls) == before + 1, "a shortened link was not followed at all"
    assert outbound.urls()[-1] == "https://maps.app.goo.gl/abc123"
    # The stub's body carries no coordinates, so this is the "followed but nothing in it"
    # answer - which is the honest result of a page that is not a Maps page.
    assert response.status_code == 422, response.text
    assert "carries no coordinates" in response.json()["detail"]


def test_the_resolver_is_administrator_only(client):
    """It makes this deployment fetch a URL somebody typed, so it takes a session."""
    assert client.post(RESOLVE, json={"url": "https://maps.app.goo.gl/abc"}).status_code in (401, 403)
    assert client.post(
        RESOLVE, headers=bearer(WORKER), json={"url": "https://maps.app.goo.gl/abc"}
    ).status_code == 403


# ---------------------------------------------------------------------------
# 3. creating a site, and the cache it writes through to
# ---------------------------------------------------------------------------
def _create(client, **payload):
    body = {"site_name": "New Villa", "latitude": 29.351234, "longitude": 47.984712, "radius_meters": 120.0}
    body.update(payload)
    return client.post(CREATE, headers=bearer(ADMIN), json=body)


def test_a_created_site_is_in_the_table_and_in_the_cache(client):
    response = _create(client)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["site"]["site_name"] == "New Villa"
    assert body["site"]["radius_meters"] == 120.0
    assert body["site"]["site_id"] > 0

    row = db_scalar("SELECT COUNT(*) FROM construction_sites WHERE site_name = 'New Villa'")
    assert row == 1
    cached = geofence.SITE_CACHE.by_name.get("New Villa")
    assert cached is not None, "the site was written to SQLite but not to the cache the punch reads"
    assert cached.radius_meters == 120.0


def test_the_cache_is_what_the_requirement_names(client, app_module):
    """``app.state.sites_cache`` is published at startup, and the endpoint keeps it true."""
    assert app_module.app.state.sites_cache is geofence.SITE_CACHE
    _create(client, site_name="Cached Villa")
    assert "Cached Villa" in geofence.SITE_CACHE.by_name


def test_the_cached_fences_are_readable_by_an_administrator(client):
    response = client.get(CACHE, headers=bearer(ADMIN))
    assert response.status_code == 200, response.text
    body = response.json()
    names = {site["site_name"] for site in body["sites"]}
    # The fixture's two sites, out of the table and into memory.
    assert {"Downtown Tower A", "New Capital Zone B"} <= names, names
    assert body["count"] == len(body["sites"])


def test_a_worker_cannot_create_a_site_or_read_the_cache(client):
    """A worker's own valid token, and the same two routes: refused, and nothing written."""
    before = int(db_scalar("SELECT COUNT(*) FROM construction_sites") or 0)
    refused = client.post(
        CREATE,
        headers=bearer(WORKER),
        json={
            "site_name": "Not Yours",
            "latitude": 24.7136,
            "longitude": 46.6753,
            "radius_meters": 100.0,
        },
    )
    assert refused.status_code == 403, refused.text
    assert int(db_scalar("SELECT COUNT(*) FROM construction_sites") or 0) == before, (
        "a worker's refusal still created a site"
    )
    assert client.get(CACHE, headers=bearer(WORKER)).status_code == 403


def test_a_duplicate_name_is_refused(client):
    assert _create(client).status_code == 200
    again = _create(client)
    assert again.status_code == 400
    assert "already exists" in again.json()["detail"]


def test_a_location_input_is_accepted_as_an_alternative_to_the_pair(client):
    """The console's older shape: a ``lat,lon`` string, or a Maps URL, parsed server-side."""
    response = _create(
        client,
        site_name="Pasted Villa",
        latitude=None,
        longitude=None,
        location_input="https://www.google.com/maps/@30.100000,31.400000,17z",
    )
    assert response.status_code == 200, response.text
    site = response.json()["site"]
    assert (site["latitude"], site["longitude"]) == pytest.approx((30.1, 31.4))


@pytest.mark.parametrize(
    "payload",
    [
        {"latitude": 0.0, "longitude": 0.0},          # the mock-location reading
        {"latitude": 91.0, "longitude": 31.0},        # out of WGS84
        {"latitude": 30.0, "longitude": 181.0},
        {"radius_meters": 0.0},                       # a fence nobody can be inside
        {"radius_meters": -5.0},
        {"radius_meters": geofence.MAX_RADIUS_METERS + 1.0},
    ],
)
def test_an_unusable_location_or_radius_is_refused(client, payload):
    assert _create(client, **payload).status_code in (400, 422)


def test_a_site_with_no_location_at_all_is_refused(client):
    response = _create(client, latitude=None, longitude=None)
    assert response.status_code == 400
    assert "needs coordinates" in response.json()["detail"]


def test_a_name_that_carries_markup_is_refused(client):
    """A site name is written into ``attendance_logs.site_name``, so it is not free text."""
    response = _create(client, site_name="<script>alert(1)</script>")
    assert response.status_code in (400, 422)


def test_creating_a_site_is_audited(client):
    _create(client, site_name="Audited Villa")
    actions = db_scalar(
        "SELECT COUNT(*) FROM audit_log WHERE action = 'site_create' AND entity_id = 'Audited Villa'"
    )
    assert actions == 1


def test_the_console_route_also_refreshes_the_cache(client):
    """``/admin/sites/add`` writes the same table, so it has to keep the cache true too."""
    response = client.post(
        "/api/v1/admin/sites/add",
        headers=bearer(ADMIN),
        json={"site_name": "Console Villa", "location_input": "29.9,31.9", "radius": 80.0},
    )
    assert response.status_code == 200, response.text
    assert "Console Villa" in geofence.SITE_CACHE.by_name, (
        "a site added through the console's own form is invisible to the punch path until a "
        "restart"
    )


def test_deleting_a_site_forgets_its_fence(client):
    _create(client, site_name="Doomed Villa")
    assert "Doomed Villa" in geofence.SITE_CACHE.by_name
    response = client.post(
        "/api/v1/admin/sites/delete", headers=bearer(ADMIN), data={"site_name": "Doomed Villa"}
    )
    assert response.status_code == 200, response.text
    assert "Doomed Villa" not in geofence.SITE_CACHE.by_name, (
        "a deleted site still answers punches"
    )


# ---------------------------------------------------------------------------
# 4. the punch against a named site, in memory
# ---------------------------------------------------------------------------
def _punch(client, *, site_id=None, site_name=None, latitude=None, longitude=None, user_id=WORKER):
    body = {"user_id": user_id, "latitude": latitude, "longitude": longitude}
    if site_id is not None:
        body["site_id"] = site_id
    if site_name is not None:
        body["site_name"] = site_name
    return client.post(PUNCH, headers=bearer(user_id), json=body)


def test_a_punch_inside_the_named_site_is_approved(client):
    """The seeded site, named by the id the create endpoint would have returned."""
    fence = geofence.SITE_CACHE.by_name["Downtown Tower A"]
    response = _punch(client, site_id=fence.site_id, latitude=30.05, longitude=31.23)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "approved"
    assert body["site_id"] == fence.site_id
    assert body["site_name"] == "Downtown Tower A"
    assert body["radius_meters"] == pytest.approx(fence.radius_meters)
    assert body["distance_meters"] < 1.0


def test_a_punch_outside_the_named_site_is_refused(client):
    """Inside *a* fence is not the question when the punch names a different one."""
    fence = geofence.SITE_CACHE.by_name["Downtown Tower A"]
    response = _punch(client, site_id=fence.site_id, latitude=29.98, longitude=31.75)
    assert response.status_code == 403, response.text
    body = response.json()
    assert body["reason"] == "outside_geofence"
    assert body["distance_meters"] > fence.radius_meters


def test_a_punch_can_name_the_site_by_name_too(client):
    """The mobile client's shape: it has a site list, not rowids."""
    response = _punch(client, site_name="New Capital Zone B", latitude=29.98, longitude=31.75)
    assert response.status_code == 200, response.text
    assert response.json()["site_name"] == "New Capital Zone B"


def test_a_punch_that_names_no_site_keeps_the_old_behaviour(client):
    """The compatibility rule: the deployment fence answers, exactly as before."""
    response = _punch(client, latitude=geofence.DEFAULT_LATITUDE, longitude=geofence.DEFAULT_LONGITUDE)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["site_id"] is None
    assert body["site_name"] is None
    assert body["radius_meters"] == pytest.approx(geofence.DEFAULT_RADIUS_METERS)


def test_a_site_this_deployment_does_not_have_is_a_404(client):
    """Not a silent fall back to the deployment fence: a stale client is told."""
    assert _punch(client, site_id=999_999, latitude=30.05, longitude=31.23).status_code == 404
    assert _punch(client, site_name="Nowhere Villa", latitude=30.05, longitude=31.23).status_code == 404


def test_a_freshly_created_site_can_be_punched_immediately(client):
    """The write-through, end to end: create, then punch against the new fence."""
    created = _create(
        client, site_name="Fresh Villa", latitude=24.7136, longitude=46.6753, radius_meters=60.0
    )
    assert created.status_code == 200, created.text
    site_id = created.json()["site"]["site_id"]

    inside = _punch(client, site_id=site_id, latitude=24.7136, longitude=46.6753)
    assert inside.status_code == 200, inside.text
    assert inside.json()["radius_meters"] == pytest.approx(60.0)

    # 200 m away is outside a 60 m fence.
    outside = _punch(client, site_id=site_id, latitude=24.7154, longitude=46.6753)
    assert outside.status_code == 403, outside.text


def test_the_named_site_decision_needs_no_database_at_all(monkeypatch):
    """The requirement's "0 DB queries", as something a test can fail.

    The database is pointed at a path that does not exist, so any query raises
    ``OperationalError`` - the same trick ``test_geofence`` uses for the deployment fence -
    and then the whole named-site decision is run: resolve the site, measure the fix,
    refuse. It answers correctly, which is only possible if both the fence and the site came
    out of process memory.

    WHY THIS IS NOT AN HTTP REQUEST. A punch *request* does read the database, and that is
    not a defect to be papered over: ``security.get_current_user`` loads the account from
    ``users`` on every request, which is what makes a rotated password or a deactivated
    account visible to every worker immediately. The zero-query claim is about the geofence
    half - the cache, the Haversine and the refusal - so that is the half measured here, and
    the request-level path is covered by the tests around it.
    """
    from pathlib import Path

    fence = geofence.SITE_CACHE.by_name["Downtown Tower A"]
    monkeypatch.setattr(
        "database.DB_PATH", Path("Z:/there-is-no-database-here/times.db"), raising=False
    )

    # The lookup the handler does, with no database in reach.
    site = geofence.SITE_CACHE.resolve(fence.site_id, None)
    assert site is not None and site.site_id == fence.site_id

    # And the decision itself: outside the named fence, answered from memory.
    verdict = geofence.evaluate(51.5074, -0.1278, None, site)
    assert verdict.approved is False
    assert verdict.reason == "outside_geofence"
    assert verdict.distance_meters > 1_000_000
    assert verdict.verification_ms < 1.0, verdict.verification_ms

    # Inside it, the same way - so the approval half of the decision is memory-only too.
    inside = geofence.evaluate(30.05, 31.23, None, site)
    assert inside.approved is True
    assert inside.distance_meters < 1.0


def test_the_site_lookup_is_under_a_millisecond():
    """The lookup itself, without a request around it: two dict gets and a Haversine."""
    import time

    fence = geofence.SITE_CACHE.by_name["Downtown Tower A"]
    started = time.perf_counter()
    for _ in range(1000):
        site = geofence.SITE_CACHE.resolve(fence.site_id, None)
        geofence.evaluate(30.05, 31.23, None, site)
    average_ms = (time.perf_counter() - started) * 1000.0 / 1000.0
    assert average_ms < 1.0, f"the named-site decision averaged {average_ms:.4f} ms"


def test_a_refused_named_punch_writes_nothing(client):
    """The fail-fast rule, for the site path as much as the deployment one."""
    before = int(db_scalar("SELECT COUNT(*) FROM attendance_punches") or 0)
    fence = geofence.SITE_CACHE.by_name["Downtown Tower A"]
    assert _punch(client, site_id=fence.site_id, latitude=51.5074, longitude=-0.1278).status_code == 403
    assert int(db_scalar("SELECT COUNT(*) FROM attendance_punches") or 0) == before


def test_the_punch_still_refuses_another_account(client):
    """Naming a site does not make the body's ``user_id`` trusted."""
    fence = geofence.SITE_CACHE.by_name["Downtown Tower A"]
    response = _punch(
        client, site_id=fence.site_id, latitude=30.05, longitude=31.23, user_id=WORKER
    )
    assert response.status_code == 200
    other = client.post(
        PUNCH,
        headers=bearer(WORKER),
        json={"user_id": ADMIN, "site_id": fence.site_id, "latitude": 30.05, "longitude": 31.23},
    )
    assert other.status_code == 403


# ---------------------------------------------------------------------------
# 5. the page
# ---------------------------------------------------------------------------
def test_the_creation_page_is_served_with_a_map_policy(client):
    response = client.get("/sites/new")
    assert response.status_code == 200, response.text
    assert "text/html" in response.headers["content-type"]
    policy = response.headers["content-security-policy"]
    assert "https://unpkg.com" in policy
    assert "https://tile.openstreetmap.org" in policy


def test_the_creation_page_names_its_own_assets_with_content_hashes(client):
    response = client.get("/sites/new")
    assert response.headers["cache-control"] == "no-cache, must-revalidate"
    assert "/add_site.js?v=" in response.text
    assert "/geofence.css?v=" in response.text


def test_the_creation_pages_assets_are_root_absolute(client):
    """The page is two segments deep, so a bare asset path resolves into the API router.

    ``/sites/new`` makes ``add_site.js`` mean ``/sites/add_site.js``, which the API answers
    with a JSON 404 - and a browser refuses to execute a script whose MIME type is not
    JavaScript, so the page renders and does nothing at all. That is exactly what happened
    the first time this page was loaded in a real browser (``test_pages_boot_in_a_browser``
    caught it), so the rule is pinned here as well as there.
    """
    import re

    body = client.get("/sites/new").text
    relative = [
        url
        for url in re.findall(r'(?:src|href)="([^"]+)"', body)
        if not url.startswith(("/", "#", "//")) and "://" not in url
    ]
    assert relative == [], (
        f"these asset paths are relative and will resolve under /sites/: {relative}"
    )


@pytest.mark.parametrize("asset", ["add_site.js", "geofence.css", "api-config.js"])
def test_the_creation_pages_assets_are_served_from_the_site_root(client, asset):
    response = client.get(f"/{asset}")
    assert response.status_code == 200, asset
    assert response.headers["content-type"] != "application/json", (
        f"/{asset} is answered by the API router, so the browser refuses to execute it"
    )


def test_the_page_route_is_declared_a_page_not_a_public_endpoint():
    import readiness

    assert "GET /sites/new" in readiness.PAGE_ROUTES
    assert "GET /sites/new" not in readiness.PUBLIC_ROUTES


def test_the_worker_punch_screen_keeps_the_strict_policy(client):
    """The exception is two pages wide, and the phone's page is not one of them."""
    import netguard

    assert client.get("/").headers["content-security-policy"] == netguard.CSP_HTML


# ---------------------------------------------------------------------------
# 4. the window fields, which this route accepts and used to write unvalidated
#
# ``POST /sites`` writes the same two columns ``/admin/sites/add`` writes, so it has to apply
# the same rule to them. It used to accept anything: ``25:00`` posted here was stored, and a
# stored window ``shift_windows`` cannot parse degrades to the company hours at the gate.
# ---------------------------------------------------------------------------
def test_the_site_window_is_stored_as_written(client):
    response = _create(
        client,
        site_name="Hourly Villa",
        clock_in_window_start="07:00",
        clock_in_window_end="15:30",
        site_timezone="Asia/Kuwait",
    )
    assert response.status_code == 200, response.text
    assert response.json()["site"]["clock_in_window_start"] == "07:00"
    stored = db_scalar(
        "SELECT clock_in_window_start || '-' || clock_in_window_end || ' ' || site_timezone "
        "FROM construction_sites WHERE site_name = 'Hourly Villa'"
    )
    assert stored == "07:00-15:30 Asia/Kuwait"


@pytest.mark.parametrize("value", ["25:00", "07:60", "7:00", "07:00:00", "banana", "07", "-1:00"])
def test_a_window_that_is_not_hhmm_is_refused(client, value):
    response = _create(client, site_name="Bad Hours Villa", clock_in_window_start=value)
    assert response.status_code == 422, response.text
    assert "HH:MM" in response.text
    assert db_scalar(
        "SELECT COUNT(*) FROM construction_sites WHERE site_name = 'Bad Hours Villa'"
    ) == 0, "a refused window still created the site"


def test_a_blank_window_means_inherit_rather_than_an_error(client):
    """An empty time box is what a form sends for "nobody set this": NULL, not a refusal."""
    response = _create(
        client,
        site_name="Inheriting Villa",
        clock_in_window_start="",
        clock_in_window_end="",
        site_timezone="",
    )
    assert response.status_code == 200, response.text
    assert db_scalar(
        "SELECT COUNT(*) FROM construction_sites WHERE site_name = 'Inheriting Villa' "
        "AND clock_in_window_start IS NULL AND clock_in_window_end IS NULL "
        "AND site_timezone IS NULL"
    ) == 1


def test_a_timezone_this_server_cannot_resolve_is_refused(client):
    response = _create(client, site_name="Zoned Villa", site_timezone="Africa/Cario")
    assert response.status_code == 422, response.text
    assert "timezone" in response.text.lower()
    assert db_scalar("SELECT COUNT(*) FROM construction_sites WHERE site_name = 'Zoned Villa'") == 0


def test_the_two_site_routes_refuse_a_bad_window_the_same_way(client):
    """The console's route and the requirement's route write one table, so they agree."""
    console = client.post(
        "/api/v1/admin/sites/add",
        headers=bearer(ADMIN),
        json={
            "site_name": "Console Bad Hours",
            "location_input": "29.9,31.9",
            "radius": 80.0,
            "clock_in_window_start": "25:00",
        },
    )
    requirement = _create(client, site_name="Requirement Bad Hours", clock_in_window_start="25:00")
    assert console.status_code == requirement.status_code == 422
    assert "HH:MM" in console.text and "HH:MM" in requirement.text


def test_the_shift_time_names_the_requirement_uses_are_accepted(client):
    """``shift_start_time`` / ``shift_end_time``: the same two fields, spelled as the
    requirement spells them. One stored value under two accepted names - the alias is read
    into the columns the shift pipeline already reads, so hours cannot live in two places."""
    response = _create(
        client, site_name="Aliased Villa", shift_start_time="06:15", shift_end_time="14:45"
    )
    assert response.status_code == 200, response.text
    assert response.json()["site"]["clock_in_window_start"] == "06:15"
    assert (
        db_scalar(
            "SELECT clock_in_window_start || '-' || clock_in_window_end "
            "FROM construction_sites WHERE site_name = 'Aliased Villa'"
        )
        == "06:15-14:45"
    )


def test_the_aliased_shift_time_is_validated_the_same_way(client):
    """The alias is a name for the field, not a way around its rule."""
    for payload in ({"shift_start_time": "25:00"}, {"shift_end_time": "7:00"}):
        response = _create(client, site_name="Aliased Bad Hours", **payload)
        assert response.status_code == 422, response.text
        assert "HH:MM" in response.text
    assert (
        db_scalar(
            "SELECT COUNT(*) FROM construction_sites "
            "WHERE site_name = 'Aliased Bad Hours'"
        )
        == 0
    ), "a refused window still created the site"


def test_a_category_that_does_not_exist_is_refused(client):
    response = _create(client, site_name="Homeless Villa", category_id=987654)
    assert response.status_code == 404, response.text
    assert "category" in response.json()["detail"].lower()


def test_a_shared_place_link_with_a_loc_prefix_is_read(client):
    """``?q=loc:29.351234,47.984712`` is the shape the mobile app's share sheet writes."""
    found = sites.coordinates_from_text(
        "https://www.google.com/maps?q=loc:29.351234,47.984712&z=16"
    )
    assert found == pytest.approx((29.351234, 47.984712))
