/**
 * Notes — the written channel between a worker and the office.
 *
 * Two modes: the list of tickets, and the thread of one ticket. The compose form is a sheet
 * rather than a screen, so a worker who is halfway through writing does not lose the list
 * behind them.
 *
 * The category vocabulary is not hardcoded here: it comes back with the list response
 * (``categories``), so a deployment that adds one does not need a new APK.
 */

import { request } from '../../../core/http.js';
import type { Screen, ScreenContext } from '../../shell.js';
import { setBadges } from '../../shell.js';
import { el, esc, on, dayKey, fmtDay, fmtDateTime } from '../../dom.js';
import { icon } from '../../icons.js';
import { openSheet } from '../../components/sheet.js';
import { toastError, toastOk } from '../../components/toast.js';

interface NoteRow {
  id: number;
  category: string;
  category_label: string;
  subject: string;
  body: string;
  status: string;
  priority: string;
  created_at: string;
  last_reply_at: string | null;
  worker_unread: number;
  open: boolean;
  last_message: string | null;
}

interface NotesResponse {
  notes: NoteRow[];
  open: number;
  unread: number;
  max_open: number;
  categories: string[];
}

interface MessageRow {
  id: number;
  author_role: string;
  author_name: string | null;
  body: string;
  created_at: string;
  internal?: boolean;
}

interface ThreadResponse {
  note: NoteRow;
  messages: MessageRow[];
}

const CATEGORY_LABELS: Record<string, string> = {
  password_reset: 'Password reset',
  missing_item: 'Missing item',
  shift_hours: 'Shift hours',
  enrollment: 'Enrollment',
  working_conditions: 'Working conditions',
  other: 'Something else',
};

const STATUS_BADGE: Record<string, { label: string; cls: string }> = {
  open: { label: 'Open', cls: 'is-info' },
  in_progress: { label: 'In progress', cls: 'is-warn' },
  resolved: { label: 'Resolved', cls: 'is-live' },
  closed: { label: 'Closed', cls: '' },
};

function prettyCategory(id: string): string {
  return CATEGORY_LABELS[id] ?? id.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
}

