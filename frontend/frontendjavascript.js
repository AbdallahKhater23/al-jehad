// =====================================================================
//  Platform / device detection
// =====================================================================
const Platform = {
    get isIOS() {
        return /iPad|iPhone|iPod/.test(navigator.userAgent) ||
            (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
    },
    get isAndroid() { return /Android/i.test(navigator.userAgent); },
    get isStandalone() {
        return window.matchMedia('(display-mode: standalone)').matches || navigator.standalone === true;
    }
};

// =====================================================================
//  Device: decides which of the two dedicated UIs to render.
//  This is a real layout switch (different DOM per mode), not just CSS.
// =====================================================================
const Device = {
    mql: window.matchMedia('(max-width: 767px), (pointer: coarse) and (max-width: 1023px)'),
    get override() { return localStorage.getItem('layoutOverride'); },
    get isMobile() {
        const forced = this.override;
        if (forced === 'mobile') return true;
        if (forced === 'desktop') return false;
        return this.mql.matches;
    },
    get mode() { return this.isMobile ? 'mobile' : 'desktop'; },
    watch(handler) {
        const listener = () => handler(this.mode);
        if (this.mql.addEventListener) this.mql.addEventListener('change', listener);
        else this.mql.addListener(listener);
    }
};

// =====================================================================
//  State
// =====================================================================
const State = {
    user: JSON.parse(sessionStorage.getItem('user')) || null,
    mediaStream: null,
    gpsCoords: null,
    workerTab: localStorage.getItem('workerTab') || 'clock',
    adminTab: 'Live Ops',
    // Held here so switching tabs (or language, which re-renders) does not silently
    // drop the search an admin is in the middle of reading the roster through.
    credentialsQuery: '',
    // Same reason for the notes inbox: an admin working through "what is waiting"
    // should not lose the filter every time they open a note and come back.
    notesQuery: '',
    notesStatus: '',
    // And for the shift board: the board repaints itself every 45 seconds, and an
    // admin halfway through typing a name must not have it wiped under them.
    liveOpsQuery: '',
    liveOpsSite: '',
    liveOpsSort: 'longest',
    theme: localStorage.getItem('theme') || 'light',

    toggleTheme() {
        this.theme = this.theme === 'light' ? 'dark' : 'light';
        localStorage.setItem('theme', this.theme);
        this.applyTheme();
    },
    applyTheme() {
        const dark = this.theme === 'dark';
        document.documentElement.classList.toggle('dark', dark);
        document.documentElement.setAttribute('data-theme', this.theme);
        const meta = document.querySelector('meta[name="theme-color"]');
        if (meta) meta.setAttribute('content', dark ? '#111827' : '#2563eb');
    },
    /** The bearer token. It lives inside the stored user object so a reload keeps it. */
    get token() {
        return (this.user && this.user.token) || null;
    },
    saveUser(userData) {
        this.user = userData;
        sessionStorage.setItem('user', JSON.stringify(userData));
    },
    clearUser() {
        this.user = null;
        sessionStorage.removeItem('user');
        this.stopCamera();
    },
    setWorkerTab(tab) {
        this.workerTab = tab;
        localStorage.setItem('workerTab', tab);
    },
    stopCamera() {
        if (this.mediaStream) {
            this.mediaStream.getTracks().forEach(track => track.stop());
            this.mediaStream = null;
        }
    }
};

// =====================================================================
//  Feedback helpers
// =====================================================================
const Toast = {
    show(message, kind = 'info', ms = 4200) {
        const root = document.getElementById('toastRoot');
        if (!root) { alert(message); return; }
        const el = document.createElement('div');
        el.className = `toast ${kind}`;
        el.textContent = message;
        root.appendChild(el);
        setTimeout(() => {
            el.style.transition = 'opacity .2s ease';
            el.style.opacity = '0';
            setTimeout(() => el.remove(), 220);
        }, ms);
    },
    success(message) { this.show(message, 'success'); },
    error(message) { this.show(message, 'error', 7000); },
    info(message) { this.show(message, 'info'); }
};

const Modal = {
    open(html, { dismissible = true } = {}) {
        const root = document.getElementById('modalRoot');
        root.innerHTML = `<div class="modal-backdrop"><div class="modal-card" role="dialog" aria-modal="true">${html}</div></div>`;
        const backdrop = root.firstElementChild;
        if (dismissible) {
            backdrop.addEventListener('click', (event) => {
                if (event.target === backdrop) Modal.close();
            });
        }
        return backdrop;
    },
    close() {
        const root = document.getElementById('modalRoot');
        if (root) root.innerHTML = '';
    },
    get isOpen() {
        const root = document.getElementById('modalRoot');
        return !!(root && root.firstElementChild);
    }
};

// =====================================================================
//  API Service
// =====================================================================
// Tunnels (ngrok & friends) show an HTML warning page to unrecognised clients.
// Sending this header keeps fetch() calls returning JSON instead of that page.
const TUNNEL_HOST_SUFFIXES = ['ngrok-free.dev', 'ngrok-free.app', 'ngrok.app', 'ngrok.io', 'ngrok.dev'];

//: Endpoints that check a credential sent in the *body* rather than the bearer token.
//:
//: A 401 from one of these is a complaint about that credential, not a dead session, and
//: the difference is the whole message: conflating the two replaced the server's "Invalid
//: credentials or user ID" with "Your session expired", which reads like a server fault
//: and hides the one thing the person can act on (they mistyped their password).
//:
//: ``/attendance/verify`` used to be on this list because it re-checked the worker's
//: password; it no longer takes one, so every 401 it can answer is about the token and
//: belongs in the ordinary "the session is dead" path below.
const BODY_CREDENTIAL_ENDPOINTS = new Set(['/auth/login']);

//: Ports used by static dev servers that host the frontend folder on its own (VS Code
//: Live Server). A page served from one of these is genuinely a different origin from
//: the API, so it is the one case that still points at the API's default port.
const STATIC_DEV_SERVER_PORTS = new Set(['5500', '5501']);

const API = {
    // Resolve at runtime instead of hardcoding http://<host>:8000. Hardcoding
    // HTTP is what broke geolocation/camera on phones (see backend/serve.py),
    // and it would also be blocked as mixed content on an https:// tunnel.
    resolveBaseURL(loc = window.location) {
        const override = localStorage.getItem('apiBaseURL');
        if (override) return override.replace(/\/+$/, '');
        const { protocol, hostname, port, origin } = loc;
        if (protocol === 'file:') return `http://${hostname || 'localhost'}:8000/api/v1`;
        // ``serve.py`` serves the page and the API from a single origin - on the default
        // port, on any ``--port``, on a LAN IP, and behind an HTTPS tunnel (ngrok,
        // Cloudflare, ...) - so the page's own origin is the answer, whatever the port.
        // This used to special-case 80/443/8000 and send every other port to :8000,
        // which made ``serve.py --port 8443`` call a server that was not there - or,
        // worse, a different one that happened to be on 8000: a second database, read
        // and written without a word in the UI.
        if (!STATIC_DEV_SERVER_PORTS.has(port)) return `${origin}/api/v1`;
        // A static dev server hosting the frontend folder on its own: the API really is
        // on another origin. Anything else can set ``localStorage.apiBaseURL``, which is
        // checked above.
        return `${protocol}//${hostname}:8000/api/v1`;
    },
    isTunnelHost(hostname = window.location.hostname) {
        const host = String(hostname || '').toLowerCase();
        return TUNNEL_HOST_SUFFIXES.some(suffix => host === suffix || host.endsWith('.' + suffix));
    },
    get baseURL() {
        if (!this._base) this._base = this.resolveBaseURL();
        return this._base;
    },
    async request(endpoint, options = {}) {
        const headers = { ...options.headers };
        // Never send a placeholder: "Bearer dummy" is a signature failure, so the
        // server answers 401 "Invalid token" and every tap looks like a mystery
        // bug. A missing token is a broken session and is reported as one below.
        const token = State.token;
        if (token) headers['Authorization'] = `Bearer ${token}`;
        // Skip the tunnel's browser-warning interstitial so /api/v1/* always
        // returns JSON, even when the response is cached as HTML.
        if (this.isTunnelHost()) headers['ngrok-skip-browser-warning'] = 'true';

        let body = options.body;
        if (!(body instanceof FormData)) {
            headers['Content-Type'] = 'application/json';
            if (body) body = JSON.stringify(body);
        }

        let response;
        try {
            response = await fetch(`${this.baseURL}${endpoint}`, { ...options, headers, body });
        } catch (networkError) {
            // `.offline` is what lets the attendance flow tell "the server did not
            // answer" (keep the punch locally) from "the server said no" (show it).
            const offlineError = new Error(`${I18n.__('serverUnreachable')} (${this.baseURL})`);
            offlineError.offline = true;
            throw offlineError;
        }
        if (!response.ok) {
            const err = await response.json().catch(() => null);
            // Any other 401 means the session cannot be used - expired, rotated, signed
            // by a previous key, or missing its token entirely. Sign out cleanly and say
            // so, instead of forwarding the server's "Invalid token" (which reads like a
            // server bug and leaves the worker with nothing to do).
            if (response.status === 401 && !BODY_CREDENTIAL_ENDPOINTS.has(endpoint)) {
                if (State.user) {
                    State.clearUser();
                    if (typeof UI !== 'undefined') UI.renderApp();
                }
                throw new Error(I18n.__('sessionExpiredSignInAgain'));
            }
            throw new Error(this.describeError(err, response));
        }
        return response.json();
    },

    /**
     * Save text the page built itself - a CSV assembled from the rows on screen.
     *
     * There is no request here on purpose. A download that has to be re-derived on the
     * server can disagree with the table it was taken from; a file built from the very
     * rows being displayed cannot. The report being exported is the whole aggregated
     * period (the endpoint does not paginate), so building it here loses nothing.
     */
    saveFile(filename, text, mime = 'text/csv;charset=utf-8;') {
        const link = document.createElement('a');
        link.href = URL.createObjectURL(new Blob([text], { type: mime }));
        link.download = filename;
        document.body.appendChild(link);
        link.click();
        link.remove();
    },

    /**
     * A readable message from any error body FastAPI can return.
     *
     * ``detail`` is a string for most endpoints, but the liveness gate and the
     * offline endpoints return ``{error_code, message}`` and a validation failure
     * returns a list. Passing those straight into ``new Error()`` printed
     * "[object Object]" to the worker.
     */
    describeError(payload, response) {
        const detail = payload && payload.detail;
        if (typeof detail === 'string' && detail) return detail;
        if (detail && typeof detail === 'object') {
            if (typeof detail.message === 'string' && detail.message) {
                return detail.error_code ? `${detail.message} (${detail.error_code})` : detail.message;
            }
            if (typeof detail.error_code === 'string' && detail.error_code) return detail.error_code;
            if (Array.isArray(detail)) {
                const parts = detail.map((item) => (item && item.msg) || JSON.stringify(item));
                if (parts.length) return parts.join('; ');
            }
            return JSON.stringify(detail);
        }
        return `HTTP ${response.status}`;
    }
};

// =====================================================================
//  Location service - the part that used to throw "Permission denied (Code 1)"
// =====================================================================
const Location = {
    get isSecure() {
        return window.isSecureContext === true ||
            window.location.protocol === 'https:' ||
            ['localhost', '127.0.0.1', '::1'].includes(window.location.hostname);
    },
    get httpsLink() {
        // Already on HTTPS (e.g. a tunnel)? Then this page IS the secure link.
        if (window.location.protocol === 'https:') return window.location.origin;
        // Otherwise point at the local HTTPS server that serve.py starts on :8000.
        return `https://${window.location.hostname}:8000`;
    },
    get isSupported() { return 'geolocation' in navigator; },

    async permissionState() {
        if (!navigator.permissions || !navigator.permissions.query) return 'unknown';
        try {
            const status = await navigator.permissions.query({ name: 'geolocation' });
            return status.state; // granted | prompt | denied
        } catch (e) {
            return 'unknown';
        }
    },

    /** Resolves "lat,lon" or rejects with the raw GeolocationPositionError. */
    current() {
        return new Promise((resolve, reject) => {
            if (!this.isSupported) {
                reject({ code: 4, message: I18n.__('gpsUnsupported') });
                return;
            }
            navigator.geolocation.getCurrentPosition(
                (position) => resolve(`${position.coords.latitude},${position.coords.longitude}`),
                (error) => reject(error),
                {
                    enableHighAccuracy: true,
                    timeout: 20000,
                    // A 30s cached fix is what makes check-in work indoors / in a
                    // container park where a fresh GPS lock can take minutes.
                    maximumAge: 30000
                }
            );
        });
    },

    platformSteps() {
        if (Platform.isIOS) {
            return [
                I18n.__('iosStep1'),
                I18n.__('iosStep2'),
                I18n.__('iosStep3')
            ];
        }
        if (Platform.isAndroid) {
            return [
                I18n.__('androidStep1'),
                I18n.__('androidStep2'),
                I18n.__('androidStep3')
            ];
        }
        return [
            I18n.__('desktopStep1'),
            I18n.__('desktopStep2'),
            I18n.__('desktopStep3')
        ];
    }
};

const Camera = {
    get isSupported() { return !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia); },
    async start() {
        State.stopCamera();
        const stream = await navigator.mediaDevices.getUserMedia({
            audio: false,
            // 'user' (front camera) is the right one for a selfie check-in,
            // and it also works on a laptop webcam.
            video: { facingMode: 'user', width: { ideal: 1280 }, height: { ideal: 960 } }
        });
        State.mediaStream = stream;
        return stream;
    },
    explain(error) {
        const name = error && error.name;
        if (name === 'NotAllowedError' || name === 'SecurityError') {
            return Location.isSecure
                ? { title: I18n.__('cameraBlockedTitle'), steps: Location.platformSteps() }
                : { title: I18n.__('insecureTitle'), steps: [I18n.__('insecureCamera')] };
        }
        if (name === 'NotFoundError' || name === 'DevicesNotFoundError') {
            return { title: I18n.__('cameraMissingTitle'), steps: [I18n.__('cameraMissingBody')] };
        }
        if (name === 'NotReadableError') {
            return { title: I18n.__('cameraBusyTitle'), steps: [I18n.__('cameraBusyBody')] };
        }
        return { title: I18n.__('cameraFailedTitle'), steps: [String((error && error.message) || error)] };
    }
};

