import { dbGet, dbPut, dbDelete, dbGetAllByIndex } from './db.js';
import {
  fmtAccuracy,
  fmtCoord,
  newPunchId,
  randomHex,
  monotonicNow,
  parseTs,
  formatTs,
  roundCoord,
  sha256Hex,
  signPunch,
  SIGNATURE_VERSION,
  type CanonicalFields,
} from './signing.js';

export type PunchAction = 'Clock In' | 'Clock Out';

export interface AnchorRecord {
  worker_id: string;
  device_id: string;
  anchor_id: string;
  server_time: string;
  anchor_signature: string;
  signature_version: number;
  max_offline_hours: number;
  mono_ms: number;
  wall_ms: number;
  fetched_at: string;
}

export interface DeviceRecord {
  worker_id: string;
  device_id: string;
  device_key: string;
  key_epoch: number;
  registered_at: string;
  origin: string;
}

export interface QueuedPunch {
  device_id: string;
  worker_id: string;
  action: PunchAction;
  client_punch_id: string;
  nonce: string;
  anchor_id: string;
  effective_timestamp: string;
  anchor_server_time: string;
  anchor_signature: string;
  monotonic_offset_s: number;
  lat: number | null;
  lon: number | null;
  accuracy: number | null;
  client_timestamp: string | null;
  client_offset_s: number | null;
  photo_sha256: string | null;
  signature: string;
  signature_version: number;
  status: 'queued' | 'synced' | 'rejected' | 'duplicate';
  server_status?: string;
  rejection_code?: string | null;
  effective_time?: string | null;
  site?: string | null;
  attempts: number;
  created_at: string;
  captured_wall_ms: number;
  captured_mono_ms: number;
  flushed_at?: string;
  photo_uploaded_at?: string;
}

export class OfflineError extends Error {
  code: string;
  constructor(code: string, message?: string) {
    super(message ?? code);
    this.name = 'OfflineError';
    this.code = code;
  }
}

function metaKey(kind: string, workerId: string): string {
  return `${kind}:${workerId}`;
}

async function getMeta<T>(key: string): Promise<T | null> {
  const rec = await dbGet<{ key: string; value: T }>('meta', key);
  return rec ? rec.value : null;
}

async function setMeta<T>(key: string, value: T): Promise<void> {
  await dbPut('meta', { key, value });
}

async function delMeta(key: string): Promise<void> {
  await dbDelete('meta', key);
}

export async function loadDevice(workerId: string): Promise<DeviceRecord | null> {
  return getMeta<DeviceRecord>(metaKey('device', workerId));
}

export async function saveDevice(workerId: string, rec: DeviceRecord): Promise<void> {
  await setMeta(metaKey('device', workerId), rec);
}

export async function loadAnchor(workerId: string): Promise<AnchorRecord | null> {
  return getMeta<AnchorRecord>(metaKey('anchor', workerId));
}

export async function saveAnchor(workerId: string, rec: AnchorRecord): Promise<void> {
  await setMeta(metaKey('anchor', workerId), rec);
}

export async function clearAnchor(workerId: string): Promise<void> {
  await delMeta(metaKey('anchor', workerId));
}

export async function clearDevice(workerId: string): Promise<void> {
  await delMeta(metaKey('device', workerId));
}

export function anchorAgeSeconds(anchor: AnchorRecord): number {
  return Math.max(0, (monotonicNow() - anchor.mono_ms) / 1000);
}

export function anchorFresh(anchor: AnchorRecord | null): boolean {
  if (!anchor) return false;
  const max = Number(anchor.max_offline_hours || 72) * 3600;
  return anchorAgeSeconds(anchor) <= max;
}

export async function queuedPunches(workerId: string): Promise<QueuedPunch[]> {
  const rows = await dbGetAllByIndex<QueuedPunch>(
    'punches',
    'by_worker_status',
    IDBKeyRange.only([String(workerId), 'queued']),
  );
  return rows.sort((a, b) => (a.captured_mono_ms || 0) - (b.captured_mono_ms || 0));
}

export async function historyPunches(workerId: string, limit = 50): Promise<QueuedPunch[]> {
  const rows = await dbGetAllByIndex<QueuedPunch>('punches', 'by_worker', String(workerId));
  return rows.sort((a, b) => (b.captured_mono_ms || 0) - (a.captured_mono_ms || 0)).slice(0, limit);
}

export async function pendingCount(workerId: string): Promise<number> {
  return (await queuedPunches(workerId)).length;
}

