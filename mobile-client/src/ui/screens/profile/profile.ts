/**
 * Profile — the account, the device, and the way out.
 *
 * The device panel is the one part of this screen that is not just an account view. An
 * offline punch is only replayable while the phone still holds its signing key and a
 * server-signed anchor that has not aged out, so "can I take a punch with no signal right
 * now?" is a question with a real answer — and a worker standing in a dead spot deserves to
 * see it before they need it, not after.
 */

import { request } from '../../../core/http.js';
import { store } from '../../../core/store.js';
import { resolveApiBaseUrl } from '../../../core/config.js';
import { clearPersistedSession } from '../../../core/http.js';
import { ensureDevice, anchorState, rotateDevice } from '../../../offline/device.js';
import { pendingCount } from '../../../offline/queue.js';
import { syncForCurrentWorker } from '../../../core/sync-loop.js';
import type { Screen, ScreenContext } from '../../shell.js';
import { el, esc, on, fmtDateTime, fmtAgo, parseServerTs } from '../../dom.js';
import { icon } from '../../icons.js';
import { openSheet, confirmDialog } from '../../components/sheet.js';
import { isDarkTheme, onThemeChange, saveTheme } from '../../theme.js';
import { toastError, toastOk } from '../../components/toast.js';

interface DeviceInfo {
  device_id: string | null;
  key_epoch: number | null;
  registered_at: string | null;
}

interface AnchorInfo {
  anchor_id: string | null;
  fetched_at: string | null;
  age_seconds: number;
  expires_in_seconds: number;
  max_offline_hours: number;
  fresh: boolean;
}

