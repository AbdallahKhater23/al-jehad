import { dbGet, dbPut, dbDelete, dbGetAll, dbGetAllByIndex } from './db.js';
import { request } from '../core/http.js';
import { store } from '../core/store.js';
import { formatTs } from './signing.js';
import { adoptAnchor } from './device.js';
import { queuedPunches, toSyncPayload, type QueuedPunch } from './queue.js';

const OFFLINE_BATCH_CHUNK = 25;
const OFFLINE_HISTORY_RETENTION_DAYS = 7;
const OFFLINE_PHOTO_RETENTION_HOURS = 72;

export interface SyncSummary {
  sent: number;
  applied: number;
  flagged: number;
  rejected: number;
  duplicates: number;
  pending: number;
  offline: boolean;
  error: string | null;
  message?: string;
  results: Array<{
    client_punch_id: string;
    action: string;
    status: string;
    code: string | null;
    effective_time: string | null;
  }>;
  photos: { uploaded: number; pending: number; error: string | null };
  at?: string;
}

interface SyncVerdict {
  client_punch_id: string;
  status: string;
  code?: string | null;
  effective_time?: string | null;
  site?: string | null;
  flagged?: boolean;
}

interface SyncResponse {
  applied?: number;
  results?: SyncVerdict[];
  next_anchor?: {
    anchor_id: string;
    server_time: string;
    anchor_signature: string;
    signature_version?: number;
    max_offline_hours?: number;
  };
}

function localStatus(serverStatus: string): QueuedPunch['status'] {
  if (serverStatus === 'accepted' || serverStatus === 'flagged') return 'synced';
  if (serverStatus === 'duplicate') return 'duplicate';
  if (serverStatus === 'rejected') return 'rejected';
  return 'queued';
}

async function uploadPhoto(
  punch: QueuedPunch,
  blob: Blob,
): Promise<{ ok: boolean; offline?: boolean; message?: string }> {
  const form = new FormData();
  form.append('client_punch_id', punch.client_punch_id);
  const name = String(blob.type || '').includes('png') ? 'selfie.png' : 'selfie.jpg';
  form.append('photo', blob, name);
  try {
    await request<unknown>({ method: 'POST', path: '/attendance/sync/photo', formData: form });
    return { ok: true };
  } catch (e) {
    const err = e as { status?: number; offline?: boolean; message?: string; code?: string };
    if (err.offline) return { ok: false, offline: true, message: err.message };
    return { ok: false, message: err.message ?? 'photo upload failed' };
  }
}

async function loadPhotoBlob(clientPunchId: string): Promise<Blob | null> {
  const rec = await dbGet<{ client_punch_id: string; blob: Blob }>('photos', clientPunchId).catch(
    () => null,
  );
  return rec ? rec.blob : null;
}

async function prune(workerId: string): Promise<void> {
  const punches = await dbGetAllByIndex<QueuedPunch>('punches', 'by_worker', String(workerId));
  const historyCutoff = Date.now() - OFFLINE_HISTORY_RETENTION_DAYS * 86_400_000;
  const photoCutoff = Date.now() - OFFLINE_PHOTO_RETENTION_HOURS * 3_600_000;
  const keepPhoto = new Set<string>();

  for (const p of punches) {
    if (p.status === 'queued') {
      keepPhoto.add(p.client_punch_id);
      continue;
    }
    if ((p.captured_wall_ms || 0) < historyCutoff) {
      await dbDelete('punches', p.client_punch_id);
      continue;
    }
    if (!p.photo_uploaded_at && (p.captured_wall_ms || 0) >= photoCutoff) {
      keepPhoto.add(p.client_punch_id);
    }
  }

  const photos = await dbGetAll<{ client_punch_id: string }>('photos').catch(() => []);
  for (const ph of photos) {
    if (!keepPhoto.has(ph.client_punch_id)) await dbDelete('photos', ph.client_punch_id);
  }
}

