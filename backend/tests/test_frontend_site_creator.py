"""The site-creation page: adding one site, and then another, without reloading.

WHY THIS EXISTS
---------------
``/sites/new`` is the console's visual way in - paste a Maps link or click the map, name the
place, choose the radius, save - and its whole value is that an administrator can do it twice
in a row for two buildings without leaving the tab. That is exactly what it could not do.

``Pages.reset()`` faded the pin and the circle to ``opacity: 0`` after a successful save, and
``Map.place()`` *reused* those same two overlays for the next site (it creates a layer only
when it has none). So everything after the first save was drawn invisible, on a map that still
answered every click and every keystroke: the page looked locked, and the only way out was a
reload. Three more pieces of state leaked with it - the view stayed wherever site A was,
``Number("")`` turned a cleared coordinate box into latitude 0, and a second ``mount`` threw
inside Leaflet because the container was already initialised (which is what made "navigate
away and come back" the documented workaround).

So this suite drives the real ``add_site.js`` against a stubbed DOM and a stubbed Leaflet and
asks the questions the bug is made of: is the form empty again, is the map back in its default
view, is the second site's pin *on the map*, and does a refused link leave the page usable?

It then asks the questions the *next* version of the page is made of, because a form that adds
nine sites has to answer three things without the administrator hunting for them: did that paste
work (the parse HUD), which hours are these (the cross-midnight switch and its sentence), and
where does the next site start (the caret, in the link box, after a save). The category is a
radio group rather than a select, so it is driven the way a radio is picked.

Node is optional; without it this suite skips rather than fails.
"""

from __future__ import annotations

import pytest

import frontend_vm
from frontend_vm import NODE

pytestmark = [
    pytest.mark.regression,
    pytest.mark.skipif(NODE is None, reason="node is not installed"),
]

#: The page's own two files, and nothing else: this page is standalone by design (it does not
#: load the console's bundle), and loading it would let a bug in the page hide behind it.
SCRIPTS = ["api-config.js", "add_site.js"]