// =====================================================================
//  UI Controller
// =====================================================================
// ---------------------------------------------------------------------
//  The console: what it is made of
// ---------------------------------------------------------------------
//  Eight tabs used to hang off one flat list, in the order they were built.
//  They are three groups now, because the console answers exactly three
//  questions and always has: who is on site (Operations), who is allowed in
//  (People), and how the system is set up (Configuration).
//
//  The icon on each tab is inline SVG and never an emoji: an emoji cannot take
//  a token colour, renders differently on every phone, and in a right-to-left
//  layout it may or may not mirror - a nav that shifts shape between the three
//  languages this app speaks is a nav nobody can learn.
const ADMIN_ICONS = {
    liveOps: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 12h3.5l2-6 4 12 2-6H21"></path></svg>',
    approvals: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"></circle><path d="m8.5 12.5 2.5 2.5 4.5-5.5"></path></svg>',
    sites: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 21s7-5.7 7-11a7 7 0 1 0-14 0c0 5.3 7 11 7 11Z"></path><circle cx="12" cy="10" r="2.5"></circle></svg>',
    shifts: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="5" width="18" height="16" rx="2"></rect><path d="M8 3v4M16 3v4M3 11h18"></path></svg>',
    credentials: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="8" cy="15" r="4"></circle><path d="m11 12 8-8M17 6l2 2M15 8l2 2"></path></svg>',
    links: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M10.5 13.5a4.5 4.5 0 0 0 6.4 0l2.6-2.6a4.5 4.5 0 0 0-6.4-6.4l-1 1"></path><path d="M13.5 10.5a4.5 4.5 0 0 0-6.4 0l-2.6 2.6a4.5 4.5 0 0 0 6.4 6.4l1-1"></path></svg>',
    notes: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12a8 8 0 0 1-8 8H8l-5 3 1.4-4.3A8 8 0 1 1 21 12Z"></path></svg>',
    admin: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 6h8M16 6h4M4 12h4M12 12h8M4 18h8M16 18h4"></path><circle cx="14" cy="6" r="2"></circle><circle cx="10" cy="12" r="2"></circle><circle cx="14" cy="18" r="2"></circle></svg>',
    info: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"></circle><path d="M12 11v5"></path><path d="M12 8h.01"></path></svg>',
    alert: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M10.3 4.3 2.6 17.5A2 2 0 0 0 4.3 20.5h15.4a2 2 0 0 0 1.7-3L13.7 4.3a2 2 0 0 0-3.4 0Z"></path><path d="M12 9v4"></path><path d="M12 16.5h.01"></path></svg>',
    theme: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 14.5A8.5 8.5 0 0 1 9.5 4a8.5 8.5 0 1 0 10.5 10.5Z"></path></svg>',
    logout: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 4h3a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-3"></path><path d="M10 8l-4 4 4 4"></path><path d="M6 12h9"></path></svg>'
};

// ---------------------------------------------------------------------
//  The handset: what it is made of
// ---------------------------------------------------------------------
//  The same rule as the console, one screen closer to the thumb: **icons are
//  inline SVG and never emoji.** The worker app used to be four emoji in a tab
//  bar, a moon for the theme switch and a pin and a camera in the device panel.
//  An emoji cannot take a token colour, so the active tab could not darken with
//  the theme; it is drawn by the phone's own font, so the same app showed four
//  different pictures on four different handsets; and a screen reader announces
//  it as its Unicode name, which turned the nav into "spiral calendar, Clock".
//
//  * ``clockIn`` / ``clockOut`` are one picture mirrored - an arrow crossing a
//    doorway, pointing in or out - so the two states are told apart by the
//    direction of travel and not only by green-and-red, which is the pair a
//    colour-blind worker on a bright screen is most likely to lose.
const HAND_ICONS = {
    clock: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"></circle><path d="M12 7v5l3.5 2"></path></svg>',
    history: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="4" y="3" width="16" height="18" rx="2"></rect><path d="M8 8h8M8 12h8M8 16h5"></path></svg>',
    notes: ADMIN_ICONS.notes,
    profile: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="8" r="4"></circle><path d="M4.5 20.5a7.5 7.5 0 0 1 15 0"></path></svg>',
    clockIn: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 4h3a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-3"></path><path d="M4 12h9"></path><path d="m9.5 8.5 3.5 3.5-3.5 3.5"></path></svg>',
    clockOut: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M10 4H7a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h3"></path><path d="M15 12H6"></path><path d="m10.5 8.5-3.5 3.5 3.5 3.5"></path></svg>',
    pin: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 21s7-5.7 7-11a7 7 0 1 0-14 0c0 5.3 7 11 7 11Z"></path><circle cx="12" cy="10" r="2.5"></circle></svg>',
    camera: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 8h2.5L8 6h8l1.5 2H20a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V9a1 1 0 0 1 1-1Z"></path><circle cx="12" cy="13" r="3"></circle></svg>',
    alert: ADMIN_ICONS.alert,
    info: ADMIN_ICONS.info,
    back: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m14.5 6-6 6 6 6"></path></svg>',
    cloudOff: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M7 18h9.5a4.5 4.5 0 0 0 .6-8.96A6 6 0 0 0 6.2 8.3"></path><path d="M4 20 20 4"></path></svg>'
};

