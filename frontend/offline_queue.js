/* =====================================================================
 *  offline_queue.js -- the client half of the offline punch contract.
 *
 *  WHY THIS FILE EXISTS
 *  --------------------
 *  A remote site loses signal and the worker still has to clock in and out. The
 *  server half of the feature (backend/offline_sync.py) only accepts a punch it
 *  can prove: the device's own wall clock is *evidence*, never authority. This
 *  file is the other half - it captures the punch while offline and replays it,
 *  signed, once the phone reconnects.
 *
 *  THE CONTRACT, IN ORDER
 *  ----------------------
 *  1. register once  POST /attendance/devices/register -> device_key, returned
 *                    exactly once. Kept in IndexedDB and never requested again.
 *  2. anchor         POST /attendance/anchors -> a moment the *server* signed.
 *                    Held in IndexedDB while offline; it is the only thing that
 *                    lets a punch claim a time.
 *  3. queue          clock-in/out + GPS + photo hash, HMAC-signed over a
 *                    canonical field string, stored in IndexedDB.
 *  4. replay         POST /attendance/sync -> a per-punch verdict; the response
 *                    carries a fresh anchor for the next offline window.
 *
 *  WHAT IS SIGNED (and why it must match exactly)
 *  ----------------------------------------------
 *  OFFLINE_CRYPTO.canonicalPunch() is a byte-for-byte port of
 *  offline_sync.canonical_punch(): same line order, same "v1" prefix, same
 *  "%.6f"/"%.1f" formatting, and an empty string - never "null" - for a missing
 *  field. Two implementations that disagree by one byte produce two different
 *  signatures, so backend/tests/test_offline_client_signing.py runs this file
 *  under Node and feeds the result through the real endpoint.
 *
 *  TIME, HONESTLY
 *  --------------
 *  * monotonic_offset_s comes from the *monotonic* clock
 *    (performance.timeOrigin + performance.now()). The user cannot wind that
 *    backwards, and unlike performance.now() alone it survives a page reload, so
 *    a punch queued before a crash still has a valid offset. The anchor is
 *    stamped server-side, so the measured offset is short by roughly one network
 *    round trip - bounded, sub-second, and always in the safe direction.
 *  * client_timestamp / client_offset_s come from the device wall clock and are
 *    used only as a *tamper signal*: client_offset_s is the drift of the phone
 *    clock away from the anchored clock, so an honest phone reports ~0 and a
 *    phone whose clock was moved reports hours. The server refuses such a punch
 *    (clock_tampered) rather than silently relocating it.
 *  * Residual risk, stated rather than hidden: a phone that is offline for a day
 *    can still claim any offset inside OFFLINE_PUNCH_MAX_AGE_HOURS. That is
 *    inherent to offline capture; the bound, plus the visible disagreement
 *    between client_timestamp and effective_time in the admin triage view, is
 *    what keeps it accountable.
 *
 *  AUTHORISATION
 *  -------------
 *  A queued punch is authorised by the session token that replays it, not by the
 *  password typed at capture time (the server cannot check a password with no
 *  network). The selfie is hashed and kept on the phone - there is no upload
 *  endpoint for it yet - so photo_sha256 binds the punch to that image.
 * ===================================================================== */
'use strict';

const OFFLINE_DB_NAME = 'site_attendance_offline';
const OFFLINE_DB_VERSION = 1;
/** Server default is OFFLINE_BATCH_MAX=50; stay under it and halve on a 413. */
const OFFLINE_BATCH_CHUNK = 25;
/** Keep local photos as long as the punch itself can still be replayed. */
const OFFLINE_PHOTO_RETENTION_HOURS = 72;
/** Local history of settled punches; the server keeps the authoritative copy. */
const OFFLINE_HISTORY_RETENTION_DAYS = 7;

class OfflineError extends Error {
    constructor(code, message) {
        super(message || code);
        this.name = 'OfflineError';
        this.code = code;
    }
}

