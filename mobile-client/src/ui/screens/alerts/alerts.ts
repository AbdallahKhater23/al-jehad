/**
 * Alerts — the worker's inbox.
 *
 * This is the *record* half of the notification channel: the app has no service worker and
 * the server cannot deliver a web push to a native WebView, so an event that happened while
 * the phone was off lives here and is what the Alerts badge counts. That is also why the
 * screen polls while it is open and refreshes on foreground.
 */

import { request } from '../../../core/http.js';
import type { Screen, ScreenContext } from '../../shell.js';
import { setBadges } from '../../shell.js';
import { el, esc, on } from '../../dom.js';
import { icon } from '../../icons.js';
import { toastError, toastOk } from '../../components/toast.js';

interface NotificationRow {
  id: number;
  kind: string;
  title: string;
  body: string;
  created_at: string;
  read: boolean;
  delivered: boolean;
}

interface NotificationsResponse {
  unread: number;
  notifications: NotificationRow[];
}

/**
 * What kind of notice this is, in words.
 *
 * A chip rather than a coloured tile, and words rather than a glyph: a crossing
 * wants a different reaction from a day the system closed for the worker, and
 * that difference has to survive a reader who cannot see the colour.
 */
function kindLabel(kind: string): string {
  const words = String(kind || '').replace(/[_-]+/g, ' ').trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : '';
}