BODY = r"""
const results = {};
const env = boot();

// Every request the page made, in order, read from Node's side of the boundary: the page
// cannot see this log, so what it asserts is the request that really went out.
const posted = [];

env.setResponder((url, init) => {
    const target = String(url);
    const method = String((init && init.method) || 'GET').toUpperCase();
    if (target.indexOf('/admin/site_categories') >= 0) {
        return { status: 200, body: [
            { category_id: 1, name: 'Warehouse', site_count: 2 },
            { category_id: 2, name: 'Factory', site_count: 0 }
        ] };
    }
    if (target.indexOf('/resolve-maps-link') >= 0) {
        if (resolveRefusal) {
            return { status: 422, body: { detail: 'That link has no coordinates in it.' } };
        }
        return { status: 200, body: { latitude: 29.351234, longitude: 47.984712, source: 'redirect' } };
    }
    if (target.indexOf('/sites') >= 0 && method === 'POST') {
        const sent = JSON.parse(String((init && init.body) || '{}'));
        posted.push(sent);
        return { status: 200, body: {
            status: 'success',
            site: Object.assign({ site_id: posted.length }, sent),
            message: 'Site created.',
            cached_sites: posted.length
        } };
    }
    return { status: 404, body: { detail: 'no such route' } };
});

let resolveRefusal = false;

// The console stores the session in ``localStorage``; this page reads that copy, so a suite
// that wants a signed-in page has to write it there rather than into a boot option.
env.evaluate(`localStorage.setItem('session', JSON.stringify({ user: {
    token: 'test-token', id: '5000', name: 'Head Admin', role: 'head_admin'
} }))`);

// The picker asks the server at boot, and the page booted before this suite's responder
// was in place - so the row filled from the harness's default answer, not from these
// categories. Asking again is the same call the boot made, now that the answer is the
// one under test. (The request was made either way: ``reads`` below is about the call.)
results.categoriesLoaded = await env.evaluate("window.SiteCreator.Pages.loadCategories()");

// --- the library arrives ----------------------------------------------------------
// A stand-in for Leaflet's interface that records what the page asks of it. It lives inside
// this context because that is where the page looks for it, and ``Map.ensure`` resolves on the
// global being present - the same code path the CDN's load event drives in a browser.
results.library = await env.evaluate(`(async () => {
    // The tag the page appended at boot, before this stub existed. It asks the CDN once; the
    // count is taken again at the end to show that mounting did not ask a second time - a
    // second Leaflet over an initialised container is the throw this page was reported with.
    const askedBefore = document.head.__children.length;
    const calls = { maps: 0, markers: 0, circles: 0, removed: 0, destroyed: 0, views: [], tileLayers: 0 };
    window.__leafletCalls = calls;
    function layer(kind, position, options) {
        const self = {
            __kind: kind,
            __position: { lat: position[0], lng: position[1] },
            __radius: options && options.radius,
            __options: Object.assign({}, options || {}),
            __opacity: 1,
            __attached: false,
            addTo(map) { self.__attached = true; map.__layers.push(self); return self; },
            on(event, handler) { (self.__handlers = self.__handlers || {})[event] = handler; return self; },
            fire(event) { if (self.__handlers && self.__handlers[event]) self.__handlers[event](); },
            getLatLng() { return self.__position; },
            setLatLng(next) {
                self.__position = Array.isArray(next) ? { lat: next[0], lng: next[1] } : next;
                return self;
            },
            setRadius(radius) { self.__radius = radius; return self; },
            setStyle(style) { self.__options = Object.assign({}, self.__options, style); return self; },
            setOpacity(value) { self.__opacity = value; return self; }
        };
        return self;
    }
    window.L = {
        calls,
        map(element, options) {
            calls.maps += 1;
            calls.lastMapOptions = options;
            return {
                __kind: 'map',
                __layers: [],
                __view: null,
                setView(center, zoom, opts) {
                    calls.views.push({ center, zoom, animate: !!(opts && opts.animate) });
                    this.__view = center;
                    return this;
                },
                on(event, handler) { (this.__handlers = this.__handlers || {})[event] = handler; return this; },
                fire(event, payload) { if (this.__handlers && this.__handlers[event]) this.__handlers[event](payload); },
                removeLayer(target) {
                    const at = this.__layers.indexOf(target);
                    if (at >= 0) this.__layers.splice(at, 1);
                    target.__attached = false;
                    calls.removed += 1;
                    return this;
                },
                remove() { calls.destroyed += 1; this.__layers.length = 0; return this; }
            };
        },
        tileLayer() { calls.tileLayers += 1; return { addTo() { return this; } }; },
        circle(position, options) { calls.circles += 1; return layer('circle', position, options); },
        marker(position, options) { calls.markers += 1; return layer('marker', position, options); }
    };
    await window.SiteCreator.Map.ensure();
    window.SiteCreator.Map.mount();
    return {
        ready: window.SiteCreator.Map.ready,
        askedBefore,
        askedAfter: document.head.__children.length,
        asked: document.head.__children.map((node) => node.src)
    };
})()`);

// --- 1. site A: a pin, a payload, and a form left ready for the next one -----------
await env.evaluate(`window.SiteCreator.Pages.setCenter(30.100000, 31.400000, 'typed')`);
results.afterFirstPin = JSON.parse(await env.evaluate(`JSON.stringify({
    attached: window.SiteCreator.Map.map.__layers.length,
    markers: window.__leafletCalls.markers,
    circles: window.__leafletCalls.circles
})`));

env.evaluate(`document.getElementById('site-name').value = 'Tower A'`);
// The pills are a radio group, so the category is chosen the way a browser chooses one.
env.evaluate(`window.SiteCreator.Category.select('1')`);
// The footer's switch, as the markup has it: on - the page stays a place to add the next
// site. (A stubbed DOM does not read the markup's ``checked`` attribute, so the suite says
// it out loud; the browser suite asserts the attribute itself.)
env.evaluate(`document.getElementById('site-mode').checked = true`);
env.evaluate(`document.getElementById('site-window-start').value = '07:00'`);
env.evaluate(`document.getElementById('site-window-end').value = '15:30'`);
env.evaluate(`document.getElementById('site-window-timezone').value = 'Asia/Kuwait'`);
results.firstSave = await env.evaluate(`window.SiteCreator.Pages.save()`);

// What the report calls "the form is ready for Site B": every box, and the map's own state.
results.afterFirstSave = JSON.parse(await env.evaluate(`JSON.stringify({
    name: document.getElementById('site-name').value,
    mapsUrl: document.getElementById('site-maps-url').value,
    lat: document.getElementById('site-lat').value,
    lng: document.getElementById('site-lng').value,
    radius: document.getElementById('site-radius').value,
    category: window.SiteCreator.Category.value(),
    pillsChecked: window.SiteCreator.Category.pills.filter((pill) => pill.input.checked).length,
    start: document.getElementById('site-window-start').value,
    end: document.getElementById('site-window-end').value,
    zone: document.getElementById('site-window-timezone').value,
    draft: { lat: window.SiteCreator.Draft.lat, lng: window.SiteCreator.Draft.lng, radius: window.SiteCreator.Draft.radius },
    attached: window.SiteCreator.Map.map.__layers.length,
    removed: window.__leafletCalls.removed,
    view: window.__leafletCalls.views[window.__leafletCalls.views.length - 1],
    tab: window.SiteCreator.Pages.tab,
    saveLabel: document.getElementById('site-save').textContent,
    saveDisabled: document.getElementById('site-save').disabled,
    focus: window.SiteCreator.Pages.lastFocus,
    overnight: document.getElementById('site-overnight').checked,
    summary: document.getElementById('site-shift-summary').textContent,
    hud: document.getElementById('site-link-hud').className,
    toasts: document.getElementById('geo-toasts').__children.length,
    toastText: (document.getElementById('geo-toasts').__children.slice(-1)[0].__children || [])
        .map((node) => node.textContent).join('')
})`));

// --- 2. site B, in the same page load: the pin has to be *visible* -----------------
await env.evaluate(`window.SiteCreator.Pages.setCenter(29.950000, 31.750000, 'typed')`);
results.afterSecondPin = JSON.parse(await env.evaluate(`JSON.stringify({
    markers: window.__leafletCalls.markers,
    circles: window.__leafletCalls.circles,
    attached: window.SiteCreator.Map.map.__layers.length,
    invisible: window.SiteCreator.Map.map.__layers.filter((item) => item.__opacity === 0).length,
    markerOpacity: window.SiteCreator.Map.marker.__opacity,
    circleFillOpacity: window.SiteCreator.Map.circle.__options.fillOpacity,
    circleRadius: window.SiteCreator.Map.circle.__radius,
    drawnAt: window.SiteCreator.Map.marker.__position
})`));

env.evaluate(`document.getElementById('site-name').value = 'Tower B'`);
results.secondSave = await env.evaluate(`window.SiteCreator.Pages.save()`);
results.afterSecondSave = JSON.parse(await env.evaluate(`JSON.stringify({
    attached: window.SiteCreator.Map.map.__layers.length,
    draftLat: window.SiteCreator.Draft.lat,
    name: document.getElementById('site-name').value
})`));

// --- 3. a cleared coordinate box is not the equator -------------------------------
await env.evaluate(`window.SiteCreator.Pages.setCenter(30.500000, 31.500000, 'typed')`);
env.evaluate(`document.getElementById('site-lat').value = ''`);
await env.evaluate(`window.SiteCreator.Pages.applyTyped()`);
results.clearedBox = JSON.parse(await env.evaluate(`JSON.stringify({
    lat: window.SiteCreator.Draft.lat,
    lng: window.SiteCreator.Draft.lng
})`));

// --- 4. a refused link: said inline, and the page still usable ---------------------
results.beforeRefusal = JSON.parse(await env.evaluate(`JSON.stringify({
    lat: window.SiteCreator.Draft.lat, lng: window.SiteCreator.Draft.lng
})`));
resolveRefusal = true;
env.evaluate(`document.getElementById('site-maps-url').value = 'https://maps.app.goo.gl/not-a-place'`);
results.refused = await env.evaluate(`window.SiteCreator.Pages.resolve()`);
results.afterRefusal = JSON.parse(await env.evaluate(`JSON.stringify({
    alert: document.getElementById('site-resolve-error').textContent,
    buttonDisabled: document.getElementById('site-resolve').disabled,
    lat: window.SiteCreator.Draft.lat,
    lng: window.SiteCreator.Draft.lng,
    urlKept: document.getElementById('site-maps-url').value
})`));

// --- 5. a shared shortlink is followed on the server, and the alert clears ----------
resolveRefusal = false;
env.evaluate(`document.getElementById('site-maps-url').value = 'https://maps.app.goo.gl/somewhere-real'`);
results.shortlink = await env.evaluate(`window.SiteCreator.Pages.resolve()`);
results.afterShortlink = JSON.parse(await env.evaluate(`JSON.stringify({
    lat: window.SiteCreator.Draft.lat,
    lng: window.SiteCreator.Draft.lng,
    alert: document.getElementById('site-resolve-error').textContent,
    tab: window.SiteCreator.Pages.tab,
    buttonDisabled: document.getElementById('site-resolve').disabled
})`));

// --- 6. the shapes the requirement names, read in the browser ----------------------
results.shapes = JSON.parse(await env.evaluate(`JSON.stringify({
    appShortened: window.SiteCreator.SiteLink.isShortened('https://maps.app.goo.gl/abc123'),
    googlShortened: window.SiteCreator.SiteLink.isShortened('https://goo.gl/maps/abc123'),
    appGooShortened: window.SiteCreator.SiteLink.isShortened('https://app.goo.gl/abc123'),
    fullLinkNotShortened: window.SiteCreator.SiteLink.isShortened('https://www.google.com/maps/@29.1,31.2,17z'),
    addressBar: window.SiteCreator.SiteLink.coordinatesFrom('https://www.google.com/maps/@29.351234,47.984712,17z'),
    dataBlob: window.SiteCreator.SiteLink.coordinatesFrom('https://www.google.com/maps/place/x/data=!3d29.351234!4d47.984712'),
    pathShape: window.SiteCreator.SiteLink.coordinatesFrom('https://www.google.com/maps/search/29.351234,47.984712'),
    queryPair: window.SiteCreator.SiteLink.coordinatesFrom('https://www.google.com/maps?q=29.351234,47.984712'),
    locPrefixed: window.SiteCreator.SiteLink.coordinatesFrom('https://www.google.com/maps?q=loc:29.351234,47.984712'),
    rawPair: window.SiteCreator.SiteLink.coordinatesFrom('29.351234, 47.984712'),
    rawPairNoSpace: window.SiteCreator.SiteLink.coordinatesFrom('29.351234,47.984712'),
    notALink: window.SiteCreator.SiteLink.coordinatesFrom('ask somebody at the gate'),
    mockReading: window.SiteCreator.SiteLink.coordinatesFrom('0,0')
})`));

// The two-site story is frozen here: the sections below add their own sites, and a suite that
// asked "what did the page post?" would otherwise grow with them.
results.posted = posted.slice();
results.allPosted = posted;

// --- 8. the parse HUD ---------------------------------------------------------------------
// Driven through the handlers the page bound, not by calling the helpers they call: the point
// is that typing in the box is what makes the HUD answer.
env.evaluate("document.getElementById('site-maps-url').value = 'https://www.google.com/maps/@29.351234,47.984712,17z'");
env.evaluate("document.getElementById('site-maps-url').__handlers.input()");
results.hudValid = JSON.parse(await env.evaluate(`JSON.stringify({
    className: document.getElementById('site-link-hud').className,
    text: document.getElementById('site-link-hud-text').textContent
})`));

env.evaluate("document.getElementById('site-maps-url').value = 'https://maps.app.goo.gl/somewhere-real'");
env.evaluate("document.getElementById('site-maps-url').__handlers.input()");
results.hudShortened = JSON.parse(await env.evaluate(`JSON.stringify({
    className: document.getElementById('site-link-hud').className,
    text: document.getElementById('site-link-hud-text').textContent
})`));

// A half-typed box is left alone: no check, and no complaint either. The complaint is the blur.
env.evaluate("document.getElementById('site-maps-url').value = 'ask somebody at the gate'");
env.evaluate("document.getElementById('site-maps-url').__handlers.input()");
results.hudQuiet = JSON.parse(await env.evaluate(`JSON.stringify({
    className: document.getElementById('site-link-hud').className,
    text: document.getElementById('site-link-hud-text').textContent
})`));
env.evaluate("document.getElementById('site-maps-url').__handlers.blur()");
results.hudJunkBlur = JSON.parse(await env.evaluate(`JSON.stringify({
    alert: document.getElementById('site-resolve-error').textContent,
    lat: window.SiteCreator.Draft.lat
})`));

// --- 9. the category pills ----------------------------------------------------------------
results.pills = JSON.parse(await env.evaluate(`JSON.stringify({
    values: window.SiteCreator.Category.pills.map((pill) => pill.value),
    selected: window.SiteCreator.Category.value(),
    checked: window.SiteCreator.Category.pills.filter((pill) => pill.input.checked).length
})`));
// Picked through the handler a browser fires when a radio is chosen, so the wiring is what is
// under test rather than the setter beside it.
env.evaluate("window.SiteCreator.Category.pills.find((pill) => pill.value === '2').input.__handlers.change()");
results.pillPicked = JSON.parse(await env.evaluate(`JSON.stringify({
    selected: window.SiteCreator.Category.value(),
    marked: window.SiteCreator.Category.pills
        .filter((pill) => pill.label.classList.contains('is-checked'))
        .map((pill) => pill.value)
})`));

// --- 10. the shift window -----------------------------------------------------------------
env.evaluate("document.getElementById('site-name').value = 'Night Tower'");
env.evaluate("window.SiteCreator.Pages.setCenter(30.100000, 31.400000, 'typed')");
env.evaluate("document.getElementById('site-window-start').value = '21:30'");
env.evaluate("document.getElementById('site-window-end').value = '05:30'");
env.evaluate("document.getElementById('site-window-start').__handlers.change()");
results.crossing = JSON.parse(await env.evaluate(`JSON.stringify({
    overnight: document.getElementById('site-overnight').checked,
    summary: document.getElementById('site-shift-summary').textContent
})`));

// Switching it off is the administrator saying "these hours are not an overnight shift" - and
// that has to stop the save rather than be quietly reinterpreted as one.
env.evaluate("document.getElementById('site-overnight').checked = false");
env.evaluate("document.getElementById('site-overnight').__handlers.change()");
const postsBeforeWindowRefusal = posted.length;
results.refusedWindow = await env.evaluate("window.SiteCreator.Pages.save()");
results.afterRefusedWindow = JSON.parse(await env.evaluate(`JSON.stringify({
    error: document.getElementById('site-window-error').textContent,
    focus: window.SiteCreator.Pages.lastFocus
})`));
results.windowRefusalPosts = posted.length - postsBeforeWindowRefusal;

// Neither shape: the same minute at both ends.
env.evaluate("document.getElementById('site-window-end').value = '21:30'");
results.equalTimes = await env.evaluate("window.SiteCreator.Shift.validate()");

// And the ordinary shape, described the way a person says it.
env.evaluate("document.getElementById('site-window-end').value = '15:30'");
env.evaluate("document.getElementById('site-window-start').value = '07:00'");
env.evaluate("document.getElementById('site-window-start').__handlers.change()");
results.daytime = JSON.parse(await env.evaluate(`JSON.stringify({
    overnight: document.getElementById('site-overnight').checked,
    summary: document.getElementById('site-shift-summary').textContent,
    problem: window.SiteCreator.Shift.validate()
})`));

// --- 11. an empty name is refused under the field -----------------------------------------
env.evaluate("document.getElementById('site-name').value = ''");
const postsBeforeNameRefusal = posted.length;
results.refusedName = await env.evaluate("window.SiteCreator.Pages.save()");
results.afterRefusedName = JSON.parse(await env.evaluate(`JSON.stringify({
    error: document.getElementById('site-name-error').textContent,
    focus: window.SiteCreator.Pages.lastFocus
})`));
results.nameRefusalPosts = posted.length - postsBeforeNameRefusal;

// --- 12. what a save leaves behind: a toast, an empty form, and the caret -------------------
env.evaluate("document.getElementById('site-name').value = 'Night Tower'");
results.keepGoingSave = await env.evaluate("window.SiteCreator.Pages.save()");
results.afterKeepGoing = JSON.parse(await env.evaluate(`JSON.stringify({
    focus: window.SiteCreator.Pages.lastFocus,
    name: document.getElementById('site-name').value,
    url: document.getElementById('site-maps-url').value,
    lat: document.getElementById('site-lat').value,
    category: window.SiteCreator.Category.value(),
    overnight: document.getElementById('site-overnight').checked,
    summary: document.getElementById('site-shift-summary').textContent,
    hud: document.getElementById('site-link-hud').className,
    attached: window.SiteCreator.Map.map.__layers.length,
    saveDisabled: document.getElementById('site-save').disabled,
    toasts: document.getElementById('geo-toasts').__children.length,
    toastClass: (document.getElementById('geo-toasts').__children.slice(-1)[0] || {}).className,
    toastText: (
        (document.getElementById('geo-toasts').__children.slice(-1)[0] || {}).__children || []
    ).map((node) => node.textContent).join('')
})`));

// The other mode: one site per visit, so the caret goes to the way out instead.
env.evaluate("document.getElementById('site-mode').checked = false");
env.evaluate("document.getElementById('site-mode').__handlers.change()");
env.evaluate("document.getElementById('site-name').value = 'Day Tower'");
env.evaluate("window.SiteCreator.Pages.setCenter(29.950000, 31.750000, 'typed')");
results.oneOffSave = await env.evaluate("window.SiteCreator.Pages.save()");
results.afterOneOff = JSON.parse(await env.evaluate(`JSON.stringify({
    focus: window.SiteCreator.Pages.lastFocus,
    note: document.getElementById('site-mode-note').textContent,
    name: document.getElementById('site-name').value
})`));

// --- 13. the reset button is the same promise, on demand -----------------------------------
env.evaluate("document.getElementById('site-mode').checked = true");
env.evaluate("document.getElementById('site-mode').__handlers.change()");

env.evaluate("window.SiteCreator.Category.select('1')");
env.evaluate("document.getElementById('site-name').value = 'Half typed'");
env.evaluate("document.getElementById('site-maps-url').value = 'https://www.google.com/maps/@29.1,31.2,17z'");
env.evaluate("document.getElementById('site-reset').__handlers.click()");
results.afterResetButton = JSON.parse(await env.evaluate(`JSON.stringify({
    name: document.getElementById('site-name').value,
    url: document.getElementById('site-maps-url').value,
    category: window.SiteCreator.Category.value(),
    focus: window.SiteCreator.Pages.lastFocus,
    draft: window.SiteCreator.Draft.lat,
    tab: window.SiteCreator.Pages.tab
})`));

// --- 14. what the two saves posted, and what the picker asked for -------------------
results.reads = env.requests.map((request) => String(request.url)).filter(
    (url) => url.indexOf('/admin/site_categories') >= 0
).length;
"""


