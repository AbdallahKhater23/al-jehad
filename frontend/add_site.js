/**
 * The visual site-creation page.
 *
 * THE SHAPE OF THIS FILE, AND WHY
 * -------------------------------
 *   1. ``SiteLink`` - reading coordinates out of a Google Maps URL. Pure functions, no DOM,
 *      no network: the same shapes the server's resolver reads, so a link the browser can
 *      answer costs no request at all.
 *   2. ``Draft``    - the state. ``{lat, lng, radius}``. No map may write to it; every edit
 *      goes through ``Pages.setCenter`` / ``Pages.setRadius``, which update the state and
 *      *then* ask the renderer to draw. That is what makes a click, a drag and a typed
 *      number all end in the same place, and what keeps the form usable when the CDN that
 *      serves Leaflet never answers.
 *   3. ``Map``      - one renderer: Leaflet on OpenStreetMap raster tiles, no API key. A
 *      draggable marker, a semi-transparent circle bound to it, and a click on the canvas
 *      that moves both.
 *   4. ``Pages``    - the form: the two tabs, the resolver, the slider, the save.
 *
 * The radius slider is the interaction worth reading closely: it calls ``circle.setRadius``,
 * which recomputes the overlay's path in the SVG layer Leaflet already has. It never
 * re-creates the map, never recentres and never asks the tile server for anything, so
 * dragging it across its whole range costs one repaint per frame and no network.
 *
 * No build step, no framework: the same plain-script style the rest of ``frontend/`` uses.
 * ``api-config.js`` is loaded before this file, so the API base is resolved by the console's
 * own rule (a ``localStorage.apiBaseURL`` override, otherwise the page's own origin) rather
 * than by a second copy of it.
 */
