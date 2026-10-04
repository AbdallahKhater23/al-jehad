/**
 * Device registration and anchor custody — the two things a punch needs before it
 * can be signed.
 *
 * The contract, in order (see ``backend/offline_sync.py``):
 *
 *   1. ``POST /attendance/devices/register`` returns ``device_key`` **exactly once**.
 *      Losing it means re-registering, which bumps ``key_epoch`` and invalidates every
 *      signature already queued on the phone — so the key is written to Preferences
 *      *and* mirrored into the offline database before anything else happens.
 *   2. ``POST /attendance/anchors`` returns a moment the **server** signed. The device
 *      key cannot sign a trustworthy anchor (the client holds it), so the anchor is the
 *      only thing that lets an offline punch claim a time.
 *   3. ``POST /attendance/sync`` returns a fresh anchor in its reply, which is adopted
 *      here so reconnecting renews authority without a second round trip.
 *
 * Two independent stores are used deliberately: IndexedDB holds the working record the
 * queue signs with, and Preferences holds the key itself, because Android can evict a
 * WebView's IndexedDB under storage pressure and a lost key is an un-replayable queue.
 */

import { Preferences } from '@capacitor/preferences';
import { request } from '../core/http.js';
import { randomHex, formatTs, monotonicNow, SIGNATURE_VERSION } from './signing.js';
import {
  OfflineError,
  loadDevice,
  saveDevice,
  loadAnchor,
  saveAnchor,
  clearDevice,
  clearAnchor,
  queuedPunches,
  anchorAgeSeconds,
  type AnchorRecord,
  type DeviceRecord,
} from './queue.js';

const DEVICE_KEY_PREFIX = 'offline_device_key:';

export interface RegisterResponse {
  device_id: string;
  worker_id: string;
  key_epoch: number;
  device_key: string;
  signature_version?: number;
  server_time?: string;
}

export interface AnchorResponse {
  anchor_id: string;
  device_id: string;
  worker_id: string;
  server_time: string;
  anchor_signature: string;
  signature_version?: number;
  max_offline_hours?: number;
}

/** A stable, per-worker device id. Never reused across workers. */
export function newDeviceId(workerId: string): string {
  return `android-${workerId}-${randomHex(8)}`.slice(0, 120);
}

async function stashKey(workerId: string, key: string): Promise<void> {
  await Preferences.set({ key: `${DEVICE_KEY_PREFIX}${workerId}`, value: key });
}

async function readStashedKey(workerId: string): Promise<string | null> {
  try {
    const { value } = await Preferences.get({ key: `${DEVICE_KEY_PREFIX}${workerId}` });
    return value ?? null;
  } catch {
    return null;
  }
}

/**
 * The device record, restored from Preferences when IndexedDB lost it.
 *
 * A device that is registered server-side but has no local key cannot sign anything, and
 * re-registering is refused (409) precisely so a queued punch is not silently orphaned.
 * Recovering the key from Preferences is what keeps that refusal from being terminal.
 */
export async function ensureDevice(workerId: string): Promise<DeviceRecord> {
  const id = String(workerId);
  if (!id) throw new OfflineError('no_session', 'Sign in before registering a device.');

  const stored = await loadDevice(id);
  if (stored?.device_key && stored.worker_id === id) return stored;

  const stashed = await readStashedKey(id);
  if (stashed && stored?.device_id) {
    const restored: DeviceRecord = { ...stored, device_key: stashed };
    await saveDevice(id, restored);
    return restored;
  }

  const deviceId = stored?.device_id ?? newDeviceId(id);
  return registerDevice(id, deviceId);
}

export async function registerDevice(workerId: string, deviceId: string): Promise<DeviceRecord> {
  const id = String(workerId);
  let body: RegisterResponse;
  try {
    body = await request<RegisterResponse>({
      method: 'POST',
      path: '/attendance/devices/register',
      body: { device_id: deviceId, note: 'android client (offline queue)' },
    });
  } catch (e) {
    const err = e as { status?: number; code?: string; message?: string; offline?: boolean };
    if (err.status === 409) {
      throw new OfflineError(
        'device_key_lost',
        'This device is already registered but its signing key is gone. Sync everything waiting on ' +
          'it, then re-register the device.',
      );
    }
    if (err.status === 401) {
      throw new OfflineError('unauthorized', 'Session expired. Sign in again.');
    }
    if (err.offline) {
      throw new OfflineError('offline', 'No connection: a device cannot be registered offline.');
    }
    throw new OfflineError(err.code ?? 'register_failed', err.message ?? 'Device registration failed.');
  }

  if (!body?.device_key) {
    throw new OfflineError('register_failed', 'The server did not return a signing key.');
  }

  const record: DeviceRecord = {
    worker_id: id,
    device_id: body.device_id || deviceId,
    device_key: body.device_key,
    key_epoch: Number(body.key_epoch ?? 0),
    registered_at: formatTs(Date.now()),
    origin: 'android',
  };

  // Key first, then the record: a crash between the two leaves the recoverable state
  // (key present, record missing) rather than the unrecoverable one.
  await stashKey(id, record.device_key);
  await saveDevice(id, record);
  return record;
}

