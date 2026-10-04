/**
 * The clock screen — the reason the app exists.
 *
 * The flow, in the order a worker meets it:
 *
 *   1. What is my shift? (elapsed timer, site, whether it is already overdue)
 *   2. Where am I? (the geofence pre-check, before the shutter rather than after)
 *   3. Take the punch (selfie -> online verify, or a signed offline queue entry)
 *
 * The screen owns the *rendering* and the timer; the punch decision tree lives in
 * ``ui/punch.ts`` and the geofence in ``ui/geofence.ts``, so the three refusal paths and the
 * transit rules can be read as a whole instead of being spread through a render function.
 *
 * One rule behind all of it: **nothing about the punch is a surprise after the shutter.** The
 * server refuses a punch from outside every fence and asks a second time about a short shift,
 * so both facts are surfaced here, before the camera opens.
 */

import { request } from '../../../core/http.js';
import { store } from '../../../core/store.js';
import { syncForCurrentWorker } from '../../../core/sync-loop.js';
import { refreshAnchor } from '../../../offline/device.js';
import type { Screen, ScreenContext } from '../../shell.js';
import { el, esc, on, fmtClock, fmtHours } from '../../dom.js';
import { icon } from '../../icons.js';
import { openSheet } from '../../components/sheet.js';
import { captureSelfie } from '../../components/capture.js';
import { toastError, toastOk, toastWarn } from '../../components/toast.js';
import { checkGeofence, publishGeofence, type GeofenceResult } from '../../geofence.js';
import {
  confirmEarlyCheckout,
  resolveAction,
  type PunchOutcome,
  type PunchLocation,
  type PunchAction,
} from '../../punch.js';
import type { CapturedPhoto } from '../../../native/camera.js';

interface StatsResponse {
  worker_id: string;
  approval_status: string;
  total_hours: number;
  regular_hours: number;
  pending_hours: number;
  overtime_hours: number;
  overtime_notify_hours: number;
  break_minutes: number;
  paid_day_hours: number;
  on_site_day_hours: number;
  auto_close_at_regular: number;
  flagged_for_review: boolean;
  active_session: {
    site_name: string;
    clock_in_time: string;
    late_flag: string | null;
    in_transit: boolean;
    seconds_on_site: number;
  } | null;
}