export function createProfileScreen(onSignedOut: () => void): Screen {
  return {
    title: () => 'Profile',
    subtitle: () => store.getState().session?.user.name ?? '',
    mount(host: HTMLElement, ctx: ScreenContext): () => void {
      let device: DeviceInfo = { device_id: null, key_epoch: null, registered_at: null };
      let anchor: AnchorInfo | null = null;
      let queueDepth = 0;
      let serverUrl = '';
      let deviceState: 'ready' | 'attention' | 'unknown' = 'unknown';
      let deviceNote = 'Checking…';
      let lastSyncAt: string | null = null;
      const disposers: Array<() => void> = [];

      host.innerHTML = `<div id="profile-root"></div>`;
      const root = host.querySelector('#profile-root') as HTMLElement;

      async function loadDevice(): Promise<void> {
        const session = store.getState().session;
        if (!session) return;
        try {
          const record = await ensureDevice(session.user.id);
          device = {
            device_id: record.device_id,
            key_epoch: record.key_epoch,
            registered_at: record.registered_at,
          };
          const state = await anchorState(session.user.id);
          anchor = state.anchor
            ? {
                anchor_id: state.anchor.anchor_id,
                fetched_at: state.anchor.fetched_at,
                age_seconds: state.age_seconds,
                expires_in_seconds: state.expires_in_seconds,
                max_offline_hours: state.anchor.max_offline_hours,
                fresh: state.fresh,
              }
            : null;
          queueDepth = await pendingCount(session.user.id);

          if (!state.anchor) {
            deviceState = 'attention';
            deviceNote = 'No time anchor held. Connect once to enable offline punches.';
          } else if (!state.fresh) {
            deviceState = 'attention';
            deviceNote = 'The time anchor has expired. Connect to renew it.';
          } else {
            deviceState = 'ready';
            deviceNote = `Ready — offline punches valid for ${Math.max(0, Math.round(state.expires_in_seconds / 3600))} h more.`;
          }
        } catch (e) {
          const err = e as { code?: string; message?: string };
          deviceState = 'attention';
          deviceNote =
            err.code === 'device_key_lost'
              ? 'This device is registered but its signing key is gone. Re-register it below.'
              : (err.message ?? 'The device state could not be read.');
        }
        paint();
      }

      function renderDevicePanel(): string {
        const pct = anchor && anchor.max_offline_hours > 0
          ? Math.max(0, Math.min(100, Math.round((anchor.expires_in_seconds / (anchor.max_offline_hours * 3600)) * 100)))
          : 0;
        const fillClass = !anchor || !anchor.fresh ? 'device-anchor__fill--expired' : pct < 25 ? 'device-anchor__fill--low' : '';
        return `
          <div class="hand-card">
            <div class="hand-section-head">
              <h2 class="hand-section-title">This phone</h2>
              <span class="ui-badge ${
                deviceState === 'ready' ? 'is-live' : deviceState === 'attention' ? 'is-warn' : ''
              }">
                ${deviceState === 'ready' ? 'Ready' : deviceState === 'attention' ? 'Attention' : 'Checking'}
              </span>
            </div>
            <p class="hand-section-note">${esc(deviceNote)}</p>

            ${
              anchor
                ? `<div class="device-anchor">
                     <div class="device-anchor__bar">
                       <div class="device-anchor__fill ${fillClass}" style="width:${pct}%"></div>
                     </div>
                     <div class="device-anchor__meta">
                       <span>Anchor issued ${esc(fmtAgo(parseServerTs(anchor.fetched_at)))}</span>
                       <span>${anchor.fresh ? `${Math.max(0, Math.round(anchor.expires_in_seconds / 3600))} h left` : 'expired'}</span>
                     </div>
                   </div>`
                : ''
            }

            <dl class="hand-dl">
              <div class="hand-dl-row">
                <dt>Device</dt>
                <dd class="is-mono">${esc(device.device_id ?? 'Not registered')}</dd>
              </div>
              ${
                device.key_epoch !== null
                  ? `<div class="hand-dl-row">
                       <dt>Signing key</dt>
                       <dd class="is-mono">#${esc(String(device.key_epoch))}</dd>
                     </div>`
                  : ''
              }
              <div class="hand-dl-row">
                <dt>Punches waiting to sync</dt>
                <dd class="is-mono">${esc(String(queueDepth))}</dd>
              </div>
            </dl>

            <div class="ui-row" style="margin-top:12px">
              <button class="ui-btn ui-btn-quiet is-grow" type="button" data-action="sync">
                ${icon('refresh', 16)} Sync now
              </button>
              <button class="ui-btn ui-btn-quiet is-grow" type="button" data-action="rotate">
                ${icon('key', 16)} Re-register
              </button>
            </div>
          </div>
        `;
      }

      function renderAccount(): string {
        const session = store.getState().session;
        if (!session) return '';
        const user = session.user;
        return `
          <div class="hand-card">
            <div class="hand-section-head">
              <h2 class="hand-section-title">Signed in as</h2>
              <span class="ui-badge">${esc(user.role.replace(/_/g, ' '))}</span>
            </div>
            <dl class="hand-dl">
              <div class="hand-dl-row">
                <dt>Name</dt>
                <dd>${esc(user.name)}</dd>
              </div>
              <div class="hand-dl-row">
                <dt>Worker ID</dt>
                <dd class="is-mono">${esc(user.id)}</dd>
              </div>
              ${
                user.email ? `<div class="hand-dl-row"><dt>Email</dt><dd>${esc(user.email)}</dd></div>` : ''
              }
              ${
                user.phone ? `<div class="hand-dl-row"><dt>Phone</dt><dd>${esc(user.phone)}</dd></div>` : ''
              }
            </dl>
          </div>
        `;
      }

      function renderRows(): string {
        const session = store.getState().session;
        const expires = session?.expiresAt ? fmtDateTime(session.expiresAt.slice(0, 19).replace('T', ' ')) : '—';
        return `
          <div class="hand-card">
            <div class="hand-section-head">
              <h2 class="hand-section-title">This session</h2>
            </div>
            <dl class="hand-dl">
              <div class="hand-dl-row">
                <dt>Expires</dt>
                <dd>${esc(expires)}</dd>
              </div>
              <div class="hand-dl-row">
                <dt>Server</dt>
                <dd class="is-mono">${esc(serverUrl)}</dd>
              </div>
              <div class="hand-dl-row">
                <dt>Last sync</dt>
                <dd>${esc(lastSyncAt ? fmtAgo(parseServerTs(lastSyncAt)) : 'never')}</dd>
              </div>
            </dl>
          </div>
        `;
      }

      function renderActions(): string {
        // ``aria-label``: a button whose content is ``<dt>``/``<dd>`` has no accessible
        // name in Chrome -- the term markup defeats name-from-content, so without the
        // label a screen reader announces this row as just "button".
        //
        // The "Server address" row that used to sit under this one is gone with the
        // gear: the API base is fixed at build time (``core/config.ts``), so there is
        // nothing for a worker to set here.
        return `
          <div class="hand-card">
            <dl class="hand-dl">
              <button class="hand-dl-row is-action" type="button" data-action="password"
                      aria-label="Change password">
                <dt aria-hidden="true">${icon('key', 16)} Change password</dt>
                <dd aria-hidden="true">${icon('chevron', 16)}</dd>
              </button>
            </dl>
          </div>
        `;
      }

      /**
       * The screen's own switch.
       *
       * The web's worker profile has no switch either -- its one on/off setting is a
       * labelled button -- but the handset has one real boolean a worker can change, and a
       * phone is where a switch is expected. The state is carried by ``aria-checked`` as
       * well as by the colour, and the whole row is the target.
       */
      function renderSwitches(): string {
        return `
          <div class="hand-card">
            <div class="hand-section-head">
              <h2 class="hand-section-title">Screen</h2>
            </div>
            <button class="hand-switch" type="button" role="switch"
                    aria-checked="${isDarkTheme() ? 'true' : 'false'}" data-action="theme">
              <span class="hand-switch__text">
                <span class="hand-switch__label">Dark screen</span>
                <span class="hand-switch__hint">Easier at night, harder in direct sunlight.</span>
              </span>
              <span class="hand-switch__track" aria-hidden="true"><span class="hand-switch__thumb"></span></span>
            </button>
          </div>
        `;
      }

      function renderSignOut(): string {
        return `
          <button class="ui-btn ui-btn-danger is-block" type="button" data-action="signout">
            ${icon('logout', 16)} Sign out
          </button>
          <p class="hand-faint" style="text-align:center;font-size:12px">
            ${
              queueDepth > 0
                ? `${queueDepth} punch${queueDepth === 1 ? '' : 'es'} still waiting on this phone will stay here until they sync.`
                : ''
            }
          </p>
          <p class="hand-faint" style="text-align:center;font-size:12px">
            Attendance mobile · offline signing v1
          </p>
        `;
      }

      function paint(): void {
        root.innerHTML = `
          <div class="hand-screen">
            ${renderAccount()}
            ${renderDevicePanel()}
            ${renderSwitches()}
            ${renderRows()}
            ${renderActions()}
            ${renderSignOut()}
          </div>
        `;
      }

      async function changePassword(): Promise<void> {
        openSheet({
          title: 'Change password',
          body: `
            <div class="hand-field-group">
              <label class="hand-field-label" for="pw-current">Current password</label>
              <input class="ui-field" id="pw-current" type="password" autocomplete="current-password" />
            </div>
            <div class="hand-field-group">
              <label class="hand-field-label" for="pw-new">New password</label>
              <input class="ui-field" id="pw-new" type="password" autocomplete="new-password" />
              <span class="ui-hint">At least 8 characters.</span>
            </div>
            <div class="hand-field-group">
              <label class="hand-field-label" for="pw-confirm">Repeat new password</label>
              <input class="ui-field" id="pw-confirm" type="password" autocomplete="new-password" />
              <span class="ui-field-error" data-error hidden></span>
            </div>
            <div class="hand-alert is-info">
              ${icon('info', 16)}
              <p>Changing your password signs out every device, including this one.</p>
            </div>
          `,
          actions: [
            {
              label: 'Change password',
              kind: 'primary',
              onClick: (sheet) => {
                void submitPassword(sheet);
              },
            },
            { label: 'Cancel', kind: 'ghost', onClick: (sheet) => sheet.close() },
          ],
        });

        async function submitPassword(sheet: ReturnType<typeof openSheet>): Promise<void> {
          const current = (sheet.root.querySelector('#pw-current') as HTMLInputElement).value;
          const next = (sheet.root.querySelector('#pw-new') as HTMLInputElement).value;
          const confirm = (sheet.root.querySelector('#pw-confirm') as HTMLInputElement).value;
          const error = sheet.root.querySelector('[data-error]') as HTMLElement;

          const fail = (message: string): void => {
            error.textContent = message;
            error.hidden = false;
          };
          error.hidden = true;

          if (!current) return fail('Enter your current password.');
          if (next.length < 8) return fail('The new password must be at least 8 characters.');
          if (next !== confirm) return fail('The two new passwords do not match.');

          sheet.setBusy(0, true);
          try {
            await request<{ message?: string }>({
              method: 'POST',
              path: '/auth/change_password',
              body: { current_password: current, new_password: next },
            });
            sheet.close();
            toastOk('Password updated. Please sign in again.');
            // token_version was bumped, so every token — this one included — is dead.
            await clearPersistedSession();
            store.setSession(null);
            onSignedOut();
          } catch (e) {
            const err = e as { status?: number; message?: string };
            sheet.setBusy(0, false);
            fail(err.status === 401 ? 'That current password is not right.' : (err.message ?? 'The password could not be changed.'));
          }
        }
      }

      async function rotate(): Promise<void> {
        const confirmed = await confirmDialog({
          title: 'Re-register this phone?',
          message:
            'This gives the phone a new signing key. Anything still queued on it must sync first, or it will lose its signature.',
          confirmLabel: 'Re-register',
          danger: true,
        });
        if (!confirmed) return;
        const session = store.getState().session;
        if (!session) return;
        try {
          await rotateDevice(session.user.id);
          toastOk('This phone was re-registered.');
          await loadDevice();
        } catch (e) {
          const err = e as { code?: string; message?: string };
          toastError(
            err.code === 'queue_not_empty'
              ? (err.message ?? 'Sync the waiting punches first.')
              : (err.message ?? 'The device could not be re-registered.'),
          );
        }
      }

      async function signOut(): Promise<void> {
        const confirmed = await confirmDialog({
          title: 'Sign out?',
          message:
            queueDepth > 0
              ? `${queueDepth} punch${queueDepth === 1 ? '' : 'es'} have not synced. They stay on this phone, but they need a sign-in to upload.`
              : 'You will need your ID and password to sign in again.',
          confirmLabel: 'Sign out',
          danger: true,
        });
        if (!confirmed) return;
        await clearPersistedSession();
        store.setSession(null);
        toastOk('Signed out.');
        onSignedOut();
      }

      disposers.push(
        on(root, 'click', (event) => {
          const target = (event.target as HTMLElement).closest('[data-action]') as HTMLElement | null;
          if (!target) return;
          const action = target.getAttribute('data-action');
          if (action === 'sync') {
            void (async () => {
              toastOk('Syncing…');
              const summary = await syncForCurrentWorker();
              if (summary && summary.error) {
                toastError(summary.message ?? 'Sync did not finish.');
              } else {
                toastOk('Everything is synced.');
              }
              await loadDevice();
            })();
          } else if (action === 'rotate') void rotate();
          else if (action === 'theme') void saveTheme(!isDarkTheme());
          else if (action === 'password') void changePassword();
          else if (action === 'signout') void signOut();
        }),
      );

      // The header's toggle and this switch are the same switch, so whichever one is
      // used, the other follows: the theme store tells every listener, and the state is
      // read from the document rather than kept in a variable that could drift.
      disposers.push(onThemeChange(() => paint()));

      void (async () => {
        try {
          serverUrl = await resolveApiBaseUrl();
        } catch {
          serverUrl = 'not configured';
        }
        lastSyncAt = store.getState().lastSyncAt;
        await loadDevice();
      })();

      ctx.main.scrollTop = 0;
      return () => disposers.forEach((off) => off());
    },
  };
}