// =====================================================================
//  Crypto: canonical strings, HMAC signing, hashing
// =====================================================================
const OFFLINE_CRYPTO = {
    SIGNATURE_VERSION: 1,

    /**
     * Python's fixed-point formatting, i.e. round-half-EVEN.
     *
     * Number.toFixed() rounds halves away from zero, so it disagrees with Python on
     * an exact tie - and a tie is reachable with real GPS data (an accuracy of
     * exactly 8.25 is a genuine reading, not a contrived one). A disagreement here
     * is a punch the phone can sign but the server can never verify, because the
     * server re-formats every value before checking the signature.
     */
    fixed(value, decimals) {
        if (value === null || value === undefined || value === '') return '';
        const number = Number(value);
        if (!isFinite(number)) return '';
        const factor = Math.pow(10, decimals);
        const scaled = number * factor;
        const floor = Math.floor(scaled);
        const fraction = scaled - floor;
        let rounded;
        if (fraction > 0.5) rounded = floor + 1;
        else if (fraction < 0.5) rounded = floor;
        else rounded = floor % 2 === 0 ? floor : floor + 1;   // half to even
        const text = (rounded / factor).toFixed(decimals);
        // Python keeps the sign of a value that rounds to zero: -0.0000004 -> "-0.000000".
        return rounded === 0 && number < 0 ? `-${text}` : text;
    },

    /** Python `f"{float(v):.6f}"`, and "" for a missing value. */
    fmtCoord(value) {
        return this.fixed(value, 6);
    },

    /** Python `f"{float(v):.1f}"`, and "" for a missing value. */
    fmtAccuracy(value) {
        return this.fixed(value, 1);
    },

    /** Round a coordinate to the 6 decimals that get signed and transmitted. */
    roundCoord(value) {
        if (value === null || value === undefined || value === '') return null;
        const number = Number(value);
        if (!isFinite(number)) return null;
        return Math.round(number * 1e6) / 1e6;
    },

    /**
     * The exact bytes that are signed. Changing this breaks every enrolled phone.
     * Port of offline_sync.canonical_punch().
     */
    canonicalPunch(fields) {
        return [
            'v' + this.SIGNATURE_VERSION,
            fields.device_id,
            fields.worker_id,
            fields.action,
            fields.client_punch_id,
            fields.nonce,
            fields.anchor_id,
            fields.effective_timestamp,
            this.fmtCoord(fields.lat),
            this.fmtCoord(fields.lon),
            this.fmtAccuracy(fields.accuracy),
            fields.photo_sha256 || ''
        ].join('\n');
    },

    /** URL-safe base64 (what the server hands out) -> raw bytes. */
    b64urlToBytes(token) {
        const normalized = String(token).trim().replace(/-/g, '+').replace(/_/g, '/');
        const padded = normalized + '='.repeat((4 - (normalized.length % 4)) % 4);
        const binary = atob(padded);
        const bytes = new Uint8Array(binary.length);
        for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
        return bytes;
    },

    bytesToHex(bytes) {
        let hex = '';
        for (let i = 0; i < bytes.length; i += 1) hex += bytes[i].toString(16).padStart(2, '0');
        return hex;
    },

    /** SHA-256 hex of a Blob, ArrayBuffer, Uint8Array or string. */
    async sha256Hex(input) {
        let data = input;
        if (typeof Blob !== 'undefined' && input instanceof Blob) data = await input.arrayBuffer();
        if (data instanceof ArrayBuffer) data = new Uint8Array(data);
        if (typeof data === 'string') data = new TextEncoder().encode(data);
        const digest = await crypto.subtle.digest('SHA-256', data);
        return this.bytesToHex(new Uint8Array(digest));
    },

    /** Hex HMAC-SHA256 over canonicalPunch(). Port of offline_sync.sign_punch(). */
    async signPunch(keyToken, fields) {
        const key = await crypto.subtle.importKey(
            'raw', this.b64urlToBytes(keyToken), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']
        );
        const signature = await crypto.subtle.sign(
            'HMAC', key, new TextEncoder().encode(this.canonicalPunch(fields))
        );
        return this.bytesToHex(new Uint8Array(signature));
    },

    /**
     * Wall-clock arithmetic on naive "YYYY-MM-DD HH:MM:SS" strings - the same
     * arithmetic Python does with datetime.strptime + timedelta. Interpreted as
     * UTC on purpose: parsing as local time would make a DST transition between
     * the anchor and the punch move the punch by a whole hour.
     */
    parseTs(value) {
        const match = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})/.exec(String(value || ''));
        if (!match) return NaN;
        return Date.UTC(
            Number(match[1]), Number(match[2]) - 1, Number(match[3]),
            Number(match[4]), Number(match[5]), Number(match[6])
        );
    },

    formatTs(epochMs) {
        const date = new Date(epochMs);
        if (isNaN(date.getTime())) return '';
        const pad = (n) => String(n).padStart(2, '0');
        return `${date.getUTCFullYear()}-${pad(date.getUTCMonth() + 1)}-${pad(date.getUTCDate())} ` +
            `${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}:${pad(date.getUTCSeconds())}`;
    },

    /** Monotonic epoch milliseconds; immune to the user changing the clock. */
    monotonicNow() {
        if (typeof performance !== 'undefined' && typeof performance.now === 'function'
            && typeof performance.timeOrigin === 'number') {
            return performance.timeOrigin + performance.now();
        }
        return Date.now();
    },

    randomHex(bytes) {
        const buffer = new Uint8Array(bytes);
        crypto.getRandomValues(buffer);
        return this.bytesToHex(buffer);
    },

    newPunchId() {
        if (typeof crypto.randomUUID === 'function') return crypto.randomUUID();
        return this.randomHex(16);
    }
};