export function createClockScreen(): Screen {
  return {
    title: () => 'Clock',
    subtitle: () => store.getState().shift?.siteName ?? 'Attendance',
    actions: () => [
      (() => {
        const node = el(
          `<button class="ui-btn ui-btn-quiet is-icon" type="button" aria-label="Refresh">${icon('refresh', 22)}</button>`,
        ) as HTMLButtonElement;
        on(node, 'click', () => document.dispatchEvent(new CustomEvent('hand:refresh-clock')));
        return node;
      })(),
    ],
    mount(host: HTMLElement, ctx: ScreenContext): () => void {
      const disposers: Array<() => void> = [];
      let stats: StatsResponse | null = null;
      let geo: GeofenceResult = {
        state: 'checking',
        siteName: null,
        windowLabel: null,
        arrival: null,
        coords: null,
        lat: null,
        lon: null,
        accuracy: null,
      };
      /** The frame the worker has already taken, kept for a retry or a confirmation. */
      let pendingPhoto: CapturedPhoto | null = null;
      let busy = false;
      let destroyed = false;
      let timer: number | null = null;
      const mountedAt = Date.now();

      host.innerHTML = `<div id="clock-root"></div>`;
      const root = host.querySelector('#clock-root') as HTMLElement;

      // ---- the live timer ----------------------------------------------------
      function tick(): void {
        if (destroyed) return;
        const session = stats?.active_session;
        if (!session) {
          // Off shift there is no elapsed figure -- the card carries the wall clock, and
          // it has to keep time or it is a still photograph of a clock.
          const now = root.querySelector<HTMLElement>('#clock-now');
          if (now) now.textContent = wallClock();
          return;
        }
        const elapsedNode = root.querySelector('#clock-elapsed');
        if (!elapsedNode) return;
        const seconds =
          Number(session.seconds_on_site || 0) + Math.floor((Date.now() - mountedAt) / 1000);
        elapsedNode.textContent = fmtClock(seconds);
        // The ring and the line under it are the same figure twice: the dial says how
        // much of the paid day is gone, the sentence says what is left of it. Both are
        // driven from one percentage so they cannot disagree.
        const target = Number(stats?.on_site_day_hours || 8) * 3600;
        const pct = Math.min(100, Math.round((seconds / target) * 100));
        const ring = root.querySelector<SVGCircleElement>('#clock-ring');
        if (ring) {
          ring.setAttribute('stroke-dasharray', `${pct} 100`);
          ring.classList.toggle('is-over', pct >= 100);
        }
        const left = root.querySelector<HTMLElement>('#clock-remaining');
        if (left) {
          const remaining = Math.max(0, target - seconds);
          const over = seconds > target;
          left.textContent = over
            ? `${fmtHours((seconds - target) / 3600)} over the ${fmtHours(target / 3600)} paid day`
            : `${fmtHours(remaining / 3600)} left of the ${fmtHours(target / 3600)} paid day`;
          left.classList.toggle('is-done', over);
        }
        const elapsed = root.querySelector<HTMLElement>('#clock-elapsed');
        elapsed?.classList.toggle('is-over', pct >= 100);
      }

      // ---- render ------------------------------------------------------------
      function currentAction(): PunchAction {
        return resolveAction({
          active: Boolean(stats?.active_session),
          inTransit: Boolean(stats?.active_session?.in_transit),
        });
      }

      function paint(): void {
        const session = stats?.active_session ?? null;
        const active = Boolean(session);
        const queued = store.getState().offlineQueueDepth;
        const lastSync = store.getState().lastSyncAt;
        const approval = String(stats?.approval_status ?? 'active').toLowerCase();

        // One column with a real gap, exactly as the web's clock panel is laid out: the
        // card, then the one action, then the facts that qualify it. The welcome, the
        // review alert and the inbox band sit above the card on the web, for the same
        // reason they sit above it here -- they are about the account, not about today.
        root.innerHTML = `
          <div class="hand-panel">
            ${approval === 'pending_approval' ? renderApprovalBanner() : ''}
            ${renderQueueBanner(queued, lastSync)}
            ${renderHero(session, active)}
            ${renderSiteAlert()}
            ${renderPhotoPreview()}
            ${renderAction(active, session)}
            ${renderTotals()}
          </div>
        `;
      }

      function renderApprovalBanner(): string {
        // The web's ``.hand-alert is-danger``: one edge, one wash, the words carrying the
        // meaning. The account, not the shift, is what is waiting.
        return `
          <div class="hand-alert is-danger">
            ${icon('alert', 16)}
            <p><strong>Waiting for approval</strong> Your account cannot record attendance yet.
            An administrator has to approve it first — this updates on its own when they do.</p>
          </div>
        `;
      }

      /**
       * The queue, as the web's offline band.
       *
       * Nothing is drawn when the queue is empty: on the web, "everything is synced" is
       * the absence of a warning, not a second card. The last-check time lives on Profile
       * beside the other session facts, where it is a fact rather than a banner.
       */
      function renderQueueBanner(queued: number, lastSync: string | null): string {
        void lastSync;
        if (queued === 0) return '';
        return `
          <div class="hand-alert">
            ${icon('wifiOff', 16)}
            <p><strong>${queued} punch${queued === 1 ? '' : 'es'} waiting to sync</strong>
            Saved on this phone and signed. They upload by themselves when there is signal.</p>
            <button class="ui-btn ui-btn-sm" type="button" data-action="sync-now">Sync now</button>
          </div>
        `;
      }

      /** The clock on the wall, for the card with no shift to count. */
      function wallClock(): string {
        const now = new Date();
        return [now.getHours(), now.getMinutes(), now.getSeconds()]
          .map((part) => String(part).padStart(2, '0'))
          .join(':');
      }

      /** The status the hero's dot and word carry, from the geofence where there is one. */
      function siteState(): { onSite: boolean; known: boolean; label: string } {
        if (geo.state === 'on_site') {
          return { onSite: true, known: true, label: geo.siteName ?? 'Your site' };
        }
        if (geo.state === 'off_site') return { onSite: false, known: true, label: '' };
        if (geo.state === 'checking') return { onSite: false, known: false, label: '' };
        return { onSite: false, known: false, label: '' };
      }

      /**
       * The shift card: what this is, which state it is in, how long it has run, and the
       * numbers it is paid by -- one band each, left aligned, in the web's own order.
       *
       * The site is the one fact here the worker did not choose, so it is a quiet chip in
       * the corner rather than a second headline, and it truncates instead of wrapping.
       */
      function renderHero(session: StatsResponse['active_session'], active: boolean): string {
        const onSiteHours = Number(stats?.on_site_day_hours || 8);
        const paidDay = Number(stats?.paid_day_hours || 8) || 8;
        const breaks = Number(stats?.break_minutes ?? 0);
        const overtime = Number(stats?.overtime_notify_hours ?? 0);
        const month = Number(stats?.total_hours ?? 0);
        const site = siteState();
        const statusWord = active
          ? session?.in_transit
            ? 'On the road'
            : 'On shift'
          : 'Off shift';
        const siteChip = site.label
          ? `<p class="hand-hero-site">Site <strong>${esc(site.label)}</strong></p>`
          : '';

        return `
          <div class="hand-hero ${active ? 'is-live' : ''}">
            <div class="hand-hero-top">
              <p class="hand-hero-status">
                <span class="hand-dot ${active && site.onSite ? '' : 'is-off'}" aria-hidden="true"></span>${esc(statusWord)}
              </p>
              ${siteChip}
            </div>
            <p class="hand-hero-word">${active ? 'You are clocked in' : 'Currently clocked out'}</p>
            ${
              active && session
                ? `
                  <div class="hand-figure">
                    <div class="hand-figure-main">
                      <span class="hand-timer" id="clock-elapsed">${esc(fmtClock(Number(session.seconds_on_site)))}</span>
                      <span class="hand-timer-label">Time on shift</span>
                    </div>
                    <svg class="hand-ring" viewBox="0 0 48 48" aria-hidden="true">
                      <circle class="hand-ring-track" cx="24" cy="24" r="20"></circle>
                      <circle class="hand-ring-fill" id="clock-ring" cx="24" cy="24" r="20" pathLength="100"
                              stroke-dasharray="0 100"></circle>
                    </svg>
                  </div>
                  <p class="hand-day-line" id="clock-remaining"></p>
                `
                : `
                  <div class="hand-figure">
                    <div class="hand-figure-main">
                      <span class="hand-timer is-now" id="clock-now">${esc(wallClock())}</span>
                      <span class="hand-timer-label" id="clock-today">${esc(
                        new Date().toDateString(),
                      )}</span>
                    </div>
                  </div>
                `
            }
            <div class="hand-policy">
              ${
                active && session
                  ? `<span class="hand-policy-cell is-wide">
                       <span class="hand-policy-label">Clocked in at</span>
                       <span class="hand-policy-value is-mono">${esc(session.clock_in_time)}${
                         session.late_flag ? ` · ${esc(session.late_flag)}` : ''
                       }</span>
                     </span>`
                  : ''
              }
              <span class="hand-policy-cell">
                <span class="hand-policy-label">Paid day</span>
                <span class="hand-policy-value"><b>${esc(String(paidDay))}</b> h</span>
              </span>
              <span class="hand-policy-cell">
                <span class="hand-policy-label">Unpaid break</span>
                <span class="hand-policy-value"><b>${esc(String(breaks))}</b> min</span>
              </span>
              ${
                overtime > 0
                  ? `<span class="hand-policy-cell">
                       <span class="hand-policy-label">Overtime after</span>
                       <span class="hand-policy-value"><b>${esc(String(overtime))}</b> h</span>
                     </span>`
                  : ''
              }
            </div>
            <p class="hand-hero-foot">
              <span>Hours this month</span>
              <b class="is-mono">${esc(month.toFixed(2))} h</b>
            </p>
            ${
              Number(stats?.on_site_day_hours ?? 0) > 0
                ? `<p class="hand-hero-meta">On site counts ${esc(String(onSiteHours))} h of the paid day.</p>`
                : ''
            }
          </div>
        `;
      }

      /**
       * What the site check found, when it is not simply "you are here".
       *
       * Above the button on purpose: whether a punch can be recorded is exactly what the
       * web puts above its action, and a refusal the worker only meets after the shutter is
       * a refusal they cannot act on.
       */
      function renderSiteAlert(): string {
        switch (geo.state) {
          case 'checking':
            return '';
          case 'on_site':
            return '';
          case 'off_site':
            return `
              <div class="hand-alert">
                ${icon('alert', 16)}
                <p><strong>Not at a site</strong> ${
                  esc(geo.arrival ?? 'A punch from here will be flagged for review.')
                }</p>
                <button class="ui-btn ui-btn-sm" type="button" data-action="recheck">Retry</button>
              </div>
            `;
          case 'denied':
          case 'offline':
          case 'error':
            return `
              <div class="hand-alert is-danger">
                ${icon('alert', 16)}
                <p><strong>Location unavailable</strong> ${esc(
                  geo.message ?? 'The site check could not run.',
                )}</p>
                <button class="ui-btn ui-btn-sm" type="button" data-action="recheck">Retry</button>
              </div>
            `;
        }
      }

      function renderPhotoPreview(): string {
        const action = currentAction();
        if (!actionNeedsPhoto(action)) {
          return `
            <div class="hand-card is-flat clock-preview">
              <div class="clock-preview__frame">${icon('pin', 24)}</div>
              <div class="clock-preview__body">
                <span>No selfie needed to confirm an arrival.</span>
                <span class="clock-preview__hint">Arriving is a location check — your face was verified when the shift began.</span>
              </div>
            </div>
          `;
        }
        if (!pendingPhoto) {
          return `
            <div class="hand-card is-flat clock-preview">
              <div class="clock-preview__frame">${icon('camera', 24)}</div>
              <div class="clock-preview__body">
                <span>A selfie is taken with every punch.</span>
                <span class="clock-preview__hint">Your face must be visible and well lit.</span>
              </div>
            </div>
          `;
        }
        const url = URL.createObjectURL(pendingPhoto.blob);
        const kb = Math.round(pendingPhoto.bytes / 1024);
        // Revoked after the preview has had time to load; the object URL only has to outlive
        // this render.
        window.setTimeout(() => URL.revokeObjectURL(url), 30_000);
        return `
          <div class="hand-card is-flat clock-preview">
            <div class="clock-preview__frame"><img src="${esc(url)}" alt="Selfie preview" /></div>
            <div class="clock-preview__body">
              <span>Selfie ready — ${esc(pendingPhoto.width)}×${esc(pendingPhoto.height)}, ${esc(kb)} KB</span>
              <span class="clock-preview__hint">${
                pendingPhoto.resized ? 'Resized to the server’s frame policy.' : 'Within the server’s frame policy.'
              }</span>
            </div>
            <button class="ui-btn ui-btn-sm" type="button" data-action="clear-photo">Retake</button>
          </div>
        `;
      }

      function actionNeedsPhoto(action: PunchAction): boolean {
        return action !== 'Transit Checkpoint';
      }

      /**
       * The one action, in the web's own button: the icon and the word on one line, the
       * fill carrying the state (green in, red out).
       *
       * The readiness footnote under it is the web's ``.hand-action-line`` -- whether this
       * phone can record the punch at all is a footnote to the button, not a card beside
       * the shift -- and it is the same dot the web draws: green when a fix is held, amber
       * while it is being checked, grey when it could not be taken.
       */
      function renderAction(active: boolean, session: StatsResponse['active_session']): string {
        const action = currentAction();
        const label = action === 'Transit Checkpoint' ? 'Confirm arrival' : action;
        const ready = geo.state === 'on_site';
        const checking = geo.state === 'checking';
        const readyText = checking
          ? 'Location · checking…'
          : ready
            ? 'Location · ready'
            : 'Location · not confirmed';
        const dotClass = ready ? 'is-ok' : checking ? '' : 'is-warn';
        return `
          <button class="clock-button hand-clock ${action === 'Clock Out' ? 'out' : 'in'}"
                  type="button" data-action="punch" ${busy ? 'disabled' : ''}>
            ${action === 'Clock Out' ? icon('logout', 18) : icon('clock', 18)}
            <span>${esc(busy ? 'Working…' : label)}</span>
          </button>
          ${
            active && session && action === 'Clock Out'
              ? `<p class="hand-note-sub">A clock-out away from a site is refused until an administrator closes the shift.</p>`
              : ''
          }
          ${
            active
              ? `<button class="ui-btn ui-btn-quiet is-block" type="button" data-action="ask-checkout">Ask an administrator to close it</button>`
              : ''
          }
          <p class="hand-action-line" data-punch-ready="${ready ? 1 : 0}">
            <span class="hand-ready-dot ${dotClass}" aria-hidden="true"></span>${esc(readyText)}
          </p>
        `;
      }

      /**
       * The month so far, as the web draws its figures: a label and a value per cell, in
       * the same policy grid the shift card uses, so the two read as one table.
       */
      function renderTotals(): string {
        if (!stats) return '';
        const overtime = Number(stats.overtime_hours);
        const cell = (label: string, value: number, tone = ''): string => `
          <span class="hand-policy-cell">
            <span class="hand-policy-label">${esc(label)}</span>
            <span class="hand-policy-value"><b${tone ? ` class="${tone}"` : ''}>${esc(value.toFixed(2))}</b> h</span>
          </span>
        `;
        return `
          <div class="hand-card">
            <div class="hand-section-head">
              <h2 class="hand-section-title">This month</h2>
            </div>
            <div class="hand-policy" style="margin-top:0;padding-top:0;border-top:0">
              ${cell('Recorded', Number(stats.total_hours))}
              ${cell('Approved', Number(stats.regular_hours))}
              ${cell('Awaiting approval', Number(stats.pending_hours))}
            </div>
            ${
              overtime > 0
                ? `<div class="hand-alert is-info" style="margin-top:12px">
                     ${icon('info', 16)}
                     <p>${esc(overtime.toFixed(2))} h of overtime is waiting for a decision.</p>
                   </div>`
                : ''
            }
          </div>
        `;
      }

      // ---- data --------------------------------------------------------------
      async function loadStats(): Promise<void> {
        try {
          const body = await request<StatsResponse>({ path: '/worker/me/stats' });
          stats = body;
          store.setShift({
            active: Boolean(body.active_session),
            siteName: body.active_session?.site_name ?? null,
            clockInTime: body.active_session?.clock_in_time ?? null,
            startSource: body.active_session?.in_transit ? 'transit' : null,
          });
          paint();
        } catch (e) {
          const err = e as { offline?: boolean; message?: string };
          if (stats) return; // keep the last known shift rather than blanking the screen
          root.innerHTML = `
            <div class="hand-panel" id="clock-root-panel">
              <div class="hand-alert is-danger">
                ${icon('wifiOff', 16)}
                <p><strong>Working offline</strong> ${esc(
                  err.offline
                    ? 'No connection to the server. Punches you take are saved and signed on this phone.'
                    : (err.message ?? 'The server could not be reached.'),
                )}</p>
              </div>
              ${renderQueueBanner(store.getState().offlineQueueDepth, store.getState().lastSyncAt)}
              <button class="ui-btn ui-btn-quiet is-block" type="button" data-action="reload">Try again</button>
            </div>
          `;
        }
      }

      async function loadGeofence(): Promise<void> {
        geo = { ...geo, state: 'checking' };
        paint();
        geo = await checkGeofence();
        publishGeofence(geo);
        paint();
      }

      // ---- the punch ---------------------------------------------------------
      async function doPunch(): Promise<void> {
        if (busy) return;
        const approval = String(stats?.approval_status ?? 'active').toLowerCase();
        if (approval === 'pending_approval') {
          toastWarn('Your account is still waiting for approval.');
          return;
        }

        const action = currentAction();
        let photo: CapturedPhoto | null = pendingPhoto;

        // The arrival takes no photograph; everything else does. The overlay is opened here
        // rather than inside the punch module so the preview the worker just saw is the one
        // that gets submitted — and so a cancelled shutter returns to this screen, not a
        // half-finished request.
        if (actionNeedsPhoto(action) && !photo) {
          const captured = await captureSelfie();
          if (!captured.photo) return;
          photo = captured.photo;
          pendingPhoto = photo;
          paint();
        }

        busy = true;
        paint();
        try {
          // The fix the geofence check already took is reused, so the indicator and the
          // punch cannot be measured from two different positions.
          const location = geo.coords
            ? { coords: geo.coords, lat: geo.lat, lon: geo.lon, accuracy: geo.accuracy }
            : null;

          const outcome = await handleOutcome(
            await takePunchWith(action, photo, location),
          );
          void outcome;
        } finally {
          busy = false;
          if (!destroyed) paint();
        }
      }

      /**
       * Submit a punch whose frame the screen has already captured.
       *
       * ``takePunch`` owns its own capture, which would open a second overlay behind the
       * preview the worker just saw. The screen therefore captures and this submits — but the
       * *classification* of the answer is the module's either way, so the early-checkout and
       * off-site branches cannot drift between the two entry points.
       */
      async function takePunchWith(
        action: PunchAction,
        photo: CapturedPhoto | null,
        location: PunchLocation | null,
      ): Promise<PunchOutcome> {
        const { submitOnline, queueOffline } = await import('../../punch.js');
        const { isOnline } = await import('../../../native/network.js');
        if (!isOnline()) return queueOffline(action, photo, location);
        return submitOnline(action, photo, location, { onRefusal: (m) => toastError(m) });
      }

      async function handleOutcome(outcome: PunchOutcome): Promise<void> {
        switch (outcome.kind) {
          case 'recorded':
            pendingPhoto = null;
            toastOk(outcome.message);
            await loadStats();
            void refreshAnchor(store.getState().session?.user.id ?? '').catch(() => {});
            return;

          case 'queued':
            pendingPhoto = null;
            toastOk(outcome.message);
            await loadStats();
            return;

          case 'needs_confirmation': {
            const { confirmDialog } = await import('../../components/sheet.js');
            const f = outcome.figures;
            const confirmed = await confirmDialog({
              title: 'Clock out early?',
              message: `You have worked ${f.paidHours.toFixed(2)} h of the ${f.regularHours.toFixed(2)} h paid day. Clocking out now records ${f.paidHours.toFixed(2)} h, not the full day.`,
              detail: `
                <div class="hand-card">
                  <div class="hand-spread"><span class="hand-dim">On site</span><span class="hand-mono">${esc(f.elapsedHours.toFixed(2))} h</span></div>
                  <div class="hand-spread"><span class="hand-dim">Paid so far</span><span class="hand-mono">${esc(f.paidHours.toFixed(2))} h</span></div>
                  <div class="hand-spread"><span class="hand-dim">Paid day</span><span class="hand-mono">${esc(f.regularHours.toFixed(2))} h</span></div>
                </div>
              `,
              confirmLabel: 'Clock out anyway',
              cancelLabel: 'Stay clocked in',
              danger: true,
            });
            if (!confirmed) return;
            const location = geo.coords
              ? { coords: geo.coords, lat: geo.lat, lon: geo.lon, accuracy: geo.accuracy }
              : null;
            const second = await confirmEarlyCheckout(outcome.action, pendingPhoto, location, {
              onRefusal: (m) => toastError(m),
            });
            await handleOutcome(second);
            return;
          }

          case 'needs_admin':
            openSheet({
              title: 'Close this shift?',
              body: `
                <p class="hand-dim">${esc(outcome.message)}</p>
                ${
                  outcome.openHours !== null
                    ? `<div class="hand-card"><div class="hand-spread"><span class="hand-dim">Open for</span><span class="hand-mono">${esc(outcome.openHours.toFixed(2))} h</span></div></div>`
                    : ''
                }
                <p class="hand-faint">It keeps counting until an administrator closes it and sets the hours they judge.</p>
              `,
              actions: [
                {
                  label: 'Ask an administrator',
                  kind: 'primary',
                  onClick: (sheet) => {
                    void askCheckout().then(() => sheet.close());
                  },
                },
                { label: 'Not now', kind: 'ghost', onClick: (sheet) => sheet.close() },
              ],
            });
            return;

          case 'refused':
            toastError(outcome.message);
            return;

          case 'cancelled':
            return;
        }
      }

      async function askCheckout(): Promise<void> {
        try {
          const body = await request<{ message?: string }>({
            method: 'POST',
            path: '/worker/me/request_checkout',
            body: {},
          });
          toastOk(body.message ?? 'Your request was sent.');
        } catch (e) {
          toastError((e as Error).message ?? 'The request could not be sent.');
        }
      }

      // ---- wiring ------------------------------------------------------------
      disposers.push(
        on(root, 'click', (event) => {
          const target = (event.target as HTMLElement).closest('[data-action]') as HTMLElement | null;
          if (!target) return;
          const action = target.getAttribute('data-action');
          if (action === 'punch') void doPunch();
          else if (action === 'recheck') void loadGeofence();
          else if (action === 'clear-photo') {
            pendingPhoto = null;
            paint();
          } else if (action === 'ask-checkout') void askCheckout();
          else if (action === 'sync-now') {
            void (async () => {
              const summary = await syncForCurrentWorker();
              if (summary?.error) toastError(summary.message ?? 'Sync did not finish.');
              else toastOk('Everything is synced.');
              await loadStats();
            })();
          } else if (action === 'reload') void loadStats();
        }),
      );

      const onRefresh = (): void => {
        void loadStats();
        void loadGeofence();
      };
      document.addEventListener('hand:refresh-clock', onRefresh);
      disposers.push(() => document.removeEventListener('hand:refresh-clock', onRefresh));

      // The queue banner follows the store, so a sync that lands while the screen is open
      // updates the count without a refetch.
      disposers.push(
        store.subscribe(() => {
          if (!destroyed) paint();
        }),
      );

      timer = window.setInterval(tick, 1000);
      disposers.push(() => {
        if (timer !== null) window.clearInterval(timer);
      });

      void (async () => {
        await loadStats();
        await loadGeofence();
      })();

      ctx.main.scrollTop = 0;
      return () => {
        destroyed = true;
        disposers.forEach((off) => off());
      };
    },
  };
}