(function (global) {
    "use strict";

    var API_BASE = null;
    var SESSION_KEY = "session";

    // ---------------------------------------------------------------- state
    /** The site being created. Nothing renders this; the map and the form read it. */
    var Draft = {
        lat: null,
        lng: null,
        radius: 100,
        //: Set by the resolver, for the "found on the map" line.
        source: null
    };

    var SLIDER = { min: 20, max: 500, step: 5 };
    var DEFAULT_VIEW = { zoom: 16 };
    //: Where the map sits before anything is picked. Only a starting view: the pin is not
    //: placed until a coordinate arrives, so "nothing chosen yet" is a real state and the
    //: form refuses to save from it rather than posting a fence at a guess.
    var FALLBACK_CENTER = { lat: 30.05, lng: 31.23 };

    // ---------------------------------------------------------------- helpers
    function round(value, places) {
        var factor = Math.pow(10, places);
        return Math.round(value * factor) / factor;
    }

    function clamp(value, low, high) {
        return Math.min(high, Math.max(low, value));
    }

    function isFiniteNumber(value) {
        return typeof value === "number" && isFinite(value);
    }

    function plausible(lat, lng) {
        if (!isFiniteNumber(lat) || !isFiniteNumber(lng)) return false;
        if (lat < -90 || lat > 90 || lng < -180 || lng > 180) return false;
        // (0,0) is the canonical mock-location reading and is refused by the API too, so it
        // is refused here rather than posted and rejected.
        return !(lat === 0 && lng === 0);
    }

    function setText(id, value) {
        var node = document.getElementById(id);
        if (node) node.textContent = String(value);
    }

    /** The API base, through the console's own resolver when it is loaded. */
    function apiBase() {
        if (API_BASE) return API_BASE;
        if (typeof global.resolveAPIBase === "function") {
            API_BASE = global.resolveAPIBase(global.location);
            return API_BASE;
        }
        // ``api-config.js`` is loaded by the page, so this is the fallback for a page opened
        // in a context that did not run it - not the primary rule.
        API_BASE = (global.location && global.location.protocol === "file:")
            ? "https://al-jehad1.abdallahtamet281.workers.dev/api/v1"
            : String((global.location && global.location.origin) || "") + "/api/v1";
        return API_BASE;
    }

    /** The console's stored session, or ``null``. Never invented. */
    function session() {
        var raw = null;
        try { raw = localStorage.getItem(SESSION_KEY); } catch (err) { raw = null; }
        if (!raw) return null;
        try {
            var stored = JSON.parse(raw);
            return stored && stored.user ? stored.user : null;
        } catch (err) {
            return null;
        }
    }

    function request(path, options) {
        options = options || {};
        var headers = { Accept: "application/json" };
        var user = session();
        if (user && user.token) headers.Authorization = "Bearer " + user.token;
        var body = options.body;
        if (body !== undefined) {
            headers["Content-Type"] = "application/json";
            body = JSON.stringify(body);
        }
        return fetch(apiBase() + path, {
            method: options.method || "GET",
            headers: headers,
            body: body
        }).then(function (response) {
            return response.json().catch(function () { return {}; }).then(function (payload) {
                return { status: response.status, ok: response.ok, body: payload };
            });
        });
    }

    function describeError(body) {
        if (!body) return "no detail";
        if (typeof body.detail === "string") return body.detail;
        if (Array.isArray(body.detail) && body.detail.length) {
            return body.detail.map(function (item) {
                return (item.loc ? item.loc.join(".") + ": " : "") + (item.msg || "");
            }).join("; ");
        }
        return JSON.stringify(body);
    }

    // ---------------------------------------------------------------- toasts
    var Toast = {
        show: function (message, kind) {
            var root = document.getElementById("geo-toasts");
            if (!root) return;
            var node = document.createElement("div");
            node.className = "geo-toast" + (kind ? " is-" + kind : "");
            // ``textContent``: every string here came off the wire or out of an error, and
            // ``innerHTML`` is a parser.
            node.textContent = String(message);
            root.appendChild(node);
            setTimeout(function () { node.classList.add("is-leaving"); }, 4200);
            setTimeout(function () { if (node.parentNode) node.parentNode.removeChild(node); }, 4800);
        }
    };

    // ---------------------------------------------------------------- the link
    /**
     * Reading a Google Maps URL. The same four shapes the server's resolver reads, in the
     * same order, so a link one side can answer is a link the other side can answer - and
     * the browser answering it is a round trip this page does not make.
     */
    var SiteLink = {
        //: ``@29.351234,47.984712,17z`` - the address bar.
        AT: /@(-?\d{1,3}\.\d+),(-?\d{1,3}\.\d+)/,
        //: ``!3d29.351234!4d47.984712`` - the encoded place blob.
        DATA: /!3d(-?\d{1,3}\.\d+)!4d(-?\d{1,3}\.\d+)/,
        //: ``/search/29.351234,47.984712`` - the mobile app.
        PATH: /\/(?:search|place|dir|ll|loc:)\/(-?\d{1,3}\.\d+),(-?\d{1,3}\.\d+)/,
        //: ``29.351234,47.984712``, with or without a space after the comma.
        PAIR: /^\s*(-?\d{1,3}\.\d+)\s*,\s*(-?\d{1,3}\.\d+)\s*$/,
        //: A loose sweep, used last so a URL carrying both answers with the ``@`` pair.
        LOOSE: /(-?\d{1,3}\.\d{4,}),\s*(-?\d{1,3}\.\d{4,})/,

        /** ``{lat, lng}`` from any of the shapes, or ``null``. */
        coordinatesFrom: function (text) {
            var raw = String(text || "");
            if (!raw) return null;

            var pair = raw.match(SiteLink.PAIR);
            if (pair && plausible(parseFloat(pair[1]), parseFloat(pair[2]))) {
                return { lat: parseFloat(pair[1]), lng: parseFloat(pair[2]) };
            }

            // The query string, which is where a share sheet puts it.
            var query = "";
            var mark = raw.indexOf("?");
            if (mark >= 0) query = raw.slice(mark + 1).split("#")[0];
            if (query) {
                var params = query.split("&");
                for (var index = 0; index < params.length; index += 1) {
                    var pieces = params[index].split("=");
                    var key = decodeURIComponent(pieces[0] || "").toLowerCase();
                    if (["q", "query", "ll", "center", "daddr", "destination", "sll"].indexOf(key) < 0) continue;
                    var value = decodeURIComponent((pieces.slice(1).join("=") || "").replace(/\+/g, " "));
                    var found = value.match(SiteLink.PAIR);
                    if (found && plausible(parseFloat(found[1]), parseFloat(found[2]))) {
                        return { lat: parseFloat(found[1]), lng: parseFloat(found[2]) };
                    }
                }
            }

            var patterns = [SiteLink.DATA, SiteLink.AT, SiteLink.PATH, SiteLink.LOOSE];
            for (var at = 0; at < patterns.length; at += 1) {
                var match = raw.match(patterns[at]);
                if (match && plausible(parseFloat(match[1]), parseFloat(match[2]))) {
                    return { lat: parseFloat(match[1]), lng: parseFloat(match[2]) };
                }
            }
            return null;
        },

        /** A shortened share link: no coordinates in it, so the server has to follow it. */
        isShortened: function (text) {
            return /^https?:\/\/(maps\.app\.goo\.gl|goo\.gl)\//i.test(String(text || "").trim());
        }
    };

    // ---------------------------------------------------------------- the map
    var LEAFLET_JS = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js";

    var Map = {
        map: null,
        marker: null,
        circle: null,
        ready: false,

        /** Load Leaflet once, or resolve if it is already here. */
        ensure: function () {
            if (global.L && global.L.map) return Promise.resolve();
            if (this.loading) return this.loading;
            this.loading = new Promise(function (resolve, reject) {
                var script = document.createElement("script");
                script.src = LEAFLET_JS;
                script.async = true;
                script.addEventListener("load", function () { resolve(); });
                script.addEventListener("error", function () {
                    reject(new Error("could not load " + LEAFLET_JS));
                });
                document.head.appendChild(script);
            });
            return this.loading;
        },

        mount: function () {
            var host = document.getElementById("site-map");
            if (!host || !global.L) return;
            var center = Draft.lat === null ? FALLBACK_CENTER : { lat: Draft.lat, lng: Draft.lng };
            this.map = global.L.map(host, {
                center: [center.lat, center.lng],
                zoom: DEFAULT_VIEW.zoom,
                zoomControl: true,
                attributionControl: true
            });
            global.L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
                maxZoom: 19,
                attribution: "&copy; OpenStreetMap contributors"
            }).addTo(this.map);

            // The three gestures. Each reports what the person did; none of them writes the
            // state - the page does, and then calls ``draw`` back. That round trip is what
            // keeps the pin, the circle and the two fields from ever disagreeing.
            var self = this;
            this.map.on("click", function (event) {
                Pages.setCenter(event.latlng.lat, event.latlng.lng, "map");
            });
            this.ready = true;
            // A pin that exists only once there is a coordinate: an empty map is a question,
            // and a pin at a default position is an answer nobody gave.
            if (Draft.lat !== null) this.place(Draft.lat, Draft.lng, Draft.radius);
        },

        /** Put the pin and the circle on the map, creating them the first time. */
        place: function (lat, lng, radius) {
            if (!this.ready) return;
            if (!this.circle) {
                this.circle = global.L.circle([lat, lng], {
                    radius: radius,
                    color: "#2563eb",
                    weight: 2,
                    fillColor: "#3b82f6",
                    fillOpacity: 0.22
                }).addTo(this.map);
            } else {
                this.circle.setLatLng([lat, lng]);
                this.circle.setRadius(radius);
            }
            if (!this.marker) {
                var self = this;
                this.marker = global.L.marker([lat, lng], { draggable: true }).addTo(this.map);
                this.marker.on("drag", function () {
                    var position = self.marker.getLatLng();
                    // Overlay only while the pointer is down: the circle follows the pin, and
                    // the state is written on the settle so the fields do not fight the drag.
                    if (self.circle) self.circle.setLatLng(position);
                });
                this.marker.on("dragend", function () {
                    var position = self.marker.getLatLng();
                    Pages.setCenter(position.lat, position.lng, "pin");
                });
            } else {
                this.marker.setLatLng([lat, lng]);
            }
        },

        /** Redraw the pin and circle from the state. Called after every edit. */
        draw: function () {
            if (!this.ready || Draft.lat === null) return;
            this.place(Draft.lat, Draft.lng, Draft.radius);
        },

        /** The overlay resize the requirement names: geometry only, no tile work. */
        setRadius: function (radius) {
            if (this.ready && this.circle) this.circle.setRadius(radius);
        },

        /** Bring the view to a place the resolver found. A *view* change, so it pans. */
        look: function (lat, lng) {
            if (!this.ready) return;
            this.map.setView([lat, lng], DEFAULT_VIEW.zoom, { animate: false });
        }
    };

    // ---------------------------------------------------------------- the page
    var Pages = {
        tab: "link",

        setTab: function (name) {
            this.tab = name === "manual" ? "manual" : "link";
            var linkTab = document.getElementById("site-tab-link");
            var manualTab = document.getElementById("site-tab-manual");
            var linkPanel = document.getElementById("site-panel-link");
            var manualPanel = document.getElementById("site-panel-manual");
            var isLink = this.tab === "link";
            if (linkTab) {
                linkTab.classList.toggle("is-active", isLink);
                linkTab.setAttribute("aria-selected", String(isLink));
            }
            if (manualTab) {
                manualTab.classList.toggle("is-active", !isLink);
                manualTab.setAttribute("aria-selected", String(!isLink));
            }
            if (linkPanel) linkPanel.hidden = !isLink;
            if (manualPanel) manualPanel.hidden = isLink;
        },

        /**
         * Move the draft to a place, and bring everything else to it.
         *
         * The one entry point for *every* way a location arrives - a click, a drag, a typed
         * number, a resolved link - so the four cannot drift: the state is written first and
         * the renderer is asked to draw it, never the other way round.
         */
        setCenter: function (lat, lng, source) {
            if (!plausible(lat, lng)) {
                Toast.show("That is not a usable coordinate pair (0,0 is a mock reading).", "error");
                return false;
            }
            Draft.lat = clamp(round(lat, 6), -90, 90);
            Draft.lng = clamp(round(lng, 6), -180, 180);
            Draft.source = source || Draft.source;
            Map.draw();
            this.paint();
            return true;
        },

        setRadius: function (radius) {
            if (!isFiniteNumber(radius)) return;
            Draft.radius = clamp(round(radius, 1), SLIDER.min, SLIDER.max);
            Map.setRadius(Draft.radius);
            this.paint();
        },

        /** Write the state into the fields and the HUD. Never the other way round. */
        paint: function () {
            var has = Draft.lat !== null;
            var latText = has ? round(Draft.lat, 6).toFixed(6) : "";
            var lngText = has ? round(Draft.lng, 6).toFixed(6) : "";
            var radius = round(Draft.radius, 1);

            var latInput = document.getElementById("site-lat");
            var lngInput = document.getElementById("site-lng");
            var slider = document.getElementById("site-radius");
            // Only write an input the person is not typing in: overwriting a half-typed
            // number is how a field becomes unusable.
            if (latInput && document.activeElement !== latInput) latInput.value = latText;
            if (lngInput && document.activeElement !== lngInput) lngInput.value = lngText;
            if (slider && document.activeElement !== slider) slider.value = String(radius);

            setText("site-radius-out", radius + " m");
            setText("site-hud-lat", has ? latText : "—");
            setText("site-hud-lng", has ? lngText : "—");
            setText("site-hud-radius", radius + " m");
            // The area is the figure that tells an administrator whether the fence is a
            // building or a block: 100 m reads as a number, 31,416 m² reads as a field.
            setText(
                "site-hud-area",
                Math.round(Math.PI * radius * radius).toLocaleString() + " m²"
            );

            var save = document.getElementById("site-save");
            if (save) {
                save.textContent = has ? "Create the site" : "Pick a location first";
                save.disabled = !has;
            }
        },

        /** Take a pasted link: the browser's own parse first, the server only if it must. */
        resolve: function () {
            var field = document.getElementById("site-maps-url");
            var text = field && field.value !== undefined ? String(field.value).trim() : "";
            var box = document.getElementById("site-resolve-result");
            if (!text) {
                Toast.show("Paste a Google Maps link, or the two numbers.", "error");
                return Promise.resolve(false);
            }

            var local = SiteLink.coordinatesFrom(text);
            if (local) {
                Pages.setCenter(local.lat, local.lng, "link");
                Map.look(local.lat, local.lng);
                if (box) {
                    box.textContent = "Read in the browser:\n" + JSON.stringify(
                        { latitude: local.lat, longitude: local.lng }, null, 2
                    );
                }
                Toast.show("Pin moved to " + round(local.lat, 6) + ", " + round(local.lng, 6) + ".", "ok");
                Pages.setTab("manual");
                return Promise.resolve(true);
            }

            // No coordinates in the text. A shortened link is the case the server exists for:
            // the numbers only appear after the redirect has been followed.
            var button = document.getElementById("site-resolve");
            if (button) button.disabled = true;
            if (box) box.textContent = "Following the link on the server…";
            return request("/resolve-maps-link", { method: "POST", body: { url: text } })
                .then(function (answer) {
                    if (answer.status === 401 || answer.status === 403) {
                        Toast.show("Sign in to the console first: resolving a link needs your session.", "error");
                        if (box) box.textContent = "Not signed in.";
                        return false;
                    }
                    if (!answer.ok) {
                        var reason = describeError(answer.body);
                        Toast.show("Could not read that link: " + reason, "error");
                        if (box) box.textContent = "HTTP " + answer.status + "\n" + reason;
                        return false;
                    }
                    var latitude = Number(answer.body.latitude);
                    var longitude = Number(answer.body.longitude);
                    Pages.setCenter(latitude, longitude, "link");
                    Map.look(latitude, longitude);
                    if (box) {
                        box.textContent = "Resolved on the server:\n" + JSON.stringify(answer.body, null, 2);
                    }
                    Toast.show("Pin moved to " + round(latitude, 6) + ", " + round(longitude, 6) + ".", "ok");
                    Pages.setTab("manual");
                    return true;
                })
                .catch(function (err) {
                    Toast.show("Could not reach the server: " + (err && err.message ? err.message : err), "error");
                    return false;
                })
                .then(function (settled) {
                    if (button) button.disabled = false;
                    return settled;
                });
        },

        /** Read the typed coordinates out of the fields. */
        applyTyped: function () {
            var lat = Number(document.getElementById("site-lat").value);
            var lng = Number(document.getElementById("site-lng").value);
            if (!isFinite(lat) || !isFinite(lng) || (lat === 0 && lng === 0 && !Draft.lat)) {
                return false;
            }
            return this.setCenter(lat, lng, "typed");
        },

        /** Create the site, and report exactly what the server stored. */
        save: function () {
            var nameField = document.getElementById("site-name");
            var name = nameField && nameField.value !== undefined ? String(nameField.value).trim() : "";
            if (!name) {
                Toast.show("Give the site a name first.", "error");
                if (nameField) nameField.focus();
                return Promise.resolve(false);
            }
            if (Draft.lat === null) {
                Toast.show("Pick a location first: paste a link, type the pair, or click the map.", "error");
                return Promise.resolve(false);
            }
            var button = document.getElementById("site-save");
            if (button) button.disabled = true;
            var payload = {
                site_name: name,
                latitude: round(Draft.lat, 6),
                longitude: round(Draft.lng, 6),
                radius_meters: round(Draft.radius, 1)
            };
            return request("/sites", { method: "POST", body: payload })
                .then(function (answer) {
                    var box = document.getElementById("site-save-result");
                    if (answer.status === 401 || answer.status === 403) {
                        Toast.show("Only an administrator may create a site.", "error");
                        return false;
                    }
                    if (!answer.ok) {
                        var reason = describeError(answer.body);
                        Toast.show("Could not create the site: " + reason, "error");
                        if (box) box.textContent = "HTTP " + answer.status + "\n" + reason;
                        return false;
                    }
                    if (box) box.textContent = JSON.stringify(answer.body, null, 2);
                    Toast.show(
                        "Site created with a " + round(Draft.radius, 1) + " m fence. The next punch is checked against it.",
                        "ok"
                    );
                    setText("site-status", "Created " + name + ". Add another, or go back to the console.");
                    return true;
                })
                .catch(function (err) {
                    Toast.show("Could not reach the server: " + (err && err.message ? err.message : err), "error");
                    return false;
                })
                .then(function (created) {
                    // Only a *refused* save leaves the button live: a created site resets the
                    // form, and the next save starts from a fresh draft.
                    if (created) {
                        Pages.reset();
                    } else if (button) {
                        button.disabled = false;
                    }
                    return created;
                });
        },

        /** Back to an empty form: the button, and what a successful save does. */
        reset: function () {
            Draft.lat = null;
            Draft.lng = null;
            Draft.radius = 100;
            Draft.source = null;
            var name = document.getElementById("site-name");
            if (name) name.value = "";
            var url = document.getElementById("site-maps-url");
            if (url) url.value = "";
            var lat = document.getElementById("site-lat");
            var lng = document.getElementById("site-lng");
            if (lat) lat.value = "";
            if (lng) lng.value = "";
            var box = document.getElementById("site-resolve-result");
            if (box) box.textContent = "";
            var result = document.getElementById("site-save-result");
            if (result) result.textContent = "";
            if (Map.circle && Map.ready) {
                Map.circle.setStyle({ opacity: 0, fillOpacity: 0 });
                if (Map.marker) Map.marker.setOpacity(0);
            }
            Pages.setTab("link");
            Pages.paint();
        }
    };

    // ---------------------------------------------------------------- wiring
    function bind() {
        var linkTab = document.getElementById("site-tab-link");
        if (linkTab) linkTab.addEventListener("click", function () { Pages.setTab("link"); });
        var manualTab = document.getElementById("site-tab-manual");
        if (manualTab) manualTab.addEventListener("click", function () { Pages.setTab("manual"); });

        var resolve = document.getElementById("site-resolve");
        if (resolve) resolve.addEventListener("click", function () { Pages.resolve(); });
        var url = document.getElementById("site-maps-url");
        if (url) {
            // Enter in the link box resolves it: the paste-then-reach-for-the-mouse step is
            // the whole reason this field exists.
            url.addEventListener("keydown", function (event) {
                if (event.key === "Enter") {
                    event.preventDefault();
                    Pages.resolve();
                }
            });
            url.addEventListener("paste", function () {
                // A pasted link is *read*, not sent: the coordinates are in the text often
                // enough that asking the server first would be a round trip for nothing.
                setTimeout(function () {
                    var text = String(url.value || "").trim();
                    var found = SiteLink.coordinatesFrom(text);
                    if (found) {
                        Pages.setCenter(found.lat, found.lng, "link");
                        Map.look(found.lat, found.lng);
                        Toast.show("Found the coordinates in that link.", "ok");
                    } else if (SiteLink.isShortened(text)) {
                        Toast.show("That is a shortened link: press Find it to follow it.", "info");
                    }
                }, 0);
            });
        }

        var slider = document.getElementById("site-radius");
        if (slider) {
            // ``input`` fires while the handle moves: the state is updated and the circle
            // resized on every frame, through ``setRadius`` - an overlay redraw.
            slider.addEventListener("input", function () { Pages.setRadius(Number(slider.value)); });
        }

        var lat = document.getElementById("site-lat");
        if (lat) {
            lat.addEventListener("change", function () { Pages.applyTyped(); });
            lat.addEventListener("blur", function () { Pages.applyTyped(); });
        }
        var lng = document.getElementById("site-lng");
        if (lng) {
            lng.addEventListener("change", function () { Pages.applyTyped(); });
            lng.addEventListener("blur", function () { Pages.applyTyped(); });
        }

        var save = document.getElementById("site-save");
        if (save) save.addEventListener("click", function () { Pages.save(); });
        var reset = document.getElementById("site-reset");
        if (reset) {
            reset.addEventListener("click", function () {
                Pages.reset();
                Toast.show("Cleared. Paste another link or click the map.", "info");
            });
        }
    }

    function boot() {
        bind();
        var user = session();
        if (user && user.token) {
            setText("site-status", "Signed in as " + (user.name || user.id) + ". Nothing is created until you save.");
        }
        Pages.setTab("link");
        Pages.paint();
        // The map first, then the form: the fields are usable while the CDN is still
        // answering, and a CDN that never answers leaves a page that can still be filled in.
        Map.ensure().then(function () {
            Map.mount();
            setText("site-map-note", "Click anywhere to drop the pin, drag it to fine-tune, drag the slider for the radius.");
        }).catch(function (err) {
            setText(
                "site-map-note",
                "The map library did not load (" + err.message + "). Type the coordinates instead - " +
                "the link resolver, the slider and the save all still work."
            );
        });
    }

    // Exported for the page and for the frontend suites, which drive these in a Node VM.
    global.SiteCreator = {
        Draft: Draft,
        SLIDER: SLIDER,
        SiteLink: SiteLink,
        Map: Map,
        Pages: Pages,
        Toast: Toast,
        apiBase: apiBase,
        request: request,
        boot: boot
    };

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", boot);
    } else {
        boot();
    }
})(typeof window !== "undefined" ? window : this);
