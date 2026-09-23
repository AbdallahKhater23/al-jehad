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
    //: An administrator who has stepped out of the console and onto the handset to clock
    //: in. The clock, history and profile screens a worker gets, with a way back - because
    //: a manager who covers a site as well has hours of their own to record.
    handsetMode: false,
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
        // Whoever signs in next is a different account, and the handset choice belonged to
        // the last one. A stored session is not a screen anybody was looking at.
        this.handsetMode = false;
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
    open(html, { dismissible = true, overCamera = false } = {}) {
        const root = document.getElementById('modalRoot');
        // ``overCamera`` lifts this dialog above the open camera overlay, which otherwise
        // covers it: the early clock-out question is asked mid-punch, with the camera up.
        const layer = overCamera ? 'modal-backdrop is-over-camera' : 'modal-backdrop';
        root.innerHTML = `<div class="${layer}"><div class="modal-card" role="dialog" aria-modal="true">${html}</div></div>`;
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

//: The base URLs are owned by ``api-config.js`` (loaded by the page before this file):
//: production Worker origin, direct-Railway diagnostics origin, the normaliser and the
//: resolution order all live there. This file only consumes them.
const PRODUCTION_API_BASE_URL = (typeof API_BASE_URL !== 'undefined' && API_BASE_URL) || 'https://al-jehad1.abdallahtamet281.workers.dev/api/v1';
const FALLBACK_API_BASE_URL = (typeof API_FALLBACK_BASE_URL !== 'undefined' && API_FALLBACK_BASE_URL) || 'https://al-jehad-production.up.railway.app/api/v1';
const normalizeBaseURL = (typeof normalizeAPIBaseURL === 'function')
    ? normalizeAPIBaseURL
    : function (candidate) {   // same rule, kept for a page that loaded this file alone
        let url = String(candidate || '').trim();
        if (!url) return '';
        url = url.replace(/\/+$/, '');
        if (/(^|\/)api\/v\d+$/.test(url)) return url;
        const cut = url.match(/^(https?:\/\/[^/]+)\/api\/v\d+(?=\/|$)/);
        if (cut) return cut[0];
        return url;
    };

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
        // Resolution lives in ``api-config.js`` (see its header for the order); this
        // delegation keeps every caller of ``API.resolveBaseURL`` on the one rule.
        if (typeof resolveAPIBase === 'function') return normalizeBaseURL(resolveAPIBase(loc));
        // Fallback for a page that loaded this file alone: the same order, inline.
        const fromWindow = (typeof API_BASE_URL !== 'undefined' && API_BASE_URL) ? API_BASE_URL : null;
        if (fromWindow) return normalizeBaseURL(fromWindow);
        const override = localStorage.getItem('apiBaseURL');
        if (override) return normalizeBaseURL(override);
        const { protocol, hostname, port, origin } = loc;
        if (protocol === 'file:') return normalizeBaseURL(PRODUCTION_API_BASE_URL);
        // ``serve.py`` serves the page and the API from a single origin - on the default
        // port, on any ``--port``, on a LAN IP, and behind an HTTPS tunnel (ngrok,
        // Cloudflare, ...) - so the page's own origin is the answer, whatever the port.
        // This used to special-case 80/443/8000 and send every other port to :8000,
        // which made ``serve.py --port 8443`` call a server that was not there - or,
        // worse, a different one that happened to be on 8000: a second database, read
        // and written without a word in the UI.
        if (!STATIC_DEV_SERVER_PORTS.has(port)) return normalizeBaseURL(`${origin}/api/v1`);
        // A static dev server hosting the frontend folder on its own: the page is on a
        // different origin from every API this deployment fronts, so point at the
        // production Worker. Anything else can set ``localStorage.apiBaseURL``, which is
        // checked above.
        return normalizeBaseURL(PRODUCTION_API_BASE_URL);
    },
    isTunnelHost(hostname = window.location.hostname) {
        const host = String(hostname || '').toLowerCase();
        return TUNNEL_HOST_SUFFIXES.some(suffix => host === suffix || host.endsWith('.' + suffix));
    },
    get baseURL() {
        if (!this._base) this._base = this.resolveBaseURL();
        return this._base;
    },
    /** The direct Railway origin, for diagnostics when the Worker is the suspect. */
    get fallbackBaseURL() {
        return normalizeBaseURL(FALLBACK_API_BASE_URL);
    },
    /** Route subsequent requests straight at Railway (diagnostics switch). */
    useFallback() {
        this._base = this.fallbackBaseURL;
        return this._base;
    },
    async request(endpoint, options = {}) {
        const headers = { Accept: 'application/json', ...options.headers };
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
            // The throw stays one readable sentence, but the parts a caller may need to
            // *act* on travel with it. A refusal that is really a question - the early
            // clock-out warning - is indistinguishable from a refusal that is an answer
            // once it has been flattened to a string.
            const failure = new Error(this.describeError(err, response));
            failure.status = response.status;
            const detail = err && err.detail;
            if (detail && typeof detail === 'object' && !Array.isArray(detail)) {
                failure.errorCode = detail.error_code || null;
                failure.detail = detail;
            }
            throw failure;
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
//  The printable half of a download
// =====================================================================
/**
 * Reporting on paper - the PDF half of a download, for either role.
 *
 * Printed by the browser rather than generated on the server, and deliberately: no PDF
 * library is pinned, no Arabic-capable font ships with one, and a sheet the browser draws
 * already has the reader's own fonts and text direction right - in Cairo, in Muscat, on a
 * machine whose fonts nobody configured. "Save as PDF" in that dialog is what writes the
 * file, which is why nothing in this app ever writes a PDF itself.
 *
 * It lives here, between the two roles, because both of them print: the console's Shifts
 * tab and the worker's own timesheet build different sheets, but the same three things
 * have to be true of the paper - and the frame around the report is the same document
 * either way, so this is also where a report sheet is *built* (``sheetHtml``).
 */
const PrintReport = {
    //: The sheet this object put in the page, so it is the one taken back out. A
    //: document-wide search for ``.print-sheet`` would also find one left behind by a
    //: print whose dialog never closed.
    _sheet: null,

    /**
     * The sheet itself, for either report: the title, what it covers, the table, what it
     * adds up to, and the note that says which of those figures count.
     *
     * A report on paper is the same document twice over. Only the *content* is the
     * report's own - the console's columns are the administrator's, the worker's are
     * theirs - and the frame around it has to be identical, because it is the same piece
     * of paper to whoever is holding it. Hand-built twice, the frame is how one screen
     * ends up printing a sheet that says something subtly different about the same rule
     * (the note at the foot, the arrow between two dates, the \"no rows\" sentence), and
     * how a class the stylesheet knows about ends up on one sheet and not the other.
     *
     * ``title``, the ``meta`` lines, the ``columns`` and ``empty`` are plain text and are
     * escaped here. Each cell of ``rows`` is the opposite on purpose: HTML the caller has
     * already built and escaped, because building a cell is the one thing that is
     * genuinely the report's own (a console cell is a badge with a tone, a worker's is a
     * figure with a unit). ``totals`` is the same kind of hand-built HTML: a figure with
     * the emphasis the report gives it.
     */
    sheetHtml({ title, meta, columns, rows, totals, empty, note }) {
        const lines = (meta || []).filter(Boolean)
            .map((line) => `<p class="print-sheet-meta">${UI.escapeHtml(line)}</p>`).join('');
        const body = (rows || []).length > 0
            ? `<table class="print-sheet-table">
                    <thead><tr>${(columns || []).map((label) => `<th>${UI.escapeHtml(label)}</th>`).join('')}</tr></thead>
                    <tbody>${rows.map((cells) => `<tr>${cells.map((cell) => `<td>${cell}</td>`).join('')}</tr>`).join('')}</tbody>
               </table>`
            : (empty ? `<p class="print-sheet-empty">${UI.escapeHtml(empty)}</p>` : '');
        // The note is not a parameter of the report: it is the rule both reports are read
        // under - only approved hours count - and a sheet that stated it differently would
        // be describing different arithmetic. The company's own name is the same kind of
        // thing, and is drawn by the same rule: on every sheet, because a timesheet handed
        // to payroll belongs to somebody.
        const foot = note || I18n.__('shiftsApprovedOnly');
        return `${this.brandHtml()}<p class="print-sheet-title">${UI.escapeHtml(title)}</p>${lines}${body}
            ${totals ? `<p class="print-sheet-totals">${totals}</p>` : ''}
            <p class="print-sheet-note">${UI.escapeHtml(foot)}</p>`;
    },

    /**
     * Whose document this is, at the top of the sheet: the company's name, its legal
     * suffix and founding year, and its mark.
     *
     * Not a parameter of ``sheetHtml``, for the reason the note at the foot is not: a
     * report is *this company's* report, and a sheet that could be built without it would
     * be one that reaches payroll anonymously. The name and the mark come from the same
     * ``BRAND`` record the login panel and the console rail draw (see ``Brand``), so a
     * company that renamed itself prints its new name on the paper it already had, with no
     * code change - which is the whole point of the settings row.
     *
     * Two details worth keeping:
     *
     * * **A line the company set to empty is not printed.** That is the difference between
     *   ``null`` (nobody configured it, the shipped lockup prints) and ``''`` (the company
     *   removed it) all the way out here on paper, and it is why these are filtered rather
     *   than ``||``-ed onto a default;
     * * **``dir="ltr"`` on the lockup.** A wordmark does not mirror: in an Arabic build
     *   the page is RTL, and the trailing period of "INTERNATIONAL CO." is bidi-neutral, so
     *   the browser would park it at the left of the Latin run. The login panel carries the
     *   same note for the same reason.
     *
     * ``alt=""`` on the mark on purpose: the company's name is printed right beside it, and
     * a screen reader describing the logo as well would say the same thing twice.
     */
    brandHtml() {
        const lines = [BRAND.name, BRAND.legal, BRAND.est, BRAND.tagline]
            .map((line) => String(line === null || line === undefined ? '' : line).trim());
        const [name, legal, est, tagline] = lines;
        const under = [legal, est].filter(Boolean).join(' · ');
        const lockup = [
            name ? `<p class="print-sheet-company">${UI.escapeHtml(name)}</p>` : '',
            under ? `<p class="print-sheet-sub">${UI.escapeHtml(under)}</p>` : '',
            tagline ? `<p class="print-sheet-tagline">${UI.escapeHtml(tagline)}</p>` : ''
        ].join('');
        const mark = BRAND.mark
            ? `<img class="print-sheet-mark" src="${UI.escapeHtml(BRAND.mark)}" alt="">`
            : '';
        // Nothing configured at all - every line removed and no mark - is a company that
        // has said so, and a titled header with nothing in it is worse than no header.
        if (!mark && !lockup) return '';
        return `<header class="print-sheet-brand">${mark}<div class="print-sheet-lockup" dir="ltr">${lockup}</div></header>`;
    },

    /**
     * "Period: 2026-08-01 → 2026-08-31", the line both sheets say it on.
     *
     * One label, one arrow, one separator - so two reports printed from the same month
     * cannot describe it two ways - and it is built left to right like the rest of the
     * page, which is what a reader of an RTL build expects of a date range that is
     * mirroring around it. ``extra`` is what the sheet was narrowed to, in plain text.
     */
    periodLine(period, extra) {
        const range = `${(period && period.start) || ''} → ${(period && period.end) || ''}`;
        return `${I18n.__('shiftsPeriod')}: ${range}${extra ? ` · ${extra}` : ''}`;
    },

    /**
     * Prints ``html`` as the whole document, named ``filename`` in the dialog.
     *
     * The page behind it is still on screen, and the stylesheet is what takes it out of
     * the paper: ``body.is-printing-report`` hides everything that is not the sheet, and
     * only while a sheet is being printed - so a plain Ctrl+P on the page still prints
     * the page.
     *
     * Cleanup waits for ``afterprint``. Firefox and Safari render the sheet *after*
     * ``print()`` returns, so emptying it here would hand the reader a blank page. The
     * listener takes itself off, because one per print would pile up on ``window`` for as
     * long as the tab lives, each restoring a title that is no longer the tab's.
     */
    sheet(html, filename) {
        this.clear();
        const node = document.createElement('div');
        node.className = 'print-sheet';
        node.innerHTML = html;
        document.body.appendChild(node);
        this._sheet = node;
        document.body.classList.add('is-printing-report');
        // The dialog names the file after the document title, so the title carries the
        // period exactly as the CSV's own file name does - and no extension, because the
        // dialog appends one: a title already ending in ".pdf" saves as "....pdf.pdf".
        const screenTitle = document.title;
        document.title = filename;
        const afterPrint = () => {
            window.removeEventListener('afterprint', afterPrint);
            document.title = screenTitle;
            this.clear();
        };
        window.addEventListener('afterprint', afterPrint);
        window.print();
    },

    /** Take the sheet back out, and the page back out of the way of nothing. */
    clear() {
        document.body.classList.remove('is-printing-report');
        const node = this._sheet;
        this._sheet = null;
        if (node && typeof node.remove === 'function') node.remove();
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
    /**
     * The frame on screen, as a JPEG. The one step every capture path shares.
     *
     * What the bytes are *for* differs - a punch posts them to the punch endpoint, the
     * console's Credentials tab posts them as the reference photo the punches will be
     * matched against - but turning a live video element into bytes is the same three lines
     * either way, and two copies of it is how one path starts sending a full-resolution
     * frame while the other sends something cropped or re-compressed.
     */
    async snapshot(video) {
        const canvas = document.createElement('canvas');
        canvas.width = video.videoWidth;
        canvas.height = video.videoHeight;
        canvas.getContext('2d').drawImage(video, 0, 0);
        return new Promise((resolve) => canvas.toBlob(resolve, 'image/jpeg', 0.9));
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
    // A bell rather than the triangle: the triangle is the "late" badge on the shift board,
    // and an Alerts tab that wears the same glyph as a late shift reads as one more board.
    alerts: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M18 9a6 6 0 1 0-12 0c0 4.5-1.2 5.8-2 7h16c-.8-1.2-2-2.5-2-7Z"></path><path d="M10 19.5a2 2 0 0 0 4 0"></path></svg>',
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
//:
//: This is what the application *ships with*, and for a deployment that never opens the
//: Company panel it is also what every screen prints, byte for byte. What the company
//: actually configured lives in the server's settings row and is merged over these values
//: by ``Brand`` below - so ``BRAND`` is the record every screen and every sheet reads, and
//: there is exactly one of it.
const BRAND_DEFAULTS = {
    name: 'AL-JEHAD',
    legal: 'INTERNATIONAL CO.',
    est: 'EST 1983',
    tagline: 'STONE . MARBLE . GRANITE',
    mark: 'logo-mark.svg',
    //: The vendor whose software this is, credited at the foot of every page. A wordmark
    //: like the company's, so it is the same string in all three languages; ``poweredBy``
    //: in i18n.js is only the words around it. Not a setting: this is whose product it is,
    //: which is not something the customer decides.
    poweredBy: 'دوامك اسهل'
};

//: The lockup as the whole app reads it - login panel, handset header, console rail, every
//: printed sheet. Mutated **in place** by ``Brand`` rather than replaced, so a screen that
//: holds a reference to it (or a template string that read it a line earlier) cannot be
//: reading yesterday's copy.
const BRAND = { ...BRAND_DEFAULTS };

/**
 * Whose company this is, as configured on the server - and it is not a constant any more.
 *
 * The name, the legal suffix, the founding year, the tagline and the mark used to be the
 * object above: a code change and a redeploy to rename the company, and a company that
 * wanted its own logo at the top of the sheet it hands to payroll got the shipped one. They
 * are a settings row now (``company_settings``), edited on the console's Admin tab beside
 * the shift rules, and this is the one place that turns that row into what the app draws.
 *
 * THREE THINGS IT IS CAREFUL ABOUT
 * --------------------------------
 * * **It never blocks the first paint.** ``load`` is not awaited: the cache is applied
 *   synchronously before the first render, and the network answer only corrects what
 *   changed since - because a login screen that waits for a cell tower to tell it the
 *   company's name is worse than a name that lands a moment later. The one exception is
 *   ``restore`` with a warm cache, which is right before anything is drawn at all.
 * * **``null`` and ``''`` are different answers.** The server sends ``null`` for a line
 *   nobody configured (the shipped lockup prints) and ``''`` for one the company deleted
 *   (nothing prints there). Flattening them would put a legal suffix back on the sheet of
 *   a company that deliberately removed it.
 * * **The mark is a URL, not a file name.** A configured logo is served by the API, so it
 *   is resolved against ``API.baseURL`` - the page may be on a LAN address, a chosen port
 *   or a tunnel, and only this browser knows which origin it is talking to. The version
 *   travels in the query string the server sent, so a replaced logo is a different URL.
 */
const Brand = {
    //: Where the last answer from the server is kept. A reload with a warm cache draws the
    //: company's own name immediately, and an offline handset still shows it.
    KEY: 'branding',

    cached() {
        try {
            const raw = localStorage.getItem(this.KEY);
            return raw ? JSON.parse(raw) : null;
        } catch (error) {
            // A cached lockup that cannot be parsed is not a reason to fail a boot.
            return null;
        }
    },

    remember(data) {
        try {
            localStorage.setItem(this.KEY, JSON.stringify(data));
        } catch (error) {
            // Private mode, or a full quota. The lockup is on screen either way.
        }
    },

    /**
     * Merges one server answer (or one cache entry) into ``BRAND``, and says whether
     * anything changed - the caller repaints only when it did.
     */
    apply(data) {
        let changed = false;
        const lines = ['name', 'legal', 'est', 'tagline'];
        for (const key of lines) {
            const stored = data ? data[key] : null;
            const value = (stored === null || stored === undefined) ? BRAND_DEFAULTS[key] : String(stored);
            if (BRAND[key] !== value) {
                BRAND[key] = value;
                changed = true;
            }
        }
        const logo = data && data.logo_url ? String(data.logo_url) : null;
        const mark = logo ? `${API.baseURL}${logo}` : BRAND_DEFAULTS.mark;
        if (BRAND.mark !== mark) {
            BRAND.mark = mark;
            changed = true;
        }
        return changed;
    },

    /** The last known settings, synchronously. Called before the first render. */
    restore() {
        return this.apply(this.cached());
    },

    /**
     * The company's name as prose rather than as a wordmark: "AL-JEHAD INTERNATIONAL CO.".
     *
     * The lockup's two lines carry a line break that only the artwork knows about, so a
     * sentence that names the company has to join them itself. Either being empty is fine -
     * a company with no legal suffix is one wordmark - and both being empty is a login
     * footer that does not pretend to know whose app this is.
     */
    fullName() {
        return [BRAND.name, BRAND.legal]
            .map((part) => String(part === null || part === undefined ? '' : part).trim())
            .filter(Boolean)
            .join(' ');
    },

    /**
     * Asks the server whose company this is. Returns whether it changed anything.
     *
     * A failure - offline, a tunnel that is down, a server that has not been migrated yet -
     * is not an error the reader needs to see: the lockup on screen is the last one that was
     * known, which on a first visit is the shipped one. Nothing about a name is worth an
     * alarm on the login screen.
     */
    async load() {
        let data = null;
        try {
            data = await API.request('/branding');
        } catch (error) {
            return false;
        }
        if (!data) return false;
        this.remember(data);
        return this.apply(data);
    }
};

//  The array order is the order the tabs were built in, and it is what the
//  rest of the app iterates (``test_frontend_credentials`` pins it as the
//  console's inventory). What the *nav* draws is the grouped order below - the
//  two are deliberately separate, so re-ordering the rail is a decision about
//  the rail and not a rename of the tab list.
const ADMIN_TABS = [
    { id: 'Live Ops', key: 'activeShifts', hint: 'hintLiveOps', group: 'navGroupOperations', icon: 'liveOps' },
    { id: 'Approvals', key: 'pendingReviews', hint: 'hintApprovals', group: 'navGroupOperations', icon: 'approvals' },
    // What the server has written for an administrator, and - for the ones that record a
    // decision - what nobody has accepted yet. Sits beside Approvals because both are queues
    // waiting on a person, and this is the one whose items nobody typed.
    { id: 'Alerts', key: 'adminAlerts', hint: 'hintAlerts', group: 'navGroupOperations', icon: 'alerts' },
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
        // Before anything is drawn: the chosen language may be a file this tab has not
        // fetched yet, and a first paint in English that corrects itself a moment later is
        // the one thing choosing a language is meant to prevent. Resolves immediately for
        // English and for a language already in memory, so the usual boot is untouched.
        await I18n.loadStored();
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
        // Whose company this is: the cache first, so a reload draws the company's own
        // name on the very first paint, then the settings row when it answers. Both are
        // deliberately not awaited - see ``Brand`` - and the repaint is skipped when
        // somebody has already started typing.
        Brand.restore();
        Brand.load().then((changed) => { if (changed) this.repaintAfterBrand(); }).catch(() => {});
        this.renderApp();
        // Registers this phone as a signing device, refreshes the server-signed time
        // anchor and drains anything left queued from a previous offline stint.
        // Deliberately not awaited: it must never delay the first paint.
        if (typeof OFFLINE !== 'undefined') OFFLINE.init().catch(() => {});
        // The push channel's service worker registers here rather than at the moment
        // a worker first enables notifications, so the first enable is one tap instead
        // of a tap plus a registration wait on a site's cellular link. It registers
        // for everybody including admins (any account can hold a subscription); no
        // permission is requested here - that is the switch on the profile tab, and a
        // prompt fired at boot is how an app teaches people to refuse.
        if (typeof WORKER_MODULES !== 'undefined') WORKER_MODULES.initPush().catch(() => {});
        // A push tapped with the app closed lands here: the worker has no way to say
        // "that one" except through this message, and marking it read is what makes
        // the badge agree with the lock screen they just cleared.
        if ('serviceWorker' in navigator) {
            navigator.serviceWorker.addEventListener('message', (event) => {
                const data = event.data || {};
                if (data.type !== 'alert-clicked' || data.id == null) return;
                WORKER_MODULES.markAlertRead(String(data.id)).catch(() => {});
            });
        }
        // A notice read on *another* device is the one read this tab cannot hear about - the
        // message above only covers a push tapped in this tab - so coming back to the tab is
        // the moment the badge is re-read. Both events are watched because they are not the
        // same one: ``visibilitychange`` fires when the page is hidden and shown (a phone
        // locked and unlocked), ``focus`` when the window is returned to without the page
        // ever being hidden (a desktop alt-tab). ``resyncUnreadBadge`` collapses the pair.
        this.bindUnreadResync();
    },

    //: Set once: ``init`` runs a single time in production, but the suites call it on several
    //: environments, and a second ``focus`` listener would ask the server twice per return.
    _unreadResyncBound: false,

    /** Watch for the tab being returned to, so the unread badge can be re-read. */
    bindUnreadResync() {
        if (this._unreadResyncBound) return;
        this._unreadResyncBound = true;
        if (typeof window !== 'undefined' && window.addEventListener) {
            window.addEventListener('focus', () => this.resyncUnreadBadge());
            // The same return-to-tab moments, for the console's crossings badge: a crossing
            // answered from another admin's console is invisible to this one until it is
            // re-asked, and "Approvals 3" pointing at a queue somebody already emptied is a
            // badge that trains nobody to read it.
            window.addEventListener('focus', () => this.refreshApprovalsBadge());
        }
        if (typeof document !== 'undefined' && document.addEventListener) {
            document.addEventListener('visibilitychange', () => {
                if (document.visibilityState !== 'hidden') {
                    this.resyncUnreadBadge();
                    this.refreshApprovalsBadge();
                }
            });
        }
    },

    /**
     * Draws again with the company's own name, once the settings row has answered.
     *
     * A first visit on a cold cache paints the shipped lockup and corrects it a moment
     * later; a second visit paints the configured one immediately and never gets here.
     * The one thing not worth a company's name is losing what somebody has typed, so a
     * panel with a filled-in field keeps its values - and is drawn correctly on the next
     * navigation, because by then the answer is in the cache either way.
     */
    repaintAfterBrand() {
        if (this.isMidEntry()) return;
        this.renderApp();
    },

    /** Whether any field on screen already holds something somebody typed. */
    isMidEntry() {
        const fields = document.querySelectorAll('#app input, #app textarea, #app select');
        return Array.prototype.some.call(fields, (field) => String(field.value || '') !== '');
    },

    renderApp() {
        const container = this.appContainer;
        if (!State.user) {
            container.className = '';
            return this.renderLogin();
        }
        // A ``worker`` belongs on the handset and nowhere else, and a ``moallem`` with them:
        // a lead worker is not a console operator here - every ``/admin/*`` endpoint refuses
        // that role (``admin_only`` names ``admin`` and ``head_admin``) - so the console was
        // a screen of failing requests for them, and the handset, where they clock in and
        // read their own hours exactly like a worker, is the one screen they can use.
        if (State.user.role === 'worker' || State.user.role === 'moallem') {
            return this.renderWorkerPortal();
        }
        // An administrator reaches the console by default and steps onto the handset from it:
        // ``handsetMode`` is set only through ``openHandset()``, which refuses a role that
        // cannot clock in, so the two ways onto this screen are one rule that cannot drift.
        if (State.handsetMode && this.canOpenHandset()) return this.renderWorkerPortal();
        return this.renderAdminConsole();
    },

    /**
     * The vendor's credit, which closes every page: login, the handset, the console, and
     * the page an administrator is left on when the console cannot load.
     *
     * A line rather than a second lockup. The app belongs to AL-JEHAD; this is who wrote
     * it, and a vendor mark set at the size of the company's would read as a second owner
     * of the screen. It is returned as a ``<p>`` because every host already has a footer
     * of its own - the login screen's company line, the handset's page, the console's
     * frame - and a ``<footer>`` inside a ``<footer>`` is not a landmark, it is invalid.
     */
    // -----------------------------------------------------------------
    //  The console, and the handset an administrator may step onto
    // -----------------------------------------------------------------
    //: Roles that run the console *and* work a shift of their own. ``worker`` is absent
    //: because the handset is already where it lives, and ``head_admin`` is absent on
    //: purpose: it is the role that owns the deployment rather than a rota. Adding one
    //: here is the whole change - every control below reads this list.
    handsetRoles: ['admin'],

    /** Does this account reach the console but still clock in? */
    canOpenHandset() {
        return this.handsetRoles.includes((State.user || {}).role);
    },

    /**
     * Leave the console for the clock.
     *
     * This is not a second, lighter flow: an administrator who works a site gets the
     * identical punch card a worker gets - the same geofence, the same liveness check and
     * the same face match against their own enrolled template - because the record has to
     * rest on the same evidence whoever the worker is. The role buys nothing at the gate.
     */
    openHandset() {
        if (!this.canOpenHandset()) return;
        State.handsetMode = true;
        this.renderApp();
    },

    /** Back to the console, releasing the camera the punch card had open. */
    closeHandset() {
        State.handsetMode = false;
        State.stopCamera();
        this.renderApp();
    },

    /** The way from the console to the clock, for the roles that have one. */
    handsetButtonHtml() {
        if (!this.canOpenHandset()) return '';
        return `<button type="button" class="ui-btn ui-btn-sm" data-open-handset="true" title="${this.escapeHtml(I18n.__('handsetHint'))}">${HAND_ICONS.clock}<span>${this.escapeHtml(I18n.__('clockIn'))}</span></button>`;
    },

    /**
     * One listener each for the two controls that move between the console and the
     * handset.
     *
     * Bound on the elements that were just rendered, like ``bindWorkerTabs``, so a repaint
     * cannot leave a second listener behind. Deliberate rather than an ``onclick``: the
     * document policy still allows inline event attributes only because the console has
     * them at 86 call sites, and that allowance is exactly what an injected
     * ``<img onerror>`` needs. ``tests/test_frontend_xss.py`` pins the count per file and
     * refuses a rise, so a new button is bound here instead.
     */
    bindHandsetControls(root) {
        const scope = root || document;
        const bind = (selector, run) => {
            const button = typeof scope.querySelector === 'function' ? scope.querySelector(selector) : null;
            if (!button || typeof button.addEventListener !== 'function') return;
            button.addEventListener('click', (event) => {
                event.preventDefault();
                run.call(this);
            });
        };
        bind('[data-open-handset]', this.openHandset);
        bind('[data-close-handset]', this.closeHandset);
    },

    /** A role code in the reader's own language. */
    roleLabel(role) {
        const keys = {
            worker: 'roleWorker',
            moallem: 'roleMoallem',
            admin: 'roleAdmin',
            head_admin: 'roleHeadAdmin'
        };
        return keys[role] ? I18n.__(keys[role]) : String(role || '');
    },

    /**
     * The way back to the console, on the handset's own header.
     *
     * Only for an administrator who stepped out here. A worker has no console to return
     * to, and a button that promised one would be a dead end on their screen.
     */
    closeHandsetButtonHtml() {
        if (!State.handsetMode || !this.canOpenHandset()) return '';
        return `<button type="button" class="icon-button" data-close-handset="true" aria-label="${this.escapeHtml(I18n.__('backToConsole'))}" title="${this.escapeHtml(I18n.__('backToConsole'))}">${ADMIN_ICONS.admin}</button>`;
    },

    creditHtml() {
        const line = I18n.__('poweredBy').replace('{brand}', BRAND.poweredBy);
        return `<p class="app-credit">${this.escapeHtml(line)}</p>`;
    },

    /**
     * The company's four lines as the login panel draws them.
     *
     * A line the company emptied is **not drawn**, which is the visible half of the rule the
     * settings row stores: ``''`` means "this line is not printed", and a paragraph that
     * exists to hold nothing is a blank row in the middle of the lockup - the same reason the
     * printed sheet omits it. ``null`` never reaches here; it was resolved to the shipped
     * line when the settings were merged (see ``Brand``).
     */
    loginLockupHtml() {
        const lines = [
            ['login-company', BRAND.name],
            ['login-legal', BRAND.legal],
            ['login-est', BRAND.est],
            ['login-tagline', BRAND.tagline]
        ];
        return lines
            .filter(([, value]) => String(value === null || value === undefined ? '' : value).trim() !== '')
            .map(([className, value]) => `<p class="${className}">${this.escapeHtml(value)}</p>`)
            .join('');
    },

    /**
     * The sentence at the foot of the login screen, naming the company this is running for.
     *
     * It used to be a translated constant with the name typed into it, which is how the one
     * screen whose job is to say whose app this is would have gone on naming the old company
     * after a rename. The prose stays in the translation tables; the name in it is the
     * company's own, and a company that removed both of its lockup lines gets no sentence
     * rather than one that cannot say whose it is.
     */
    companyFooterHtml() {
        const name = Brand.fullName();
        if (!name) return '';
        return this.escapeHtml(I18n.__('companyFooter').replace('{brand}', name));
    },

    langSelectHtml() {
        const options = [['en', 'EN'], ['ar', 'AR'], ['hi', 'HI'], ['ur', 'UR']];
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
                        <img class="login-mark" src="${this.escapeHtml(BRAND.mark)}" alt="" width="78" height="103" decoding="async">
                        <!-- dir="ltr" is load-bearing: in Arabic the page is RTL, and a
                             trailing period on "INTERNATIONAL CO." is bidi-neutral, so the
                             browser parks it at the *left* of the Latin run. A logo does
                             not mirror. -->
                        <div class="login-lockup" dir="ltr">${this.loginLockupHtml()}</div>
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
            <footer class="login-footer">${this.companyFooterHtml()}${this.creditHtml()}</footer>`;
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
                <p class="hand-who-role">${this.escapeHtml(this.roleLabel(State.user.role))}</p>
            </div>
            <div class="hand-actions">
                ${this.closeHandsetButtonHtml()}
                ${this.langSelectHtml()}
                <button type="button" onclick="UI.toggleTheme()" class="icon-button" aria-label="${this.escapeHtml(I18n.__('theme'))}">${ADMIN_ICONS.theme}</button>
                <button type="button" onclick="UI.logout()" class="icon-button" aria-label="${this.escapeHtml(I18n.__('logout'))}">${ADMIN_ICONS.logout}</button>
            </div>`;
    },

    //: The worker's bottom tabs. ``notes`` is the written channel to the admin: a
    //: password request, a missing item, a question about hours - each of which used to
    //: require catching somebody on the phone, and then leaving no record of it. ``alerts``
    //: is the other direction, and the newer of the two: what the *system* has told the
    //: worker, which until now had nowhere to land - a push went to a phone that had never
    //: subscribed, and the record behind it was readable by nobody.
    workerTabIds: ['clock', 'history', 'alerts', 'notes', 'profile'],

    workerTabBarHtml() {
        const tabs = [
            { id: 'clock', icon: 'clock', label: I18n.__('clock') },
            { id: 'history', icon: 'history', label: I18n.__('history') },
            { id: 'alerts', icon: 'alert', label: I18n.__('alerts') },
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
                    ${this.alertTabBadgeHtml(tab.id)}
                </button>`).join('')}
        </nav>`;
    },

    /**
     * The unread count, on the alerts tab's icon.
     *
     * A number in the corner rather than a dot: "there is something" is not the useful
     * half of it - how much is waiting is what decides whether this is worth reading now.
     * It sits over the icon because a numeral appended to the tab's word would push the
     * word off the bar's centre line, and the tab bar is a grid of equal columns.
     *
     * ``aria-hidden`` on purpose. The button's accessible name is the tab's label, and
     * "Alerts 3" announced as the name of the tab is not what the tab is called; the count
     * reaches a screen reader in a sentence that says what it is counting - the banner on
     * the clock panel, which is a button reading "3 new alerts, <the newest one>, See all".
     */
    alertTabBadgeHtml(tabId) {
        const count = (typeof WORKER_MODULES === 'undefined' || tabId !== 'alerts')
            ? 0 : Math.max(0, Number(WORKER_MODULES.alertCount) || 0);
        if (count <= 0) return '';
        return `<span class="hand-tab-count" data-alert-count="${count}" aria-hidden="true">${
            count > 99 ? '99+' : count}</span>`;
    },

    /**
     * The tab bar redrawn where it stands.
     *
     * The bar is painted from one string that reads the current tab and the current count,
     * so anything that changes either has the same three lines to run. Called by tab
     * switches and by the alerts count arriving - a badge that only appeared after the next
     * tab switch would be a badge nobody sees.
     */
    repaintWorkerTabBar() {
        const nav = document.querySelector('.hand-tabs');
        if (!nav) return;
        nav.outerHTML = this.workerTabBarHtml();
        this.bindWorkerTabs(this.appContainer);
    },

    //: When the unread count was last re-read on returning to this tab, so the pair of events
    //: that one return produces (``visibilitychange`` and ``focus``) costs one request.
    _alertsResyncedAt: 0,

    /**
     * Re-read the unread count when this tab comes back into view.
     *
     * The badge is a number from the moment it was last fetched, and a notice can be read
     * somewhere else entirely: a push tapped on the worker's other phone, a shift closed by an
     * administrator. This tab has no way to hear about that, so a number painted before the
     * phone was put down would point at an inbox that is already empty. Returning to the tab
     * is that moment, and the count is re-asked rather than remembered.
     *
     * The handset is the only screen with a badge, so this asks nothing on the console or the
     * sign-in screen. A failure is silence, for the same reason it is on the clock panel: a
     * count that cannot be read is not worth an error on a phone at a gate, and the badge keeps
     * whatever it last said rather than being cleared on a guess.
     */
    resyncUnreadBadge() {
        const worker = !!State.user && (State.user.role === 'worker' || State.user.role === 'moallem');
        if (!State.token || !(worker || State.handsetMode)) return;
        if (typeof WORKER_MODULES === 'undefined' || !WORKER_MODULES.refreshAlerts) return;
        const now = Date.now();
        if (now - this._alertsResyncedAt < 1000) return;
        this._alertsResyncedAt = now;
        WORKER_MODULES.refreshAlerts().catch(() => {});
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
            <footer class="hand-credit">${this.creditHtml()}</footer>
        `;
        this.bindWorkerTabs(container);
        this.bindHandsetControls(container);
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
                    <div class="hand-card is-bare"><div id="workerDashboard"></div></div>
                    <div class="hand-card"><div id="devicePanel"></div></div>
                </div>
                <div class="hand-desk-col">
                    ${/* The inbox leads the column on a desk, because it is the only thing on
                         this screen that is addressed to the reader rather than recorded about
                         them - and on a wide screen there is room to show it open rather than
                         behind a tab. Read notices stay in the list: the point of the record is
                         that it is still there next week. */ ''}
                    <section class="hand-card"><div id="workerAlerts"></div></section>
                    <section class="hand-card">
                        <div class="hand-section-head">
                            <h3 class="hand-section-title">${this.escapeHtml(I18n.__('timesheetHistory'))}</h3>
                        </div>
                        <div id="historyTable">${this.loadingHtml()}</div>
                    </section>
                    <section class="hand-card"><div id="workerNotes"></div></section>
                    <section class="hand-card"><div id="pushSettings"></div></section>
                </div>
            </div>
            <footer class="hand-credit is-desk">${this.creditHtml()}</footer>
        `;
        this.bindHandsetControls(container);
        await WORKER_MODULES.renderClockPanel(document.getElementById('workerDashboard'));
        await WORKER_MODULES.renderAlerts(document.getElementById('workerAlerts'));
        await WORKER_MODULES.renderHistory(document.getElementById('historyTable'));
        await WORKER_MODULES.renderNotes(document.getElementById('workerNotes'));
        const pushHost = document.getElementById('pushSettings');
        if (pushHost) {
            pushHost.innerHTML = WORKER_MODULES.pushSettingsHtml();
            WORKER_MODULES.bindPushSettings(pushHost);
        }
        this.renderDevicePanel(document.getElementById('devicePanel'));
    },

    setWorkerTab(tab) {
        State.setWorkerTab(tab);
        if (Device.isMobile) {
            // The bar is repainted from the same source that drew it, so the aria-current
            // and what is on screen cannot disagree after a tab switch.
            this.repaintWorkerTabBar();
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
        } else if (tab === 'alerts') {
            main.innerHTML = `<div id="workerAlerts"></div>`;
            await WORKER_MODULES.renderAlerts(document.getElementById('workerAlerts'));
        } else if (tab === 'notes') {
            main.innerHTML = `<div id="workerNotes"></div>`;
            await WORKER_MODULES.renderNotes(document.getElementById('workerNotes'));
        } else if (tab === 'profile') {
            main.innerHTML = `<div id="workerProfile"></div><div id="pushSettings"></div><div id="devicePanel"></div>`;
            this.renderWorkerProfile(document.getElementById('workerProfile'));
            // The push card is bound to its own host rather than the profile's: two
            // delegated listeners on one ancestor would both answer a tap, and the
            // card re-renders in place while the account card does not.
            const pushHost = document.getElementById('pushSettings');
            if (pushHost) {
                pushHost.innerHTML = WORKER_MODULES.pushSettingsHtml();
                WORKER_MODULES.bindPushSettings(pushHost);
            }
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
            <div class="ui-stack">
                <h3 class="ui-title">${title}</h3>
                ${steps.map((step, index) => `
                    <div class="help-step"><span class="help-num">${index + 1}</span><span>${step}</span></div>`).join('')}
                ${extraHtml}
                <button onclick="Modal.close()" class="ui-btn ui-btn-primary is-block">${I18n.__('close')}</button>
            </div>
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
                <div class="ui-stack">
                    <h3 class="ui-title">${title}</h3>
                    <p class="ui-note is-body">${body}</p>
                    ${steps.map((step, index) => `
                        <div class="help-step"><span class="help-num">${index + 1}</span><span>${step}</span></div>`).join('')}
                    ${!Location.isSecure ? `
                        <div class="ui-alert is-warn is-stacked">
                            <p class="ui-strong">${I18n.__('openThisLink')}</p>
                            <p class="ui-note is-mono">${Location.httpsLink}</p>
                            <button onclick="UI.copyHttpsLink()" class="ui-btn ui-btn-warn ui-btn-sm">${I18n.__('copyLink')}</button>
                            <p class="ui-note">${I18n.__('insecureTunnelHint')}</p>
                        </div>` : ''}
                    <hr class="ui-divider">
                    <p class="ui-note">${I18n.__('gpsBlockedBody')}</p>
                    <div class="ui-pair">
                        <button id="retryGps" class="ui-btn ui-btn-primary is-grow">${I18n.__('retry')}</button>
                    </div>
                    <button id="cancelLocation" class="ui-btn ui-btn-quiet is-block">${I18n.__('cancel')}</button>
                </div>
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
            <div class="camera-head" style="padding-top:calc(16px + var(--safe-top))">
                <div>
                    <p class="camera-action">${I18n.__(actionKey)}</p>
                    <p class="camera-coords" id="cameraCoords">📍 ${coords}</p>
                    <p class="camera-window" id="cameraWindow" data-verdict="" hidden></p>
                </div>
                <button onclick="UI.closeCamera()" class="icon-button" style="background:rgba(255,255,255,.12);border-color:rgba(255,255,255,.25);color:#fff">✕</button>
            </div>
            <video id="attendanceVideo" autoplay playsinline muted></video>
            <div class="camera-controls">
                <p class="camera-framing" id="framingHint" data-framing="" hidden></p>
                <div class="camera-controls-row">
                    <div class="camera-spacer"></div>
                    <button id="captureBtn" class="shutter-button" aria-label="${I18n.__('captureSubmit')}"></button>
                    <div class="camera-spacer"></div>
                </div>
                <p class="camera-hint">${I18n.__('captureHint')}</p>
            </div>
        `;
        document.body.appendChild(overlay);
        this._cameraOpen = true;
        document.body.style.overflow = 'hidden';
        // Started before the stream is awaited, so the line is usually there by the time the
        // worker has their face in the frame. It never blocks the camera: see ``loadSiteWindow``.
        this.loadSiteWindow(coords, action).catch(() => {});

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

    async submitAttendance(action, coords, confirmedEarly = false) {
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
        const blob = await Camera.snapshot(video);
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
        // Only on the second attempt: the server decides whether a shift is short, and
        // this is the phone saying the worker was told and meant it.
        if (confirmedEarly) formData.append('confirm_early_checkout', '1');

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
            // The early clock-out warning is a question, not a refusal: the server stopped
            // *before* it wrote a row or closed the shift, so answering it is just another
            // punch. A cancel therefore costs the worker nothing but the tap - the shift,
            // the hours and the open camera are all exactly as they were.
            if (err && err.errorCode === 'confirm_early_checkout') {
                captureButton.disabled = false;
                if (await this.confirmEarlyClockOut(err.detail)) {
                    return this.submitAttendance(action, coords, true);
                }
                return;
            }
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
     * Ask before recording a shift that is short of the paid day.
     *
     * Resolves ``true`` when the worker chose to record it and ``false`` when they changed
     * their mind. The numbers are the server's - it sent them with the refusal - so the
     * sentence the worker reads on a site in Cairo says the same thing the record will.
     */
    confirmEarlyClockOut(detail) {
        const paid = Number((detail && detail.paid_hours) || 0).toFixed(2);
        const regular = Number((detail && detail.regular_hours) || 0).toFixed(2);
        return new Promise((resolve) => {
            Modal.open(`
                <div class="ui-stack">
                    <h3 class="ui-title">${I18n.__('earlyClockOutTitle')}</h3>
                    <p class="ui-note is-body">${I18n.__('earlyClockOutBody')
                        .replace('{paid}', paid)
                        .replace('{regular}', regular)}</p>
                    <div class="ui-pair">
                        <button id="confirmEarlyOut" class="ui-btn ui-btn-primary is-grow">${I18n.__('earlyClockOutConfirm')}</button>
                        <button id="cancelEarlyOut" class="ui-btn ui-btn-quiet is-grow">${I18n.__('cancel')}</button>
                    </div>
                </div>
            `, { dismissible: false, overCamera: true });
            // Not dismissible: the worker must choose. A backdrop tap that silently
            // resolved the promise would leave a punch in limbo, neither sent nor cancelled.
            const finish = (answer) => { Modal.close(); resolve(answer); };
            document.getElementById('confirmEarlyOut').addEventListener('click', () => finish(true));
            document.getElementById('cancelEarlyOut').addEventListener('click', () => finish(false));
        });
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

    /**
     * The clock-in window where this phone is standing, on the punch card itself.
     *
     * The window a punch is judged by belongs to the site the geofence puts the phone inside,
     * and until now the only place that judgement appeared was an administrator's notification
     * ("arrival outside the clock-in window") - a sentence about the worker that the worker
     * never read. This asks the server the same question the punch will ask a moment later, so
     * "am I early, on time or late" is answered while there is still time to do something
     * about it, instead of afterwards.
     *
     * Clock-in only. A clock-out is not judged by this window - the hours are - so printing
     * "late" over somebody who is *leaving* would be a lie told by the screen.
     *
     * It is advice, never a gate. A failed request (no signal, an older server, a session that
     * expired while the card was open) leaves the line hidden and the shutter exactly as usable
     * as it was; the punch is still judged on the server, from a fresh fix, at the shutter. The
     * line is read from the fix the card opened with and is therefore a moment stale - what it
     * says is the site's window, not a second-by-second race with the record.
     */
    async loadSiteWindow(coords, action) {
        if (action !== 'Clock In') return;
        let info;
        try {
            info = await API.request(
                `/worker/me/site-window?location_input=${encodeURIComponent(coords)}`
            );
        } catch (err) {
            return;
        }
        // Re-read the element: the worker may have closed the card, or taken the photo, while
        // this request was in flight, and painting into a removed card is painting into nothing.
        const line = document.getElementById('cameraWindow');
        if (!line) return;
        if (info && info.on_site) {
            line.textContent = this.siteWindowLine(info);
            line.dataset.verdict = String(info.verdict || '');
        } else {
            // Not inside any geofence: the punch will be refused, so the useful thing to say is
            // where to stand, not a window for a site this worker is not standing on.
            line.textContent = I18n.__('punchWindowOffSite');
            line.dataset.verdict = 'off_site';
        }
        line.hidden = false;
    },

    /**
     * One line: the site, the window in force there, and where now falls in it.
     *
     * Composed here rather than on the server because this card exists in three languages, and
     * the server sends the arithmetic (verdict and minutes) rather than a sentence. The site
     * name and the window label are values off the wire, so they arrive as text, not as markup.
     */
    siteWindowLine(info) {
        const where = I18n.__('punchWindowSite')
            .replace('{site}', String(info.site_name || ''))
            .replace('{window}', String((info.window || {}).window || ''))
            .trim();
        return `${where} · ${this.siteWindowVerdict(info)}`;
    },

    siteWindowVerdict(info) {
        const minutes = this.windowMinutes(info.minutes_off);
        if (info.verdict === 'early') return I18n.__('punchWindowEarly').replace('{minutes}', minutes);
        if (info.verdict === 'late') return I18n.__('punchWindowLate').replace('{minutes}', minutes);
        return I18n.__('punchWindowOnTime');
    },

    /** "25m" / "1h 5m" - how far outside the window this arrival is. */
    windowMinutes(value) {
        const minutes = Math.max(0, Math.round(Number(value) || 0));
        const hours = Math.floor(minutes / 60);
        return hours > 0 ? `${hours}h ${minutes % 60}m` : `${minutes}m`;
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
        //: The good window, in real distances. Face width is ~0.16 m; on a ~66° phone
        //: front camera the face's share of the frame is (0.16 / (2·d·tan 33°))², which
        //: puts 0.7 m at ~3% of the frame area and 1.2 m at ~1.1%. The old floor (12%)
        //: demanded ~0.36 m — an arm's-length-plus selfie — and told everybody standing
        //: a sensible step away to "move closer". The window below accepts the whole
        //: 0.7–1.5 m band the pipeline was measured for, warns only when the face is
        //: genuinely marginal (<1%: past ~1.9 m), and asks for more distance only above
        //: 35% (~0.21 m, where the face no longer fits the alignment template's margins).
        //: The warn floor is 0.6% (~1.6 m): genuinely out of range, not merely far.
        if (metrics.face.area < 0.006) return 'framingCloser';
        if (metrics.face.area > 0.35) return 'framingFurther';
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
        if (typeof UI_MODULES !== 'undefined') return this.paintAdminConsole();
        // First administrator frame of the session: the console's own module is not here
        // yet. ``loadConsoleModule`` fetches it once (two frames can race, and the second
        // must not start a second copy of a 255 KB download), then paints. Nothing is
        // rendered until it lands, so there is no half-console on screen.
        return this.loadConsoleModule().then(() => {
            // A ``#shifts=2026-08-01..2026-08-31`` fragment is parsed by the console
            // module, which did not exist when ``init`` looked at it, so the link is
            // adopted here - before the first paint, so the console opens on that period.
            return this.applyShiftsLink()
                .catch(() => {})
                .then(() => this.paintAdminConsole());
        }, (err) => this.renderConsoleFailure(err));
    },

    /**
     * The console's two layouts, once its module is in memory.
     *
     * Separate from ``renderAdminConsole`` on purpose: every later repaint (a theme
     * toggle, a rotate, a language switch) takes this path synchronously, so nothing the
     * console does ends up waiting a microtask on a promise that is already resolved.
     */
    paintAdminConsole() {
        if (Device.isMobile) this.renderAdminMobile();
        else this.renderAdminDesktop();
        this.refreshApprovalsBadge();
        return this.renderAdminTab(State.adminTab);
    },

    //: When the crossings badge was last re-read, so the pair of events one tab return
    //: produces (``visibilitychange`` and ``focus``) costs one request - the same throttle
    //: ``resyncUnreadBadge`` runs for the worker's unread badge.
    _crossingsResyncedAt: 0,

    /**
     * Read the unanswered-crossing count and paint it on the Approvals tab.
     *
     * The count is a number from the moment it was fetched, and a crossing is answered by
     * *somebody else's* console as often as by this one - two admins on two phones is the
     * normal case, not the edge case - so it is re-asked when the tab returns to view,
     * the same moments the worker's unread badge resyncs on.
     *
     * Asked on the console only: a worker's handset has no Approvals tab, and a request
     * every gate selfie costs would be refused with 403 anyway. A failure is silence, for
     * the reason every badge in this app is: a count that cannot be read is not worth an
     * error replacing a screen that works, and the badge keeps whatever it last said.
     */
    async refreshApprovalsBadge(force) {
        const admin = !!State.user && State.user.role !== 'worker' && State.user.role !== 'moallem';
        if (!State.token || !admin) return;
        if (typeof API === 'undefined' || !API.request) return;
        const now = Date.now();
        if (!force && now - this._crossingsResyncedAt < 1000) return;
        this._crossingsResyncedAt = now;
        try {
            const data = await API.request('/admin/overtime/crossings/count');
            State.crossingsWaiting = Math.max(0, Number(data && data.count) || 0);
        } catch (err) {
            return; // keep the last painted count; the next return-to-tab retries
        }
        this.paintApprovalsBadge();
    },

    /**
     * Repaint every Approvals tab button's badge in place.
     *
     * The rail and the phone strip both render a button with ``data-admin-tab="Approvals"``
     * but only one layout is up at a time; repainting by query rather than re-rendering the
     * nav means a decision on the open tab clears its own badge without rebuilding the rail.
     * ``aria-hidden`` on the numeral, matching the worker's unread badge: "Approvals 3" is
     * not the tab's name, and the count reaches a screen reader through the tab's own
     * ``aria-label`` instead.
     */
    paintApprovalsBadge() {
        if (typeof document === 'undefined' || !document.querySelectorAll) return;
        const count = Math.max(0, Number(State.crossingsWaiting) || 0);
        document.querySelectorAll('[data-admin-tab="Approvals"]').forEach((button) => {
            if (!button || typeof button.querySelector !== 'function') return;
            let badge = null;
            try { badge = button.querySelector('[data-crossings-badge]'); } catch (err) { badge = null; }
            if (count > 0) {
                if (!badge) {
                    badge = document.createElement('span');
                    badge.className = 'tab-count';
                    badge.setAttribute('data-crossings-badge', 'true');
                    badge.setAttribute('aria-hidden', 'true');
                    if (typeof button.appendChild === 'function') button.appendChild(badge);
                }
                badge.textContent = count > 99 ? '99+' : String(count);
                badge.setAttribute('data-crossings-badge', String(count));
                button.setAttribute('aria-label', `${I18n.__('pendingReviews')} - ${I18n.__('approvalsBadgeLabel')} ${count}`);
            } else if (badge && typeof badge.remove === 'function') {
                badge.remove();
                button.removeAttribute('aria-label');
            }
        });
    },

    /**
     * Fetch ``admin_modules.js``, once per session.
     *
     * It is the largest file in the frontend and no worker screen can reach a line of it,
     * so it is not in ``index.html``: a phone at a gate used to download the whole back
     * office, on the connection this app is documented to assume is the worst one. The
     * promise is what makes it once - a repaint while the first request is still in flight
     * joins the same download instead of starting another.
     */
    loadConsoleModule() {
        if (typeof UI_MODULES !== 'undefined') return Promise.resolve(UI_MODULES);
        if (this._consoleModule) return this._consoleModule;
        this._consoleModule = new Promise((resolve, reject) => {
            const script = document.createElement('script');
            script.src = 'admin_modules.js';
            script.addEventListener('load', () => {
                // A 200 that is not this module - a captive portal, a truncated deploy -
                // leaves the binding missing. Fail where the message can still name it.
                if (typeof UI_MODULES === 'undefined') reject(new Error(I18n.__('consoleUnavailable')));
                else resolve(UI_MODULES);
            });
            script.addEventListener('error', () => reject(new Error(I18n.__('consoleUnavailable'))));
            (document.head || document.body).appendChild(script);
        });
        this._consoleModule = this._consoleModule.catch((err) => {
            // A failure is not cached: the next frame tries again, so a connection that
            // comes back is enough to get the console without a sign-out and a sign-in.
            this._consoleModule = null;
            throw err;
        });
        return this._consoleModule;
    },

    /**
     * What an administrator sees when the console's module cannot be fetched.
     *
     * Deliberately not the sign-in screen: they are still signed in, their session is
     * still valid, and telling them otherwise sends them to re-enter a password that was
     * never the problem. What they need is the reason and a way back in.
     */
    renderConsoleFailure(err) {
        const container = this.appContainer;
        container.className = 'admin';
        container.innerHTML = `
            <div class="admin-frame">
                <div class="ui-surface console-failure">
                    <div class="ui-stack">
                        <p class="ui-card-title">${this.escapeHtml(I18n.__('consoleUnavailable'))}</p>
                        <p class="ui-hint">${this.escapeHtml(err && err.message ? err.message : '')}</p>
                        <div class="ui-row">
                            <button type="button" class="ops-btn ops-btn-primary" data-console-failure="reload">${I18n.__('reload')}</button>
                            <button type="button" class="ops-btn" data-console-failure="logout">${I18n.__('logout')}</button>
                        </div>
                    </div>
                </div>
                <footer class="admin-credit">${this.creditHtml()}</footer>
            </div>`;
        // Bound here rather than in the markup: the document policy still allows inline
        // handlers only because older screens use them, and this is not one of them.
        container.querySelector('[data-console-failure="reload"]')
            .addEventListener('click', () => location.reload());
        container.querySelector('[data-console-failure="logout"]')
            .addEventListener('click', () => this.logout());
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
        const role = this.roleLabel(user.role);
        return `
            <span class="admin-identity">
                <span class="ops-avatar" aria-hidden="true">${this.escapeHtml(this.adminInitials(name))}</span>
                <span class="admin-identity-text">
                    <span class="admin-identity-name">${this.escapeHtml(name)}</span>
                    <span class="admin-identity-role">${this.escapeHtml(role)}</span>
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
        this.revealActiveConsoleTab();
    },

    /**
     * Scroll the phone's tab strip far enough to show the tab that is lit up.
     *
     * Eight tabs come to about 820px and only about 290-360 of them fit on a handset,
     * so the strip is a window onto a list that is mostly off screen - and nothing ever
     * moved that window. Tapping "Admin", the last tab, left it at x=729 inside a 358px
     * strip: the band kept showing "Active Shifts" while the pane below changed to a
     * screen the header could no longer name. It is the same on a reload, whenever the
     * remembered tab is not one of the first two.
     *
     * The strip is moved and nothing else - ``scrollIntoView`` scrolls the page as well,
     * and on this screen that would yank the pane out from under the thumb that tapped.
     * Instant, not smooth, for the same reason: the pane is being replaced in the same
     * frame, and a strip still gliding into place afterwards reads as lag.
     *
     * ``move`` is a *visual* distance, so it needs no left-to-right branch: in a
     * right-to-left strip (Arabic, Urdu) ``scrollLeft`` counts the other way and the
     * arithmetic below lands on the same answer.
     */
    revealActiveConsoleTab() {
        const strip = document.querySelector('.admin-tabs');
        if (!strip) return; // the desktop rail does not scroll, and has no strip
        const active = strip.querySelector('[aria-current="page"]');
        if (!active) return;
        const frame = strip.getBoundingClientRect();
        const box = active.getBoundingClientRect();
        if (!box.width || !frame.width) return;
        // 14px of the neighbouring tab stays in view. It is the only affordance this
        // strip has: tabs run to both edges, so a strip scrolled to the end looks
        // exactly like a strip that does not scroll.
        const margin = 14;
        let move = 0;
        if (box.left < frame.left + margin) move = box.left - frame.left - margin;
        else if (box.right > frame.right - margin) move = box.right - frame.right + margin;
        if (!move) return;
        strip.scrollLeft += move;
    },

    renderAdminMobile() {
        const container = this.appContainer;
        container.className = 'admin';
        const active = adminTabRecord(State.adminTab);
        container.innerHTML = `
            <header class="admin-mobile-head">
                ${this.adminNavHtml('strip')}
            </header>
            <div class="admin-frame">
                <div class="admin-mobile-bar">
                    <div class="admin-mobile-bar-lead">
                        <span class="admin-brand-mark" aria-hidden="true"><img src="${BRAND.mark}" alt="" width="18" height="24"></span>
                        <h2 class="admin-title" id="adminTitle">${this.escapeHtml(I18n.__(active.key))}</h2>
                    </div>
                    <div class="admin-actions">
                        ${this.handsetButtonHtml()}
                        ${this.langSelectHtml()}
                        <button type="button" onclick="UI.toggleTheme()" class="icon-button" aria-label="${this.escapeHtml(I18n.__('theme'))}">${ADMIN_ICONS.theme}</button>
                        <button type="button" onclick="UI.logout()" class="icon-button" aria-label="${this.escapeHtml(I18n.__('logout'))}">${ADMIN_ICONS.logout}</button>
                    </div>
                </div>
                <p class="ui-hint admin-mobile-hint" id="adminSubtitle">${this.escapeHtml(I18n.__(active.hint))}</p>
                <div id="adminContent" class="ui-surface"></div>
                <footer class="admin-credit">${this.creditHtml()}</footer>
            </div>`;
        this.bindHandsetControls(container);
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
                                ${this.handsetButtonHtml()}
                                ${this.adminIdentityHtml()}
                                ${this.langSelectHtml()}
                                <button type="button" onclick="UI.toggleTheme()" class="icon-button" aria-label="${this.escapeHtml(I18n.__('theme'))}">${ADMIN_ICONS.theme}</button>
                                <button type="button" onclick="UI.logout()" class="icon-button" aria-label="${this.escapeHtml(I18n.__('logout'))}">${ADMIN_ICONS.logout}</button>
                            </div>
                        </header>
                        <div id="adminContent" class="ui-surface"></div>
                    </div>
                </div>
                <footer class="admin-credit">${this.creditHtml()}</footer>
            </div>`;
        this.bindHandsetControls(container);
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
                case 'Alerts': await UI_MODULES.renderAlerts(content); break;
                case 'Sites': await UI_MODULES.renderSites(content); break;
                case 'Credentials': await UI_MODULES.renderCredentials(content); break;
                case 'Links': await UI_MODULES.renderLinks(content); break;
                case 'Notes': await UI_MODULES.renderNotes(content); break;
                case 'Admin': await UI_MODULES.renderAdminManagement(content); break;
                case 'Shifts': await UI_MODULES.renderShifts(content); break;
                default:
                    content.innerHTML = `<div class="ui-empty">${tab} ${I18n.__('comingSoon')}</div>`;
            }
        } catch (err) {
            // The server's sentence is escaped like any other server text: it is written by
            // whichever endpoint refused the request, and "it is our own message" stops
            // being true the moment one of them interpolates something a client sent.
            content.innerHTML = `<p class="ui-note is-body is-danger">${I18n.__('error')}: ${this.escapeHtml(err.message)}</p>`;
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
                <div class="ui-grid three">
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
                    <div class="ui-row is-bottom">
                        <button type="button" data-force-in="true" onclick="UI.forceIn()"
                                class="ops-btn ops-btn-primary is-block"${disabledAttribute}>${UI_MODULES.OPS_ICONS.person}${escapeHtml(I18n.__('forceIn'))}</button>
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

    /**
     * The Force Out question, asked before anything is written.
     *
     * A forced close is the one clock-out nobody's phone pressed, and its usual case is the
     * forgotten clock-out: the session has run on for hours the worker was not on site. The
     * clock's own figure is offered as the default and the administrator can name the hours
     * they are actually authorising instead - both numbers end up on the record (the audit
     * event carries the clock's figure beside the recorded one), and hours past the paid day
     * are held for the ordinary overtime approval either way.
     */
    forceOutModal(workerId, workerName, clockInTime) {
        const id = this.escapeHtml ? this.escapeHtml(String(workerId)) : String(workerId);
        const name = this.escapeHtml ? this.escapeHtml(String(workerName || workerId)) : String(workerName || workerId);
        const clockIn = this.escapeHtml ? this.escapeHtml(String(clockInTime || '')) : String(clockInTime || '');
        const hoursField = [
            `<p class="ops-note" style="margin-top:0">`,
            this.escapeHtml(I18n.__('forceOutHint')),
            `</p>`,
            clockIn ? `<p class="ops-note">${this.escapeHtml(I18n.__('approvalsClockIn'))}: ${clockIn}</p>` : '',
            `<label class="ops-stat-label" for="forceOutHours">${this.escapeHtml(I18n.__('forceOutHoursLabel'))}</label>`,
            `<input id="forceOutHours" class="ops-field" type="number" min="0" max="24" step="0.25" inputmode="decimal" />`,
            `<p class="ops-note">${this.escapeHtml(I18n.__('forceOutHoursHint'))}</p>`
        ].join('');
        const backdrop = Modal.open(`
            <h3 style="margin-top:0">${this.escapeHtml(I18n.__('forceOutTitle').replace('{name}', name))}</h3>
            ${hoursField}
            <div class="ui-row" style="margin-top:16px">
                <button type="button" class="ops-btn ops-btn-danger" id="forceOutGo">${this.OPS_ICONS ? this.escapeHtml(this.OPS_ICONS.check) : ''}${this.escapeHtml(I18n.__('forceOutConfirm'))}</button>
                <button type="button" class="ops-btn" onclick="Modal.close()">${this.escapeHtml(I18n.__('cancel'))}</button>
            </div>`, { dismissible: true });
        if (!backdrop) return;
        backdrop.querySelector('#forceOutGo').addEventListener('click', async () => {
            const field = backdrop.querySelector('#forceOutHours');
            const raw = String((field && field.value) || '').trim();
            let hours = null;
            if (raw) {
                hours = Number(raw);
                if (!isFinite(hours) || hours < 0 || hours > 24) {
                    Toast.error(I18n.__('forceOutHoursHint'));
                    return;
                }
            }
            const go = backdrop.querySelector('#forceOutGo');
            if (go) go.disabled = true;
            try {
                const body = { admin_id: State.user.id, worker_id: String(workerId), site_name: '' };
                if (hours !== null) body.hours = hours;
                const res = await API.request('/admin/force_clock_out', { method: 'POST', body });
                Modal.close();
                Toast.success(res.message || I18n.__('forceOut'));
                this.renderAdminTab(State.adminTab);
            } catch (err) {
                if (go) go.disabled = false;
                Toast.error(err.message);
            }
        });
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
