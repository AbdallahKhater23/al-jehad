"""The geofence editor, driven in the frontend VM: state, geometry, and the two engines.

WHY THIS EXISTS
---------------
The editor's whole design claim is that the *map is not the state*. Two engines render one
``Geofence`` object, and an interaction writes the state and then asks the renderer to draw
it - which is what makes a drag survive an engine switch, what makes the radius slider an
overlay redraw rather than a map reload, and what makes the page still usable when the CDN
that serves Leaflet never answers.

None of that is visible from the backend, and none of it is visible from a page that merely
loads: it is visible from *driving* the page. So this suite runs ``geofence.js`` in the same
Node VM the console's suites use, installs a stand-in ``L`` (Leaflet's interface, recorded
rather than rendered), and asserts the three things that would each be a bug in production:

1. a gesture moves the state, and the engine is brought to the state rather than the other
   way round;
2. ``setRadius`` calls the engine's own geometry API and nothing else - no ``setView``, no
   re-mount, no tile request;
3. the payloads the two save paths send are exactly the shapes the two endpoints document,
   including the legacy one's raw numeric *strings*.

HOW A SUITE DRIVES A STANDALONE PAGE
------------------------------------
The harness's body runs in **Node's** scope, not the page's: ``boot()`` builds the VM, loads
the shipped files into it, and hands back an ``env`` whose ``evaluate`` runs an expression
*inside* that context. So everything about the page - its state, its DOM, its stand-in map -
is read and driven through ``env.evaluate``, and the assertions below are on the object that
one such call returns.

The files are loaded by ``boot()`` *before* a suite's code runs, which means ``geofence.js``
has already evaluated by the time this suite can install a map library: its own boot finds
none, appends a CDN tag that never answers, and leaves the panel painted and the map empty.
That is not a limitation of the harness - it is the page's documented failure mode (see the
``admin_geofence.html`` header), and this suite asserts it *first* and then activates the
engine by hand, which is what the page's own ``boot()`` does when the CDN does answer.
"""

from __future__ import annotations

import pytest

import frontend_vm
from frontend_vm import NODE

pytestmark = [
    pytest.mark.regression,
    pytest.mark.skipif(NODE is None, reason="node is not installed"),
]

#: Only the editor's own file: it is standalone by design (the console's bundle is not loaded
#: here), and loading the console's files would let a bug hide behind them.
SCRIPTS = ["geofence.js"]


