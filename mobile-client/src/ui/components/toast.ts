/**
 * Toasts — the app's one non-blocking message channel.
 *
 * Deliberately non-blocking: a punch flow must never be interrupted by a modal that the
 * worker has to dismiss before they can carry on. A failure that needs a decision gets a
 * sheet; a failure that is merely *information* gets a toast that disappears on its own.
 */

import { esc } from '../dom.js';

export type ToastKind = 'info' | 'ok' | 'warn' | 'error';

export interface ToastOptions {
  kind?: ToastKind;
  /** Milliseconds on screen. Errors default to longer, because they are worth reading. */
  durationMs?: number;
  /** An optional single action, e.g. "Retry". */
  action?: { label: string; onClick: () => void };
}

const DEFAULT_DURATION: Record<ToastKind, number> = {
  info: 3200,
  ok: 3200,
  warn: 5200,
  error: 6500,
};

let host: HTMLElement | null = null;

function ensureHost(): HTMLElement {
  if (host && host.isConnected) return host;
  host = document.createElement('div');
  host.className = 'hand-toasts';
  host.setAttribute('role', 'status');
  host.setAttribute('aria-live', 'polite');
  document.body.appendChild(host);
  return host;
}

/** Show a toast. Returns a function that dismisses it early. */
export function toast(message: string, opts: ToastOptions = {}): () => void {
  const kind = opts.kind ?? 'info';
  const root = ensureHost();
  const node = document.createElement('div');
  node.className = `hand-toast hand-toast--${kind}`;
  node.innerHTML =
    `<span class="hand-toast__text">${esc(message)}</span>` +
    (opts.action
      ? `<button class="ui-btn ui-btn-sm" type="button">${esc(opts.action.label)}</button>`
      : '');
  root.appendChild(node);

  let removed = false;
  const remove = (): void => {
    if (removed) return;
    removed = true;
    node.classList.add('hand-toast--out');
    window.setTimeout(() => node.remove(), 220);
  };

  if (opts.action) {
    node.querySelector('button')?.addEventListener('click', (event) => {
      event.stopPropagation();
      opts.action?.onClick();
      remove();
    });
  }

  // A toast carrying an action stays until it is answered: dismissing text somebody was
  // asked to act on is how the action gets missed. Tapping the toast itself still dismisses
  // it, so it can never trap the screen.
  if (!opts.action) {
    const duration = opts.durationMs ?? DEFAULT_DURATION[kind];
    if (duration > 0) window.setTimeout(remove, duration);
  } else {
    node.addEventListener('click', remove);
  }
  return remove;
}

export const toastOk = (message: string, opts?: ToastOptions): (() => void) =>
  toast(message, { ...opts, kind: 'ok' });

export const toastError = (message: string, opts?: ToastOptions): (() => void) =>
  toast(message, { ...opts, kind: 'error' });

export const toastWarn = (message: string, opts?: ToastOptions): (() => void) =>
  toast(message, { ...opts, kind: 'warn' });

/** Clear every toast on screen — used when a session ends. */
export function clearToasts(): void {
  host?.replaceChildren();
}

// The live region is created once, here, before any toast exists: a region inserted in the
// same task as its first message is not announced by every screen reader. Module scripts
// run after the document is parsed, so the body is there; the check is for a unit-style
// import that is not.
if (typeof document !== 'undefined' && document.body) ensureHost();
