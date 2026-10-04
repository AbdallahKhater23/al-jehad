/**
 * The punch flow: location -> selfie -> online verify, or a signed offline entry.
 *
 * WHY THIS IS ITS OWN MODULE
 * --------------------------
 * Clocking in is the one operation in this app that writes a payable record, and it has
 * three genuinely different outcomes that all look like failure from the outside:
 *
 *   1. **Recorded** — the server verified the face and wrote the row.
 *   2. **Asked again** — the server refused a short clock-out with
 *      ``confirm_early_checkout`` and the figures to spell the question out. This is not an
 *      error; it is the server *delegating a decision to the worker*, and re-posting the same
 *      payload with the confirm flag is the completion of one punch, not a second one.
 *   3. **Queued** — no signal, or the request never reached the server. The punch is signed
 *      with the device key against a server-issued anchor and stored, and it becomes real
 *      attendance when it syncs.
 *
 * Keeping that decision tree in a screen's render function is how the transit case got
 * hardcoded to ``false`` and how a 409 would have been shown as a failure. It lives here,
 * where it can be read as a whole.
 *
 * WHAT IS DELIBERATELY NOT HERE
 * -----------------------------
 * Nothing about a *short* shift is decided on the phone. The server owns the shift rules,
 * and the first attempt never sends ``confirm_early_checkout`` — that is what makes the
 * warning happen at all. The phone's only job is to relay the question and the numbers.
 */

import { request, type ApiError } from '../core/http.js';
import { store } from '../core/store.js';
import { captureLocation, requestLocationPermission } from '../native/geolocation.js';
import { pickPhoto, shrinkPhoto, CameraError, type CapturedPhoto } from '../native/camera.js';

/**
 * The location fields a punch actually transmits.
 *
 * Narrower than ``CapturedLocation`` on purpose: the geofence pre-check hands its fix to the
 * punch so both are measured from the same position, and it holds exactly these four fields.
 * Requiring the full capture object (formatted strings, a timestamp) would force the caller
 * to fabricate fields the signature never reads.
 */
export interface PunchLocation {
  coords: string | null;
  lat: number | null;
  lon: number | null;
  accuracy: number | null;
}
import { isOnline } from '../native/network.js';
import { ensureDevice, refreshAnchor } from '../offline/device.js';
import { enqueuePunch, loadAnchor, anchorFresh, pendingCount } from '../offline/queue.js';

/** The three actions ``POST /attendance/verify`` accepts. */
export type PunchAction = 'Clock In' | 'Clock Out' | 'Transit Checkpoint';

export interface ShiftSnapshot {
  active: boolean;
  /** The shift was opened away from every geofence and no fence has confirmed it yet. */
  inTransit: boolean;
}

/**
 * Which action a tap means, from what the server says the shift is.
 *
 * This is the rule the whole flow hangs on, and it is not "active ? out : in":
 *
 *   * no shift                      -> ``Clock In`` (which, for a transit-enabled account
 *                                      away from every fence, *opens the travel shift*).
 *   * an unconfirmed travel shift   -> ``Transit Checkpoint``: "I am here". The server
 *                                      answers this with a geofence check and **no selfie**,
 *                                      because the arrival is a fact about a place rather
 *                                      than a second identity check.
 *   * a confirmed shift             -> ``Clock Out``.
 *
 * Sending ``Clock Out`` for an unconfirmed travel shift is refused with
 * ``off_site_checkout_needs_admin``, and sending ``Clock In`` twice is refused with
 * ``already_clocked_in``. Both are avoidable here.
 */
export function resolveAction(shift: ShiftSnapshot | null): PunchAction {
  if (!shift?.active) return 'Clock In';
  if (shift.inTransit) return 'Transit Checkpoint';
  return 'Clock Out';
}

/** Does this action need a photograph? Only the arrival is answered without one. */
export function actionNeedsSelfie(action: PunchAction): boolean {
  return action !== 'Transit Checkpoint';
}

export type PunchOutcome =
  | { kind: 'recorded'; action: PunchAction; message: string; site: string | null; hours: number | null }
  | { kind: 'queued'; action: PunchAction; pending: number; message: string }
  | { kind: 'needs_confirmation'; action: PunchAction; figures: EarlyCheckoutFigures }
  | { kind: 'needs_admin'; action: PunchAction; openHours: number | null; message: string }
  | { kind: 'refused'; action: PunchAction; message: string; code: string | null }
  | { kind: 'cancelled' };

export interface EarlyCheckoutFigures {
  paidHours: number;
  regularHours: number;
  elapsedHours: number;
  breakHours: number;
}

interface VerifyResponse {
  status?: string;
  message?: string;
  action?: string;
  site?: string;
  site_name?: string;
  hours?: number;
  hours_worked?: number;
  paid_hours?: number;
  transit_hours?: number;
}