@pytest.fixture(scope="module")
def results():
    return frontend_vm.run(BODY, scripts=SCRIPTS)


def test_the_page_draws_the_map_it_is_given(results):
    library = results["library"]
    assert library["ready"] is True, "the page never mounted a map"
    # One ask, for the CDN, at boot - and no second one from mounting with the library
    # already present.
    assert library["askedBefore"] == 1, library["askedBefore"]
    assert library["askedAfter"] == 1, "mounting asked the CDN for Leaflet a second time"
    assert library["asked"] and "leaflet" in library["asked"][0], library["asked"]


def test_the_first_site_is_created_and_the_form_is_ready_for_the_next(results):
    """The report's first half: one save, then an *empty* form - no reload in between."""
    after = results["afterFirstSave"]
    assert results["firstSave"] is True
    assert after["name"] == "", "the site name survived the save"
    assert after["mapsUrl"] == "" and after["lat"] == "" and after["lng"] == ""
    assert after["category"] == "" and after["start"] == "" and after["end"] == "" and after["zone"] == ""
    assert after["draft"] == {"lat": None, "lng": None, "radius": 100}, after["draft"]
    assert after["tab"] == "link", "the form did not come back to the paste-a-link state"
    assert after["saveDisabled"] is True, "the button offers to save a draft with no location"