export async function syncNow(workerId: string, deviceId: string): Promise<SyncSummary> {
  const summary: SyncSummary = {
    sent: 0,
    applied: 0,
    flagged: 0,
    rejected: 0,
    duplicates: 0,
    pending: 0,
    offline: false,
    error: null,
    results: [],
    photos: { uploaded: 0, pending: 0, error: null },
  };

  if (!workerId || !deviceId) {
    summary.error = 'no_session';
    return summary;
  }

  summary.pending = await queuedPunches(workerId)
    .then((r) => r.length)
    .catch(() => 0);

  let chunkSize = OFFLINE_BATCH_CHUNK;
  let queue = await queuedPunches(workerId);
  let rounds = 0;

  while (queue.length && rounds < 20 && !summary.offline) {
    rounds += 1;
    const batch = queue.slice(0, chunkSize);

    let body: SyncResponse;
    try {
      body = await request<SyncResponse>({
        method: 'POST',
        path: '/attendance/sync',
        body: { device_id: deviceId, punches: batch.map(toSyncPayload) },
      });
    } catch (e) {
      const err = e as { status?: number; offline?: boolean; message?: string; code?: string };
      if (err.offline) {
        summary.offline = true;
        summary.error = 'offline';
        break;
      }
      if (err.status === 413 && chunkSize > 1) {
        chunkSize = Math.max(1, Math.floor(chunkSize / 2));
        continue;
      }
      if (err.status === 401) {
        summary.error = 'unauthorized';
        summary.message = 'Session expired — sign in again.';
        break;
      }
      if (err.status === 403 && err.code === 'device_revoked') {
        summary.error = 'device_revoked';
        summary.message = 'This device was revoked.';
        break;
      }
      if (err.status === 404) {
        summary.error = err.code ?? 'device_unknown';
        summary.message = err.message ?? 'Device unknown — re-register.';
        break;
      }
      summary.error = 'sync_failed';
      summary.message = err.message ?? 'Sync failed';
      break;
    }

    const verdicts = new Map((body.results ?? []).map((r) => [r.client_punch_id, r]));
    summary.sent += batch.length;
    summary.applied += Number(body.applied) || 0;

    for (const rec of batch) {
      // A punch the server did not answer for is treated as rejected, never as settled:
      // ``no_verdict`` keeps it visible instead of pretending it landed.
      const v: SyncVerdict = verdicts.get(rec.client_punch_id) ?? {
        client_punch_id: rec.client_punch_id,
        status: 'rejected',
        code: 'no_verdict',
      };
      const status = localStatus(v.status);
      const settled: QueuedPunch = {
        ...rec,
        status,
        server_status: v.status,
        rejection_code: v.code ?? null,
        effective_time: v.effective_time ?? rec.effective_timestamp,
        site: v.site ?? null,
        flushed_at: formatTs(Date.now()),
      };
      await dbPut('punches', settled);
      if (status === 'rejected') summary.rejected += 1;
      else if (status === 'duplicate') summary.duplicates += 1;
      else if (v.flagged) summary.flagged += 1;

      summary.results.push({
        client_punch_id: rec.client_punch_id,
        action: rec.action,
        status: v.status,
        code: v.code ?? null,
        effective_time: v.effective_time ?? null,
      });

      if (status === 'synced' && rec.photo_sha256) {
        const blob = await loadPhotoBlob(rec.client_punch_id);
        if (blob) {
          const up = await uploadPhoto(rec, blob);
          if (up.ok) {
            summary.photos.uploaded += 1;
            await dbDelete('photos', rec.client_punch_id).catch(() => {});
            await dbPut('punches', { ...settled, photo_uploaded_at: formatTs(Date.now()) });
          } else {
            summary.photos.pending += 1;
            summary.photos.error = up.message ?? 'photo_upload_failed';
            if (up.offline) {
              summary.offline = true;
              break;
            }
          }
        }
      }
    }

    if (body.next_anchor) {
      await adoptAnchor(
        workerId,
        {
          anchor_id: body.next_anchor.anchor_id,
          server_time: body.next_anchor.server_time,
          anchor_signature: body.next_anchor.anchor_signature,
          signature_version: body.next_anchor.signature_version,
        },
        deviceId,
      );
    }

    // No-progress guard. The verdicts are what settle a punch, so this compares the
    // batch against what the queue *still holds*: a batch that is entirely still
    // pending after an accepted response would spin forever if resent unchanged.
    const remaining = await queuedPunches(workerId);
    const stillPending = new Set(remaining.map((r) => r.client_punch_id));
    const progressed = batch.some((b) => !stillPending.has(b.client_punch_id));
    if (!progressed) {
      summary.error = 'no_progress';
      summary.message =
        'The server accepted the batch but settled none of its punches; stopping instead of retrying in a loop.';
      break;
    }
    queue = remaining;
  }

  summary.pending = await queuedPunches(workerId)
    .then((r) => r.length)
    .catch(() => summary.pending);
  summary.at = formatTs(Date.now());
  store.setOfflineQueueDepth(summary.pending);
  store.setLastSyncAt(summary.at);
  await prune(workerId).catch(() => {});
  return summary;
}
