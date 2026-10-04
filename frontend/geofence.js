/**
 * The geofence editor's own script.
 *
 * THE SHAPE OF THIS FILE, AND WHY
 * -------------------------------
 *   1. ``Geofence``   - the state. One object: ``{lat, lng, radius}``. No engine may write
 *                       to it; every edit goes through a function here that updates the
 *                       state and *then* tells the renderer to draw. That is the decoupling
 *                       the requirements ask for: the map cannot become the source of truth,
 *                       and switching engines cannot lose an edit.
 *   2. ``Engines``    - two renderers of the same state, behind one interface:
 *                       ``mount``, ``setCenter``, ``setRadius``, ``onEdit``, ``view``.
 *                       Leaflet is built on OpenStreetMap raster tiles and needs no key;
 *                       Google Maps is loaded on demand and only when a key is available.
 *   3. the page       - the panel, the slider, the HUD, the two save paths, the toasts.
 *
 * The radius slider is the interaction worth reading closely: it calls ``setRadius`` on the
 * engine, which is ``circle.setRadius(...)`` in Leaflet and ``Circle.setRadius(...)`` in
 * Google - an overlay redraw on tiles the browser already holds. It never re-creates the
 * map, never recentres, and never re-requests a tile, which is what "smoothly resizes the
 * visual circle without tile re-renders" means in practice.
 *
 * No build step, no framework: this is the same plain-script style the rest of
 * ``frontend/`` uses, so it can be served from the same folder and opened from disk.
 */