def test_the_old_pin_is_taken_off_the_map_and_the_view_returns_to_the_default_box(results):
    """Removed, not faded: a hidden pin under a live map is what read as a lock-up."""
    after = results["afterFirstSave"]
    assert after["attached"] == 0, "the previous site's pin and circle are still on the map"
    assert after["removed"] == 2, after["removed"]
    # ``FALLBACK_CENTER`` - the box the page opens on, so site B is not drawn off-screen.
    assert after["view"]["center"] == [30.05, 31.23], after["view"]
    assert after["view"]["animate"] is False


def test_the_second_site_gets_its_own_visible_pin(results):
    """The regression: the second draft used to reuse site A's hidden overlay."""
    after = results["afterSecondPin"]
    assert after["markers"] == 2 and after["circles"] == 2, after
    assert after["attached"] == 2, "the second site is not on the map"
    assert after["invisible"] == 0, "a layer on the map is still at opacity 0"
    assert after["markerOpacity"] == 1
    # A fresh overlay carries the constructor's own transparency, not the 0 an earlier draft
    # faded it to - and it is the radius the slider is showing, not a stale one.
    assert after["circleFillOpacity"] == 0.22, after["circleFillOpacity"]
    assert after["circleRadius"] == 100, after["circleRadius"]
    assert (after["drawnAt"]["lat"], after["drawnAt"]["lng"]) == (29.95, 31.75)