BODY = r"""
const results = {};
const env = boot();

// The two answers the page asks for: the fence it reads on boot, and the fence the *saves*
// stored. They are different on purpose - the second is what the server decided (a clamped
// radius, the coordinates it actually wrote), and the "active fence" line has to report that
// rather than what was typed.
env.setResponder((url, init) => {
    const posting = init && String(init.method || 'GET').toUpperCase() === 'POST';
    if (String(url).indexOf('/geofence') >= 0 && !posting) {
        return { status: 200, body: {
            geofence: {
                latitude: 30.05, longitude: 31.23, radius_meters: 100,
                updated_at: '2026-10-03 09:00:00', generation: 1, source: 'database'
            },
            slider: { min_meters: 20, max_meters: 500, step_meters: 5 }
        } };
    }
    return { status: 200, body: {
        geofence: {
            latitude: 30.2, longitude: 31.4, radius_meters: 250,
            updated_at: '2026-10-03 10:00:00', generation: 2, source: 'modern'
        },
        slider: { min_meters: 20, max_meters: 500, step_meters: 5 }
    } };
});

// --- 1. the CDN never answered, and the page is still a page ----------------------
results.beforeTheLibrary = env.evaluate(`(function () {
    const editor = window.GeofenceEditor;
    return {
        loaded: !!editor,
        engine: editor.Engines.active === null ? null : editor.Engines.active.name,
        // The panel painted itself from the state it boots with, with no map in sight.
        hud: document.getElementById('geo-lat').textContent,
        // And the tag the loader appended is the CDN's, recorded rather than discarded.
        asked: document.head.__children.map((node) => node.src)
    };
})()`);

// --- 2. the library arrives: the engine is activated over the state already painted ----
results.page = await env.evaluate(`(async () => {
    const out = {};
    const editor = window.GeofenceEditor;

    // A stand-in for Leaflet's interface: it records what the page asks of it, so "the circle
    // was resized" is an observation about the *engine* rather than about the page's own
    // variable. It lives inside this context because that is where the page will look for it.
    window.L = (function makeLeaflet() {
        const calls = { tileLayers: 0, setRadius: [], setLatLng: [], setView: [], markers: 0, circles: 0 };
        function layer() { return { addTo() { return this; }, on() { return this; } }; }
        const L = {
            calls,
            map(element, options) {
                calls.mapOptions = options;
                return {
                    __kind: 'map',
                    setView(center, zoom) { calls.setView.push({ center, zoom }); return this; },
                    getZoom() { return options.zoom; },
                    on(event, handler) { (this.__handlers = this.__handlers || {})[event] = handler; return this; },
                    fire(event, payload) { if (this.__handlers && this.__handlers[event]) this.__handlers[event](payload); }
                };
            },
            tileLayer(url) {
                calls.tileLayers += 1;
                calls.tileUrl = url;
                return layer();
            },
            // An array in, an object out - the conversion Leaflet itself does. A stub that
            // handed back the array would make the page's own reads (which are all .lat and
            // .lng) undefined, and every assertion about "the engine was brought to the
            // state" would be comparing undefined with undefined.
            latLng(value) {
                if (Array.isArray(value)) return { lat: value[0], lng: value[1] };
                return { lat: value.lat, lng: value.lng };
            },
            circle(center, options) {
                calls.circles += 1;
                calls.circleOptions = options;
                const self = {
                    __kind: 'circle',
                    __center: L.latLng(center),
                    __radius: options.radius,
                    addTo() { return this; },
                    on(event, handler) { (this.__handlers = this.__handlers || {})[event] = handler; return this; },
                    fire(event) { if (this.__handlers && this.__handlers[event]) this.__handlers[event](); },
                    setRadius(radius) { self.__radius = radius; calls.setRadius.push(radius); return this; },
                    getRadius() { return self.__radius; },
                    setLatLng(position) { self.__center = L.latLng(position); calls.setLatLng.push(position); return this; }
                };
                return self;
            },
            marker(position, options) {
                calls.markers += 1;
                calls.markerOptions = options;
                const self = {
                    __kind: 'marker',
                    __position: L.latLng(position),
                    addTo() { return this; },
                    on(event, handler) { (this.__handlers = this.__handlers || {})[event] = handler; return this; },
                    fire(event) { if (this.__handlers && this.__handlers[event]) this.__handlers[event](); },
                    // Normalised on the way in, like the real one: the page passes an array
                    // to setLatLng, which Leaflet accepts, and a stub that stored the array
                    // would make getLatLng().lat undefined.
                    setLatLng(next) { self.__position = L.latLng(next); calls.setLatLng.push(next); return this; },
                    getLatLng() { return self.__position; }
                };
                return self;
            }
        };
        return L;
    })();

    // What the page's own boot does when the CDN answers: ensureLeaflet resolves on the
    // global being present, so this is the same code path, one turn later.
    await editor.Engines.useLeaflet();
    await editor.Pages.load();

    out.booted = true;
    out.engine = editor.Engines.active.name;
    out.tileLayers = window.L.calls.tileLayers;
    out.tileUrl = window.L.calls.tileUrl;
    out.markerDraggable = window.L.calls.markerOptions.draggable;
    // The fence the server reported is what the panel shows, and what the slider was set to.
    out.loadedState = { lat: editor.Geofence.lat, lng: editor.Geofence.lng, radius: editor.Geofence.radius };
    out.loadedSlider = document.getElementById('geo-radius-slider').value;

    // --- a click on the canvas moves the state, and the engine follows -------------
    const before = { lat: editor.Geofence.lat, lng: editor.Geofence.lng };
    const map = editor.Engines.active.map;
    map.fire('click', { latlng: { lat: 30.10, lng: 31.30 } });
    out.afterClick = { lat: editor.Geofence.lat, lng: editor.Geofence.lng };
    out.movedByClick = (editor.Geofence.lat !== before.lat) && (editor.Geofence.lng !== before.lng);
    // The state is the truth: the engine was brought to it.
    out.circleFollowedClick =
        editor.Engines.active.circle.__center.lat === editor.Geofence.lat &&
        editor.Engines.active.marker.__position.lat === editor.Geofence.lat;

    // --- the slider resizes the circle through the engine's geometry API only ------
    const radiusCallsBefore = window.L.calls.setRadius.length;
    const viewCallsBefore = window.L.calls.setView.length;
    const markersBefore = window.L.calls.markers;
    const circlesBefore = window.L.calls.circles;
    const tileLayersBefore = window.L.calls.tileLayers;
    const slider = document.getElementById('geo-radius-slider');
    slider.value = '250';
    // The handler the page bound, not a paraphrase of it: input is the event a dragging
    // handle fires, and the stub element records what addEventListener was given.
    slider.__handlers.input();
    out.sliderState = editor.Geofence.radius;
    out.radiusCalls = window.L.calls.setRadius.length - radiusCallsBefore;
    out.lastRadiusCall = window.L.calls.setRadius[window.L.calls.setRadius.length - 1];
    // Nothing else moved: no recentre, no new marker, no new circle, no tile request.
    out.viewCalls = window.L.calls.setView.length - viewCallsBefore;
    out.newMarkers = window.L.calls.markers - markersBefore;
    out.newCircles = window.L.calls.circles - circlesBefore;
    out.newTileLayers = window.L.calls.tileLayers - tileLayersBefore;
    out.hudRadius = document.getElementById('geo-radius').textContent;

    // --- a pin drag reports the edit, and the settle syncs the engine --------------
    const marker = editor.Engines.active.marker;
    marker.__position = { lat: 30.20, lng: 31.40 };
    marker.fire('drag');
    out.duringDrag = { lat: editor.Geofence.lat, lng: editor.Geofence.lng };
    marker.fire('dragend');
    out.afterDragEnd = { lat: editor.Geofence.lat, lng: editor.Geofence.lng };

    // --- the two payloads ---------------------------------------------------------
    out.modern = editor.modernPayload();
    out.legacy = editor.legacyPayload();

    // --- saving: the modern path, then the legacy one ------------------------------
    // What each save *posted* is read off the harness's own request log, which lives in
    // Node's scope - the page cannot see it, and reading it there rather than intercepting
    // fetch inside the page is what keeps this a test of the request that really went out
    // rather than of a stub the page was handed.
    await editor.Pages.save(false);
    await editor.Pages.save(true);

    // The server's answer is what the "active fence" line reports, not what was typed.
    out.savedState = {
        lat: editor.Geofence.saved.lat,
        lng: editor.Geofence.saved.lng,
        radius: editor.Geofence.saved.radius
    };
    out.savedSummary = document.getElementById('geo-fence-summary').textContent;

    // --- the engine switch keeps the state ----------------------------------------
    const stateBeforeSwitch = { lat: editor.Geofence.lat, lng: editor.Geofence.lng, radius: editor.Geofence.radius };
    // Google is absent and there is no key: the switch must refuse without losing the edit.
    try {
        await editor.Engines.useGoogle('');
        out.googleRefused = false;
    } catch (err) {
        out.googleRefused = true;
        out.googleReason = String(err && err.message);
    }
    out.stateAfterSwitchAttempt = { lat: editor.Geofence.lat, lng: editor.Geofence.lng, radius: editor.Geofence.radius };
    out.statePreserved = JSON.stringify(stateBeforeSwitch) === JSON.stringify(out.stateAfterSwitchAttempt);

    // ...and Leaflet can be re-activated over the same state.
    await editor.Engines.useLeaflet();
    out.afterReactivation = { lat: editor.Geofence.lat, lng: editor.Geofence.lng, radius: editor.Geofence.radius };
    out.reactivatedEngine = editor.Engines.active.name;

    return out;
})()`);

// The two POSTs the saves made, by path: the modern payload and the legacy one. Filtered by
// method *and* path so an unrelated request (the boot read) cannot be mistaken for a save.
const posts = env.requests.filter((entry) => String(entry.method || 'GET').toUpperCase() === 'POST');
const byPath = (suffix) => posts.filter((entry) => String(entry.url).endsWith(suffix)).pop();
const modernPost = byPath('/api/v1/geofence');
const legacyPost = byPath('/api/v1/legacy/set-geofence');
results.savedModernUrl = modernPost ? modernPost.url : null;
results.savedLegacyUrl = legacyPost ? legacyPost.url : null;
results.savedModern = modernPost ? JSON.parse(modernPost.body) : null;
results.savedLegacy = legacyPost ? JSON.parse(legacyPost.body) : null;
"""