(function (global) {
    "use strict";

    // ---------------------------------------------------------------- state
    /** The one fence being edited. Millimetre-ish precision: 6 decimals is ~11 cm. */
    var Geofence = {
        lat: 30.05,
        lng: 31.23,
        radius: 100,
        //: What the server last told us, for the "unsaved changes" hint and the legacy preview.
        saved: { lat: 30.05, lng: 31.23, radius: 100 },
        accuracy: null,
        updatedAt: null,
        generation: 0,
        source: "default"
    };

    var SLIDER = { min: 20, max: 500, step: 5 };
    var DEFAULT_VIEW = { center: [30.05, 31.23], zoom: 16 };
    //: Set by ``boot`` from the page's own origin, so a deployment on a LAN address or a
    //: tunnel calls itself rather than a hardcoded host. The console's own resolver
    //: (``api-config.js``) is not loaded here - this page is standalone - so the rule is
    //: spelled out once, in ``apiBase``.
    var API_BASE = null;
    var SESSION_KEY = "session";

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

    /** The API base: the page's own origin plus ``/api/v1``, or the console's override.
     *
     *  ``api-config.js`` is not loaded on this page (it is a console asset and this page
     *  must work on its own), so the two rules that matter are repeated here: a per-browser
     *  ``localStorage.apiBaseURL`` override wins, and otherwise the API is wherever the page
     *  came from. The old console hardcoded ``:8000`` for any other port, which is how a
     *  deployment on ``--port 8443`` came to call a *different* server on 8000.
     */
    function apiBase() {
        if (API_BASE) return API_BASE;
        var override = null;
        try { override = localStorage.getItem("apiBaseURL"); } catch (err) { override = null; }
        var base;
        if (override) {
            base = String(override).replace(/\/+$/, "");
            if (!/\/api\/v\d+$/.test(base)) base = base.replace(/\/api$/, "") + "/api/v1";
        } else if (location.protocol === "file:") {
            base = "http://localhost:8000/api/v1";
        } else {
            base = location.origin + "/api/v1";
        }
        API_BASE = base;
        return API_BASE;
    }

    /** The console's stored session token, or ``null``. Never invented. */
    function sessionToken() {
        var raw = null;
        try { raw = localStorage.getItem(SESSION_KEY); } catch (err) { raw = null; }
        if (!raw) return null;
        try {
            var stored = JSON.parse(raw);
            var user = stored && stored.user;
            return (user && user.token) ? String(user.token) : null;
        } catch (err) {
            return null;
        }
    }

    function request(path, options) {
        options = options || {};
        var headers = { Accept: "application/json" };
        var token = sessionToken();
        if (token) headers.Authorization = "Bearer " + token;
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

    // ---------------------------------------------------------------- toasts
    var Toast = {
        show: function (message, kind) {
            var root = document.getElementById("geo-toasts");
            if (!root) return;
            var node = document.createElement("div");
            node.className = "geo-toast" + (kind ? " is-" + kind : "");
            // ``textContent``, not markup: every string here came off the wire or out of an
            // error, and ``innerHTML`` is a parser.
            node.textContent = String(message);
            root.appendChild(node);
            setTimeout(function () { node.classList.add("is-leaving"); }, 4200);
            setTimeout(function () { if (node.parentNode) node.parentNode.removeChild(node); }, 4800);
        }
    };

    // ---------------------------------------------------------------- engines
    /** The interface both engines implement. ``onEdit`` is called with ``{lat, lng}`` when a
     *  *gesture* moves the centre - a click on the canvas or a drag of the pin - and with
     *  ``{radius}`` when the circle is dragged. The page writes the state; the engine only
     *  reports what the person did. */
    function BaseEngine(name) {
        this.name = name;
        this.ready = false;
        this.center = { lat: Geofence.lat, lng: Geofence.lng };
        this.radius = Geofence.radius;
        this.editHandler = null;
    }
    BaseEngine.prototype.mount = function () { throw new Error("mount() is not implemented"); };
    BaseEngine.prototype.setCenter = function (lat, lng) { this.center = { lat: lat, lng: lng }; };
    BaseEngine.prototype.setRadius = function (radius) { this.radius = radius; };
    BaseEngine.prototype.setView = function (lat, lng, zoom) { /* optional */ };
    BaseEngine.prototype.view = function () { return { lat: this.center.lat, lng: this.center.lng, radius: this.radius }; };
    BaseEngine.prototype.onEdit = function (handler) { this.editHandler = handler; };
    BaseEngine.prototype.emitEdit = function (payload) {
        if (this.editHandler) this.editHandler(payload);
    };

    // -- Leaflet --------------------------------------------------------------
    var LEAFLET_JS = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js";
    var LEAFLET_CSS = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css";

    function LeafletEngine() {
        BaseEngine.call(this, "leaflet");
        this.map = null;
        this.marker = null;
        this.circle = null;
    }
    LeafletEngine.prototype = Object.create(BaseEngine.prototype);
    LeafletEngine.prototype.constructor = LeafletEngine;

    LeafletEngine.prototype.mount = function (element) {
        var self = this;
        this.map = global.L.map(element, {
            center: [Geofence.lat, Geofence.lng],
            zoom: DEFAULT_VIEW.zoom,
            zoomControl: true,
            attributionControl: true
        });
        global.L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
            maxZoom: 19,
            attribution: "&copy; OpenStreetMap contributors"
        }).addTo(this.map);

        this.circle = global.L.circle([Geofence.lat, Geofence.lng], {
            radius: Geofence.radius,
            color: "#2563eb",
            weight: 2,
            fillColor: "#3b82f6",
            fillOpacity: 0.22
        }).addTo(this.map);

        this.marker = global.L.marker([Geofence.lat, Geofence.lng], { draggable: true }).addTo(this.map);

        // The three gestures. Each reports the edit; none of them writes the state itself -
        // the page does, and then calls ``setCenter``/``setRadius`` back. That round trip is
        // what keeps the pin, the circle and the HUD from ever disagreeing.
        this.marker.on("drag", function () {
            var position = self.marker.getLatLng();
            self.circle.setLatLng(position);          // overlay only: no tile work at all
            self.emitEdit({ lat: position.lat, lng: position.lng, dragging: true });
        });
        this.marker.on("dragend", function () {
            var position = self.marker.getLatLng();
            self.emitEdit({ lat: position.lat, lng: position.lng });
        });
        this.map.on("click", function (event) {
            self.emitEdit({ lat: event.latlng.lat, lng: event.latlng.lng });
        });
        this.circle.on("editRadius", function () {
            self.emitEdit({ radius: self.circle.getRadius() });
        });

        this.ready = true;
    };

    LeafletEngine.prototype.setCenter = function (lat, lng) {
        BaseEngine.prototype.setCenter.call(this, lat, lng);
        if (!this.ready) return;
        this.marker.setLatLng([lat, lng]);
        this.circle.setLatLng([lat, lng]);
    };

    LeafletEngine.prototype.setRadius = function (radius) {
        BaseEngine.prototype.setRadius.call(this, radius);
        // The whole point of this call: ``setRadius`` recomputes the overlay's path in the
        // SVG layer Leaflet already has. It does not move the view, does not re-create the
        // layer and does not ask the tile server for anything.
        if (this.ready) this.circle.setRadius(radius);
    };

    LeafletEngine.prototype.setView = function (lat, lng, zoom) {
        if (this.ready) this.map.setView([lat, lng], zoom || this.map.getZoom(), { animate: false });
    };

    // -- Google Maps ----------------------------------------------------------
    var GOOGLE_JS = "https://maps.googleapis.com/maps/api/js";

    function GoogleEngine(apiKey) {
        BaseEngine.call(this, "google");
        this.apiKey = apiKey;
        this.map = null;
        this.marker = null;
        this.circle = null;
    }
    GoogleEngine.prototype = Object.create(BaseEngine.prototype);
    GoogleEngine.prototype.constructor = GoogleEngine;

    GoogleEngine.prototype.mount = function (element) {
        var self = this;
        this.map = new global.google.maps.Map(element, {
            center: { lat: Geofence.lat, lng: Geofence.lng },
            zoom: DEFAULT_VIEW.zoom,
            mapTypeId: global.google.maps.MapTypeId.SATELLITE,
            mapTypeControl: true,
            streetViewControl: false,
            fullscreenControl: false
        });
        this.circle = new global.google.maps.Circle({
            map: this.map,
            center: { lat: Geofence.lat, lng: Geofence.lng },
            radius: Geofence.radius,
            editable: true,
            draggable: false,
            strokeColor: "#2563eb",
            strokeWeight: 2,
            fillColor: "#3b82f6",
            fillOpacity: 0.22
        });
        this.marker = new global.google.maps.Marker({
            map: this.map,
            position: { lat: Geofence.lat, lng: Geofence.lng },
            draggable: true,
            title: "Fence centre"
        });

        this.marker.addListener("drag", function () {
            var position = self.marker.getPosition();
            var point = { lat: position.lat(), lng: position.lng() };
            self.circle.setCenter(point);
            self.emitEdit({ lat: point.lat, lng: point.lng, dragging: true });
        });
        this.marker.addListener("dragend", function () {
            var position = self.marker.getPosition();
            self.emitEdit({ lat: position.lat(), lng: position.lng() });
        });
        this.map.addListener("click", function (event) {
            self.emitEdit({ lat: event.latLng.lat(), lng: event.latLng.lng() });
        });
        this.circle.addListener("radius_changed", function () {
            self.emitEdit({ radius: self.circle.getRadius() });
        });
        this.circle.addListener("center_changed", function () {
            var centre = self.circle.getCenter();
            self.marker.setPosition(centre);
            self.emitEdit({ lat: centre.lat(), lng: centre.lng(), dragging: true });
        });

        this.ready = true;
    };

    GoogleEngine.prototype.setCenter = function (lat, lng) {
        BaseEngine.prototype.setCenter.call(this, lat, lng);
        if (!this.ready) return;
        var point = { lat: lat, lng: lng };
        this.marker.setPosition(point);
        this.circle.setCenter(point);
    };

    GoogleEngine.prototype.setRadius = function (radius) {
        BaseEngine.prototype.setRadius.call(this, radius);
        // Same contract as Leaflet's: geometry only. Google redraws the circle's path on the
        // imagery already loaded.
        if (this.ready) this.circle.setRadius(radius);
    };

    GoogleEngine.prototype.setView = function (lat, lng, zoom) {
        if (!this.ready) return;
        this.map.setCenter({ lat: lat, lng: lng });
        if (zoom) this.map.setZoom(zoom);
    };

    // -- loading, and the switch ---------------------------------------------
    var Engines = {
        active: null,
        leafletScript: null,
        googleScript: null,

        /** Load a script once; resolve when it is there, reject with a readable reason.
         *
         *  "Already here" means a node carrying *this* url, not merely an element with this
         *  id. The check used to be "an element exists", which is a different question: a
         *  DOM in which any id answers with a node (the frontend suite's stub does, so that
         *  markup-only ids can be read back) would then take the already-loading branch for a
         *  tag that was never appended, and the CDN request would never be made. Asking for
         *  the src costs nothing and cannot be satisfied by a node that is not this script.
         */
        loadScript: function (url, id) {
            return new Promise(function (resolve, reject) {
                var existing = document.getElementById(id);
                var isThisScript = Boolean(existing && String(existing.src || "") === String(url));
                if (isThisScript && existing.dataset.loaded === "1") { resolve(); return; }
                if (isThisScript) {
                    existing.addEventListener("load", function () { resolve(); });
                    existing.addEventListener("error", function () { reject(new Error("could not load " + url)); });
                    return;
                }
                var script = document.createElement("script");
                script.id = id;
                script.src = url;
                script.async = true;
                script.addEventListener("load", function () {
                    script.dataset.loaded = "1";
                    resolve();
                });
                script.addEventListener("error", function () {
                    reject(new Error("could not load " + url));
                });
                document.head.appendChild(script);
            });
        },

        /** Leaflet, from the CDN. The stylesheet is already in the document; the script is
         *  loaded here so a CDN that never answers does not block the page's own boot. */
        ensureLeaflet: function () {
            var self = this;
            if (global.L && global.L.map) return Promise.resolve();
            if (this.leafletScript) return this.leafletScript;
            this.leafletScript = this.loadScript(LEAFLET_JS, "leaflet-js");
            return this.leafletScript;
        },

        /** Google Maps, on demand, with the key the operator typed (or the one the page was
         *  served with). The key is never stored by this page: it is passed to Google and
         *  kept in memory for the session only. */
        ensureGoogle: function (apiKey) {
            var self = this;
            if (global.google && global.google.maps && global.google.maps.Map) return Promise.resolve();
            if (!apiKey) return Promise.reject(new Error("no Google Maps API key supplied"));
            if (this.googleScript) return this.googleScript;
            this.googleScript = this.loadScript(
                GOOGLE_JS + "?key=" + encodeURIComponent(apiKey) + "&loading=async",
                "google-maps-js"
            );
            return this.googleScript;
        },

        /** Make ``engine`` the renderer, without touching the state. */
        activate: function (engine) {
            var host = document.getElementById("geo-map");
            if (host) host.innerHTML = "";
            this.active = engine;
            engine.onEdit(function (edit) { Pages.applyEdit(edit); });
            engine.mount(host);
            engine.setCenter(Geofence.lat, Geofence.lng);
            engine.setRadius(Geofence.radius);
            return engine;
        },

        useLeaflet: function () {
            var self = this;
            return this.ensureLeaflet().then(function () {
                self.activate(new LeafletEngine());
                Pages.setEngineLabel("OpenStreetMap (Leaflet)", true);
            });
        },

        useGoogle: function (apiKey) {
            var self = this;
            return this.ensureGoogle(apiKey).then(function () {
                self.activate(new GoogleEngine(apiKey));
                Pages.setEngineLabel("Google Maps", true);
            });
        },

        /** Redraw the active engine from the state. Called after every edit. */
        sync: function () {
            if (!this.active || !this.active.ready) return;
            this.active.setCenter(Geofence.lat, Geofence.lng);
            this.active.setRadius(Geofence.radius);
        }
    };

    // ---------------------------------------------------------------- the page
    var Pages = {
        engineName: "leaflet",

        applyEdit: function (edit) {
            if (!edit) return;
            if (isFiniteNumber(edit.lat) && isFiniteNumber(edit.lng)) {
                Geofence.lat = clamp(edit.lat, -90, 90);
                Geofence.lng = clamp(edit.lng, -180, 180);
            }
            if (isFiniteNumber(edit.radius)) {
                Geofence.radius = clamp(round(edit.radius, 1), SLIDER.min, SLIDER.max);
            }
            this.paint();
            // While a gesture is in flight the engine is already drawing it, and calling
            // back into it would fight the pointer. On the settle (``dragend``, a click, the
            // slider) the state is authoritative and the engine is brought to it.
            if (!edit.dragging) Engines.sync();
        },

        paint: function () {
            var lat = round(Geofence.lat, 6);
            var lng = round(Geofence.lng, 6);
            var radius = round(Geofence.radius, 1);

            setText("geo-lat", lat.toFixed(6));
            setText("geo-lng", lng.toFixed(6));
            setText("geo-radius", radius + " m");
            setText("geo-accuracy", Geofence.accuracy === null ? "—" : Geofence.accuracy + " m");
            setText("geo-radius-out", radius + " m");

            var latInput = document.getElementById("geo-lat-input");
            var lngInput = document.getElementById("geo-lng-input");
            var slider = document.getElementById("geo-radius-slider");
            // Only write an input the person is not typing in: overwriting a half-typed
            // number is how a field becomes unusable.
            if (latInput && document.activeElement !== latInput) latInput.value = String(lat);
            if (lngInput && document.activeElement !== lngInput) lngInput.value = String(lng);
            if (slider && document.activeElement !== slider) slider.value = String(radius);

            var changed = (
                round(Geofence.lat, 6) !== round(Geofence.saved.lat, 6) ||
                round(Geofence.lng, 6) !== round(Geofence.saved.lng, 6) ||
                radius !== round(Geofence.saved.radius, 1)
            );
            var save = document.getElementById("geo-save");
            if (save) save.textContent = changed ? "Save geofence (unsaved changes)" : "Save geofence";

            setText(
                "geo-fence-summary",
                "Active fence: " + round(Geofence.saved.lat, 6) + ", " + round(Geofence.saved.lng, 6) +
                " · " + round(Geofence.saved.radius, 1) + " m" +
                (Geofence.source === "default" ? " (cold-boot default)" : "") +
                (Geofence.updatedAt ? " · updated " + Geofence.updatedAt : "")
            );

            // The legacy preview is always visible: an administrator sending an edit down
            // the old path should be able to read exactly what that caller will receive.
            var preview = document.getElementById("geo-legacy-preview");
            if (preview) {
                preview.textContent = JSON.stringify(legacyPayload(), null, 2);
            }
        },

        setEngineLabel: function (name, ok) {
            this.engineName = name;
            var note = document.getElementById("geo-map-note");
            if (note) {
                note.textContent = ok
                    ? "Engine: " + name + ". Click the map to move the centre, drag the pin, drag the circle."
                    : "Engine: " + name + " (unavailable). The coordinates below can still be edited and saved.";
            }
            var leaflet = document.getElementById("geo-engine-leaflet");
            var google = document.getElementById("geo-engine-google");
            if (leaflet) {
                leaflet.classList.toggle("is-active", name === "OpenStreetMap (Leaflet)");
                leaflet.setAttribute("aria-pressed", String(name === "OpenStreetMap (Leaflet)"));
            }
            if (google) {
                google.classList.toggle("is-active", name === "Google Maps");
                google.setAttribute("aria-pressed", String(name === "Google Maps"));
            }
        },

        load: function () {
            return request("/geofence").then(function (answer) {
                if (answer.status === 401 || answer.status === 403) {
                    Toast.show("Sign in to the console first: this page reads the fence with your session.", "error");
                    setText("geo-map-note", "Not signed in. Open the console, sign in, and reload this page.");
                    return;
                }
                if (!answer.ok) {
                    Toast.show("Could not read the fence (" + answer.status + ").", "error");
                    return;
                }
                var fence = (answer.body && answer.body.geofence) || {};
                Geofence.lat = clamp(Number(fence.latitude) || Geofence.lat, -90, 90);
                Geofence.lng = clamp(Number(fence.longitude) || Geofence.lng, -180, 180);
                Geofence.radius = clamp(Number(fence.radius_meters) || Geofence.radius, SLIDER.min, SLIDER.max);
                Geofence.saved = { lat: Geofence.lat, lng: Geofence.lng, radius: Geofence.radius };
                Geofence.updatedAt = fence.updated_at || null;
                Geofence.source = fence.source || "database";
                Geofence.generation = fence.generation || 0;
                if (answer.body && answer.body.slider) Pages.applySliderBounds(answer.body.slider);
                Engines.sync();
                Pages.paint();
                var testLat = document.getElementById("geo-test-lat");
                var testLng = document.getElementById("geo-test-lng");
                if (testLat && !testLat.value) testLat.value = String(round(Geofence.lat, 6));
                if (testLng && !testLng.value) testLng.value = String(round(Geofence.lng, 6));
            }).catch(function (err) {
                Toast.show("Could not reach the server: " + (err && err.message ? err.message : err), "error");
                setText("geo-map-note", "The server did not answer. The map still works; saving will not.");
            });
        },

        applySliderBounds: function (bounds) {
            var slider = document.getElementById("geo-radius-slider");
            if (!slider || !bounds) return;
            if (isFiniteNumber(bounds.min_meters)) SLIDER.min = bounds.min_meters;
            if (isFiniteNumber(bounds.max_meters)) SLIDER.max = bounds.max_meters;
            if (isFiniteNumber(bounds.step_meters)) SLIDER.step = bounds.step_meters;
            slider.min = String(SLIDER.min);
            slider.max = String(SLIDER.max);
            slider.step = String(SLIDER.step);
        },

        save: function (useLegacy) {
            var payload = useLegacy ? legacyPayload() : modernPayload();
            var path = useLegacy ? "/legacy/set-geofence" : "/geofence";
            var button = document.getElementById("geo-save");
            if (button) button.disabled = true;
            return request(path, { method: "POST", body: payload }).then(function (answer) {
                if (answer.status === 401) {
                    Toast.show("Your session expired. Sign in again, then save.", "error");
                    return;
                }
                if (answer.status === 403) {
                    Toast.show("Only an administrator may change the fence.", "error");
                    return;
                }
                if (!answer.ok) {
                    Toast.show("Save failed (" + answer.status + "): " + describeError(answer.body), "error");
                    return;
                }
                var fence = (answer.body && answer.body.geofence) || {};
                Geofence.saved = {
                    lat: Number(fence.latitude),
                    lng: Number(fence.longitude),
                    radius: Number(fence.radius_meters)
                };
                Geofence.updatedAt = fence.updated_at || null;
                Geofence.source = fence.source || (useLegacy ? "legacy" : "modern");
                Geofence.generation = fence.generation || 0;
                Pages.paint();
                Toast.show(
                    useLegacy
                        ? "Saved through the legacy endpoint (normalised and stored)."
                        : "Geofence saved. The next punch is checked against it.",
                    "ok"
                );
            }).catch(function (err) {
                Toast.show("Could not reach the server: " + (err && err.message ? err.message : err), "error");
            }).then(function () {
                if (button) button.disabled = false;
            });
        },

        testPunch: function () {
            var lat = Number(document.getElementById("geo-test-lat").value);
            var lng = Number(document.getElementById("geo-test-lng").value);
            var accuracyRaw = document.getElementById("geo-test-accuracy").value;
            var payload = { user_id: null, latitude: lat, longitude: lng };
            if (accuracyRaw !== "" && isFinite(Number(accuracyRaw))) {
                payload.accuracy_meters = Number(accuracyRaw);
            }
            var token = sessionToken();
            if (!token) { Toast.show("Sign in first: a punch is recorded against your account.", "error"); return; }
            payload.user_id = accountIdFromSession();
            if (!payload.user_id) {
                Toast.show("Your stored session has no account id; sign in again.", "error");
                return;
            }
            request("/attendance/punch", { method: "POST", body: payload }).then(function (answer) {
                var box = document.getElementById("geo-test-result");
                if (box) {
                    box.textContent = "HTTP " + answer.status + "\n" + JSON.stringify(answer.body, null, 2);
                }
                if (answer.ok) {
                    Toast.show(
                        "Approved at " + answer.body.distance_meters + " m (decision in " +
                        answer.body.verification_ms + " ms).",
                        "ok"
                    );
                } else {
                    Toast.show("Refused (" + answer.status + "): " + describeError(answer.body), "error");
                }
            }).catch(function (err) {
                Toast.show("Could not reach the server: " + (err && err.message ? err.message : err), "error");
            });
        }
    };

    /** The account id in the stored session, for the punch test only. */
    function accountIdFromSession() {
        var raw = null;
        try { raw = localStorage.getItem(SESSION_KEY); } catch (err) { raw = null; }
        if (!raw) return null;
        try {
            var stored = JSON.parse(raw);
            var user = stored && stored.user;
            return user && user.id ? String(user.id) : null;
        } catch (err) {
            return null;
        }
    }

    function setText(id, value) {
        var node = document.getElementById(id);
        if (node) node.textContent = String(value);
    }

    function modernPayload() {
        return {
            latitude: round(Geofence.lat, 6),
            longitude: round(Geofence.lng, 6),
            radius_meters: round(Geofence.radius, 1)
        };
    }

    /** The legacy triple, as the raw numeric **strings** the old endpoint documents. */
    function legacyPayload() {
        return {
            lat: String(round(Geofence.lat, 6)),
            lng: String(round(Geofence.lng, 6)),
            rad: String(round(Geofence.radius, 1))
        };
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

    // ---------------------------------------------------------------- wiring
    function bind() {
        var slider = document.getElementById("geo-radius-slider");
        if (slider) {
            // ``input`` fires while the handle moves: the state is updated and the circle
            // resized on every frame of the drag, through ``setRadius`` - an overlay redraw.
            slider.addEventListener("input", function () {
                Pages.applyEdit({ radius: Number(slider.value) });
            });
        }

        var latInput = document.getElementById("geo-lat-input");
        if (latInput) {
            latInput.addEventListener("change", function () {
                Pages.applyEdit({ lat: Number(latInput.value) });
            });
        }
        var lngInput = document.getElementById("geo-lng-input");
        if (lngInput) {
            lngInput.addEventListener("change", function () {
                Pages.applyEdit({ lng: Number(lngInput.value) });
            });
        }

        var save = document.getElementById("geo-save");
        if (save) {
            save.addEventListener("click", function () {
                var legacy = document.getElementById("geo-legacy-toggle");
                Pages.save(Boolean(legacy && legacy.checked));
            });
        }
        var reset = document.getElementById("geo-reset");
        if (reset) {
            reset.addEventListener("click", function () {
                Pages.load().then(function () {
                    Toast.show("Reloaded the fence the server is using.", "ok");
                });
            });
        }

        var leafletButton = document.getElementById("geo-engine-leaflet");
        if (leafletButton) {
            leafletButton.addEventListener("click", function () {
                Engines.useLeaflet().catch(function (err) {
                    Toast.show("OpenStreetMap tiles could not load: " + err.message, "error");
                    Pages.setEngineLabel("OpenStreetMap (Leaflet)", false);
                });
            });
        }
        var googleButton = document.getElementById("geo-engine-google");
        if (googleButton) {
            googleButton.addEventListener("click", function () {
                var key = (document.getElementById("geo-google-key").value || "").trim();
                var fallback = document.getElementById("geo-fallback");
                if (!key) {
                    // No key: offer the field rather than failing silently. The page has to be
                    // usable without a Google account, and this is where that is decided.
                    if (fallback) fallback.hidden = false;
                    Toast.show("Google Maps needs an API key. Paste one to load the satellite engine.", "info");
                    return;
                }
                Engines.useGoogle(key).catch(function (err) {
                    Toast.show("Google Maps could not load: " + err.message, "error");
                    Pages.setEngineLabel("Google Maps", false);
                });
            });
        }
        var googleLoad = document.getElementById("geo-google-load");
        if (googleLoad) {
            googleLoad.addEventListener("click", function () {
                var key = (document.getElementById("geo-google-key").value || "").trim();
                if (!key) { Toast.show("Paste a key first.", "error"); return; }
                Engines.useGoogle(key).then(function () {
                    var fallback = document.getElementById("geo-fallback");
                    if (fallback) fallback.hidden = true;
                }).catch(function (err) {
                    Toast.show("Google Maps could not load: " + err.message, "error");
                });
            });
        }

        var legacyToggle = document.getElementById("geo-legacy-toggle");
        if (legacyToggle) {
            legacyToggle.addEventListener("change", function () { Pages.paint(); });
        }

        var testButton = document.getElementById("geo-test-punch");
        if (testButton) {
            testButton.addEventListener("click", function () { Pages.testPunch(); });
        }
    }

    function boot() {
        bind();
        Pages.setEngineLabel("OpenStreetMap (Leaflet)", false);
        Pages.paint();
        // The map first, then the fence: the panel is usable while the CDN is still
        // answering, and a CDN that never answers leaves a page that can still edit and save.
        Engines.useLeaflet().then(function () {
            Pages.paint();
            return Pages.load();
        }).catch(function (err) {
            Pages.setEngineLabel("OpenStreetMap (Leaflet)", false);
            Toast.show("The map library did not load (" + err.message + "). Coordinates can still be edited.", "error");
            Pages.load();
        });
    }

    // Exported for the page and for the frontend suites, which drive these in a Node VM.
    global.GeofenceEditor = {
        Geofence: Geofence,
        SLIDER: SLIDER,
        Engines: Engines,
        Pages: Pages,
        LeafletEngine: LeafletEngine,
        GoogleEngine: GoogleEngine,
        Toast: Toast,
        apiBase: apiBase,
        legacyPayload: legacyPayload,
        modernPayload: modernPayload,
        applyEdit: function (edit) { Pages.applyEdit(edit); },
        boot: boot
    };

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", boot);
    } else {
        boot();
    }
})(typeof window !== "undefined" ? window : this);