def test_a_second_save_in_the_same_page_load_works(results):
    """Two sites, one page load, two rows - the state that the report could not reach."""
    assert results["secondSave"] is True
    assert [site["site_name"] for site in results["posted"]] == ["Tower A", "Tower B"]
    after = results["afterSecondSave"]
    assert after["name"] == "" and after["draftLat"] is None
    assert after["attached"] == 0


def test_the_saved_site_carries_its_category_and_its_hours(results):
    """The schema the page writes into: a category, and the hours arrivals are judged by."""
    first, second = results["posted"]
    assert first["category_id"] == 1
    assert first["clock_in_window_start"] == "07:00"
    assert first["clock_in_window_end"] == "15:30"
    assert first["site_timezone"] == "Asia/Kuwait"
    assert first["radius_meters"] == 100
    # Blank means "inherit", so the second site sends NULL rather than an empty string.
    assert second["category_id"] is None
    assert second["clock_in_window_start"] is None
    assert second["clock_in_window_end"] is None
    assert second["site_timezone"] is None


def test_the_category_picker_asks_the_server_once(results):
    assert results["reads"] >= 1, "the picker never asked for the categories"


def test_a_cleared_coordinate_box_does_not_become_latitude_zero(results):
    """``Number("")`` is 0, so a half-retyped box used to move the pin to the equator."""
    assert results["clearedBox"] == {"lat": 30.5, "lng": 31.5}, results["clearedBox"]