/** The server's structured refusals, as codes the UI can branch on. */
const CODE_EARLY_CHECKOUT = 'confirm_early_checkout';
const CODE_OFF_SITE_CHECKOUT = 'off_site_checkout_needs_admin';
const CODE_PENDING_APPROVAL = 'account_pending_approval';
const CODE_ARRIVAL_CONFIRMED = 'arrival_already_confirmed';
const CODE_ARRIVAL_OUTSIDE = 'arrival_outside_geofence';

export interface PunchDeps {
  /** Where a refusal that needs reading is surfaced. */
  onRefusal?: (message: string) => void;
}

/**
 * Take one punch, end to end.
 *
 * Never throws for an expected outcome: every branch above is a return value, so a caller
 * cannot accidentally treat "the server asked a question" as a crash.
 */
export async function takePunch(deps: PunchDeps = {}): Promise<PunchOutcome> {
  const session = store.getState().session;
  if (!session) return { kind: 'refused', action: 'Clock In', message: 'Sign in first.', code: 'no_session' };

  const shiftState = store.getState().shift;
  const action = resolveAction({
    active: Boolean(shiftState?.active),
    inTransit: shiftState?.startSource === 'transit',
  });

  // ---- capture ------------------------------------------------------------
  // The location fix is taken first and is *not* fatal: a punch with no coordinates is
  // refused by the server when it is outside every fence, but a missing fix must not stop
  // the worker from trying — and the offline path records the punch with the coordinate
  // fields empty, which is exactly what the canonical string expects.
  let location: PunchLocation | null = null;
  try {
    const permission = await requestLocationPermission();
    if (permission === 'granted') location = await captureLocation(12_000);
  } catch {
    location = null;
  }

  let photo: CapturedPhoto | null = null;
  if (actionNeedsSelfie(action)) {
    try {
      photo = await pickPhoto();
    } catch (e) {
      const err = e as CameraError;
      if (err.code === 'cancelled') return { kind: 'cancelled' };
      return { kind: 'refused', action, message: err.message, code: err.code };
    }
  }

  // ---- offline ------------------------------------------------------------
  // Decided *after* the capture, so the worker gets the same shutter and the same preview
  // either way; only the transport differs.
  if (!isOnline()) {
    return queueOffline(action, photo, location);
  }

  return submitOnline(action, photo, location, deps);
}

/**
 * Post the punch. ``confirmEarly`` is only ever set by the second call, after the worker has
 * answered the question.
 */
export async function submitOnline(
  action: PunchAction,
  photo: CapturedPhoto | null,
  location: PunchLocation | null,
  deps: PunchDeps = {},
  confirmEarly = false,
): Promise<PunchOutcome> {
  const session = store.getState().session;
  if (!session) return { kind: 'refused', action, message: 'Sign in first.', code: 'no_session' };

  const form = new FormData();
  // The server accepts this field only as a self-check against the token's own subject and
  // refuses it if it names anybody else, so it is sent as the signed-in id.
  form.append('worker_id', session.user.id);
  form.append('action', action);
  form.append('location_input', location?.coords ?? '');
  if (photo) form.append('selfie', photo.blob, 'selfie.jpg');
  if (confirmEarly) form.append('confirm_early_checkout', '1');

  try {
    const body = await request<VerifyResponse>({
      method: 'POST',
      path: '/attendance/verify',
      formData: form,
      // A punch runs the liveness model and a face comparison; 45s is a slow server rather
      // than a hung one, and giving up earlier would turn a queue of punches into failures.
      timeoutMs: 45_000,
    });

    // A reconnect is exactly when the offline window is renewed, so the anchor is refreshed
    // after a successful punch. Best-effort: a failure here does not affect this punch.
    void refreshAnchor(session.user.id).catch(() => {});

    const site = body.site ?? body.site_name ?? null;
    const hours =
      typeof body.paid_hours === 'number'
        ? body.paid_hours
        : typeof body.hours === 'number'
          ? body.hours
          : typeof body.hours_worked === 'number'
            ? body.hours_worked
            : null;

    return {
      kind: 'recorded',
      action,
      message: body.message ?? `${action} recorded.`,
      site,
      hours,
    };
  } catch (e) {
    const err = e as ApiError;
    const detail = (err.detail ?? {}) as Record<string, unknown>;
    const code = err.code ?? (detail['error_code'] as string | undefined) ?? null;

    // (1) The early-checkout question. Re-posted by the caller once answered.
    if (err.status === 409 && code === CODE_EARLY_CHECKOUT) {
      return {
        kind: 'needs_confirmation',
        action,
        figures: {
          paidHours: Number(detail['paid_hours'] ?? 0),
          regularHours: Number(detail['regular_hours'] ?? 0),
          elapsedHours: Number(detail['elapsed_hours'] ?? 0),
          breakHours: Number(detail['break_hours'] ?? 0),
        },
      };
    }

    // (2) A clock-out from a shift no geofence has confirmed. The shift is still open and
    // still counting; only an administrator can close it. Not a failure to retry.
    //
    // The status is **409**, not 403: this is a conflict with the state of the shift rather
    // than a refusal of the caller's authority, and the two are matched on the code rather
    // than the status alone for exactly that reason. Getting this wrong sent the worker a
    // plain error toast instead of the "ask an administrator" offer the refusal exists to
    // produce — which is the dead end the endpoint's own docstring warns about.
    if ((err.status === 409 || err.status === 403) && code === CODE_OFF_SITE_CHECKOUT) {
      return {
        kind: 'needs_admin',
        action,
        openHours: typeof detail['open_hours'] === 'number' ? (detail['open_hours'] as number) : null,
        message:
          err.message ??
          'This shift has not been confirmed at a site, so it cannot be closed from here.',
      };
    }

    // (3) The transport never carried it. Same punch, different route.
    if (err.offline) {
      return queueOffline(action, photo, location);
    }

    // Everything else is a real refusal, and the ones with a code are worth naming.
    const friendly =
      code === CODE_PENDING_APPROVAL
        ? 'Your account is awaiting administrator approval, so attendance cannot be recorded yet.'
        : code === CODE_ARRIVAL_CONFIRMED
          ? 'This travel shift has already been confirmed at a site.'
          : code === CODE_ARRIVAL_OUTSIDE
            ? 'You are not inside the site yet, so the arrival cannot be confirmed. Move to the site and try again.'
            : (err.message ?? 'The punch was refused.');

    deps.onRefusal?.(friendly);
    return { kind: 'refused', action, message: friendly, code };
  }
}