@pytest.fixture(scope="module")
def results() -> dict:
    return frontend_vm.run(BODY, scripts=SCRIPTS)


@pytest.fixture(scope="module")
def page(results) -> dict:
    return results["page"]


def test_the_page_survives_a_cdn_that_never_answers(results):
    """The failure mode the page documents, asserted before the library is installed.

    ``geofence.js`` evaluates before this suite can give it a map library, so its own boot
    runs the branch a blocked CDN produces: the panel paints from the state it boots with,
    the tag is appended, and nothing throws. A page that only worked with Leaflet present
    would be a page that is blank on the one deployment that blocks unpkg.
    """
    before = results["beforeTheLibrary"]
    assert before["loaded"] is True, "the editor script did not evaluate"
    assert before["engine"] is None, (
        "an engine was mounted with no map library present, so this test is not exercising "
        "the branch it claims to"
    )
    assert before["hud"], "the panel did not paint its own state"
    assert any("unpkg.com" in url for url in before["asked"]), (
        f"the page did not ask the CDN for Leaflet: {before['asked']}"
    )


def test_the_page_boots_on_leaflet_with_osm_tiles(page):
    assert page["booted"] is True
    assert page["engine"] == "leaflet"
    assert page["tileLayers"] == 1, "the page did not mount exactly one tile layer"
    assert "tile.openstreetmap.org" in page["tileUrl"], page["tileUrl"]
    assert page["markerDraggable"] is True, "the pin has to be draggable to fine-tune a fence"