def test_a_refused_link_says_why_inline_and_leaves_the_page_usable(results):
    after = results["afterRefusal"]
    assert results["refused"] is False
    assert "no coordinates" in after["alert"], after["alert"]
    assert after["buttonDisabled"] is False, "a refused link left the resolver disabled"
    assert after["urlKept"] == "https://maps.app.goo.gl/not-a-place", (
        "the refused link was wiped out of the box the administrator needs to correct"
    )
    # The draft is untouched by the failure: the failure is about the link, not the page.
    assert (after["lat"], after["lng"]) == (
        results["beforeRefusal"]["lat"], results["beforeRefusal"]["lng"]
    )


def test_a_shared_shortlink_is_followed_and_the_alert_clears(results):
    assert results["shortlink"] is True
    after = results["afterShortlink"]
    assert (after["lat"], after["lng"]) == (29.351234, 47.984712)
    assert after["alert"] == "", "the previous failure's message outlived its cause"
    assert after["buttonDisabled"] is False


def test_every_maps_shape_the_requirement_names_is_read(results):
    shapes = results["shapes"]
    # The three share hosts, and a full link which is *not* one of them.
    assert shapes["appShortened"] is True
    assert shapes["googlShortened"] is True
    assert shapes["appGooShortened"] is True
    assert shapes["fullLinkNotShortened"] is False
    for key in ("addressBar", "dataBlob", "pathShape", "queryPair", "locPrefixed"):
        assert shapes[key] == {"lat": 29.351234, "lng": 47.984712}, (key, shapes[key])
    assert shapes["rawPair"] == {"lat": 29.351234, "lng": 47.984712}
    assert shapes["rawPairNoSpace"] == {"lat": 29.351234, "lng": 47.984712}
    # And the two it must *refuse* rather than guess at.
    assert shapes["notALink"] is None
    assert shapes["mockReading"] is None

