/**
 * The sync loop: when the queue is drained, and what is refreshed afterwards.
 *
 * WHY THIS IS NOT IN ``main.ts``
 * ------------------------------
 * It used to be, and two screens imported it from there — which made the application entry
 * point a *dependency* of the screens it mounts. That is a cycle (``main`` imports the clock
 * screen, the clock screen imports ``main``), and it means importing a screen for any reason
 * drags in the boot sequence and its module-scope side effects. Extracting the orchestrator
 * breaks the cycle and puts the retry policy somewhere a reader can find it without reading
 * the bootstrap first.
 *
 * WHAT IT OWNS
 * ------------
 * * one sync at a time — a reconnect burst must not post the same queue three times;
 * * the refreshes a landed sync makes necessary (the shift, because a queued clock-out may
 *   have just closed one; the badges, because the server may have answered with new ones);
 * * nothing else. The queue itself, the anchor and the signatures all live in ``offline/``.
 */

import { store } from './store.js';
import { ensureDevice } from '../offline/device.js';
import { syncNow, type SyncSummary } from '../offline/sync.js';
import { request } from './http.js';
import { setBadges } from '../ui/shell.js';

/**
 * A single-flight guard.
 *
 * Not a queue and not a debounce: a second caller while a sync is running gets ``null`` and
 * does nothing. Every trigger here (reconnect, resume, foreground, a manual tap) fires in
 * bursts, and re-posting a batch that is already in flight is how a punch gets a
 * ``replayed_nonce`` refusal for a reason the worker cannot see.
 */
let inFlight: Promise<SyncSummary | null> | null = null;

async function refreshShiftState(): Promise<void> {
  try {
    const stats = await request<{
      active_session?: { site_name?: string; clock_in_time?: string; in_transit?: boolean } | null;
    }>({ path: '/worker/me/stats' });
    const session = stats.active_session ?? null;
    store.setShift({
      active: Boolean(session),
      siteName: session?.site_name ?? null,
      clockInTime: session?.clock_in_time ?? null,
      // ``in_transit`` is what makes the next tap a "confirm arrival" rather than a
      // clock-out, so it is carried rather than inferred from the site name — the
      // placeholder is a sentence for a human, and matching on English is how a reworded
      // placeholder becomes a missing button.
      startSource: session?.in_transit ? 'transit' : null,
    });
  } catch {
    // Offline or unavailable: the last known shift state is kept rather than blanked.
  }
}

async function refreshBadges(): Promise<void> {
  try {
    const [alerts, notes] = await Promise.all([
      request<{ unread: number }>({
        path: '/worker/me/notifications',
        query: { limit: 1, unread_only: true },
      }),
      request<{ unread: number }>({ path: '/worker/notes', query: { limit: 1 } }),
    ]);
    setBadges({ alerts: Number(alerts.unread ?? 0), notes: Number(notes.unread ?? 0) });
  } catch {
    // A badge that cannot be read stays where it was.
  }
}

/** Drain the queue for a known worker and device. Returns ``null`` if a sync is already running. */
export async function runSync(workerId: string, deviceId: string): Promise<SyncSummary | null> {
  if (inFlight) return null;
  inFlight = (async () => {
    const summary = await syncNow(workerId, deviceId);
    store.setOfflineQueueDepth(summary.pending);
    if (summary.at) store.setLastSyncAt(summary.at);
    // Only worth re-reading when something actually moved: a sync that sent nothing has
    // nothing new to say about the shift or the inbox.
    if (summary.sent > 0) {
      await refreshShiftState();
      await refreshBadges();
    }
    return summary;
  })();
  try {
    return await inFlight;
  } finally {
    inFlight = null;
  }
}

/** Drain the queue for whoever is signed in. Safe to call when nobody is. */
export async function syncForCurrentWorker(): Promise<SyncSummary | null> {
  const session = store.getState().session;
  if (!session) return null;
  const device = await ensureDevice(session.user.id).catch(() => null);
  if (!device) return null;
  return runSync(session.user.id, device.device_id);
}

/** Re-read the shift and the badge counts without touching the queue. */
export async function refreshWorkerState(): Promise<void> {
  await Promise.all([refreshShiftState(), refreshBadges()]);
}

/** Whether a sync is running right now, for a screen that wants to disable a button. */
export function isSyncing(): boolean {
  return inFlight !== null;
}