/**
 * Sign the punch and store it on the phone.
 *
 * The device key and a live anchor are both required, and neither can be obtained offline:
 * the anchor is signed by the *server* precisely so that the client cannot mint one. So this
 * fails closed with an explanation rather than writing a punch the server would only reject
 * later — and a punch that is never written is one nobody is waiting for.
 */
export async function queueOffline(
  action: PunchAction,
  photo: CapturedPhoto | null,
  location: PunchLocation | null,
): Promise<PunchOutcome> {
  const session = store.getState().session;
  if (!session) return { kind: 'refused', action, message: 'Sign in first.', code: 'no_session' };

  // The offline contract carries only the two punch actions: a travel arrival is a geofence
  // decision the server has to make, and there is no signature format for it.
  if (action === 'Transit Checkpoint') {
    return {
      kind: 'refused',
      action,
      message:
        'Confirming an arrival needs a connection — it is a location decision only the server can make. Try again when you have signal.',
      code: 'transit_needs_connection',
    };
  }
  if (actionNeedsSelfie(action) && !photo) {
    return { kind: 'refused', action, message: 'A selfie is required for this punch.', code: 'no_selfie' };
  }

  try {
    const device = await ensureDevice(session.user.id);
    let anchor = await loadAnchor(session.user.id);
    if (!anchorFresh(anchor)) {
      try {
        anchor = await refreshAnchor(session.user.id);
      } catch {
        return {
          kind: 'refused',
          action,
          message:
            'This phone needs one connection to get a time anchor before it can take offline punches.',
          code: 'no_anchor',
        };
      }
    }
    if (!anchor) {
      return {
        kind: 'refused',
        action,
        message: 'No time anchor is held on this phone yet. Connect once to enable offline punches.',
        code: 'no_anchor',
      };
    }

    await enqueuePunch({
      workerId: session.user.id,
      action,
      coords: location?.coords ?? null,
      accuracy: location?.accuracy ?? null,
      photoBlob: photo?.blob ?? null,
      device,
      anchor,
    });

    const pending = await pendingCount(session.user.id);
    store.setOfflineQueueDepth(pending);
    return {
      kind: 'queued',
      action,
      pending,
      message: `${action} saved on this phone and signed. It uploads by itself when there is signal.`,
    };
  } catch (e) {
    return {
      kind: 'refused',
      action,
      message: (e as Error).message ?? 'The punch could not be saved on this phone.',
      code: 'queue_failed',
    };
  }
}

/**
 * Complete a punch the server asked about, by restating it with the confirmation.
 *
 * The figures are echoed back so the second request is byte-identical apart from the flag —
 * the server re-runs its own check and the same shift is decided once, not twice.
 */
export async function confirmEarlyCheckout(
  action: PunchAction,
  photo: CapturedPhoto | null,
  location: PunchLocation | null,
  deps: PunchDeps = {},
): Promise<PunchOutcome> {
  return submitOnline(action, photo, location, deps, true);
}

/** Exposed for tests: the classification the UI branches on. */
export const __test__ = { resolveAction, actionNeedsSelfie, shrinkPhoto };