export function createNotesScreen(): Screen {
  return {
    title: () => 'Notes',
    subtitle: () => 'Ask the office',
    actions: () => [
      (() => {
        const node = el(
          `<button class="ui-btn ui-btn-quiet is-icon" type="button" aria-label="New note">${icon('notes', 22)}</button>`,
        ) as HTMLButtonElement;
        on(node, 'click', () => {
          document.dispatchEvent(new CustomEvent('hand:new-note'));
        });
        return node;
      })(),
    ],
    mount(host: HTMLElement, ctx: ScreenContext): () => void {
      let data: NotesResponse | null = null;
      let thread: ThreadResponse | null = null;
      let loading = true;
      let error: string | null = null;
      let filter: 'all' | 'open' = 'open';
      const disposers: Array<() => void> = [];

      host.innerHTML = `<div id="notes-root"></div>`;
      const root = host.querySelector('#notes-root') as HTMLElement;

      // ---- list --------------------------------------------------------------
      function renderList(): string {
        if (loading) {
          return `<div class="hand-card">
            <div class="hand-skeleton hand-skeleton--line" style="width:45%"></div>
            <div class="hand-skeleton hand-skeleton--block"></div>
          </div>`;
        }
        if (error) {
          return `<div class="hand-alert is-danger">
              ${icon('wifiOff', 16)}
              <p><strong>Could not load your notes</strong> ${esc(error)}</p>
            </div>
            <button class="ui-btn ui-btn-quiet is-block" type="button" data-reload>Try again</button>`;
        }
        const notes = (data?.notes ?? []).filter((n) => (filter === 'open' ? n.open : true));
        if (notes.length === 0) {
          return `<div class="ui-empty">
              <span class="ui-empty-icon">${icon('notes', 22)}</span>
              <p class="ui-empty-title">${filter === 'open' ? 'No open notes' : 'No notes yet'}</p>
              <p class="ui-empty-body">Use the button in the header to ask about a password, missing hours, or anything else.</p>
            </div>`;
        }
        return `<div class="hand-stack">${notes.map(renderNoteRow).join('')}</div>`;
      }

      /**
       * One conversation, exactly as the web's ``noteCardHtml``: who spoke last, what they
       * said, the category, the state and how long ago.
       *
       * The last message is the point of a list of conversations and it says who wrote it,
       * so "the answer is here" needs no badge to be believed -- the card leads with it
       * rather than with the worker's own original words.
       */
      function renderNoteRow(note: NoteRow): string {
        const badge = STATUS_BADGE[note.status] ?? { label: note.status, cls: '' };
        const unread = Number(note.worker_unread) || 0;
        return `
          <button type="button" data-note="${note.id}"
                  class="hand-card is-note${unread > 0 ? ' is-unread' : ''}">
            <div class="ui-spread">
              <p class="hand-note-subject">${esc(note.subject)}</p>
              ${unread > 0 ? `<span class="ui-badge is-danger">${esc(String(unread))} new</span>` : ''}
            </div>
            <p class="hand-note-preview">${esc(note.last_message ?? note.body)}</p>
            <p class="hand-note-tags">
              <span class="ui-badge is-quiet">${esc(prettyCategory(note.category))}</span>
              <span class="ui-badge ${badge.cls}">${esc(badge.label)}</span>
              <time class="hand-note-stamp">${esc(fmtDateTime(note.last_reply_at ?? note.created_at))}</time>
            </p>
          </button>
        `;
      }

      // ---- thread ------------------------------------------------------------
      /**
       * One conversation, as the web lays it out: the subject over the way back to the
       * list, the thread under it, and the reply box in a card of its own at the end.
       */
      function renderThread(): string {
        if (!thread) {
          return `<div class="hand-card"><div class="hand-skeleton hand-skeleton--block"></div></div>`;
        }
        const note = thread.note;
        const badge = STATUS_BADGE[note.status] ?? { label: note.status, cls: '' };
        const hint =
          note.status === 'resolved'
            ? 'The office marked this resolved. Reopen it by replying.'
            : note.status === 'closed'
              ? 'This note is closed and cannot be replied to.'
              : '';
        return `
          <button class="ui-btn ui-btn-quiet ui-btn-sm" type="button" data-back>
            ${icon('chevron', 16)} Back to notes
          </button>

          <div class="hand-section-head" style="margin-top:12px">
            <div class="ui-stack is-tight">
              <h2 class="hand-section-title">${esc(note.subject)}</h2>
              <p class="hand-section-note">
                Opened · <time datetime="${esc(note.created_at)}">${esc(fmtDateTime(note.created_at))}</time>
              </p>
            </div>
            <span class="ui-row" style="gap:6px">
              <span class="ui-badge is-quiet">${esc(prettyCategory(note.category))}</span>
              <span class="ui-badge ${badge.cls}">${esc(badge.label)}</span>
            </span>
          </div>

          <div class="hand-card is-flat">
            <p class="hand-bubble-body">${esc(note.body)}</p>
          </div>

          ${hint ? `<p class="hand-alert">${icon('info', 16)}<span>${esc(hint)}</span></p>` : ''}

          <div class="hand-thread" id="handNotesScroll">
            ${renderMessages(thread.messages)}
          </div>

          ${
            note.open
              ? `<div class="hand-card is-flat" data-reply-composer>
                   <label class="hand-field-label" for="thread-reply">Reply</label>
                   <textarea class="ui-field" id="thread-reply" rows="3"
                             placeholder="Add to this note…" maxlength="2000"></textarea>
                   <div class="ui-row" style="margin-top:8px">
                     <button class="ui-btn ui-btn-primary" type="button" data-send="${note.id}">
                       <span class="ui-btn__label">Send reply</span>
                     </button>
                     <button class="ui-btn ui-btn-quiet" type="button" data-close-note="${note.id}">
                       Close this note
                     </button>
                   </div>
                 </div>`
              : ''
          }
        `;
      }

      /**
       * The thread, oldest first, with a day marker whenever the day changes.
       *
       * A note can run across days -- a request on Thursday, answered on Sunday -- and
       * three stamps in a row make the reader do the arithmetic.
       */
      function renderMessages(messages: MessageRow[]): string {
        let lastDay = '';
        return messages
          .filter((message) => !message.internal)
          .map((message) => {
            const key = dayKey(message.created_at);
            const marker = key !== lastDay ? `<p class="hand-day">${esc(fmtDay(key))}</p>` : '';
            lastDay = key;
            return marker + renderMessage(message);
          })
          .join('');
      }

      function renderMessage(message: MessageRow): string {
        // The API marks an administrator's internal message; a worker must never see one,
        // and the server filters them — this is the belt to that brace.
        if (message.internal) return '';
        const mine = message.author_role !== 'admin' && message.author_role !== 'head_admin';
        return `
          <div class="hand-bubble-row ${mine ? 'is-mine' : ''}" data-message="${message.id}">
            <div class="hand-bubble ${mine ? 'is-mine' : ''}">
              <p class="hand-bubble-who">${esc(mine ? 'You' : (message.author_name ?? 'Office'))} ·
                <time datetime="${esc(message.created_at)}">${esc(fmtDateTime(message.created_at))}</time>
              </p>
              <p class="hand-bubble-body">${esc(message.body)}</p>
            </div>
          </div>
        `;
      }

      function paint(): void {
        root.innerHTML = `
          <div class="hand-screen">
            ${
              thread
                ? renderThread()
                : `<div class="ops-chips" role="group" aria-label="Filter">
                     <button class="ops-chip" type="button" data-filter="open" aria-pressed="${filter === 'open'}">Open</button>
                     <button class="ops-chip" type="button" data-filter="all" aria-pressed="${filter === 'all'}">All</button>
                   </div>
                   ${renderList()}`
            }
          </div>
        `;
      }

      async function load(): Promise<void> {
        loading = true;
        paint();
        try {
          data = await request<NotesResponse>({ path: '/worker/notes', query: { limit: 100 } });
          setBadges({ notes: Number(data.unread ?? 0) });
          error = null;
        } catch (e) {
          const err = e as { offline?: boolean; message?: string };
          error = err.offline ? 'No connection to the server.' : (err.message ?? 'The request failed.');
        }
        loading = false;
        paint();
      }

      async function openThread(id: number): Promise<void> {
        thread = null;
        paint();
        try {
          thread = await request<ThreadResponse>({ path: `/worker/notes/${id}` });
          paint();
          ctx.main.scrollTop = 0;
        } catch (e) {
          toastError((e as Error).message ?? 'Could not open that note.');
          thread = null;
          paint();
        }
      }

      async function sendReply(id: number): Promise<void> {
        const field = root.querySelector('#thread-reply') as HTMLTextAreaElement | null;
        const text = field?.value.trim() ?? '';
        if (!text) {
          toastError('Write something first.');
          return;
        }
        const button = root.querySelector(`[data-send="${id}"]`) as HTMLButtonElement | null;
        if (button) {
          button.innerHTML = '<span class="hand-spinner"></span><span class="ui-btn__label">Sending…</span>';
          button.toggleAttribute('disabled', true);
        }
        try {
          await request<unknown>({
            method: 'POST',
            path: `/worker/notes/${id}/replies`,
            body: { body: text },
          });
          toastOk('Reply sent.');
          await openThread(id);
        } catch (e) {
          toastError((e as Error).message ?? 'The reply could not be sent.');
          button?.toggleAttribute('disabled', false);
        }
      }

      async function closeNote(id: number): Promise<void> {
        try {
          await request<unknown>({ method: 'POST', path: `/worker/notes/${id}/close`, body: {} });
          toastOk('Note closed.');
          thread = null;
          await load();
        } catch (e) {
          toastError((e as Error).message ?? 'The note could not be closed.');
        }
      }

      // ---- compose -----------------------------------------------------------
      function openCompose(): void {
        const categories = data?.categories ?? Object.keys(CATEGORY_LABELS);
        let chosen = categories.includes('other') ? 'other' : (categories[0] ?? 'other');
        const openCount = data?.open ?? 0;
        const maxOpen = data?.max_open ?? 20;

        openSheet({
          title: 'New note',
          body: (bodyHost) => {
            // The web's composer: a real ``<label>`` over each field rather than a
            // placeholder, because a placeholder is gone the moment somebody types and
            // this is a form filled in standing up, in the sun, with a reason to be quick.
            bodyHost.innerHTML = `
              <div class="ui-stack">
                ${
                  openCount >= maxOpen
                    ? `<div class="hand-alert">
                         ${icon('alert', 16)}
                         <p><strong>You have ${esc(openCount)} open notes</strong>
                         Wait for a reply on one of those, or close one, before opening another.</p>
                       </div>`
                    : ''
                }
                <label class="hand-field-group">
                  <span class="hand-field-label">What is it about?</span>
                  <select id="note-category" class="ui-field">
                    ${categories
                      .map(
                        (id) =>
                          `<option value="${esc(id)}"${id === chosen ? ' selected' : ''}>${esc(
                            prettyCategory(id),
                          )}</option>`,
                      )
                      .join('')}
                  </select>
                </label>
                <div class="hand-field-group">
                  <label class="hand-field-label" for="note-subject">Subject</label>
                  <input class="ui-field" id="note-subject" type="text" maxlength="120"
                         placeholder="Short summary" enterkeyhint="next" />
                  <span class="ui-field-error" data-error="subject" hidden></span>
                </div>
                <div class="hand-field-group">
                  <label class="hand-field-label" for="note-body">Message</label>
                  <textarea class="ui-field" id="note-body" maxlength="2000"
                            placeholder="Explain what you need…"></textarea>
                  <span class="note-limit" id="note-limit">0 / 2000</span>
                  <span class="ui-field-error" data-error="body" hidden></span>
                </div>
              </div>
            `;

            const category = bodyHost.querySelector('#note-category') as HTMLSelectElement;
            const message = bodyHost.querySelector('#note-body') as HTMLTextAreaElement;
            const limit = bodyHost.querySelector('#note-limit') as HTMLElement;

            category.addEventListener('change', () => {
              chosen = category.value;
            });

            message.addEventListener('input', () => {
              limit.textContent = `${message.value.length} / 2000`;
              limit.classList.toggle('note-limit--over', message.value.length > 2000);
            });
          },
          actions: [
            {
              label: 'Send note',
              kind: 'primary',
              onClick: (sheet) => {
                void submit(sheet);
              },
            },
            { label: 'Cancel', kind: 'ghost', onClick: (sheet) => sheet.close() },
          ],
        });

        async function submit(sheet: ReturnType<typeof openSheet>): Promise<void> {
          const subjectField = sheet.root.querySelector('#note-subject') as HTMLInputElement;
          const bodyField = sheet.root.querySelector('#note-body') as HTMLTextAreaElement;
          const categoryField = sheet.root.querySelector('#note-category') as HTMLSelectElement | null;
          const subject = subjectField.value.trim();
          const message = bodyField.value.trim();
          // The select is the source of truth once the sheet is open; ``chosen`` is only
          // the initial selection it was built with.
          const category = categoryField?.value || chosen;

          let invalid = false;
          const showError = (key: string, text: string): void => {
            const node = sheet.root.querySelector(`[data-error="${key}"]`) as HTMLElement | null;
            if (node) {
              node.textContent = text;
              node.hidden = false;
            }
            invalid = true;
          };
          if (!subject) showError('subject', 'Give the note a subject.');
          if (!message) showError('body', 'Write the message.');
          if (invalid) return;

          sheet.setBusy(0, true);
          try {
            await request<unknown>({
              method: 'POST',
              path: '/worker/notes',
              body: { category, subject, body: message, priority: 'normal' },
            });
            sheet.close();
            toastOk('Note sent. The office will reply here.');
            await load();
          } catch (e) {
            const err = e as { status?: number; message?: string };
            sheet.setBusy(0, false);
            if (err.status === 429) {
              showError('body', err.message ?? 'You have too many open notes.');
            } else {
              toastError(err.message ?? 'The note could not be sent.');
            }
          }
        }
      }

      // ---- wiring ------------------------------------------------------------
      disposers.push(
        on(root, 'click', (event) => {
          const target = event.target as HTMLElement;
          const noteButton = target.closest('[data-note]') as HTMLElement | null;
          if (noteButton) {
            void openThread(Number(noteButton.getAttribute('data-note')));
            return;
          }
          const sendButton = target.closest('[data-send]') as HTMLElement | null;
          if (sendButton) {
            void sendReply(Number(sendButton.getAttribute('data-send')));
            return;
          }
          const closeButton = target.closest('[data-close-note]') as HTMLElement | null;
          if (closeButton) {
            void closeNote(Number(closeButton.getAttribute('data-close-note')));
            return;
          }
          if (target.closest('[data-back]')) {
            thread = null;
            void load();
            return;
          }
          const filterButton = target.closest('[data-filter]') as HTMLElement | null;
          if (filterButton) {
            filter = filterButton.getAttribute('data-filter') === 'all' ? 'all' : 'open';
            paint();
            return;
          }
          if (target.closest('[data-reload]')) void load();
        }),
      );

      const onNew = (): void => openCompose();
      document.addEventListener('hand:new-note', onNew);
      disposers.push(() => document.removeEventListener('hand:new-note', onNew));

      void load();
      ctx.main.scrollTop = 0;
      return () => disposers.forEach((off) => off());
    },
  };
}