//  A code the API sent, in the reader's language - shared by the console and the handset,
//  because both show the same two enums and both got this wrong the same way.
//
//  ``I18n.__`` answers with the *key* when a translation is missing, and these builds lag
//  the API: a note can be filed under a category, or move to a status, this frontend
//  predates. The naive ``I18n.__(`noteCat_${category}`)`` then printed the key itself, so
//  a card read ``noteCat_undefined`` (a field the server did not send) or
//  ``noteStatus_archived`` (a status added server-side later). A code with no wording is
//  shown opened up - ``tool_allowance`` reads "Tool allowance", which is the meaning
//  somebody typed into the enum - and a code that is simply absent renders as nothing, so
//  the caller can drop the chip rather than draw an empty one.
function codeLabel(namespace, code) {
    if (code === null || code === undefined || code === '') return '';
    const key = `${namespace}_${code}`;
    const label = I18n.__(key);
    if (label !== key) return label;
    const words = String(code).replace(/[_-]+/g, ' ').trim();
    return words ? words.charAt(0).toUpperCase() + words.slice(1) : '';
}

//: The company this app belongs to, and its lockup, exactly as the artwork sets it.
//: Deliberately *not* in i18n.js: a wordmark, a legal suffix and a founding year are
//: proper nouns, and a translated wordmark is a different logo. The mark is a local
//: SVG rather than the supplied PNG because that PNG carries its own dark-green
//: rectangle - which would be a second, slightly-off green on the login panel, and a
//: green box on the console's rail. See frontend/logo-mark.svg for the geometry.
const BRAND = {
    name: 'AL-JEHAD',
    legal: 'INTERNATIONAL CO.',
    est: 'EST 1983',
    tagline: 'STONE . MARBLE . GRANITE',
    mark: 'logo-mark.svg'
};

//  The array order is the order the tabs were built in, and it is what the
//  rest of the app iterates (``test_frontend_credentials`` pins it as the
//  console's inventory). What the *nav* draws is the grouped order below - the
//  two are deliberately separate, so re-ordering the rail is a decision about
//  the rail and not a rename of the tab list.
const ADMIN_TABS = [
    { id: 'Live Ops', key: 'activeShifts', hint: 'hintLiveOps', group: 'navGroupOperations', icon: 'liveOps' },
    { id: 'Approvals', key: 'pendingReviews', hint: 'hintApprovals', group: 'navGroupOperations', icon: 'approvals' },
    { id: 'Sites', key: 'sites', hint: 'hintSites', group: 'navGroupConfig', icon: 'sites' },
    { id: 'Shifts', key: 'shifts', hint: 'hintShifts', group: 'navGroupConfig', icon: 'shifts' },
    // Was three tabs - Users, Pass, Enroll - of which only one did anything: the Users
    // tab, whose single action was capturing an enrollment photo. Accounts, access and
    // passwords belong on one screen, so they are one tab now.
    { id: 'Credentials', key: 'credentials', hint: 'hintCredentials', group: 'navGroupPeople', icon: 'credentials' },
    // Quick clock links: a link that clocks one named worker in and out with no password.
    // Sits beside Credentials because both tabs answer "how does this person get access",
    // and this is the answer that hands somebody a credential rather than a password.
    { id: 'Links', key: 'quickLinks', hint: 'hintLinks', group: 'navGroupPeople', icon: 'links' },
    // What workers and moallems are asking for, in writing. Sits beside Credentials
    // because the most common request - "my password stopped working" - is answered
    // with that tab's reset, straight from the note.
    { id: 'Notes', key: 'notes', hint: 'hintNotes', group: 'navGroupPeople', icon: 'notes' },
    { id: 'Admin', key: 'admin', hint: 'hintAdmin', group: 'navGroupConfig', icon: 'admin' }
];

//: How the rail is grouped, in the order the rail shows them. Operations first
//: because it is the screen an admin lives on during the shift, Configuration
//: last because it is the one they open twice a year.
const ADMIN_GROUPS = ['navGroupOperations', 'navGroupPeople', 'navGroupConfig'];

/** The tabs, grouped for the nav. One source, so the rail and the phone strip agree. */
function adminNavGroups() {
    return ADMIN_GROUPS
        .map((key) => ({ key, tabs: ADMIN_TABS.filter((tab) => tab.group === key) }))
        .filter((group) => group.tabs.length > 0);
}

//: The tab the console is on, or the first one. Used by the header and the
//: nav, both of which read the same record so they can never disagree.
function adminTabRecord(tab) {
    return ADMIN_TABS.find((entry) => entry.id === tab) || ADMIN_TABS[0];
}

