/**
 * The geofence pre-check.
 *
 * ``GET /worker/me/site-window`` answers the same question the punch will be judged by — the
 * same fence, the same per-site window, the same predicate — one tap *before* the shutter.
 * It is deliberately read-only and unable to refuse anything: whatever it returns, the punch
 * is still decided by ``POST /attendance/verify``. A phone that cannot reach this endpoint
 * loses a line of text, not its hours.
 *
 * Why it exists at all: the punch card used to say nothing about the window until *after* the
 * photograph, and the verdict was written into an administrator's notification — a sentence
 * about the worker that the worker never read. Somebody standing at a gate should know they
 * are early, on time, or late while there is still time to do something about it.
 */

import { request, type ApiError } from '../core/http.js';
import { captureLocation, requestLocationPermission } from '../native/geolocation.js';
import { isOnline } from '../native/network.js';
import { store } from '../core/store.js';

export type GeofenceState = 'checking' | 'on_site' | 'off_site' | 'denied' | 'offline' | 'error';

export interface GeofenceResult {
  state: GeofenceState;
  /** The site the fence belongs to, when one matched. */
  siteName: string | null;
  /** A sentence about the clock-in window, when the server sent one. */
  windowLabel: string | null;
  /** The worker's own arrival verdict, when the server sent one. */
  arrival: string | null;
  /** Why there is no answer, for the states that have none. */
  message?: string;
  /** The fix this was measured from, reused by the punch so it is taken once. */
  coords: string | null;
  lat: number | null;
  lon: number | null;
  accuracy: number | null;
}

interface SiteWindowResponse {
  on_site: boolean;
  site_name: string | null;
  window: Record<string, unknown> | null;
  worker_id: string;
  /** The arrival verdict, spread into the top level by the endpoint. */
  [key: string]: unknown;
}

/** Pull a human sentence out of the arrival block, whatever shape this build sent. */
function arrivalSentence(body: SiteWindowResponse): string | null {
  const candidate =
    (body['message'] as string | undefined) ??
    (body['status'] as string | undefined) ??
    (body['arrival'] as string | undefined) ??
    null;
  return candidate ? String(candidate) : null;
}

/**
 * Check where the worker is against the site fences.
 *
 * The location fix is returned with the answer so the punch can reuse it: taking two fixes
 * seconds apart is how the indicator says "at site A" and the punch is judged against site B
 * — the worker moves a few metres, or the GPS settles, and the two disagree.
 */
export async function checkGeofence(): Promise<GeofenceResult> {
  const empty: Omit<GeofenceResult, 'state'> = {
    siteName: null,
    windowLabel: null,
    arrival: null,
    coords: null,
    lat: null,
    lon: null,
    accuracy: null,
  };

  if (!isOnline()) {
    return { ...empty, state: 'offline', message: 'No connection — the site check needs the server.' };
  }

  let permission: 'granted' | 'denied';
  try {
    permission = await requestLocationPermission();
  } catch {
    permission = 'denied';
  }
  if (permission === 'denied') {
    return {
      ...empty,
      state: 'denied',
      message: 'Location permission was refused. Allow it in Settings to check your site.',
    };
  }

  let fix;
  try {
    fix = await captureLocation(12_000);
  } catch (e) {
    return { ...empty, state: 'error', message: (e as Error).message };
  }

  if (!fix.coords) {
    return { ...empty, state: 'error', message: 'The device did not return a position.' };
  }

  // ``empty`` supplies the null fields the states above share; only the four location fields
  // are replaced here, so every early return stays one shape.
  const base = {
    ...empty,
    coords: fix.coords,
    lat: fix.lat,
    lon: fix.lon,
    accuracy: fix.accuracy,
  };

  try {
    const body = await request<SiteWindowResponse>({
      path: '/worker/me/site-window',
      query: { location_input: fix.coords },
    });
    const windowLabel =
      (body.window && typeof body.window === 'object'
        ? ((body.window['label'] as string | undefined) ?? (body.window['description'] as string | undefined))
        : undefined) ?? null;
    return {
      ...base,
      state: body.on_site ? 'on_site' : 'off_site',
      siteName: body.site_name,
      windowLabel,
      arrival: arrivalSentence(body),
    };
  } catch (e) {
    const err = e as ApiError;
    return {
      ...base,
      state: err.offline ? 'offline' : 'error',
      message: err.offline ? 'No connection — the site check needs the server.' : (err.message ?? 'The check failed.'),
    };
  }
}

/** Cache the result in the store so other screens see the same answer. */
export function publishGeofence(result: GeofenceResult): void {
  store.setGeofence({
    siteName: result.siteName,
    onSite: result.state === 'on_site',
    windowLabel: result.arrival ?? result.windowLabel,
    distanceM: null,
  });
}
