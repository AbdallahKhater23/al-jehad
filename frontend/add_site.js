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
 *   4. ``Hud``      - the parse HUD under the link box: a check the moment a pair is
 *      recognised, silence while a URL is still being typed, and the field's own complaint
 *      when the box holds something that is not a link at all.
 *   5. ``Category`` - the category pills, rendered from ``GET /admin/site_categories``.
 *      Native radios, so the row is one tab stop with the arrow keys between its options.
 *   6. ``Shift``    - the two time boxes and the cross-midnight switch. ``shift_windows``
 *      reads ``start > end`` as a window that runs through midnight, so the switch says
 *      which of the two shapes the pair is in and refuses to save the contradictory one.
 *   7. ``Pages``    - the form: the two tabs, the resolver, the slider, the save - and where
 *      the caret goes afterwards, which is what makes the second site cheap to add.
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
    //: The two marks a toast can wear, as constants. Everything else about a toast is text,
    //: because the sentence came off the wire or out of an error and ``innerHTML`` is a parser.
    var TOAST_ICON = {
        ok: '<svg class="geo-check" viewBox="0 0 20 20" width="16" height="16" aria-hidden="true"' +
            ' focusable="false"><path fill="currentColor" d="M10 0a10 10 0 1 0 0 20 10 10 0 0 0' +
            ' 0-20Zm4.7 7.7-5.4 5.4a1 1 0 0 1-1.4 0L5.3 10.5a1 1 0 0 1 1.4-1.4l1.9 1.9 4.7-4.7a1' +
            ' 1 0 0 1 1.4 1.4Z"/></svg>',
        error: '<svg class="geo-check" viewBox="0 0 20 20" width="16" height="16"' +
            ' aria-hidden="true" focusable="false"><path fill="currentColor" d="M10 0a10 10 0' +
            ' 1 0 0 20 10 10 0 0 0 0-20Zm1 14.5H9v-2h2v2Zm0-3.5H9V5h2v6Z"/></svg>'
    };

    var Toast = {
        show: function (message, kind) {
            var root = document.getElementById("geo-toasts");
            if (!root) return null;
            var node = document.createElement("div");
            node.className = "geo-toast" + (kind ? " is-" + kind : "");
            // A toast is a live region of its own: the container is polite, and the message is
            // announced when it arrives without the focus moving anywhere.
            node.setAttribute("role", "status");
            node.innerHTML = TOAST_ICON[kind] || "";
            var text = document.createElement("span");
            text.textContent = String(message);
            node.appendChild(text);
            root.appendChild(node);
            setTimeout(function () { node.classList.add("is-leaving"); }, 4200);
            setTimeout(function () { if (node.parentNode) node.parentNode.removeChild(node); }, 4800);
            return node;
        }
    };

    // ---------------------------------------------------------------- the parse HUD
    /**
     * "Did that paste work?", answered under the box it is about.
     *
     * It shows a check the moment a coordinate pair is recognised - in the browser, with no
     * request - and it stays *silent* while a URL is half typed, because a complaint that fires
     * on every keystroke is noise. The complaint is the error row below, and it arrives on blur
     * or after a resolve that failed.
     */
    var Hud = {
        set: function (text, kind) {
            var hud = document.getElementById("site-link-hud");
            if (hud) hud.className = "geo-link-hud" + (kind ? " is-" + kind : "");
            setText("site-link-hud-text", text);
        },
        clear: function () {
            var hud = document.getElementById("site-link-hud");
            if (hud) hud.className = "geo-link-hud";
            setText("site-link-hud-text", "");
        },
        /** Read what is in the box and say the most useful thing about it. */
        preview: function () {
            var field = document.getElementById("site-maps-url");
            var text = field && field.value !== undefined ? String(field.value).trim() : "";
            if (!text) {
                Hud.clear();
                return null;
            }
            var found = SiteLink.coordinatesFrom(text);
            if (found) {
                Hud.set(
                    "Coordinates found: " + round(found.lat, 6) + ", " + round(found.lng, 6),
                    "ok"
                );
                return found;
            }
            if (SiteLink.isShortened(text)) {
                // A shortened link is not a failure: the coordinates exist, behind a redirect
                // this browser cannot follow. Say which button does.
                Hud.set("Shortened link - press Find it to follow it.", "info");
                return null;
            }
            Hud.clear();
            return null;
        },
        /** On blur: an unreadable box gets its sentence. A half-typed one is left alone. */
        commit: function () {
            var field = document.getElementById("site-maps-url");
            var text = field && field.value !== undefined ? String(field.value).trim() : "";
            if (!text || Hud.preview() || SiteLink.isShortened(text)) {
                Pages.complain("");
                return;
            }
            Pages.complain(
                "That does not look like a Maps link or a coordinate pair. Paste the link from " +
                "Google Maps, or type the two numbers (29.351234, 47.984712)."
            );
        }
    };

    // ---------------------------------------------------------------- the category
    /**
     * The category pills.
     *
     * Native radios, built here rather than written into the markup because the list comes from
     * ``GET /admin/site_categories``. The row keeps its own copy of what is chosen rather than
     * asking the document which input is checked: the state this page cares about is one string,
     * and a DOM query is not needed to hold it.
     */
    var Category = {
        //: The chosen value. ``""`` is "No category", which is a real answer - the site then
        //: follows the company hours, exactly as it did before categories existed.
        selected: "",
        //: The pills on screen, in order: this page's own record of what it rendered.
        pills: [],

        value: function () { return this.selected; },

        /** Point the row at one value and repaint it. */
        select: function (value) {
            this.selected = value === undefined || value === null ? "" : String(value);
            this.pills.forEach(function (pill) {
                var chosen = String(pill.value) === Category.selected;
                pill.input.checked = chosen;
                pill.label.classList.toggle("is-checked", chosen);
            });
            return this.selected;
        },

        /** Replace the row with the categories the server has, keeping the current choice. */
        render: function (rows) {
            var host = document.getElementById("site-category-pills");
            if (!host) return false;
            while (host.firstChild) host.removeChild(host.firstChild);
            this.pills = [];
            var options = [{ value: "", label: "No category" }];
            (rows || []).forEach(function (row) {
                options.push({ value: String(row.category_id), label: String(row.name) });
            });
            options.forEach(function (option) {
                var label = document.createElement("label");
                label.className = "geo-pill";
                var input = document.createElement("input");
                input.type = "radio";
                // One name for the group: the browser then treats the pills as what they are -
                // a single choice out of a short list, with the arrow keys between the options.
                input.name = "site-category";
                input.value = option.value;
                input.id = "site-category-" + (option.value || "none");
                input.addEventListener("change", function () { Category.select(option.value); });
                var caption = document.createElement("span");
                caption.textContent = option.label;
                label.appendChild(input);
                label.appendChild(caption);
                host.appendChild(label);
                Category.pills.push({ value: option.value, input: input, label: label });
            });
            // A choice that is no longer on offer - a category deleted in another tab - falls
            // back to "no category" rather than to a value nothing on screen represents.
            var offered = this.pills.some(function (pill) {
                return String(pill.value) === Category.selected;
            });
            if (!offered) this.selected = "";
            this.select(this.selected);
            return true;
        }
    };

    // ---------------------------------------------------------------- the shift window
    /**
     * The two time boxes, and the cross-midnight switch.
     *
     * ``shift_windows`` reads ``start > end`` as a window that runs through midnight - not a
     * special case bolted on here, but the predicate the punch path already uses, and its own
     * messages print ``22:00-06:00 (overnight)``. So this object invents nothing: it says which
     * of the two shapes the pair is in, and it refuses to save a pair that closes before it
     * opens while the administrator has said it is *not* an overnight one.
     */
    var Shift = {
        HHMM: /^([01]\d|2[0-3]):[0-5]\d$/,

        value: function () {
            var field = function (id) {
                var node = document.getElementById(id);
                return node && node.value !== undefined ? String(node.value).trim() : "";
            };
            return { start: field("site-window-start"), end: field("site-window-end") };
        },

        overnight: function () {
            var box = document.getElementById("site-overnight");
            return !!(box && box.checked);
        },

        /** ``true`` when the two boxes describe a window that closes on the next day. */
        crosses: function () {
            var at = this.value();
            return !!(at.start && at.end && at.end <= at.start);
        },

        /** Minutes the window runs, or ``null`` if the pair is not two times yet. */
        duration: function () {
            var at = this.value();
            if (!this.HHMM.test(at.start) || !this.HHMM.test(at.end)) return null;
            var minutes = function (text) {
                var parts = text.split(":");
                return Number(parts[0]) * 60 + Number(parts[1]);
            };
            var span = minutes(at.end) - minutes(at.start);
            if (span <= 0) span += 24 * 60;
            return span;
        },

        /** Put the switch where the numbers say it belongs. The admin can still turn it off. */
        sync: function () {
            var box = document.getElementById("site-overnight");
            if (box) box.checked = this.crosses();
            this.say();
        },

        /** The sentence under the switch: which window this is, and how long it runs. */
        say: function () {
            var at = this.value();
            var node = document.getElementById("site-shift-summary");
            if (!node) return;
            if (!at.start && !at.end) {
                node.textContent = "";
                return;
            }
            if (!at.start || !at.end) {
                node.textContent = "One time on its own does not make a window: fill in both, or "
                    + "clear them to inherit the company hours.";
                return;
            }
            if (!this.HHMM.test(at.start) || !this.HHMM.test(at.end)) {
                node.textContent = "Times are written as a 24-hour HH:MM, for example 07:00 or 22:00.";
                return;
            }
            var span = this.duration();
            var length = Math.floor(span / 60) + " h " + (span % 60) + " m";
            node.textContent = this.crosses()
                ? "Opens " + at.start + ", closes " + at.end + " the next day - " + length +
                  " (an overnight window, which is what the punch window already reads)."
                : "Opens " + at.start + ", closes " + at.end + " - " + length + ".";
        },

        /** ``""`` when the pair is saveable, or the sentence that says what is wrong with it. */
        validate: function () {
            var at = this.value();
            if (!at.start && !at.end) return "";
            if (!at.start || !at.end) {
                return "A window needs both times, or neither: fill in the other one, or clear "
                    + "both and the site will follow the company hours.";
            }
            if (!this.HHMM.test(at.start) || !this.HHMM.test(at.end)) {
                return "Times must be a 24-hour HH:MM (07:00, 22:00).";
            }
            if (at.end === at.start) {
                return "A window that opens and closes at the same minute is open for 24 hours "
                    + "or not at all - set a different closing time.";
            }
            if (this.crosses() && !this.overnight()) {
                return "These hours close before they open, which is a window that runs past "
                    + "midnight. Switch on \"Runs past midnight\" if that is what you mean, or "
                    + "set a closing time later in the day.";
            }
            return "";
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
                    // ``?q=loc:29.351234,47.984712`` is the shape Maps itself writes when a
                    // place is shared from the mobile app: the pair is there, behind a prefix.
                    // Stripped rather than treated as a different shape, so one rule reads both.
                    value = value.replace(/^loc\s*:\s*/i, "").trim();
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
            // The hosts a Google Maps share sheet actually produces for a place: the app's
            // ``maps.app.goo.gl``, the older ``goo.gl/maps``, and ``app.goo.gl``. The server's
            // allowlist is the authority on which of them may be *fetched* - this only decides
            // whether asking it is worth a round trip.
            return /^https?:\/\/(maps\.app\.goo\.gl|app\.goo\.gl|goo\.gl)\//i.test(
                String(text || "").trim()
            );
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
            // A second mount is a *replacement*, not a second map. Leaflet refuses a container
            // it already owns ("Map container is already initialized"), and that throw inside
            // ``boot`` is a page whose form never binds again - so mounting twice used to mean
            // reloading the page to get a working form back. Destroying the instance first
            // makes boot idempotent: the second mount costs a repaint and nothing else.
            if (this.map) this.destroy();
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
                // Visible again, because this reuse path is exactly where the second site's
                // circle went missing: see ``clear`` for what used to hide it.
                this.circle.setStyle({ opacity: 1, fillOpacity: 0.22 });
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
                this.marker.setOpacity(1);
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

        /**
         * Take the pin and the circle off the map, for good.
         *
         * Removed, not faded. The previous build set both overlays to ``opacity: 0`` and then
         * *reused* them for the next site, because ``place`` only creates an overlay when it
         * has none - so everything after the first save was drawn invisible on a map that was
         * working perfectly. A map that answers every click and shows nothing is a map that
         * looks locked, and the only way out was a page reload.
         */
        clear: function () {
            if (this.map) {
                if (this.marker) this.map.removeLayer(this.marker);
                if (this.circle) this.map.removeLayer(this.circle);
            }
            this.marker = null;
            this.circle = null;
        },

        /** Throw the whole map away, so ``mount`` can build it again on the same container. */
        destroy: function () {
            if (this.map) {
                try {
                    this.map.remove();
                } catch (err) {
                    // A container Leaflet has already released is not worth a broken form:
                    // the point of this call is that the container is reusable again.
                }
            }
            this.map = null;
            this.marker = null;
            this.circle = null;
            this.ready = false;
        },

        /**
         * Back to the default box.
         *
         * A view is state too. Site A sat somewhere off the edge of Site B's view, so the
         * second draft arrived on a map that looked empty while holding a hidden pin -
         * recentring is what makes "add another" start from where the first one did.
         */
        resetView: function () {
            if (this.ready && this.map) {
                this.map.setView(
                    [FALLBACK_CENTER.lat, FALLBACK_CENTER.lng],
                    DEFAULT_VIEW.zoom,
                    { animate: false }
                );
            }
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

        /**
         * Say what went wrong, where the person is looking.
         *
         * A toast is gone in four seconds and carries no context; a parse failure has to stay
         * put long enough to be read and compared against what is still in the box. Nothing
         * here touches the draft - the pin, the fields and the radius survive every failure,
         * which is the whole difference between a refused link and a broken page.
         */
        complain: function (message) {
            this.complainAt("site-resolve-error", message);
        },

        /** The same, aimed at a named field's own error row under it. */
        complainAt: function (id, message) {
            var alert = document.getElementById(id);
            if (alert) alert.textContent = String(message || "");
        },

        /** Take a pasted link: the browser's own parse first, the server only if it must. */
        resolve: function () {
            var field = document.getElementById("site-maps-url");
            var text = field && field.value !== undefined ? String(field.value).trim() : "";
            var box = document.getElementById("site-resolve-result");
            Pages.complain("");
            Hud.clear();
            if (!text) {
                Pages.complain("Paste a Google Maps link, or the two numbers (29.351234, 47.984712).");
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
                Hud.set(
                    "Coordinates found: " + round(local.lat, 6) + ", " + round(local.lng, 6),
                    "ok"
                );
                Toast.show("Pin moved to " + round(local.lat, 6) + ", " + round(local.lng, 6) + ".", "ok");
                // The tab does *not* switch: the box the administrator just pasted into is the
                // one they will paste into again, and hiding it costs them a click every time.
                return Promise.resolve(true);
            }

            // No coordinates in the text. Either a shortened link - the numbers only exist
            // after the redirect, which is the one thing this page cannot do itself - or
            // something that is not a Maps link at all. The server tells the two apart, so it
            // is asked either way and *its* sentence is what the administrator reads.
            var button = document.getElementById("site-resolve");
            if (button) button.disabled = true;
            if (box) box.textContent = "Asking the server about that link…";
            return request("/resolve-maps-link", { method: "POST", body: { url: text } })
                .then(function (answer) {
                    if (answer.status === 401 || answer.status === 403) {
                        Pages.complain("Sign in to the console first: resolving a link needs your session.");
                        Toast.show("Sign in to the console first: resolving a link needs your session.", "error");
                        if (box) box.textContent = "Not signed in.";
                        return false;
                    }
                    if (!answer.ok) {
                        var reason = describeError(answer.body);
                        // Inline first, toast second: this is the failure the requirement names,
                        // and it has to be readable without chasing a disappearing bubble.
                        Pages.complain("Could not read that link: " + reason);
                        Toast.show("Could not read that link: " + reason, "error");
                        if (box) box.textContent = "HTTP " + answer.status + "\n" + reason;
                        return false;
                    }
                    var latitude = Number(answer.body.latitude);
                    var longitude = Number(answer.body.longitude);
                    if (!isFiniteNumber(latitude) || !isFiniteNumber(longitude)) {
                        // A 200 whose body has no numbers in it is a refusal too: the point of
                        // the round trip was a coordinate, and this one did not come back with
                        // one. Reported rather than turned into setCenter(NaN, NaN).
                        Pages.complain("That link answered without coordinates. Copy the address bar instead.");
                        if (box) box.textContent = JSON.stringify(answer.body, null, 2);
                        return false;
                    }
                    Pages.setCenter(latitude, longitude, "link");
                    Map.look(latitude, longitude);
                    if (box) {
                        box.textContent = "Resolved on the server:\n" + JSON.stringify(answer.body, null, 2);
                    }
                    Hud.set(
                        "Coordinates found: " + round(latitude, 6) + ", " + round(longitude, 6),
                        "ok"
                    );
                    Toast.show("Pin moved to " + round(latitude, 6) + ", " + round(longitude, 6) + ".", "ok");
                    return true;
                })
                .catch(function (err) {
                    var reason = err && err.message ? err.message : String(err);
                    Pages.complain("Could not reach the server: " + reason);
                    Toast.show("Could not reach the server: " + reason, "error");
                    return false;
                })
                .then(function (settled) {
                    // Live again on *every* path, including the early returns above: a second
                    // attempt must never need a reload, which is the reported bug's shape.
                    if (button) button.disabled = false;
                    return settled;
                });
        },

        /**
         * The categories, for the picker.
         *
         * Best effort on purpose: a deployment with none, or a session that cannot read them,
         * leaves the picker holding "No category" - which is a real answer rather than a
         * broken form, because a site with no category follows the company hours exactly as
         * it did before categories existed.
         */
        loadCategories: function () {
            var host = document.getElementById("site-category-pills");
            if (!host) return Promise.resolve(false);
            return request("/admin/site_categories")
                .then(function (answer) {
                    if (!answer.ok || !Array.isArray(answer.body)) {
                        Category.render([]);
                        return false;
                    }
                    Category.render(answer.body);
                    var note = document.getElementById("site-category-note");
                    if (note) {
                        note.textContent = answer.body.length
                            ? "A category carries its own hours to every site inside it."
                            : "No categories yet: add them under Sites in the console.";
                    }
                    return true;
                })
                .catch(function () {
                    // A picker that cannot be filled is an empty row, not a broken form: a site
                    // with no category is exactly as valid as it was before categories existed.
                    Category.render([]);
                    return false;
                });
        },

        /** Read the typed coordinates out of the fields. */
        applyTyped: function () {
            var latField = document.getElementById("site-lat");
            var lngField = document.getElementById("site-lng");
            var latText = latField ? String(latField.value || "").trim() : "";
            var lngText = lngField ? String(lngField.value || "").trim() : "";
            // ``Number("")`` is 0, so a cleared box used to *be* the equator: blurring a field
            // somebody was halfway through retyping moved the pin to (0, lng), and the save
            // that followed stored it. A blank box is "no coordinate", never a number - and
            // the refusal is silent because the field is merely empty, not wrong.
            if (!latText || !lngText) return false;
            var lat = Number(latText);
            var lng = Number(lngText);
            if (!isFinite(lat) || !isFinite(lng)) return false;
            return this.setCenter(lat, lng, "typed");
        },

        /** Create the site, and report exactly what the server stored. */
        save: function () {
            var nameField = document.getElementById("site-name");
            var name = nameField && nameField.value !== undefined ? String(nameField.value).trim() : "";
            if (!name) {
                // The sentence goes under the field and the caret goes into it: the name is the
                // one thing this page refuses to save without, and a toast is somewhere else.
                this.complainAt(
                    "site-name-error",
                    "Give the site a name: it is the row's key, and every attendance record for " +
                    "this site is written with it."
                );
                this.focus("site-name");
                Toast.show("Give the site a name first.", "error");
                return Promise.resolve(false);
            }
            this.complainAt("site-name-error", "");
            if (Draft.lat === null) {
                Toast.show("Pick a location first: paste a link, type the pair, or click the map.", "error");
                this.focus("site-maps-url");
                return Promise.resolve(false);
            }
            // The hours, before the request: a window that closes before it opens is either an
            // overnight shift somebody meant or a typo, and the switch is what says which.
            var windowProblem = Shift.validate();
            if (windowProblem) {
                this.complainAt("site-window-error", windowProblem);
                Shift.say();
                this.focus("site-window-end");
                Toast.show(windowProblem, "error");
                return Promise.resolve(false);
            }
            this.complainAt("site-window-error", "");
            var button = document.getElementById("site-save");
            if (button) button.disabled = true;
            // The radius is the slider's number, and a NaN on the wire is JSON's ``null``:
            // the server would read that as "no radius given" and stored its own default, which
            // is a fence nobody chose. Clamped to the slider's own bounds, so this is a guard
            // rather than a second opinion about what a radius is.
            var radius = isFiniteNumber(Draft.radius)
                ? clamp(round(Draft.radius, 1), SLIDER.min, SLIDER.max)
                : 100;
            var fieldValue = function (id) {
                var node = document.getElementById(id);
                return node && node.value !== undefined ? String(node.value).trim() : "";
            };
            var payload = {
                site_name: name,
                latitude: round(Draft.lat, 6),
                longitude: round(Draft.lng, 6),
                radius_meters: radius,
                // The schema this page writes into grew two things after the page was written:
                // the category a site belongs to, and the hours its arrivals are judged by.
                // Blank means "inherit" - the same thing the API's NULL means - so a box nobody
                // filled in is sent as null, and an uncategorised site stays uncategorised.
                category_id: Category.value() ? Number(Category.value()) : null,
                clock_in_window_start: fieldValue("site-window-start") || null,
                clock_in_window_end: fieldValue("site-window-end") || null,
                site_timezone: fieldValue("site-window-timezone") || null
            };
            return request("/sites", { method: "POST", body: payload })
                .then(function (answer) {
                    var box = document.getElementById("site-save-result");
                    if (answer.status === 401 || answer.status === 403) {
                        Toast.show("Only an administrator may create a site.", "error");
                        if (box) {
                            box.textContent = "Not signed in as an administrator (HTTP " +
                                answer.status + ").";
                        }
                        return false;
                    }
                    if (!answer.ok) {
                        var reason = describeError(answer.body);
                        Toast.show("Could not create the site: " + reason, "error");
                        if (box) box.textContent = "HTTP " + answer.status + "\n" + reason;
                        return false;
                    }
                    if (box) box.textContent = JSON.stringify(answer.body, null, 2);
                    var keepGoing = Pages.keepGoing();
                    Toast.show(
                        "Created " + name + " with a " + round(radius, 1) + " m fence." +
                        (keepGoing ? " Paste the next link." : " Back to the console when you are ready."),
                        "ok"
                    );
                    setText(
                        "site-status",
                        "Created " + name + ". " + (keepGoing
                            ? "The form is clear - add the next one."
                            : "The form is clear. The console is one click away.")
                    );
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
                        // The "create & add another" path, made explicit: empty form, pin off the
                        // map, view home, and the caret in the box the next site starts from.
                        Pages.reset();
                        Pages.focusNext();
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
            var slider = document.getElementById("site-radius");
            if (slider) slider.value = String(Draft.radius);
            // The fields the schema grew after this page was first written: a category, the
            // site's own operating hours, and its zone. Blank means "inherit", which is what
            // an empty box says - so a fresh draft clears them rather than carrying the
            // previous site's hours into the next one.
            // The pills, the switch and the three sentences: a fresh draft is a form with
            // nothing said about it yet. The note under the time boxes is the one exception -
            // it is the *form's* own help text, not a complaint, so it is put back rather than
            // cleared.
            Category.select("");
            var overnight = document.getElementById("site-overnight");
            if (overnight) overnight.checked = false;
            ["site-window-start", "site-window-end", "site-window-timezone"].forEach(function (id) {
                var field = document.getElementById(id);
                if (field) field.value = "";
            });
            ["site-name-error", "site-window-error", "site-resolve-error"].forEach(function (id) {
                var complaint = document.getElementById(id);
                if (complaint) complaint.textContent = "";
            });
            setText("site-shift-summary", "");
            Hud.clear();
            var note = document.getElementById("site-window-note");
            if (note) {
                note.textContent = "Leave the times blank to follow the company hours. A site's "
                    + "own hours decide whether an arrival is late.";
            }
            // Off the map, and the view back to the default box: the next site is a new draft,
            // and a pin left behind is a site somebody believes they are still editing.
            Map.clear();
            Map.resetView();
            Pages.setTab("link");
            Pages.paint();
        },

        /** The footer's own recorder: the toast says what happened, this says what is next. */
        lastFocus: null,

        /** Put the caret somewhere, and remember where - ``focus`` is a no-op in the VM. */
        focus: function (id) {
            var node = document.getElementById(id);
            this.lastFocus = null;
            if (node && typeof node.focus === "function") {
                node.focus();
                this.lastFocus = id;
            }
            return this.lastFocus;
        },

        /** ``true`` while the footer is in "create & add another" mode, which is the default. */
        keepGoing: function () {
            var box = document.getElementById("site-mode");
            return !box || !!box.checked;
        },

        /**
         * Where the caret goes once a site exists.
         *
         * This is the difference the whole footer is about: with the switch on the page stays a
         * place to add the *next* site, and the next site starts with a link in the clipboard -
         * so the caret goes back to the box that takes it. With the switch off the job is done,
         * and the caret goes to the way out instead.
         */
        focusNext: function () {
            return this.focus(this.keepGoing() ? "site-maps-url" : "site-console-link");
        },

        /** The sentence under the switch, said back in its own words. */
        paintMode: function () {
            var note = document.getElementById("site-mode-note");
            if (!note) return;
            note.textContent = this.keepGoing()
                ? "After a save the form clears and the caret returns to the link box."
                : "After a save the form clears and the caret goes to the console link - one site per visit.";
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
            // Live, as it is typed: the HUD answers "did that paste work?" before the mouse
            // goes anywhere near Find it, and the field's complaint waits for the blur.
            url.addEventListener("input", function () { Hud.preview(); });
            url.addEventListener("blur", function () { Hud.commit(); });
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
                    var found = Hud.preview();
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

        // The two dials the hours are read with. ``change`` rather than ``input`` on the time
        // boxes: a time field fires ``input`` on every digit while it is being typed.
        ["site-window-start", "site-window-end"].forEach(function (id) {
            var node = document.getElementById(id);
            if (node) {
                node.addEventListener("change", function () {
                    Pages.complainAt("site-window-error", "");
                    Shift.sync();
                });
            }
        });
        var overnight = document.getElementById("site-overnight");
        if (overnight) overnight.addEventListener("change", function () { Shift.say(); });

        var mode = document.getElementById("site-mode");
        if (mode) mode.addEventListener("change", function () { Pages.paintMode(); });

        var save = document.getElementById("site-save");
        if (save) save.addEventListener("click", function () { Pages.save(); });
        var reset = document.getElementById("site-reset");
        if (reset) {
            reset.addEventListener("click", function () {
                Pages.reset();
                Toast.show("Cleared. Paste another link or click the map.", "info");
                Pages.focusNext();
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
        // The three readings the form opens with: no window, no link, and the footer's own
        // mode said in words.
        Shift.say();
        Pages.paintMode();
        Hud.clear();
        // The picker's options, then the map: neither blocks the fields, and a failure in
        // either is a page that can still be filled in and saved.
        Pages.loadCategories();
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
        Hud: Hud,
        Category: Category,
        Shift: Shift,
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