def test_the_panel_reports_the_fence_the_server_stored(page):
    """The read path, and the one thing it must not do: show a number nobody stored."""
    assert page["loadedState"] == {"lat": 30.05, "lng": 31.23, "radius": 100}
    assert page["loadedSlider"] == "100"


def test_a_click_moves_the_state_and_the_engine_follows(page):
    """The direction of the dependency: the state decides, the renderer obeys."""
    assert page["movedByClick"] is True
    assert page["afterClick"] == {"lat": 30.10, "lng": 31.30}
    assert page["circleFollowedClick"] is True, (
        "the circle or the pin was left where it was, so the map is now describing a fence "
        "the state does not hold"
    )


def test_the_slider_resizes_the_circle_and_nothing_else(page):
    """The interaction the requirement names: an overlay redraw, not a map reload."""
    assert page["sliderState"] == 250
    assert page["radiusCalls"] == 1, "the radius was not pushed to the engine"
    assert page["lastRadiusCall"] == 250
    assert page["viewCalls"] == 0, "the slider recentred the map - that is a tile re-render"
    assert page["newMarkers"] == 0
    assert page["newCircles"] == 0, "the circle was re-created instead of resized"
    assert page["newTileLayers"] == 0, "the slider asked for tiles again"
    assert page["hudRadius"] == "250 m", page["hudRadius"]


def test_a_pin_drag_updates_the_state_live(page):
    assert page["duringDrag"] == {"lat": 30.20, "lng": 31.40}
    assert page["afterDragEnd"] == {"lat": 30.20, "lng": 31.40}


def test_the_modern_payload_is_the_documented_shape(page):
    modern = page["modern"]
    assert set(modern) == {"latitude", "longitude", "radius_meters"}
    # A number, not a string: the endpoint validates floats, and a payload that sent "250"
    # would be a 422 from the schema rather than a saved fence.
    assert isinstance(modern["radius_meters"], (int, float))
    assert modern["latitude"] == 30.20 and modern["longitude"] == 31.40


def test_the_legacy_payload_is_raw_numeric_strings(page):
    """The old microservice reads ``lat``/``lng``/``rad``, and it reads them as strings."""
    legacy = page["legacy"]
    assert set(legacy) == {"lat", "lng", "rad"}
    for key in ("lat", "lng", "rad"):
        assert isinstance(legacy[key], str), f"{key} is {type(legacy[key]).__name__}, not a string"
        float(legacy[key])  # and each one parses as a number


def test_the_two_save_paths_go_to_their_own_endpoints(results, page):
    assert results["savedModernUrl"], "the modern save made no request"
    assert results["savedModernUrl"].endswith("/api/v1/geofence")
    assert results["savedLegacyUrl"], "the legacy save made no request"
    assert results["savedLegacyUrl"].endswith("/api/v1/legacy/set-geofence")
    assert set(results["savedModern"]) == {"latitude", "longitude", "radius_meters"}
    assert set(results["savedLegacy"]) == {"lat", "lng", "rad"}
    assert isinstance(results["savedLegacy"]["rad"], str)


def test_the_saved_state_is_the_servers_answer_not_what_was_typed(page):
    """The "active fence" line has to report what is stored, or the HUD lies after a clamp."""
    assert page["savedState"] == {"lat": 30.2, "lng": 31.4, "radius": 250}
    assert "30.2" in page["savedSummary"] and "250" in page["savedSummary"], page["savedSummary"]


def test_switching_engines_never_loses_an_edit(page):
    """The decoupling, stated as a property: the state survives every engine change."""
    assert page["googleRefused"] is True
    assert "key" in page["googleReason"].lower(), page["googleReason"]
    assert page["statePreserved"] is True, (
        "an engine switch moved the fence; the map is deciding the state"
    )
    assert page["reactivatedEngine"] == "leaflet"
    assert page["afterReactivation"] == page["stateAfterSwitchAttempt"]