// =====================================================================
//  IndexedDB: metadata, the queue itself, and the offline photos
// =====================================================================
function idbRequest(request) {
    return new Promise((resolve, reject) => {
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => reject(request.error || new Error('IndexedDB request failed'));
    });
}

const OFFLINE_DB = {
    _opening: null,

    open() {
        if (!this._opening) {
            this._opening = new Promise((resolve, reject) => {
                const request = indexedDB.open(OFFLINE_DB_NAME, OFFLINE_DB_VERSION);
                request.onupgradeneeded = () => {
                    const db = request.result;
                    if (!db.objectStoreNames.contains('meta')) {
                        db.createObjectStore('meta', { keyPath: 'key' });
                    }
                    if (!db.objectStoreNames.contains('punches')) {
                        const store = db.createObjectStore('punches', { keyPath: 'client_punch_id' });
                        store.createIndex('by_worker_status', ['worker_id', 'status']);
                        store.createIndex('by_worker', 'worker_id');
                    }
                    if (!db.objectStoreNames.contains('photos')) {
                        db.createObjectStore('photos', { keyPath: 'client_punch_id' });
                    }
                };
                request.onsuccess = () => resolve(request.result);
                request.onerror = () => reject(request.error || new Error('IndexedDB unavailable'));
            });
            // A failed open must not be cached forever: the next call retries.
            this._opening.catch(() => { this._opening = null; });
        }
        return this._opening;
    },

    async get(store, key) {
        const db = await this.open();
        return idbRequest(db.transaction(store, 'readonly').objectStore(store).get(key));
    },

    async put(store, value) {
        const db = await this.open();
        return idbRequest(db.transaction(store, 'readwrite').objectStore(store).put(value));
    },

    async remove(store, key) {
        const db = await this.open();
        return idbRequest(db.transaction(store, 'readwrite').objectStore(store).delete(key));
    },

    async all(store) {
        const db = await this.open();
        return idbRequest(db.transaction(store, 'readonly').objectStore(store).getAll());
    },

    async allByIndex(store, indexName, range) {
        const db = await this.open();
        const index = db.transaction(store, 'readonly').objectStore(store).index(indexName);
        return idbRequest(range ? index.getAll(range) : index.getAll());
    }
};

// =====================================================================
//  HTTP: raw status + body, so a 409/413/403 is a value and not a throw
// =====================================================================
const OfflineHttp = {
    baseURL() {
        if (typeof API !== 'undefined' && API.baseURL) return API.baseURL;
        return window.location.origin + '/api/v1';
    },

    async post(path, body, token) {
        const headers = { 'Content-Type': 'application/json' };
        if (token) headers['Authorization'] = `Bearer ${token}`;
        if (typeof API !== 'undefined' && API.isTunnelHost && API.isTunnelHost()) {
            headers['ngrok-skip-browser-warning'] = 'true';
        }
        try {
            const response = await fetch(this.baseURL() + path, {
                method: 'POST', headers, body: JSON.stringify(body || {})
            });
            const parsed = await response.json().catch(() => null);
            return { ok: response.ok, status: response.status, body: parsed, offline: false };
        } catch (error) {
            return { ok: false, status: 0, body: null, offline: true, error };
        }
    },

    /** The server's machine-readable reason, when it sent one. */
    errorCode(response) {
        const detail = response && response.body && response.body.detail;
        if (detail && typeof detail === 'object') return detail.error_code || null;
        return null;
    },

    errorMessage(response) {
        const detail = response && response.body && response.body.detail;
        if (detail && typeof detail === 'object') return detail.message || detail.error_code || 'Request failed';
        if (typeof detail === 'string') return detail;
        return response && response.offline
            ? 'The server could not be reached.'
            : `Request failed (HTTP ${response && response.status}).`;
    }
};