# ---------------------------------------------------------------------------
# the page the second half of the overhaul is made of
# ---------------------------------------------------------------------------
def test_the_parse_hud_shows_the_pair_it_read_and_stays_quiet_otherwise(results):
    """Live on the input: a check for what it found, and a button to press for what it cannot."""
    valid = results["hudValid"]
    assert valid["className"] == "geo-link-hud is-ok", valid
    assert "29.351234" in valid["text"] and "47.984712" in valid["text"], valid["text"]

    shortened = results["hudShortened"]
    assert shortened["className"] == "geo-link-hud is-info", shortened
    assert "Find it" in shortened["text"], shortened["text"]

    # A box that is not a link yet gets no mark *and no complaint*: the complaint is the blur.
    quiet = results["hudQuiet"]
    assert quiet["className"] == "geo-link-hud", quiet
    assert quiet["text"] == "", quiet["text"]


def test_a_box_that_is_not_a_link_gets_its_sentence_on_blur(results):
    """The failure is said where the box is, and it does not touch the draft."""
    after = results["hudJunkBlur"]
    assert "does not look like a Maps link" in after["alert"], after["alert"]
    # The pin is still where the last real answer put it: a refused *text* is not a refused page,
    # and typing something unreadable must not move a draft that was already correct.
    assert after["lat"] == 29.351234, after["lat"]