function parseCoords(input: string | null | undefined): [number | null, number | null] {
  if (!input) return [null, null];
  const parts = String(input).split(',');
  if (parts.length !== 2) return [null, null];
  return [roundCoord(parts[0]), roundCoord(parts[1])];
}

export interface EnqueueArgs {
  workerId: string;
  action: PunchAction;
  coords?: string | null;
  accuracy?: number | null;
  photoBlob?: Blob | null;
  device: DeviceRecord;
  anchor: AnchorRecord;
}

export async function enqueuePunch(args: EnqueueArgs): Promise<QueuedPunch> {
  const { workerId, action, coords, accuracy, photoBlob, device, anchor } = args;
  if (action !== 'Clock In' && action !== 'Clock Out') {
    throw new OfflineError('invalid_action', 'Unknown punch action.');
  }
  if (!anchorFresh(anchor)) {
    throw new OfflineError('anchor_expired', 'Anchor expired — refresh while online before queuing.');
  }
  const [lat, lon] = parseCoords(coords ?? null);
  const monoOffset = Math.max(0, (monotonicNow() - anchor.mono_ms) / 1000);
  const maxSeconds = Number(anchor.max_offline_hours || 72) * 3600;
  if (monoOffset > maxSeconds) {
    throw new OfflineError(
      'anchor_expired',
      `Offline window exceeded ${anchor.max_offline_hours}h. Reconnect to refresh the anchor.`,
    );
  }
  const anchorMs = parseTs(anchor.server_time);
  if (Number.isNaN(anchorMs)) throw new OfflineError('bad_anchor', 'Stored anchor has invalid server_time');
  const effectiveTimestamp = formatTs(anchorMs + monoOffset * 1000);
  const wallElapsed = (Date.now() - anchor.wall_ms) / 1000;
  const clientOffset = Math.round(wallElapsed - monoOffset);
  const clientTimestamp = formatTs(anchorMs + wallElapsed * 1000);

  const fields: CanonicalFields = {
    device_id: device.device_id,
    worker_id: String(workerId),
    // ``CanonicalFields.action`` is the wire type (the server models it as a free string and
    // validates it); ``QueuedPunch.action`` is the narrowed pair this client can capture.
    action,
    client_punch_id: newPunchId(),
    nonce: randomHex(16),
    anchor_id: anchor.anchor_id,
    effective_timestamp: effectiveTimestamp,
    lat,
    lon,
    accuracy: accuracy ?? null,
    photo_sha256: photoBlob ? await sha256Hex(photoBlob) : null,
  };

  const signature = await signPunch(device.device_key, fields);

  const record: QueuedPunch = {
    ...fields,
    // Restated after the spread: ``CanonicalFields.action`` is the wire type (``string``),
    // while the queued record carries the narrowed pair this client can actually capture.
    action,
    anchor_server_time: anchor.server_time,
    anchor_signature: anchor.anchor_signature,
    monotonic_offset_s: monoOffset,
    client_timestamp: clientTimestamp,
    client_offset_s: clientOffset,
    signature,
    signature_version: SIGNATURE_VERSION,
    worker_id: String(workerId),
    device_id: device.device_id,
    status: 'queued',
    attempts: 0,
    created_at: formatTs(Date.now()),
    captured_wall_ms: Date.now(),
    captured_mono_ms: monotonicNow(),
  };

  await dbPut('punches', record);
  if (photoBlob) {
    await dbPut('photos', {
      client_punch_id: record.client_punch_id,
      blob: photoBlob,
      created_ms: Date.now(),
    }).catch(() => {
      // punch durability matters more than the local photo copy
    });
  }
  return record;
}

export function toSyncPayload(p: QueuedPunch): Record<string, unknown> {
  return {
    client_punch_id: p.client_punch_id,
    action: p.action,
    anchor_id: p.anchor_id,
    anchor_server_time: p.anchor_server_time,
    anchor_signature: p.anchor_signature,
    monotonic_offset_s: p.monotonic_offset_s,
    nonce: p.nonce,
    lat: p.lat,
    lon: p.lon,
    accuracy: p.accuracy,
    client_timestamp: p.client_timestamp,
    client_offset_s: p.client_offset_s,
    photo_sha256: p.photo_sha256,
    signature: p.signature,
    signature_version: p.signature_version,
  };
}

export const __test__ = { parseCoords, metaKey, fmtCoord, fmtAccuracy };