export function createAlertsScreen(): Screen {
  // The header's "mark all read" button needs the screen's own state (the unread count and
  // the loader), so the factory is captured here and filled in by ``mount``. Building it at
  // module scope would need a global to reach the screen it belongs to.
  let markAllFromHeader: () => void = () => {};

  return {
    title: () => 'Alerts',
    subtitle: () => 'Messages from the office',
    actions: () => [
      (() => {
        const node = el(
          `<button class="ui-btn ui-btn-quiet is-icon" type="button" aria-label="Mark all read">${icon('check', 22)}</button>`,
        ) as HTMLButtonElement;
        on(node, 'click', () => markAllFromHeader());
        return node;
      })(),
    ],
    mount(host: HTMLElement, ctx: ScreenContext): () => void {
      let items: NotificationRow[] = [];
      let unread = 0;
      let loading = true;
      let error: string | null = null;
      let showUnreadOnly = false;
      const disposers: Array<() => void> = [];
      let poll: number | null = null;

      host.innerHTML = `<div id="alerts-root"></div>`;
      const root = host.querySelector('#alerts-root') as HTMLElement;

      async function markAll(): Promise<void> {
        if (unread === 0) return;
        try {
          const body = await request<{ marked: number }>({
            method: 'POST',
            path: '/worker/me/notifications/read',
            body: {},
          });
          toastOk(`${body.marked} alert${body.marked === 1 ? '' : 's'} marked as read.`);
          await load();
        } catch (e) {
          toastError((e as Error).message ?? 'Could not mark them read.');
        }
      }

      async function markOne(id: number): Promise<void> {
        try {
          await request<unknown>({
            method: 'POST',
            path: '/worker/me/notifications/read',
            body: { notification_id: id },
          });
          // Optimistic: the row is greyed out immediately and the count is corrected by the
          // refetch, so a slow network does not make the tap feel ignored.
          items = items.map((n) => (n.id === id ? { ...n, read: true } : n));
          unread = Math.max(0, unread - 1);
          setBadges({ alerts: unread });
          paint();
          await load();
        } catch (e) {
          toastError((e as Error).message ?? 'Could not mark it read.');
        }
      }

      /**
       * One notice, as the web draws it: the title, the kind, the server's own sentence and
       * the moment it was sent.
       *
       * Unread is carried three ways -- the card is a button, it leads with a "New" badge,
       * and it ends with the action that clears it. A read one is not a control at all,
       * because reading a notice is the only action it has and a card that looks tappable
       * and does nothing is worse than a card that plainly does not.
       */
      function renderItem(row: NotificationRow): string {
        const unread = !row.read;
        const kind = kindLabel(row.kind);
        const inner = `
          <div class="ui-spread">
            <p class="hand-note-subject">${esc(row.title)}</p>
            ${unread ? '<span class="ui-badge is-danger">New</span>' : ''}
          </div>
          ${kind ? `<p class="hand-note-tags"><span class="ui-badge is-quiet">${esc(kind)}</span></p>` : ''}
          <p class="hand-note-preview">${esc(row.body)}</p>
          <p class="hand-note-stamp">Sent: ${esc(row.created_at)}${
            row.delivered ? '' : ' · Inbox only'
          }</p>`;
        return unread
          ? `
            <button type="button" data-read="${row.id}" class="hand-card is-note is-unread">
              ${inner}
              <span class="hand-alert-more">Mark as read</span>
            </button>`
          : `<div class="hand-card">${inner}</div>`;
      }

      function paint(): void {
        const visible = showUnreadOnly ? items.filter((n) => !n.read) : items;
        const body = loading
          ? `<div class="hand-card">
               <div class="hand-skeleton hand-skeleton--line" style="width:50%"></div>
               <div class="hand-skeleton hand-skeleton--block"></div>
               <div class="hand-skeleton hand-skeleton--line" style="width:70%"></div>
             </div>`
          : error
            ? `<div class="hand-alert is-danger">
                 ${icon('wifiOff', 16)}
                 <p><strong>Could not load your alerts</strong> ${esc(error)}</p>
               </div>
               <button class="ui-btn ui-btn-quiet is-block" type="button" data-reload>Try again</button>`
            : visible.length === 0
              ? `<div class="ui-empty">
                   <span class="ui-empty-icon">${icon('alerts', 22)}</span>
                   <p class="ui-empty-title">${showUnreadOnly ? 'Nothing unread' : 'No alerts yet'}</p>
                   <p class="ui-empty-body">${showUnreadOnly ? 'You are up to date.' : 'Approvals, review decisions and overtime notices appear here.'}</p>
                 </div>`
              : `<div class="hand-stack">${visible.map(renderItem).join('')}</div>`;

        root.innerHTML = `
          <div class="hand-screen">
            <div class="ops-chips" role="group" aria-label="Filter">
              <button class="ops-chip" type="button" data-filter="all" aria-pressed="${!showUnreadOnly}">
                All ${items.length ? `(${items.length})` : ''}
              </button>
              <button class="ops-chip" type="button" data-filter="unread" aria-pressed="${showUnreadOnly}">
                Unread ${unread ? `(${unread})` : ''}
              </button>
            </div>
            ${body}
          </div>
        `;
      }

      async function load(): Promise<void> {
        try {
          const body = await request<NotificationsResponse>({
            path: '/worker/me/notifications',
            query: { limit: 100 },
          });
          items = body.notifications ?? [];
          unread = Number(body.unread ?? 0);
          error = null;
          setBadges({ alerts: unread });
        } catch (e) {
          const err = e as { offline?: boolean; message?: string };
          error = err.offline ? 'No connection to the server.' : (err.message ?? 'The request failed.');
        }
        loading = false;
        paint();
      }

      disposers.push(
        on(root, 'click', (event) => {
          const readButton = (event.target as HTMLElement).closest('[data-read]') as HTMLElement | null;
          if (readButton) {
            void markOne(Number(readButton.getAttribute('data-read')));
            return;
          }
          const filter = (event.target as HTMLElement).closest('[data-filter]') as HTMLElement | null;
          if (filter) {
            showUnreadOnly = filter.getAttribute('data-filter') === 'unread';
            paint();
            return;
          }
          if ((event.target as HTMLElement).closest('[data-reload]')) {
            loading = true;
            paint();
            void load();
          }
        }),
      );

      // A foreground poll, because there is no push channel into a native WebView: this is
      // what makes the badge move without the worker reopening the app.
      poll = window.setInterval(() => {
        if (document.visibilityState === 'visible') void load();
      }, 60_000);
      disposers.push(() => {
        if (poll !== null) window.clearInterval(poll);
      });

      const onVisible = (): void => {
        if (document.visibilityState === 'visible') void load();
      };
      document.addEventListener('visibilitychange', onVisible);
      disposers.push(() => document.removeEventListener('visibilitychange', onVisible));

      markAllFromHeader = () => {
        void markAll();
      };

      void load();
      ctx.main.scrollTop = 0;
      return () => disposers.forEach((off) => off());
    },
  };
}