def test_the_category_pills_are_built_from_the_endpoint_and_chosen_like_radios(results):
    pills = results["pills"]
    # "No category" is always first, and it is a value rather than a missing one.
    assert pills["values"][0] == "", pills["values"]
    assert "1" in pills["values"] and "2" in pills["values"], pills["values"]
    assert pills["selected"] == "", pills
    assert pills["checked"] == 1, "the row must show exactly one choice"

    picked = results["pillPicked"]
    assert picked["selected"] == "2", picked
    assert picked["marked"] == ["2"], picked


def test_a_crossing_window_is_said_so_and_the_switch_shows_it(results):
    crossing = results["crossing"]
    assert crossing["overnight"] is True, crossing
    assert "next day" in crossing["summary"], crossing["summary"]
    assert "21:30" in crossing["summary"] and "05:30" in crossing["summary"], crossing["summary"]


def test_a_crossing_window_with_the_switch_off_is_refused_without_posting(results):
    """The switch has teeth: it is the difference between an overnight shift and a typo."""
    assert results["refusedWindow"] is False
    assert results["windowRefusalPosts"] == 0, "a refused window still posted the site"
    after = results["afterRefusedWindow"]
    assert "runs past midnight" in after["error"], after["error"]
    assert after["focus"] == "site-window-end", "the caret did not land on the time it refused"


def test_a_window_with_the_same_minute_at_both_ends_is_refused(results):
    assert "same minute" in results["equalTimes"], results["equalTimes"]


def test_a_daytime_window_is_described_in_hours_and_minutes(results):
    daytime = results["daytime"]
    assert daytime["overnight"] is False, daytime
    assert daytime["summary"] == "Opens 07:00, closes 15:30 - 8 h 30 m.", daytime["summary"]
    assert daytime["problem"] == "", daytime


def test_an_empty_name_is_refused_under_the_field_and_gets_the_caret(results):
    assert results["refusedName"] is False
    assert results["nameRefusalPosts"] == 0, "a nameless site was posted"
    after = results["afterRefusedName"]
    assert "Give the site a name" in after["error"], after["error"]
    assert after["focus"] == "site-name", after["focus"]


def test_a_save_leaves_a_toast_an_empty_form_and_the_caret_in_the_link_box(results):
    """The "create & add another" promise, in one assertion each."""
    assert results["keepGoingSave"] is True
    after = results["afterKeepGoing"]
    assert after["name"] == "" and after["url"] == "" and after["lat"] == ""
    assert after["category"] == "", "the next site inherited the last one's category"
    assert after["overnight"] is False and after["summary"] == "", after
    assert after["hud"] == "geo-link-hud", after["hud"]
    assert after["attached"] == 0, "the previous site's pin is still on the map"
    assert after["saveDisabled"] is True, "the button offers to save a draft with no location"
    # The caret is the whole point: the next site starts with a link in the clipboard.
    assert after["focus"] == "site-maps-url", after["focus"]
    # And the toast says which site, so a mistake is caught while the name is still to hand.
    assert after["toasts"] >= 1, after["toasts"]
    assert "is-ok" in after["toastClass"], after["toastClass"]
    assert "Night Tower" in after["toastText"], after["toastText"]
    assert [site["site_name"] for site in results["allPosted"]][2] == "Night Tower"


def test_the_other_mode_hands_the_caret_to_the_console_link(results):
    assert results["oneOffSave"] is True
    after = results["afterOneOff"]
    assert after["focus"] == "site-console-link", after["focus"]
    assert after["name"] == "", "the form did not clear in the one-site mode"
    assert "one site per visit" in after["note"], after["note"]
    assert [site["site_name"] for site in results["allPosted"]][3] == "Day Tower"


def test_the_reset_button_clears_what_the_save_would_have(results):
    after = results["afterResetButton"]
    assert after["name"] == "" and after["url"] == ""
    assert after["category"] == "", after["category"]
    assert after["draft"] is None, after["draft"]
    assert after["tab"] == "link", after["tab"]
    assert after["focus"] == "site-maps-url", after["focus"]