// =====================================================================
//  OFFLINE - the module the app talks to
// =====================================================================
const OFFLINE = {
    CRYPTO: OFFLINE_CRYPTO,

    deviceState: 'unknown',   // unknown | ready | key_lost | revoked | unauthorized
    lastError: null,
    lastSync: null,
    _ensuring: null,

    // ---- capability + environment -------------------------------------------------

    supported() {
        return typeof indexedDB !== 'undefined'
            && typeof crypto !== 'undefined'
            && !!crypto.subtle
            && typeof fetch === 'function';
    },

    available() { return this.supported(); },

    unavailableReason() {
        if (typeof indexedDB === 'undefined') return 'This browser has no IndexedDB.';
        if (typeof crypto === 'undefined' || !crypto.subtle) {
            return 'Offline punches need HTTPS: encryption is unavailable on this connection.';
        }
        return 'This browser cannot store offline punches.';
    },

    online() {
        return navigator.onLine !== false;
    },

    /**
     * Did this request fail because the server was unreachable (as opposed to
     * answering with an error)? Only the former justifies queueing the punch.
     */
    isConnectivityFailure(error) {
        if (error && error.offline === true) return true;
        return this.online() === false;
    },

    currentWorker() {
        if (typeof State !== 'undefined' && State.user && State.user.id) return State.user;
        return null;
    },

    _metaKey(kind, workerId) { return `${kind}:${workerId}`; },

    async getMeta(key) {
        const record = await OFFLINE_DB.get('meta', key);
        return record ? record.value : null;
    },

    async setMeta(key, value) {
        return OFFLINE_DB.put('meta', { key, value });
    },

    async delMeta(key) { return OFFLINE_DB.remove('meta', key); },

    // ---- lifecycle -----------------------------------------------------------------

    /**
     * Wire up connectivity and resume anything left over. Never throws: a failure
     * here must not stop the app from booting.
     */
    async init() {
        if (!this.available()) {
            this.deviceState = 'unsupported';
            return { ok: false, reason: 'unsupported', message: this.unavailableReason() };
        }
        try {
            await OFFLINE_DB.open();
        } catch (error) {
            this.deviceState = 'unsupported';
            this.lastError = error.message;
            return { ok: false, reason: 'db_unavailable', message: error.message };
        }

        if (!this._listening) {
            this._listening = true;
            window.addEventListener('online', () => { this.ensureReady().catch(() => {}); });
            // Returning to the tab is the other moment connectivity usually comes back.
            document.addEventListener('visibilitychange', () => {
                if (document.visibilityState === 'visible') this.ensureReady().catch(() => {});
            });
        }
        try {
            return await this.ensureReady();
        } catch (error) {
            // init() is called from app startup: it must never reject, or the boot
            // diagnostics panel reports a failure that has nothing to do with booting.
            this.lastError = error.message;
            return { ok: false, reason: error.code || 'error', message: error.message };
        }
    },

    /** Register (once), refresh the anchor, then drain the queue. Best effort. */
    async ensureReady() {
        if (this._ensuring) return this._ensuring;
        this._ensuring = this._ensureReady().finally(() => { this._ensuring = null; });
        return this._ensuring;
    },

    async _ensureReady() {
        if (!this.available()) {
            return { ok: false, reason: 'unsupported', message: this.unavailableReason() };
        }
        const worker = this.currentWorker();
        if (!worker) return { ok: false, reason: 'no_session' };
        if (!this.online()) return { ok: false, reason: 'offline' };

        try {
            await this.ensureDevice();
            this.deviceState = 'ready';
            await this.refreshAnchor();
        } catch (error) {
            this.lastError = error.message;
            this.deviceState = error.code === 'device_key_lost' ? 'key_lost' : this.deviceState;
            return { ok: false, reason: error.code || 'error', message: error.message };
        }
        const sync = await this.syncNow();
        return { ok: true, sync };
    },

    // ---- device registration -------------------------------------------------------

    /** The device key is returned once; losing it needs an explicit re-register. */
    async ensureDevice() {
        const worker = this.currentWorker();
        if (!worker) throw new OfflineError('no_session', 'Sign in before capturing an offline punch.');
        if (!this.available()) throw new OfflineError('unsupported', this.unavailableReason());

        const key = this._metaKey('device', worker.id);
        const stored = await this.getMeta(key);
        if (stored && stored.device_key && stored.worker_id === String(worker.id)) return stored;

        const record = await this._registerDevice(
            stored && stored.device_id ? stored.device_id : this.newDeviceId(worker.id), worker
        );
        await this.setMeta(key, record);
        return record;
    },

    newDeviceId(workerId) {
        return `web-${workerId}-${OFFLINE_CRYPTO.randomHex(8)}`.slice(0, 120);
    },

    async _registerDevice(deviceId, worker) {
        const response = await OfflineHttp.post(
            '/attendance/devices/register',
            { device_id: deviceId, note: 'web client (offline queue)' },
            worker.token
        );
        if (response.status === 409) {
            throw new OfflineError(
                'device_key_lost',
                'This phone is already registered but its signing key is gone. Sync everything ' +
                'waiting on it, then re-register the phone.'
            );
        }
        if (response.status === 401) throw new OfflineError('unauthorized', 'Your session expired. Sign in again.');
        if (!response.ok) throw new OfflineError('register_failed', OfflineHttp.errorMessage(response));
        if (!response.body || !response.body.device_key) {
            throw new OfflineError('register_failed', 'The server did not return a signing key.');
        }
        return {
            worker_id: String(worker.id),
            device_id: response.body.device_id || deviceId,
            device_key: response.body.device_key,
            key_epoch: response.body.key_epoch,
            registered_at: OFFLINE_CRYPTO.formatTs(Date.now()),
            origin: 'web'
        };
    },

    /**
     * Revoke this phone and register again. Refused while punches are still queued:
     * re-registering bumps key_epoch, which would invalidate their signatures before
     * they were ever delivered.
     */
    async rotateDevice() {
        const worker = this.currentWorker();
        if (!worker) throw new OfflineError('no_session', 'Sign in first.');
        const pending = await this.queuedPunches(worker.id);
        if (pending.length) {
            throw new OfflineError(
                'queue_not_empty',
                `${pending.length} punch(es) on this phone have not synced yet. Sync them before ` +
                're-registering it, or they will lose their signatures.'
            );
        }
        const stored = await this.getMeta(this._metaKey('device', worker.id));
        if (stored && stored.device_id) {
            await OfflineHttp.post(
                `/attendance/devices/${encodeURIComponent(stored.device_id)}/revoke`, {}, worker.token
            );
        }
        await this.delMeta(this._metaKey('device', worker.id));
        await this.delMeta(this._metaKey('anchor', worker.id));
        const record = await this.ensureDevice();
        this.deviceState = 'ready';
        return record;
    },

    // ---- anchors -------------------------------------------------------------------

    /**
     * Fetch a server-signed anchor. The anchor is what makes an offline punch
     * datable: while online the client can obtain one, while offline it can only
     * use the one it already holds.
     */
    async refreshAnchor() {
        const worker = this.currentWorker();
        if (!worker) throw new OfflineError('no_session', 'Sign in first.');
        const device = await this.ensureDevice();
        const response = await OfflineHttp.post(
            '/attendance/anchors', { device_id: device.device_id }, worker.token
        );
        if (response.offline) throw new OfflineError('offline', 'No connection: using the stored anchor.');
        if (response.status === 401) throw new OfflineError('unauthorized', 'Your session expired. Sign in again.');
        if (response.status === 404) {
            this.deviceState = 'key_lost';
            throw new OfflineError('device_unknown', 'The server does not know this device; re-register it.');
        }
        if (!response.ok) throw new OfflineError('anchor_failed', OfflineHttp.errorMessage(response));

        const body = response.body || {};
        const previous = await this.getMeta(this._metaKey('anchor', worker.id));
        const anchor = {
            worker_id: String(worker.id),
            device_id: device.device_id,
            anchor_id: body.anchor_id,
            server_time: body.server_time,
            anchor_signature: body.anchor_signature,
            signature_version: body.signature_version,
            max_offline_hours: body.max_offline_hours
                || (previous && previous.max_offline_hours)
                || 72,
            // Captured as the response lands, so the offset is short by one round
            // trip at most - never long, which is the direction that matters.
            mono_ms: OFFLINE_CRYPTO.monotonicNow(),
            wall_ms: Date.now(),
            fetched_at: OFFLINE_CRYPTO.formatTs(Date.now())
        };
        if (!anchor.anchor_id || !anchor.server_time || !anchor.anchor_signature) {
            throw new OfflineError('anchor_failed', 'The server returned an unusable anchor.');
        }
        await this.setMeta(this._metaKey('anchor', worker.id), anchor);
        return anchor;
    },

    async loadAnchor(workerId) {
        return this.getMeta(this._metaKey('anchor', workerId));
    },

    anchorAgeSeconds(anchor) {
        return Math.max(0, (OFFLINE_CRYPTO.monotonicNow() - anchor.mono_ms) / 1000);
    },

    anchorFresh(anchor) {
        if (!anchor) return false;
        const maxSeconds = Number(anchor.max_offline_hours || 72) * 3600;
        return this.anchorAgeSeconds(anchor) <= maxSeconds;
    },

    // ---- capture -------------------------------------------------------------------

    /**
     * Sign and queue one punch. Needs a valid anchor: with no anchor and no
     * connection there is nothing that can prove a time, so this fails closed
     * rather than writing a punch the server would only reject later.
     */
    async queuePunch({ action, coords, photoBlob, accuracy = null }) {
        const worker = this.currentWorker();
        if (!worker) throw new OfflineError('no_session', 'Sign in before capturing an offline punch.');
        if (!this.available()) throw new OfflineError('unsupported', this.unavailableReason());
        if (action !== 'Clock In' && action !== 'Clock Out') {
            throw new OfflineError('invalid_action', 'Unknown punch action.');
        }

        const device = await this.ensureDevice();
        let anchor = await this.loadAnchor(worker.id);
        if (!this.anchorFresh(anchor)) {
            anchor = await this.refreshAnchor();   // throws when there is no connection
        }

        const [lat, lon] = this.parseCoords(coords);
        const monotonicOffset = Math.max(0, (OFFLINE_CRYPTO.monotonicNow() - anchor.mono_ms) / 1000);
        const maxSeconds = Number(anchor.max_offline_hours || 72) * 3600;
        if (monotonicOffset > maxSeconds) {
            throw new OfflineError(
                'anchor_expired',
                `This phone has been offline for more than ${anchor.max_offline_hours} hours. ` +
                'Connect once to get a new time anchor, then capture the punch again.'
            );
        }

        const anchorMs = OFFLINE_CRYPTO.parseTs(anchor.server_time);
        const effectiveTimestamp = OFFLINE_CRYPTO.formatTs(anchorMs + monotonicOffset * 1000);
        // Drift between the device's wall clock and the anchored clock since the
        // anchor was issued: ~0 on an honest phone, hours on a moved one.
        const wallElapsedSeconds = (Date.now() - anchor.wall_ms) / 1000;
        const clientOffset = Math.round(wallElapsedSeconds - monotonicOffset);
        const clientTimestamp = OFFLINE_CRYPTO.formatTs(anchorMs + wallElapsedSeconds * 1000);

        const fields = {
            device_id: device.device_id,
            worker_id: String(worker.id),
            action,
            client_punch_id: OFFLINE_CRYPTO.newPunchId(),
            nonce: OFFLINE_CRYPTO.randomHex(16),
            anchor_id: anchor.anchor_id,
            effective_timestamp: effectiveTimestamp,
            lat, lon, accuracy,
            photo_sha256: photoBlob ? await OFFLINE_CRYPTO.sha256Hex(photoBlob) : null
        };

        const record = {
            ...fields,
            anchor_server_time: anchor.server_time,
            anchor_signature: anchor.anchor_signature,
            monotonic_offset_s: monotonicOffset,
            client_timestamp: clientTimestamp,
            client_offset_s: clientOffset,
            signature: await OFFLINE_CRYPTO.signPunch(device.device_key, fields),
            signature_version: OFFLINE_CRYPTO.SIGNATURE_VERSION,
            worker_id: String(worker.id),
            device_id: device.device_id,
            status: 'queued',
            attempts: 0,
            created_at: OFFLINE_CRYPTO.formatTs(Date.now()),
            captured_wall_ms: Date.now(),
            captured_mono_ms: OFFLINE_CRYPTO.monotonicNow()
        };

        await OFFLINE_DB.put('punches', record);
        if (photoBlob) {
            await OFFLINE_DB.put('photos', {
                client_punch_id: record.client_punch_id,
                blob: photoBlob,
                created_ms: Date.now()
            }).catch(() => {});   // the punch matters; the local copy is a convenience
        }
        return record;
    },

    parseCoords(coords) {
        if (!coords) return [null, null];
        const parts = String(coords).split(',');
        if (parts.length !== 2) return [null, null];
        return [OFFLINE_CRYPTO.roundCoord(parts[0]), OFFLINE_CRYPTO.roundCoord(parts[1])];
    },

    /** Exactly the fields the sync endpoint models; local bookkeeping is dropped. */
    toPayload(record) {
        return {
            client_punch_id: record.client_punch_id,
            action: record.action,
            anchor_id: record.anchor_id,
            anchor_server_time: record.anchor_server_time,
            anchor_signature: record.anchor_signature,
            monotonic_offset_s: record.monotonic_offset_s,
            nonce: record.nonce,
            lat: record.lat,
            lon: record.lon,
            accuracy: record.accuracy,
            client_timestamp: record.client_timestamp,
            client_offset_s: record.client_offset_s,
            photo_sha256: record.photo_sha256,
            signature: record.signature,
            signature_version: record.signature_version
        };
    },

    // ---- queue reads ---------------------------------------------------------------

    async queuedPunches(workerId) {
        const rows = await OFFLINE_DB.allByIndex(
            'punches', 'by_worker_status', IDBKeyRange.only([String(workerId), 'queued'])
        );
        // Monotonic capture order: the device wall clock must not be able to reorder
        // the queue any more than it can reorder the punches themselves.
        return rows.sort((a, b) => (a.captured_mono_ms || 0) - (b.captured_mono_ms || 0));
    },

    async history(workerId, limit = 50) {
        const rows = await OFFLINE_DB.allByIndex('punches', 'by_worker', String(workerId));
        return rows
            .sort((a, b) => (b.captured_mono_ms || 0) - (a.captured_mono_ms || 0))
            .slice(0, limit);
    },

    /**
     * The worker's shift state as this phone knows it: the last server state, with
     * the queued punches folded on top. Without this the clock panel would offer
     * "Clock In" again straight after an offline clock-in.
     */
    async localState(workerId, baseActive) {
        const queued = await this.queuedPunches(workerId);
        let active = !!baseActive;
        for (const punch of queued) active = punch.action === 'Clock In';
        return {
            active,
            nextAction: active ? 'Clock Out' : 'Clock In',
            queued: queued.length,
            queuedClockIns: queued.filter((p) => p.action === 'Clock In').length,
            queuedClockOuts: queued.filter((p) => p.action === 'Clock Out').length
        };
    },

    async pendingCount(workerId) {
        const worker = workerId || (this.currentWorker() && this.currentWorker().id);
        if (!worker) return 0;
        return (await this.queuedPunches(worker)).length;
    },

    /** Remember the last state the server confirmed, for offline rendering. */
    async cacheShiftState(workerId, state) {
        return this.setMeta(this._metaKey('shift', workerId), { ...state, cached_at: Date.now() });
    },

    async cachedShiftState(workerId) {
        return this.getMeta(this._metaKey('shift', workerId));
    },

    // ---- replay --------------------------------------------------------------------

    /**
     * Replay everything queued for the signed-in worker.
     *
     * Never throws for a transport or auth problem: the queue is the durable
     * artifact, so a failure leaves it intact and reports why.
     */
    async syncNow() {
        const summary = {
            sent: 0, applied: 0, flagged: 0, rejected: 0, duplicates: 0,
            pending: 0, offline: false, error: null, results: []
        };
        const worker = this.currentWorker();
        if (!worker) { summary.error = 'no_session'; return summary; }
        summary.pending = await this.pendingCount(worker.id);
        if (!this.available()) { summary.error = 'unsupported'; return summary; }
        if (!this.online()) { summary.offline = true; return summary; }

        let device;
        try {
            device = await this.ensureDevice();
            this.deviceState = 'ready';
        } catch (error) {
            this.deviceState = error.code === 'device_key_lost' ? 'key_lost' : 'error';
            summary.error = error.code || 'error';
            summary.message = error.message;
            return summary;
        }

        let chunkSize = OFFLINE_BATCH_CHUNK;
        let queue = await this.queuedPunches(worker.id);
        let rounds = 0;

        while (queue.length && rounds < 20) {
            rounds += 1;
            const batch = queue.slice(0, chunkSize);
            const response = await OfflineHttp.post(
                '/attendance/sync',
                { device_id: device.device_id, punches: batch.map((p) => this.toPayload(p)) },
                worker.token
            );

            if (response.offline) { summary.offline = true; summary.error = 'offline'; break; }
            if (response.status === 413 && chunkSize > 1) {
                chunkSize = Math.max(1, Math.floor(chunkSize / 2));
                continue;   // resend the same punches in smaller batches
            }
            if (response.status === 401) {
                this.deviceState = 'unauthorized';
                summary.error = 'unauthorized';
                summary.message = 'Your session expired; sign in again to sync these punches.';
                break;
            }
            if (response.status === 403 && OfflineHttp.errorCode(response) === 'device_revoked') {
                this.deviceState = 'revoked';
                summary.error = 'device_revoked';
                summary.message = 'This phone was revoked. An administrator must re-enable it.';
                break;
            }
            if (response.status === 404) {
                this.deviceState = 'key_lost';
                summary.error = OfflineHttp.errorCode(response) || 'device_unknown';
                summary.message = OfflineHttp.errorMessage(response);
                break;
            }
            if (!response.ok) {
                summary.error = 'sync_failed';
                summary.message = OfflineHttp.errorMessage(response);
                break;
            }

            const body = response.body || {};
            const verdicts = new Map(
                (body.results || []).map((item) => [item.client_punch_id, item])
            );
            summary.sent += batch.length;
            summary.applied += Number(body.applied) || 0;

            for (const record of batch) {
                const verdict = verdicts.get(record.client_punch_id) || {
                    status: 'rejected', code: 'no_verdict'
                };
                const settled = {
                    ...record,
                    status: this._localStatus(verdict.status),
                    server_status: verdict.status,
                    rejection_code: verdict.code || null,
                    effective_time: verdict.effective_time || record.effective_timestamp,
                    site: verdict.site || null,
                    flushed_at: OFFLINE_CRYPTO.formatTs(Date.now())
                };
                await OFFLINE_DB.put('punches', settled);
                if (settled.status === 'rejected') summary.rejected += 1;
                else if (settled.status === 'duplicate') summary.duplicates += 1;
                else if (verdict.flagged) summary.flagged += 1;
                summary.results.push({
                    client_punch_id: record.client_punch_id,
                    action: record.action,
                    status: verdict.status,
                    code: verdict.code || null,
                    effective_time: verdict.effective_time || null
                });
            }

            // A fresh anchor: reconnecting is exactly when authority is renewed.
            if (body.next_anchor) {
                const previous = await this.loadAnchor(worker.id);
                await this.setMeta(this._metaKey('anchor', worker.id), {
                    worker_id: String(worker.id),
                    device_id: device.device_id,
                    anchor_id: body.next_anchor.anchor_id,
                    server_time: body.next_anchor.server_time,
                    anchor_signature: body.next_anchor.anchor_signature,
                    signature_version: body.next_anchor.signature_version,
                    max_offline_hours: (previous && previous.max_offline_hours)
                        || Number(body.next_anchor.max_offline_hours) || 72,
                    mono_ms: OFFLINE_CRYPTO.monotonicNow(),
                    wall_ms: Date.now(),
                    fetched_at: OFFLINE_CRYPTO.formatTs(Date.now())
                });
            }

            const remaining = await this.queuedPunches(worker.id);
            if (remaining.length && this._samePunches(remaining.slice(0, batch.length), batch)) {
                // The server answered but settled nothing, so resending would spin
                // forever. Stop and report it; the punches stay queued and safe.
                summary.error = 'no_progress';
                summary.message = 'The server did not settle the last batch; stopping instead of retrying in a loop.';
                break;
            }
            queue = remaining;
        }

        summary.pending = await this.pendingCount(worker.id);
        this.lastSync = { ...summary, at: OFFLINE_CRYPTO.formatTs(Date.now()) };
        this.lastError = summary.error ? (summary.message || summary.error) : null;
        await this.setMeta(this._metaKey('last_sync', worker.id), this.lastSync).catch(() => {});
        await this.prune(worker.id).catch(() => {});
        return summary;
    },

    /** Same punches, in the same order? Used to detect a batch that made no progress. */
    _samePunches(left, right) {
        if (left.length !== right.length) return false;
        return left.every((punch, index) => punch.client_punch_id === right[index].client_punch_id);
    },

    /** Server verdict -> local queue status. Unknown verdicts stay queued. */
    _localStatus(serverStatus) {
        if (serverStatus === 'accepted' || serverStatus === 'flagged') return 'synced';
        if (serverStatus === 'duplicate') return 'duplicate';
        if (serverStatus === 'rejected') return 'rejected';
        return 'queued';
    },

    // ---- retention -----------------------------------------------------------------

    /**
     * The queue is the durable artifact while punches are pending; once settled,
     * local copies exist only to show the worker what happened, so they expire.
     */
    async prune(workerId) {
        const punches = await OFFLINE_DB.allByIndex('punches', 'by_worker', String(workerId));
        const historyCutoff = Date.now() - OFFLINE_HISTORY_RETENTION_DAYS * 86400000;
        const photoCutoff = Date.now() - OFFLINE_PHOTO_RETENTION_HOURS * 3600000;
        const keepPhoto = new Set();

        for (const punch of punches) {
            if (punch.status === 'queued') {
                keepPhoto.add(punch.client_punch_id);
                continue;
            }
            if ((punch.captured_wall_ms || 0) < historyCutoff) {
                await OFFLINE_DB.remove('punches', punch.client_punch_id);
            }
            if ((punch.captured_wall_ms || 0) >= photoCutoff) keepPhoto.add(punch.client_punch_id);
        }

        const photos = await OFFLINE_DB.all('photos').catch(() => []);
        for (const photo of photos) {
            if (!keepPhoto.has(photo.client_punch_id)) await OFFLINE_DB.remove('photos', photo.client_punch_id);
        }
    },

    /** A Blob for a punch photo, if this phone still holds it. */
    async photo(clientPunchId) {
        const record = await OFFLINE_DB.get('photos', clientPunchId).catch(() => null);
        return record ? record.blob : null;
    },

    // ---- diagnostics ---------------------------------------------------------------

    async status() {
        const worker = this.currentWorker();
        const result = {
            available: this.available(),
            reason: this.available() ? null : this.unavailableReason(),
            online: this.online(),
            deviceState: this.deviceState,
            device: null,
            anchor: null,
            queued: 0,
            lastSync: this.lastSync,
            lastError: this.lastError
        };
        if (!worker || !this.available()) return result;
        try {
            const device = await this.getMeta(this._metaKey('device', worker.id));
            const anchor = await this.loadAnchor(worker.id);
            result.device = device
                ? { device_id: device.device_id, key_epoch: device.key_epoch, registered_at: device.registered_at }
                : null;
            result.anchor = anchor
                ? {
                    anchor_id: anchor.anchor_id,
                    server_time: anchor.server_time,
                    fetched_at: anchor.fetched_at,
                    age_seconds: Math.round(this.anchorAgeSeconds(anchor)),
                    max_offline_hours: anchor.max_offline_hours
                }
                : null;
            result.queued = (await this.queuedPunches(worker.id)).length;
            result.history = (await this.history(worker.id, 5)).map((p) => ({
                action: p.action,
                status: p.status,
                code: p.rejection_code || null,
                effective_time: p.effective_time || p.effective_timestamp
            }));
        } catch (error) {
            result.reason = error.message;
        }
        return result;
    }
};

if (typeof window !== 'undefined') {
    window.OFFLINE = OFFLINE;
    window.OFFLINE_CRYPTO = OFFLINE_CRYPTO;
}
// The Node test harness imports this file to prove the canonical string and the
// HMAC match the Python server byte for byte. Browsers simply ignore this block.
if (typeof module !== 'undefined' && module.exports) {
    module.exports = { OFFLINE, OFFLINE_CRYPTO, OfflineError };
}