/**
 * Revoke this device and register a new one.
 *
 * Refused while punches are still queued: re-registering bumps ``key_epoch``, which would
 * invalidate their signatures before they were ever delivered.
 */
export async function rotateDevice(workerId: string): Promise<DeviceRecord> {
  const id = String(workerId);
  const pending = await queuedPunches(id);
  if (pending.length) {
    throw new OfflineError(
      'queue_not_empty',
      `${pending.length} punch(es) on this device have not synced yet. Sync them before ` +
        're-registering, or they will lose their signatures.',
    );
  }

  const stored = await loadDevice(id);
  if (stored?.device_id) {
    try {
      await request<unknown>({
        method: 'POST',
        path: `/attendance/devices/${encodeURIComponent(stored.device_id)}/revoke`,
        body: {},
      });
    } catch (e) {
      const err = e as { status?: number; offline?: boolean; message?: string };
      if (err.offline) {
        throw new OfflineError('offline', 'No connection: the device cannot be revoked offline.');
      }
      if (err.status !== 404) {
        throw new OfflineError('revoke_failed', err.message ?? 'The device could not be revoked.');
      }
    }
  }

  await clearDevice(id);
  await clearAnchor(id);
  await Preferences.remove({ key: `${DEVICE_KEY_PREFIX}${id}` });
  return ensureDevice(id);
}

/** Fetch a server-signed anchor and hold it. This is what makes an offline punch datable. */
export async function refreshAnchor(workerId: string): Promise<AnchorRecord> {
  const id = String(workerId);
  const device = await ensureDevice(id);

  let body: AnchorResponse;
  try {
    body = await request<AnchorResponse>({
      method: 'POST',
      path: '/attendance/anchors',
      body: { device_id: device.device_id },
    });
  } catch (e) {
    const err = e as { status?: number; code?: string; message?: string; offline?: boolean };
    if (err.offline) throw new OfflineError('offline', 'No connection: using the stored anchor.');
    if (err.status === 401) throw new OfflineError('unauthorized', 'Session expired. Sign in again.');
    if (err.status === 404) {
      throw new OfflineError('device_unknown', 'The server does not know this device; re-register it.');
    }
    if (err.status === 403 && err.code === 'device_revoked') {
      throw new OfflineError('device_revoked', 'This device was revoked by an administrator.');
    }
    throw new OfflineError(err.code ?? 'anchor_failed', err.message ?? 'The anchor could not be issued.');
  }

  if (!body?.anchor_id || !body?.server_time || !body?.anchor_signature) {
    throw new OfflineError('anchor_failed', 'The server returned an unusable anchor.');
  }

  const previous = await loadAnchor(id);
  const anchor: AnchorRecord = {
    worker_id: id,
    device_id: device.device_id,
    anchor_id: body.anchor_id,
    server_time: body.server_time,
    anchor_signature: body.anchor_signature,
    signature_version: body.signature_version ?? SIGNATURE_VERSION,
    max_offline_hours: body.max_offline_hours || previous?.max_offline_hours || 72,
    // Stamped as the response lands, so the measured offset is short by at most one
    // round trip — never long, which is the direction that matters.
    mono_ms: monotonicNow(),
    wall_ms: Date.now(),
    fetched_at: formatTs(Date.now()),
  };
  await saveAnchor(id, anchor);
  return anchor;
}

/** Adopt the anchor a sync reply carried, without a second round trip. */
export async function adoptAnchor(
  workerId: string,
  next: { anchor_id: string; server_time: string; anchor_signature: string; signature_version?: number },
  deviceId: string,
): Promise<AnchorRecord> {
  const id = String(workerId);
  const previous = await loadAnchor(id);
  const anchor: AnchorRecord = {
    worker_id: id,
    device_id: deviceId,
    anchor_id: next.anchor_id,
    server_time: next.server_time,
    anchor_signature: next.anchor_signature,
    signature_version: next.signature_version ?? SIGNATURE_VERSION,
    max_offline_hours: previous?.max_offline_hours || 72,
    mono_ms: monotonicNow(),
    wall_ms: Date.now(),
    fetched_at: formatTs(Date.now()),
  };
  await saveAnchor(id, anchor);
  return anchor;
}

export interface AnchorState {
  anchor: AnchorRecord | null;
  fresh: boolean;
  age_seconds: number;
  expires_in_seconds: number;
}

export async function anchorState(workerId: string): Promise<AnchorState> {
  const anchor = await loadAnchor(String(workerId));
  if (!anchor) return { anchor: null, fresh: false, age_seconds: 0, expires_in_seconds: 0 };
  const age = anchorAgeSeconds(anchor);
  const max = Number(anchor.max_offline_hours || 72) * 3600;
  return {
    anchor,
    fresh: age <= max,
    age_seconds: Math.round(age),
    expires_in_seconds: Math.max(0, Math.round(max - age)),
  };
}