const UI = {
    get appContainer() { return document.getElementById('app'); },

    async init() {
        State.applyTheme();
        I18n.applyDirection();
        // A session restored from a build that never stored the token cannot
        // authenticate anything, so clear it before the first tap rather than
        // letting every action fail with "Invalid token".
        if (State.user && !State.token) {
            State.clearUser();
            Toast.info(I18n.__('sessionExpiredSignInAgain'));
        }
        // Re-render when the viewport crosses the mobile/desktop breakpoint
        // (phone rotate, window resize, tablet keyboard, ...).
        Device.watch(() => this.renderApp());
        // A shared shifts link (#shifts=2026-08-01..2026-08-31) is adopted before the
        // first render, so opening one lands on that period instead of Live Ops. The
        // listener is what makes a link pasted into *this* tab (or a back-button step)
        // work without a reload - the app itself never navigates.
        this.applyShiftsLink();
        window.addEventListener('hashchange', () => this.applyShiftsLink());
        this.renderApp();
        // Registers this phone as a signing device, refreshes the server-signed time
        // anchor and drains anything left queued from a previous offline stint.
        // Deliberately not awaited: it must never delay the first paint.
        if (typeof OFFLINE !== 'undefined') OFFLINE.init().catch(() => {});
    },

    renderApp() {
        const container = this.appContainer;
        if (!State.user) {
            container.className = '';
            return this.renderLogin();
        }
        if (State.user.role === 'worker') return this.renderWorkerPortal();
        return this.renderAdminConsole();
    },

    langSelectHtml() {
        const options = [['en', 'EN'], ['ar', 'AR'], ['hi', 'HI']];
        return `<select onchange="I18n.setLang(this.value)" class="icon-button" style="width:auto;padding:0 8px" aria-label="${I18n.__('lang')}">
            ${options.map(([code, label]) =>
                `<option value="${code}" ${I18n.lang === code ? 'selected' : ''}>${label}</option>`).join('')}
        </select>`;
    },

    // -----------------------------------------------------------------
    //  Login (works in both layouts)
    // -----------------------------------------------------------------
    //  Two panels: the company's lockup on the left, the form on the right. The
    //  branding is not decoration here - a worker on site cellular is deciding
    //  whether this is the app their administrator told them about, and a wall of
    //  unbranded inputs does not answer that. On a phone the lockup collapses to a
    //  header strip so the form keeps the whole screen.
    renderLogin() {
        const container = this.appContainer;
        container.className = 'login-shell';
        container.innerHTML = `
            <header class="login-topbar">
                <button type="button" onclick="UI.toggleTheme()" class="icon-button" aria-label="${this.escapeHtml(I18n.__('theme'))}">${ADMIN_ICONS.theme}</button>
                ${this.langSelectHtml()}
            </header>
            <main class="login-stage">
                <section class="login-card">
                    <div class="login-brand">
                        <img class="login-mark" src="${BRAND.mark}" alt="" width="78" height="103" decoding="async">
                        <!-- dir="ltr" is load-bearing: in Arabic the page is RTL, and a
                             trailing period on "INTERNATIONAL CO." is bidi-neutral, so the
                             browser parks it at the *left* of the Latin run. A logo does
                             not mirror. -->
                        <div class="login-lockup" dir="ltr">
                            <p class="login-company">${this.escapeHtml(BRAND.name)}</p>
                            <p class="login-legal">${this.escapeHtml(BRAND.legal)}</p>
                            <p class="login-est">${this.escapeHtml(BRAND.est)}</p>
                            <p class="login-tagline">${this.escapeHtml(BRAND.tagline)}</p>
                        </div>
                    </div>
                    <div class="login-form-panel">
                        <div>
                            <h1 class="login-title">${this.escapeHtml(I18n.__('login'))}</h1>
                            <p class="login-subtitle" style="margin-top:6px">${this.escapeHtml(I18n.__('loginIntro'))}</p>
                        </div>
                        <form id="loginForm" class="login-form">
                            <div class="login-field">
                                <label class="ui-label" for="userId">${this.escapeHtml(I18n.__('userId'))}</label>
                                <input class="ui-field" type="text" id="userId" inputmode="numeric" autocomplete="username" required>
                            </div>
                            <div class="login-field">
                                <label class="ui-label" for="email">${this.escapeHtml(I18n.__('emailOrPhone'))}</label>
                                <input class="ui-field" type="text" id="email" autocomplete="username" required>
                            </div>
                            <div class="login-field">
                                <label class="ui-label" for="password">${this.escapeHtml(I18n.__('password'))}</label>
                                <input class="ui-field" type="password" id="password" autocomplete="current-password" required>
                            </div>
                            <button type="submit" class="ui-btn ui-btn-primary login-submit">${this.escapeHtml(I18n.__('login'))}</button>
                        </form>
                        ${!Location.isSecure ? `<p class="login-insecure" role="alert">${ADMIN_ICONS.alert}<span>${this.escapeHtml(I18n.__('insecureWarning'))}</span></p>` : ''}
                        <p class="login-note">${ADMIN_ICONS.info}<span>${this.escapeHtml(I18n.__('loginHelp'))}</span></p>
                    </div>
                </section>
            </main>
            <footer class="login-footer">${this.escapeHtml(I18n.__('companyFooter'))}</footer>`;
        document.getElementById('loginForm').addEventListener('submit', async (e) => {
            e.preventDefault();
            const button = e.target.querySelector('button');
            button.disabled = true;
            try {
                const res = await API.request('/auth/login', {
                    method: 'POST',
                    body: {
                        user_id: document.getElementById('userId').value,
                        email_or_phone: document.getElementById('email').value,
                        password: document.getElementById('password').value
                    }
                });
                // The token is returned beside "user", not inside it. Storing only
                // res.user is what left every later request unauthenticated.
                const token = res.token || res.access_token;
                if (!token) throw new Error(I18n.__('loginNoToken'));
                State.saveUser({ ...res.user, token });
                this.renderApp();
                // Signed in over a working connection: this is the cheapest moment to
                // register the device and pick up a time anchor.
                if (typeof OFFLINE !== 'undefined') OFFLINE.ensureReady().catch(() => {});
            } catch (err) {
                Toast.error(err.message);
            } finally {
                button.disabled = false;
            }
        });
    },

    // -----------------------------------------------------------------
    //  Worker portal - two dedicated layouts
    // -----------------------------------------------------------------
    async renderWorkerPortal() {
        if (Device.isMobile) await this.renderWorkerMobile();
        else await this.renderWorkerDesktop();
    },

    /**
     * The strip that says who is holding the phone.
     *
     * The mark sits on the company's green - the same treatment the console's rail
     * gives it - because this is the one place on the worker's screen that belongs to
     * the company rather than to the job. The name is the administrator's spelling of
     * it, so it goes through ``escapeHtml`` like every other value off the wire.
     *
     * The two controls are icon buttons carrying a ``aria-label``: the moon and the
     * escape arrow that used to be the button's *content* are SVG now, so the label is
     * the only thing a screen reader gets and it is a real word in all three languages.
     */
    workerHeaderHtml() {
        return `
            <span class="hand-brand" aria-hidden="true"><img src="${BRAND.mark}" alt=""></span>
            <div class="hand-who">
                <p class="hand-who-name">${this.escapeHtml(State.user.name || '')}</p>
                <p class="hand-who-role">${this.escapeHtml(I18n.__(State.user.role === 'moallem' ? 'roleMoallem' : 'roleWorker'))}</p>
            </div>
            <div class="hand-actions">
                ${this.langSelectHtml()}
                <button type="button" onclick="UI.toggleTheme()" class="icon-button" aria-label="${this.escapeHtml(I18n.__('theme'))}">${ADMIN_ICONS.theme}</button>
                <button type="button" onclick="UI.logout()" class="icon-button" aria-label="${this.escapeHtml(I18n.__('logout'))}">${ADMIN_ICONS.logout}</button>
            </div>`;
    },

    //: The worker's bottom tabs. ``notes`` is the written channel to the admin: a
    //: password request, a missing item, a question about hours - each of which used to
    //: require catching somebody on the phone, and then leaving no record of it.
    workerTabIds: ['clock', 'history', 'notes', 'profile'],

    workerTabBarHtml() {
        const tabs = [
            { id: 'clock', icon: 'clock', label: I18n.__('clock') },
            { id: 'history', icon: 'history', label: I18n.__('history') },
            { id: 'notes', icon: 'notes', label: I18n.__('notes') },
            { id: 'profile', icon: 'profile', label: I18n.__('profile') }
        ];
        // ``aria-current`` rather than a class is what marks the tab: it is the attribute
        // a screen reader answers "where am I" with, and the styling hangs off it, so the
        // two can never disagree. The handlers are one delegated listener (see
        // ``bindWorkerTabs``) instead of an ``onclick`` per button - the document CSP still
        // allows inline attributes, and every one removed is closer to removing it.
        return `<nav class="hand-tabs" aria-label="${this.escapeHtml(I18n.__('handSections'))}">
            ${tabs.map(tab => `
                <button type="button" data-worker-tab="${tab.id}"
                        ${State.workerTab === tab.id ? 'aria-current="page"' : ''}>
                    ${HAND_ICONS[tab.icon]}
                    <span>${this.escapeHtml(tab.label)}</span>
                </button>`).join('')}
        </nav>`;
    },

    /** One listener for the whole tab bar, so a tab needs no inline handler. */
    bindWorkerTabs(root) {
        const nav = (root || document).querySelector('.hand-tabs');
        if (!nav) return;
        nav.addEventListener('click', (event) => {
            const button = event.target.closest('button[data-worker-tab]');
            if (button) this.setWorkerTab(button.getAttribute('data-worker-tab'));
        });
    },

    /**
     * Mobile: the shell a worker holds at the gate.
     *
     * Sticky header, one column, and the four destinations in a fixed bottom bar -
     * the shape the thumb already knows. What changed is what the bar is made of: the
     * tabs now carry ``aria-current`` and one delegated listener rather than an
     * ``onclick`` each, so switching tabs costs no inline script and the bar answers a
     * screen reader's "where am I" instead of only styling the answer.
     */
    async renderWorkerMobile() {
        const container = this.appContainer;
        container.className = 'hand-app';
        container.innerHTML = `
            <header class="hand-header">
                <div class="hand-header-inner">${this.workerHeaderHtml()}</div>
            </header>
            <main class="hand-main" id="workerMain"></main>
            ${this.workerTabBarHtml()}
        `;
        this.bindWorkerTabs(container);
        await this.renderWorkerView(State.workerTab);
    },

    /** Desktop: the same screen, wide - the shift on the left, the record on the right. */
    async renderWorkerDesktop() {
        const container = this.appContainer;
        container.className = 'hand-app';
        container.innerHTML = `
            <header class="hand-header">
                <div class="hand-header-inner">${this.workerHeaderHtml()}</div>
            </header>
            <div class="hand-desk">
                <div class="hand-desk-col">
                    <div class="hand-card"><div id="workerDashboard"></div></div>
                    <div class="hand-card"><div id="devicePanel"></div></div>
                </div>
                <div class="hand-desk-col">
                    <section class="hand-card">
                        <div class="hand-section-head">
                            <h3 class="hand-section-title">${this.escapeHtml(I18n.__('timesheetHistory'))}</h3>
                        </div>
                        <div id="historyTable">${this.loadingHtml()}</div>
                    </section>
                    <section class="hand-card"><div id="workerNotes"></div></section>
                </div>
            </div>
        `;
        await WORKER_MODULES.renderClockPanel(document.getElementById('workerDashboard'));
        await WORKER_MODULES.renderHistory(document.getElementById('historyTable'));
        await WORKER_MODULES.renderNotes(document.getElementById('workerNotes'));
        this.renderDevicePanel(document.getElementById('devicePanel'));
    },

    setWorkerTab(tab) {
        State.setWorkerTab(tab);
        if (Device.isMobile) {
            // The bar is repainted from the same source that drew it, so the aria-current
            // and what is on screen cannot disagree after a tab switch.
            const nav = document.querySelector('.hand-tabs');
            if (nav) nav.outerHTML = this.workerTabBarHtml();
            this.bindWorkerTabs(this.appContainer);
            this.renderWorkerView(tab);
        } else {
            this.renderWorkerPortal();
        }
    },

    async renderWorkerView(tab) {
        const main = document.getElementById('workerMain');
        if (!main) return;
        main.innerHTML = this.loadingHtml();
        if (tab === 'history') {
            main.innerHTML = `<div id="historyTable"></div>`;
            await WORKER_MODULES.renderHistory(document.getElementById('historyTable'));
        } else if (tab === 'notes') {
            main.innerHTML = `<div id="workerNotes"></div>`;
            await WORKER_MODULES.renderNotes(document.getElementById('workerNotes'));
        } else if (tab === 'profile') {
            main.innerHTML = `<div id="workerProfile"></div><div id="devicePanel"></div>`;
            this.renderWorkerProfile(document.getElementById('workerProfile'));
            this.renderDevicePanel(document.getElementById('devicePanel'));
        } else {
            main.innerHTML = `<div id="workerDashboard"></div><div id="devicePanel"></div>`;
            await WORKER_MODULES.renderClockPanel(document.getElementById('workerDashboard'));
            this.renderDevicePanel(document.getElementById('devicePanel'));
        }
    },

    /**
     * What this phone knows about the account, and the one way out of it.
     *
     * Two cards rather than one long one, because the questions are different: the
     * account (id, name, role) is set by an administrator and the worker can only read
     * it, while the preferences below are the worker's own switches. Logging out keeps
     * the danger treatment - and its own full-width button, because "log out" is the
     * one thing on this screen that must never be tapped by accident in a pocket.
     */
    renderWorkerProfile(container) {
        const roleLabel = I18n.__(State.user.role === 'moallem' ? 'roleMoallem'
            : (State.user.role === 'admin' ? 'admin' : 'roleWorker'));
        container.innerHTML = `
            <section class="hand-card">
                <div class="hand-section-head">
                    <h3 class="hand-section-title">${this.escapeHtml(I18n.__('handAccount'))}</h3>
                </div>
                <dl class="hand-dl">
                    <div class="hand-dl-row">
                        <dt>${this.escapeHtml(I18n.__('userId'))}</dt>
                        <dd class="is-mono">${this.escapeHtml(State.user.id)}</dd>
                    </div>
                    <div class="hand-dl-row">
                        <dt>${this.escapeHtml(I18n.__('name'))}</dt>
                        <dd>${this.escapeHtml(State.user.name || '-')}</dd>
                    </div>
                    <div class="hand-dl-row">
                        <dt>${this.escapeHtml(I18n.__('role'))}</dt>
                        <dd>${this.escapeHtml(roleLabel)}</dd>
                    </div>
                </dl>
            </section>
            <section class="hand-card">
                <div class="hand-section-head">
                    <h3 class="hand-section-title">${this.escapeHtml(I18n.__('handPreferences'))}</h3>
                </div>
                <dl class="hand-dl">
                    <div class="hand-dl-row">
                        <dt>${this.escapeHtml(I18n.__('lang'))}</dt>
                        <dd>${this.langSelectHtml()}</dd>
                    </div>
                    <div class="hand-dl-row">
                        <dt>${this.escapeHtml(I18n.__('theme'))}</dt>
                        <dd><button type="button" onclick="UI.toggleTheme()" class="icon-button"
                                    aria-label="${this.escapeHtml(I18n.__('theme'))}">${ADMIN_ICONS.theme}</button></dd>
                    </div>
                </dl>
                <button type="button" onclick="UI.logout()"
                        class="ui-btn ui-btn-danger hand-logout">${ADMIN_ICONS.logout}${this.escapeHtml(I18n.__('logout'))}</button>
            </section>`;
    },

    /**
     * The wait, as a skeleton of the thing that is coming.
     *
     * A spinner in the middle of an empty page answers one question ("is it working?") when
     * the two that matter are "is it working" and "where will the answer appear". Three
     * bars of the shape the content will take, at the height it will take, answer both -
     * and because the skeleton occupies the space the content will, nothing jumps when the
     * request lands. The bars are decorative; the sentence is what a screen reader gets.
     */
    loadingHtml(message) {
        return `<div class="ui-skeleton" aria-busy="true">
            <div class="ui-skeleton-row"><span class="ui-skeleton-bar" style="width:38%"></span></div>
            <div class="ui-skeleton-row"><span class="ui-skeleton-bar" style="width:72%"></span></div>
            <div class="ui-skeleton-row"><span class="ui-skeleton-bar" style="width:56%"></span></div>
            <p class="sr-only">${this.escapeHtml(message || I18n.__('loading'))}</p>
        </div>`;
    },

    // -----------------------------------------------------------------
    //  Device readiness panel (GPS + camera self-diagnostic)
    // -----------------------------------------------------------------
    async renderDevicePanel(container) {
        if (!container) return;
        const permState = await Location.permissionState();
        const gpsOk = Location.isSupported && Location.isSecure && permState !== 'denied';
        const gpsClass = gpsOk ? 'ready' : (Location.isSecure ? 'warn' : 'blocked');
        const gpsText = !Location.isSecure ? I18n.__('gpsInsecure')
            : (permState === 'denied' ? I18n.__('gpsDenied') : I18n.__('gpsReady'));
        const camOk = Camera.isSupported && Location.isSecure;
        const camClass = camOk ? 'ready' : (Camera.isSupported ? 'blocked' : 'warn');
        const camText = !Camera.isSupported ? I18n.__('cameraUnsupported')
            : (Location.isSecure ? I18n.__('cameraReady') : I18n.__('cameraInsecure'));

        // A readiness figure the worker can act on: the dot and the border carry the state,
        // the words carry it for anyone who cannot see either, and the two test buttons are
        // the action the state implies - "GPS is not working" is not actionable, "test it" is.
        const pill = (icon, klass, text) => `
            <span class="ui-badge ${klass}">${HAND_ICONS[icon]}${this.escapeHtml(text)}</span>`;
        const badgeClass = (state) => state === 'ready' ? 'is-ok' : (state === 'warn' ? 'is-warn' : 'is-danger');
        container.innerHTML = `
            <div class="hand-section-head">
                <h3 class="hand-section-title">${this.escapeHtml(I18n.__('handThisPhone'))}</h3>
                <p class="hand-section-note">${this.escapeHtml(I18n.__('deviceStatus'))}</p>
            </div>
            <div class="ui-row">
                ${pill('pin', badgeClass(gpsClass), gpsText)}
                ${pill('camera', badgeClass(camClass), camText)}
            </div>
            <div class="ui-row" style="margin-top:12px">
                <button type="button" onclick="UI.testLocation()" class="ui-btn ui-btn-sm">${HAND_ICONS.pin}${this.escapeHtml(I18n.__('checkLocation'))}</button>
                <button type="button" onclick="UI.testCamera()" class="ui-btn ui-btn-sm">${HAND_ICONS.camera}${this.escapeHtml(I18n.__('checkCamera'))}</button>
            </div>
            ${!Location.isSecure ? `
                <div class="hand-alert" style="margin-top:12px">
                    ${HAND_ICONS.alert}
                    <div>
                        <p><strong>${this.escapeHtml(I18n.__('insecureTitle'))}</strong></p>
                        <p>${this.escapeHtml(I18n.__('insecureBody'))}</p>
                        <p class="is-mono" style="word-break:break-all">${this.escapeHtml(Location.httpsLink)}</p>
                        <button type="button" onclick="UI.copyHttpsLink()" class="ui-btn ui-btn-sm ui-btn-primary">${this.escapeHtml(I18n.__('copyLink'))}</button>
                    </div>
                </div>` : ''}`;
    },

    async testLocation() {
        Toast.info(I18n.__('gettingLocation'));
        try {
            const coords = await Location.current();
            Toast.success(`${I18n.__('locationOk')} ${coords}`);
            this.refreshPanels();
        } catch (err) {
            await this.showLocationHelp(err);
        }
    },

    async testCamera() {
        try {
            await Camera.start();
            State.stopCamera();
            Toast.success(I18n.__('cameraOk'));
        } catch (err) {
            const info = Camera.explain(err);
            this.showHelpModal(info.title, info.steps);
        }
    },

    refreshPanels() {
        const panel = document.getElementById('devicePanel');
        if (panel) this.renderDevicePanel(panel);
    },

    copyHttpsLink() {
        const link = Location.httpsLink;
        const done = () => Toast.success(I18n.__('copied'));
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(link).then(done).catch(() => prompt(I18n.__('copyLink'), link));
        } else {
            prompt(I18n.__('copyLink'), link);
        }
    },

    // -----------------------------------------------------------------
    //  Help modals
    // -----------------------------------------------------------------
    showHelpModal(title, steps, extraHtml = '') {
        Modal.open(`
            <h3 class="text-xl font-bold mb-3">${title}</h3>
            ${steps.map((step, index) => `
                <div class="help-step"><span class="help-num">${index + 1}</span><span>${step}</span></div>`).join('')}
            ${extraHtml}
            <button onclick="Modal.close()" class="mt-4 w-full py-3 rounded-xl bg-blue-600 text-white font-bold">${I18n.__('close')}</button>
        `);
    },

    /**
     * Shown when the browser refuses to hand over coordinates.
     * Resolves with a "lat,lon" / Google Maps string, or null if the worker gives up.
     */
    async showLocationHelp(err) {
        // The worker must choose one of the buttons below, so this one does not
        // close when the backdrop is tapped (it would leave the promise hanging).
        const permState = await Location.permissionState();
        const code = err && err.code;

        let title, body, steps;
        if (!Location.isSecure) {
            title = I18n.__('insecureTitle');
            body = I18n.__('insecureBody');
            steps = [I18n.__('insecureStep1')];
        } else if (permState === 'denied' || code === 1) {
            title = I18n.__('gpsBlockedTitle');
            body = I18n.__('gpsBlockedBody');
            steps = Location.platformSteps();
        } else if (code === 2) {
            title = I18n.__('gpsUnavailableTitle');
            body = I18n.__('gpsUnavailableBody');
            steps = [I18n.__('gpsUnavailableStep1'), I18n.__('gpsUnavailableStep2')];
        } else if (code === 3) {
            title = I18n.__('gpsTimeoutTitle');
            body = I18n.__('gpsTimeoutBody');
            steps = [I18n.__('gpsUnavailableStep1'), I18n.__('gpsUnavailableStep2')];
        } else {
            title = I18n.__('gpsFailedTitle');
            body = I18n.__('gpsFailedBody');
            steps = Location.platformSteps();
        }

        return new Promise((resolve) => {
            Modal.open(`
                <h3 class="text-xl font-bold mb-2">${title}</h3>
                <p class="text-sm text-gray-600 dark:text-gray-300 mb-4">${body}</p>
                ${steps.map((step, index) => `
                    <div class="help-step"><span class="help-num">${index + 1}</span><span>${step}</span></div>`).join('')}
                ${!Location.isSecure ? `
                    <div class="my-4 p-3 rounded-xl bg-amber-50 dark:bg-amber-900/25 text-sm">
                        <p class="font-semibold mb-1">${I18n.__('openThisLink')}</p>
                        <p class="font-mono break-all">${Location.httpsLink}</p>
                        <button onclick="UI.copyHttpsLink()" class="mt-2 px-3 py-2 rounded-lg bg-amber-500 text-white font-semibold">${I18n.__('copyLink')}</button>
                        <p class="mt-2 text-xs">${I18n.__('insecureTunnelHint')}</p>
                    </div>` : ''}
                <hr class="my-4 border-gray-200 dark:border-gray-700">
                <p class="text-xs text-gray-500 dark:text-gray-400 mb-4">${I18n.__('gpsBlockedBody')}</p>
                <div class="flex flex-col sm:flex-row gap-2">
                    <button id="retryGps" class="flex-1 py-3 rounded-xl bg-blue-600 text-white font-bold">${I18n.__('retry')}</button>
                </div>
                <button id="cancelLocation" class="mt-2 w-full py-2 rounded-xl text-gray-500 dark:text-gray-400">${I18n.__('cancel')}</button>
            `, { dismissible: false });

            const retryButton = document.getElementById('retryGps');
            const finish = (value) => { Modal.close(); resolve(value); };

            retryButton.addEventListener('click', async () => {
                retryButton.disabled = true;
                retryButton.textContent = I18n.__('gettingLocation');
                try {
                    // Browsers only prompt again on a fresh user gesture - a button
                    // click *is* that gesture, so retry genuinely can re-prompt.
                    const coords = await Location.current();
                    finish(coords);
                } catch (retryError) {
                    retryButton.disabled = false;
                    retryButton.textContent = I18n.__('retry');
                    Toast.error(retryError.code === 4 ? I18n.__('gpsUnsupported') : I18n.__('stillBlocked'));
                }
            });

            document.getElementById('cancelLocation').addEventListener('click', () => finish(null));
        });
    },

    // -----------------------------------------------------------------
    //  Attendance flow
    // -----------------------------------------------------------------
    async doAttendance(action) {
        if (!State.user) return;
        // An impatient double tap must not open a second camera. Two overlays mean two
        // live streams (battery and heat on a phone in the sun), two shutters on top of
        // each other, and the worker's second frame queued as a second punch. The flag
        // covers the GPS wait; the overlay check covers everything after it.
        if (this._attendanceBusy || this._cameraOpen) return;
        this._attendanceBusy = true;
        try {
            const coords = await this.acquireLocation();
            if (!coords) return;
            this.openCamera(action, coords);
        } finally {
            this._attendanceBusy = false;
        }
    },

    async acquireLocation() {
        try {
            return await Location.current();
        } catch (err) {
            console.warn('Geolocation failed:', err);
            return await this.showLocationHelp(err);
        }
    },

    async openCamera(action, coords) {
        const actionKey = action === 'Clock In' ? 'clockIn' : 'clockOut';

        if (!Camera.isSupported) {
            const info = Camera.explain({ name: 'NotAllowedError' });
            this.showHelpModal(info.title, info.steps);
            return;
        }

        const overlay = document.createElement('div');
        overlay.className = 'camera-overlay';
        overlay.id = 'cameraOverlay';
        overlay.innerHTML = `
            <div class="flex items-center justify-between p-4 text-white" style="padding-top:calc(16px + var(--safe-top))">
                <div>
                    <p class="font-bold text-lg">${I18n.__(actionKey)}</p>
                    <p class="text-xs opacity-70" id="cameraCoords">📍 ${coords}</p>
                </div>
                <button onclick="UI.closeCamera()" class="icon-button" style="background:rgba(255,255,255,.12);border-color:rgba(255,255,255,.25);color:#fff">✕</button>
            </div>
            <video id="attendanceVideo" autoplay playsinline muted></video>
            <div class="camera-controls text-white">
                <p class="camera-framing" id="framingHint" data-framing="" hidden></p>
                <div class="flex items-center gap-4">
                    <div style="width:76px"></div>
                    <button id="captureBtn" class="shutter-button" aria-label="${I18n.__('captureSubmit')}"></button>
                    <div style="width:76px"></div>
                </div>
                <p class="text-center text-xs opacity-70 mt-3">${I18n.__('captureHint')}</p>
            </div>
        `;
        document.body.appendChild(overlay);
        this._cameraOpen = true;
        document.body.style.overflow = 'hidden';

        try {
            const stream = await Camera.start();
            const video = document.getElementById('attendanceVideo');
            video.srcObject = stream;
            try { await video.play(); } catch (e) { /* autoplay policing; ignore */ }
            // Last, once there is a frame to look at: the live hint under the shutter.
            this.startFramingHint();
        } catch (err) {
            console.error('Camera error:', err);
            const info = Camera.explain(err);
            this.closeCamera();
            this.showHelpModal(info.title, info.steps);
            return;
        }

        document.getElementById('captureBtn').addEventListener('click', () => this.submitAttendance(action, coords));
    },

    async submitAttendance(action, coords) {
        const video = document.getElementById('attendanceVideo');
        const captureButton = document.getElementById('captureBtn');
        // The session is the credential, so this is the only check in front of the punch.
        // It can die while the overlay is open (another tab signed out, the password was
        // reset from the console); without this the shutter throws on ``State.user.id``
        // and the worker is left tapping a button that does nothing.
        if (!State.user) {
            this.closeCamera();
            Toast.error(I18n.__('sessionExpiredSignInAgain'));
            return;
        }
        if (!video.videoWidth) {
            Toast.error(I18n.__('cameraNotReady'));
            return;
        }

        captureButton.disabled = true;
        const canvas = document.createElement('canvas');
        canvas.width = video.videoWidth;
        canvas.height = video.videoHeight;
        canvas.getContext('2d').drawImage(video, 0, 0);

        const blob = await new Promise((resolve) => canvas.toBlob(resolve, 'image/jpeg', 0.9));
        // A geofence is a claim about *now*, and the fix taken when the camera card opened
        // can be stale by the time the shutter is pressed. Re-sending it made a rejected
        // punch only ever repeatable: the worker walked inside the gate, tapped the shutter
        // again and got the same "outside any designated construction site" while standing
        // on the site - the only way forward was to close the camera and start over. So
        // each punch takes a fresh reading, and the card is updated to show what it sends.
        const punchCoords = await this.freshCoords(coords);
        // No password: the bearer token above is the credential, and a second copy of a
        // password typed under a phone's camera is a worse secret, not a stronger one.
        const formData = new FormData();
        formData.append('worker_id', State.user.id);
        formData.append('action', action);
        formData.append('location_input', punchCoords);
        formData.append('selfie', blob, 'selfie.jpg');

        try {
            const res = await API.request('/attendance/verify', { method: 'POST', body: formData });
            this.closeCamera();
            State.gpsCoords = punchCoords;
            Toast.success(res.message || I18n.__('attendanceOk'));
            // Hold a fresh anchor: the next punch may well happen with no bars.
            if (typeof OFFLINE !== 'undefined' && OFFLINE.available()) {
                OFFLINE.refreshAnchor().catch(() => {});
            }
            await this.renderApp();
        } catch (err) {
            if (await this.queueOfflinePunch(action, punchCoords, blob, err)) return;
            // A 401 here is always about the token - the account was deactivated, the
            // password was rotated, or another tab signed out - and ``API.request`` has
            // already cleared the session and repainted the login screen. Take the overlay
            // down with it: a camera covering the login screen, with a shutter that can
            // only fail, is the worst of both. Every other refusal this endpoint makes is
            // about the punch itself (422 for a mismatched face or a liveness attack, 403
            // for the geofence), so the worker keeps their session and can try again.
            if (!State.user) {
                this.closeCamera();
                Toast.error(I18n.__('sessionExpiredSignInAgain'));
                return;
            }
            Toast.error(`${I18n.__('attendanceError')}: ${err.message}`);
            captureButton.disabled = false;
        }
    },

    /**
     * The coordinates to punch with: a fresh reading, or the one the card opened with.
     *
     * A refused re-read must never block the punch - the fix taken a moment ago is still
     * a fix, and the server is the one that decides whether it is inside the gate.
     */
    async freshCoords(fallback) {
        let fresh = null;
        try {
            fresh = await Location.current();
        } catch (err) {
            console.warn('Location re-read failed:', err);
        }
        const coords = fresh || fallback;
        const shown = document.getElementById('cameraCoords');
        if (shown && coords !== fallback) shown.textContent = `📍 ${coords}`;
        return coords;
    },

    /**
     * The server never answered, so keep the punch on the phone instead of losing it.
     * Only a connectivity failure is treated this way - an HTTP error is a real
     * answer from the server and must still be shown to the worker.
     */
    async queueOfflinePunch(action, coords, blob, error) {
        if (typeof OFFLINE === 'undefined' || !OFFLINE.available()) return false;
        if (!OFFLINE.isConnectivityFailure(error)) return false;
        try {
            const record = await OFFLINE.queuePunch({ action, coords, photoBlob: blob });
            this.closeCamera();
            State.gpsCoords = coords;
            Toast.info(`${I18n.__('offlineSaved')} ${record.effective_timestamp}`);
            await this.renderApp();
            return true;
        } catch (queueError) {
            Toast.error(queueError.message || I18n.__('offlineQueueFailed'));
            return false;
        }
    },

    closeCamera() {
        this.stopFramingHint();
        State.stopCamera();
        const overlay = document.getElementById('cameraOverlay');
        if (overlay) overlay.remove();
        document.body.style.overflow = '';
        this._cameraOpen = false;
    },

    // -----------------------------------------------------------------
    //  The framing coach
    //
    //  The card used to be a camera and a shutter: a worker learned what the server
    //  thought of their photo only *after* the punch, in a sentence about a score. This
    //  looks at the frame while the card is open and says the one thing that would help -
    //  too dark, no face yet, move closer - and nothing at all when the frame is usable.
    //
    //  It is advice, never a gate. The shutter stays live whatever the hint says: a hint
    //  that could block a punch would be a worse bug than a badly lit selfie, and the
    //  server (geofence, liveness, face match) is the thing that decides.
    // -----------------------------------------------------------------

    /** Coarse enough to cost nothing on a phone, live enough to react to a step. */
    _framing: { timer: null, detector: undefined, busy: false },

    //: How wide the sampled frame is. 160x120 grey is plenty to answer "is it dark?"
    //: and to hand the face detector a picture, and it is ~1/40th of a camera frame.
    FRAMING_SAMPLE_WIDTH: 160,
    FRAMING_SAMPLE_MS: 700,

    //: Green when the frame is usable, amber when the worker should adjust something,
    //: red when this photo cannot work as it is.
    FRAMING_TONES: {
        framingGood: 'good',
        framingNoFace: 'warn',
        framingManyFaces: 'warn',
        framingCloser: 'warn',
        framingFurther: 'warn',
        framingFlat: 'bad',
        framingDark: 'bad',
        framingBright: 'bad'
    },

    /**
     * The browser's own face detector, when this browser ships one.
     *
     * Chrome on Android has it; where it is missing the coach still reports light and
     * contrast and says nothing about faces rather than guessing at them - the point of
     * the line is that a worker can trust it.
     */
    faceDetector() {
        if (this._framing.detector !== undefined) return this._framing.detector;
        let detector = null;
        if (typeof window.FaceDetector === 'function') {
            try {
                detector = new window.FaceDetector({ fastMode: true, maxDetectedFaces: 3 });
            } catch (err) {
                detector = null;
            }
        }
        this._framing.detector = detector;
        return detector;
    },

    /**
     * Which piece of advice a frame deserves, given what was measured in it.
     *
     * Pure on purpose - numbers in, translation key out - so the rules can be read and
     * tested without a camera, a canvas or a person in front of one.
     *
     * Light comes first because it is the fix for most bad photos on a site: a frame that
     * is too dark to see a face in is not helped by advice about face size. Below that,
     * the count and then the size, because "nobody in the frame" and "two people in the
     * frame" are different instructions.
     */
    framingAdvice(metrics) {
        if (!metrics) return null;
        if (metrics.luminance < 55) return 'framingDark';
        if (metrics.luminance > 215) return 'framingBright';
        // A lens that is covered, or pointed at a wall: no detail to match a face against.
        if (metrics.contrast < 10) return 'framingFlat';
        if (!metrics.faceDetection || !metrics.face) return null;
        if (!metrics.face.count) return 'framingNoFace';
        if (metrics.face.count > 1) return 'framingManyFaces';
        if (metrics.face.area < 0.12) return 'framingCloser';
        if (metrics.face.area > 0.75) return 'framingFurther';
        return 'framingGood';
    },

    /** Light, contrast and the faces in one sampled frame of the live video. */
    async measureFraming(video, detector) {
        const height = Math.max(
            1, Math.round(this.FRAMING_SAMPLE_WIDTH * (video.videoHeight / video.videoWidth))
        );
        const canvas = document.createElement('canvas');
        canvas.width = this.FRAMING_SAMPLE_WIDTH;
        canvas.height = height;
        const context = canvas.getContext('2d');
        context.drawImage(video, 0, 0, canvas.width, canvas.height);
        const { data } = context.getImageData(0, 0, canvas.width, canvas.height);

        let sum = 0;
        let sumOfSquares = 0;
        const pixels = data.length / 4;
        for (let index = 0; index < data.length; index += 4) {
            // Rec. 601 luma: green dominates what the eye reads as brightness, and it is a
            // multiply and add per pixel rather than a colour-space conversion.
            const luma = data[index] * 0.299 + data[index + 1] * 0.587 + data[index + 2] * 0.114;
            sum += luma;
            sumOfSquares += luma * luma;
        }
        const luminance = sum / pixels;
        const metrics = {
            luminance,
            contrast: Math.sqrt(Math.max(0, sumOfSquares / pixels - luminance * luminance)),
            face: null,
            faceDetection: !!detector
        };
        if (!detector) return metrics;
        try {
            const faces = await detector.detect(canvas);
            const area = (faces || []).reduce(
                (total, face) => total + face.boundingBox.width * face.boundingBox.height, 0
            );
            metrics.face = { count: (faces || []).length, area: area / (canvas.width * canvas.height) };
        } catch (err) {
            // A detector that throws is not a detector: fall back to light and contrast
            // for the rest of this card rather than nagging the worker about faces.
            metrics.faceDetection = false;
            this._framing.detector = null;
        }
        return metrics;
    },

    /** Start watching the frame. One timer per card; restarting replaces it. */
    startFramingHint() {
        this.stopFramingHint();
        if (!document.getElementById('attendanceVideo')) return;
        const sample = async () => {
            const video = document.getElementById('attendanceVideo');
            if (!video) { this.stopFramingHint(); return; }
            // A frame that is not decoded yet measures as black, which would flash
            // "too dark" at every worker for the first moment the card is open.
            if (!video.videoWidth || this._framing.busy) return;
            this._framing.busy = true;
            try {
                this.showFramingHint(this.framingAdvice(
                    await this.measureFraming(video, this.faceDetector())
                ));
            } catch (err) {
                // Never let the coach take the camera card down with it.
                console.warn('Framing check failed:', err);
            } finally {
                this._framing.busy = false;
            }
        };
        this._framing.timer = setInterval(sample, this.FRAMING_SAMPLE_MS);
        sample();
    },

    stopFramingHint() {
        if (this._framing.timer !== null) {
            clearInterval(this._framing.timer);
            this._framing.timer = null;
        }
        this._framing.busy = false;
    },

    /** Paint one piece of advice, or hide the line when there is nothing to say. */
    showFramingHint(key) {
        const element = document.getElementById('framingHint');
        if (!element) return;
        if (!key) {
            element.hidden = true;
            element.textContent = '';
            element.dataset.framing = '';
            return;
        }
        element.hidden = false;
        element.textContent = I18n.__(key);
        element.dataset.framing = this.FRAMING_TONES[key] || 'warn';
    },

    /**
     * Honours a shifts link: ``#shifts=start..end`` selects the period and the tab.
     *
     * Runs at boot and on ``hashchange``. Junk in the fragment is ignored, and a worker
     * is left alone - they have no shifts view to land on.
     */
    async applyShiftsLink() {
        if (typeof UI_MODULES === 'undefined') return false;
        if (!UI_MODULES.adoptShiftsRangeFromUrl()) {
            // The fragment is not a usable period - mistyped, or a link from an older
            // build. The screen keeps showing the period it already had, so put that
            // period back in the address bar: a URL that describes a view which is not
            // there is worse than no URL, because it is what gets copied and shared.
            if (State.user && State.user.role !== 'worker' && State.adminTab === 'Shifts') {
                UI_MODULES.syncShiftsUrl(UI_MODULES.shiftsRange());
            }
            return false;
        }
        // At boot the admin host does not exist yet; State.adminTab is what puts the
        // console on Shifts, and renderAdminTab returns early when there is no host.
        if (State.user && State.user.role !== 'worker') await this.renderAdminTab('Shifts');
        return true;
    },

    // -----------------------------------------------------------------
    //  Admin console - rail on desktop, tab strip on a phone
    // -----------------------------------------------------------------
    renderAdminConsole() {
        if (Device.isMobile) this.renderAdminMobile();
        else this.renderAdminDesktop();
        this.renderAdminTab(State.adminTab);
    },

    //: The shell renders names, site names and timestamps, so it escapes too.
    escapeHtml(value) {
        const escapes = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
        return String(value === null || value === undefined ? '' : value)
            .replace(/[&<>"']/g, (char) => escapes[char]);
    },

    /** "MK" for "Mohamed Kamal" - the avatar on the identity chip. */
    adminInitials(name) {
        const parts = String(name || '').trim().split(/\s+/).filter(Boolean);
        if (parts.length === 0) return '?';
        return parts.slice(0, 2).map((part) => part.slice(0, 1)).join('').toUpperCase();
    },

    /**
     * Who is signed in, and as what.
     *
     * Worth the space in the header: this console grants access to other people's
     * accounts and hours, "which admin am I right now" is a question an operator
     * genuinely asks before pressing a destructive button, and the role decides
     * which of those buttons exist at all.
     */
    adminIdentityHtml() {
        const user = State.user || {};
        const name = user.name || user.id || '';
        const roleKey = { worker: 'roleWorker', moallem: 'roleMoallem', admin: 'roleAdmin', head_admin: 'roleHeadAdmin' }[user.role];
        return `
            <span class="admin-identity">
                <span class="ops-avatar" aria-hidden="true">${this.escapeHtml(this.adminInitials(name))}</span>
                <span class="admin-identity-text">
                    <span class="admin-identity-name">${this.escapeHtml(name)}</span>
                    <span class="admin-identity-role">${this.escapeHtml(roleKey ? I18n.__(roleKey) : (user.role || ''))}</span>
                </span>
            </span>`;
    },

    /**
     * The nav, in two shapes from one source.
     *
     * The rail carries the group captions, because a desktop rail has the height to
     * answer "what kind of thing is this tab". The phone strip does not: it is a
     * horizontal scroller, captions would be non-scrolling islands in it, and the
     * order is the same either way, so the grouping survives as rhythm rather than
     * as a label.
     */
    adminNavHtml(context) {
        const activeTab = adminTabRecord(State.adminTab).id;
        const label = (tab) => `${ADMIN_ICONS[tab.icon]}<span>${this.escapeHtml(I18n.__(tab.key))}</span>`;
        const button = (tab, className) => `<button type="button"${className ? ` class="${className}"` : ''} data-admin-tab="${tab.id}"${tab.id === activeTab ? ' aria-current="page"' : ''} onclick="UI.renderAdminTab('${tab.id}')">${label(tab)}</button>`;
        const navLabel = this.escapeHtml(`${I18n.__('title')} - ${I18n.__('adminConsole')}`);

        if (context === 'strip') {
            // Flattened from the same groups the rail uses, so the phone never shows
            // an order the desktop does not.
            const flat = adminNavGroups().reduce((all, group) => all.concat(group.tabs), []);
            return `<nav class="admin-tabs" id="adminNav" aria-label="${navLabel}">
                ${flat.map((tab) => button(tab, '')).join('')}
            </nav>`;
        }
        return `<nav class="admin-nav" id="adminNav" aria-label="${navLabel}">
            ${adminNavGroups().map((group) => `
                <div class="admin-nav-group" role="group" aria-labelledby="nav-${group.key}">
                    <p class="admin-nav-caption" id="nav-${group.key}">${this.escapeHtml(I18n.__(group.key))}</p>
                    ${group.tabs.map((tab) => button(tab, 'admin-nav-btn')).join('')}
                </div>`).join('')}
        </nav>`;
    },

    /**
     * The wait before a tab's data lands.
     *
     * A skeleton rather than a centred spinner, and bars rather than a row of them:
     * the pane keeps its shape, an operator can see the screen is working, and when
     * the real content arrives it does not jump the layout underneath their thumb.
     */
    consoleSkeletonHtml(label) {
        const rows = [76, 58, 68];
        return `
            <div class="ui-skeleton" aria-busy="true">
                <p class="sr-only" role="status">${this.escapeHtml(label || I18n.__('loading'))}</p>
                ${rows.map((width, index) => `<span class="ui-skeleton-bar" style="width:${width}%;animation-delay:${index * 0.12}s"></span>`).join('')}
            </div>`;
    },

    /**
     * Everything outside the pane that depends on which tab is showing.
     *
     * The title and the subtitle live in one place - the shell - rather than in each
     * tab's own markup, so "what am I looking at" cannot drift from the nav item that
     * is lit up, and so no tab has to remember to draw a heading.
     */
    paintAdminHeader(tab) {
        const record = adminTabRecord(tab);
        const title = document.getElementById('adminTitle');
        if (title) title.textContent = I18n.__(record.key);
        const subtitle = document.getElementById('adminSubtitle');
        if (subtitle) subtitle.textContent = I18n.__(record.hint);
        document.querySelectorAll('[data-admin-tab]').forEach((button) => {
            const active = button.dataset.adminTab === record.id;
            button.classList.toggle('is-active', active);
            // The lit-up tab is announced too, not only painted: a keyboard user
            // hears "current page" on the item the sighted user sees highlighted.
            if (active) button.setAttribute('aria-current', 'page');
            else button.removeAttribute('aria-current');
        });
    },

    renderAdminMobile() {
        const container = this.appContainer;
        container.className = 'admin';
        const active = adminTabRecord(State.adminTab);
        container.innerHTML = `
            <header class="admin-mobile-head">
                <div class="ui-spread" style="padding:10px 0 8px">
                    <div class="ui-row" style="gap:10px;min-width:0">
                        <span class="admin-brand-mark" aria-hidden="true"><img src="${BRAND.mark}" alt="" width="18" height="24"></span>
                        <h2 class="admin-title" id="adminTitle">${this.escapeHtml(I18n.__(active.key))}</h2>
                    </div>
                    <div class="admin-actions">
                        ${this.langSelectHtml()}
                        <button type="button" onclick="UI.toggleTheme()" class="icon-button" aria-label="${this.escapeHtml(I18n.__('theme'))}">${ADMIN_ICONS.theme}</button>
                        <button type="button" onclick="UI.logout()" class="icon-button" aria-label="${this.escapeHtml(I18n.__('logout'))}">${ADMIN_ICONS.logout}</button>
                    </div>
                </div>
                ${this.adminNavHtml('strip')}
            </header>
            <div class="admin-frame">
                <p class="ui-hint admin-mobile-hint" id="adminSubtitle">${this.escapeHtml(I18n.__(active.hint))}</p>
                <div id="adminContent" class="ui-surface"></div>
            </div>`;
    },

    renderAdminDesktop() {
        const container = this.appContainer;
        container.className = 'admin';
        const active = adminTabRecord(State.adminTab);
        container.innerHTML = `
            <div class="admin-frame">
                <div class="admin-grid">
                    <aside class="admin-rail">
                        <div class="admin-brand">
                            <span class="admin-brand-mark" aria-hidden="true"><img src="${BRAND.mark}" alt="" width="18" height="24"></span>
                            <span class="admin-brand-text">
                                <span class="admin-brand-name">${this.escapeHtml(BRAND.name)}</span>
                                <span class="admin-brand-sub">${this.escapeHtml(I18n.__('title'))}</span>
                            </span>
                        </div>
                        ${this.adminNavHtml('rail')}
                    </aside>
                    <div class="admin-main">
                        <header class="admin-header">
                            <div class="admin-header-text">
                                <h2 class="admin-title" id="adminTitle">${this.escapeHtml(I18n.__(active.key))}</h2>
                                <p class="admin-subtitle" id="adminSubtitle">${this.escapeHtml(I18n.__(active.hint))}</p>
                            </div>
                            <div class="admin-actions">
                                ${this.adminIdentityHtml()}
                                ${this.langSelectHtml()}
                                <button type="button" onclick="UI.toggleTheme()" class="icon-button" aria-label="${this.escapeHtml(I18n.__('theme'))}">${ADMIN_ICONS.theme}</button>
                                <button type="button" onclick="UI.logout()" class="icon-button" aria-label="${this.escapeHtml(I18n.__('logout'))}">${ADMIN_ICONS.logout}</button>
                            </div>
                        </header>
                        <div id="adminContent" class="ui-surface"></div>
                    </div>
                </div>
            </div>`;
    },

    async renderAdminTab(tab) {
        State.adminTab = tab;
        // The previous tab's timers die here, before anything else runs: a board
        // that kept polling from a screen nobody is looking at is just a phone
        // battery and a server log filling up.
        this.stopLiveOps();
        this.paintAdminHeader(tab);

        const content = document.getElementById('adminContent');
        if (!content) return;
        content.innerHTML = this.consoleSkeletonHtml(`${I18n.__(adminTabRecord(tab).key)}...`);

        try {
            switch (tab) {
                // The whole board - the stats, the filters, the live timer and the
                // force-in panel - lives in ``admin_modules.js`` with the other tab
                // screens. What stays here is the shell and ``forceInPanelHtml``,
                // because the panel's buttons call back into ``UI.forceIn()``.
                case 'Live Ops': await UI_MODULES.renderLiveOps(content); break;
                case 'Approvals': await UI_MODULES.renderApprovals(content); break;
                case 'Sites': await UI_MODULES.renderSites(content); break;
                case 'Credentials': await UI_MODULES.renderCredentials(content); break;
                case 'Links': await UI_MODULES.renderLinks(content); break;
                case 'Notes': await UI_MODULES.renderNotes(content); break;
                case 'Admin': await UI_MODULES.renderAdminManagement(content); break;
                case 'Shifts': await UI_MODULES.renderShifts(content); break;
                default:
                    content.innerHTML = `<div class="text-center py-10 text-gray-500">${tab} ${I18n.__('comingSoon')}</div>`;
            }
        } catch (err) {
            // The server's sentence is escaped like any other server text: it is written by
            // whichever endpoint refused the request, and "it is our own message" stops
            // being true the moment one of them interpolates something a client sent.
            content.innerHTML = `<p class="text-red-500">${I18n.__('error')}: ${this.escapeHtml(err.message)}</p>`;
        }
    },

    /**
     * Stop whatever Live Ops is running. Called on every tab change and on
     * logout, so a board nobody is looking at stops ticking and stops asking.
     */
    stopLiveOps() {
        if (typeof UI_MODULES !== 'undefined' && UI_MODULES.stopLiveOps) UI_MODULES.stopLiveOps();
    },

    /**
     * Force somebody who is *not* on shift onto one.
     *
     * Clocking a worker out is a row on the session list - they are right there. Clocking
     * one in is the opposite case: they are absent by definition, so the panel has to offer
     * the roster with the already-clocked-in removed. A deactivated account is left out too;
     * the server refuses it, and a button that always fails is a trap.
     */
    forceInPanelHtml(sessions, users, sites) {
        const escapeHtml = UI_MODULES.escapeHtml.bind(UI_MODULES);
        const onShift = new Set((sessions || []).map((session) => String(session.worker_id)));
        const available = (users || []).filter((user) =>
            user.role !== 'admin' && user.role !== 'head_admin' &&
            String(user.status || 'active').toLowerCase() === 'active' &&
            !onShift.has(String(user.id)));
        const siteNames = (sites || []).map((site) => site.site_name);
        const disabled = available.length === 0 || siteNames.length === 0;
        const disabledAttribute = disabled ? ' disabled' : '';
        // The note is the panel's voice: it explains the empty state *before* the
        // operator taps a control that would have been a trap.
        const note = disabled
            ? I18n.__('forceInNothing')
            : I18n.__('liveOpsForceReady').replace('{count}', String(available.length));
        return `
            <div class="ops-panel-body">
                <p class="ops-note" id="forceInHint" style="margin-top:0">${escapeHtml(I18n.__('forceInHint'))}</p>
                <div class="grid gap-3 sm:grid-cols-3 mt-3">
                    <div>
                        <label class="ops-stat-label" for="forceInWorker">${escapeHtml(I18n.__('liveOpsForceWorker'))}</label>
                        <select id="forceInWorker" class="ops-field" aria-describedby="forceInHint"${disabledAttribute}>
                            ${available.map((user) => `<option value="${escapeHtml(user.id)}">${escapeHtml(user.name || user.id)} (${escapeHtml(user.id)})</option>`).join('')}
                        </select>
                    </div>
                    <div>
                        <label class="ops-stat-label" for="forceInSite">${escapeHtml(I18n.__('liveOpsForceSite'))}</label>
                        <select id="forceInSite" class="ops-field" aria-describedby="forceInHint"${disabledAttribute}>
                            ${siteNames.map((name) => `<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`).join('')}
                        </select>
                    </div>
                    <div class="flex items-end">
                        <button type="button" data-force-in="true" onclick="UI.forceIn()"
                                class="ops-btn ops-btn-primary w-full"${disabledAttribute}>${UI_MODULES.OPS_ICONS.person}${escapeHtml(I18n.__('forceIn'))}</button>
                    </div>
                </div>
                <p class="ops-note" data-force-in-note role="status">${escapeHtml(note)}</p>
            </div>`;
    },

    /** The panel's one action: read the two selects and send the forced clock-in. */
    async forceIn() {
        const worker = document.getElementById('forceInWorker');
        const site = document.getElementById('forceInSite');
        if (!worker || !site) return;
        return this.forceAction(String(worker.value || ''), 'in', String(site.value || ''));
    },

    // Was referenced by the Live Ops table but never implemented, so the
    // "Force Out" button used to throw "UI.forceAction is not a function".
    async forceAction(workerId, action, siteName) {
        const isOut = action === 'out';
        const label = isOut ? I18n.__('forceOut') : I18n.__('forceIn');
        if (!confirm(`${label} - ${workerId}?`)) return;
        try {
            const res = await API.request(isOut ? '/admin/force_clock_out' : '/admin/force_clock_in', {
                method: 'POST',
                body: { admin_id: State.user.id, worker_id: workerId, site_name: siteName || '' }
            });
            Toast.success(res.message || label);
            this.renderAdminTab(State.adminTab);
        } catch (err) {
            Toast.error(err.message);
        }
    },

    toggleTheme() {
        State.toggleTheme();
        // Re-render so inline emoji/labels that depend on the theme stay in sync.
        if (State.user) this.renderApp();
    },

    logout() {
        this.stopLiveOps();
        State.clearUser();
        Modal.close();
        this.closeCamera();
        this.renderApp();
    }
};

document.addEventListener('DOMContentLoaded', () => {
    // Exposed on window for use from inline HTML handlers and for diagnostics
    // (e.g. `API.resolveBaseURL({protocol:'https:', port:'', origin:'https://x.dev'})`).
    window.UI = UI;
    window.Modal = Modal;
    window.API = API;
    UI.init();
});
