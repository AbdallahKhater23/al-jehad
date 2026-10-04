/**
 * Bottom sheets and the dialogs built on them.
 *
 * A sheet is the mobile replacement for a desktop modal: it rises from the bottom edge
 * where the thumb already is, it can be dismissed by the scrim or the Android back button,
 * and it never covers the whole screen so the worker keeps their place in the app behind it.
 *
 * Only one sheet is open at a time. Two stacked sheets on a phone means the second one is
 * hiding the first one's buttons, which is how a flow becomes a dead end.
 */

import { el, esc, on } from '../dom.js';
import { icon } from '../icons.js';
import { trapFocus } from './focus.js';

export interface SheetOptions {
  title: string;
  /** The body: markup, a node, or a builder given the sheet's own root. */
  body: string | HTMLElement | ((root: HTMLElement) => void);
  /** Footer buttons. The first is rendered as the primary action. */
  actions?: Array<{
    label: string;
    kind?: 'primary' | 'danger' | 'ghost' | 'plain';
    onClick: (sheet: SheetHandle) => void | Promise<void>;
    /** Start in the loading state. */
    busy?: boolean;
  }>;
  /** Hide the grip and the close button for a sheet that must be answered. */
  dismissible?: boolean;
  /**
   * Which control takes focus on open. ``last`` is for a destructive question, where the
   * safe answer (Cancel) should be the one an accidental Enter lands on.
   */
  initialFocus?: 'first' | 'last';
  onClose?: () => void;
}

export interface SheetHandle {
  root: HTMLElement;
  /** Whether Escape, the scrim and the close button may dismiss it. */
  dismissible: boolean;
  close: () => void;
  /** Toggle a footer button's loading state, addressed by its index. */
  setBusy: (index: number, busy: boolean) => void;
  setDisabled: (index: number, disabled: boolean) => void;
}

let current: SheetHandle | null = null;

/** Close the open sheet, if it may be dismissed. A sheet that must be answered is not. */
export function closeSheet(): void {
  if (current?.dismissible) current.close();
}

export function sheetIsOpen(): boolean {
  return current !== null;
}

let sheetSeq = 0;

export function openSheet(opts: SheetOptions): SheetHandle {
  // One at a time: the previous sheet is closed rather than stacked.
  if (current) current.close();

  const dismissible = opts.dismissible !== false;
  // A dialog has to be named: the title span is the name, referenced by id, so a screen
  // reader reads "Confirm clock-out, dialog" instead of "dialog".
  const titleId = `hand-sheet-title-${++sheetSeq}`;
  const root = el(`
    <div class="hand-sheet" role="dialog" aria-modal="true" aria-labelledby="${titleId}">
      <div class="hand-sheet__scrim"></div>
      <div class="hand-sheet__panel">
        <div class="hand-sheet__grip"></div>
        <div class="hand-sheet__head">
          <span class="hand-sheet__title" id="${titleId}">${esc(opts.title)}</span>
          ${dismissible ? `<button class="ui-btn ui-btn-quiet is-icon" type="button" data-close aria-label="Close">${icon('close', 22)}</button>` : ''}
        </div>
        <div class="hand-sheet__body"></div>
        ${opts.actions?.length ? '<div class="hand-sheet__foot"></div>' : ''}
      </div>
    </div>
  `);

  const bodyHost = root.querySelector('.hand-sheet__body') as HTMLElement;
  if (typeof opts.body === 'string') bodyHost.innerHTML = opts.body;
  else if (opts.body instanceof HTMLElement) bodyHost.appendChild(opts.body);
  else opts.body(bodyHost);

  const buttons: HTMLButtonElement[] = [];
  const foot = root.querySelector('.hand-sheet__foot') as HTMLElement | null;
  if (foot && opts.actions) {
    for (const [index, action] of opts.actions.entries()) {
      const kind = action.kind ?? (index === 0 ? 'primary' : 'plain');
      const cls =
        kind === 'primary'
          ? 'ui-btn ui-btn-primary'
          : kind === 'danger'
            ? 'ui-btn ui-btn-danger'
            : kind === 'ghost'
              ? 'ui-btn ui-btn-quiet'
              : 'ui-btn';
      const button = el(`<button class="${cls}" type="button"><span class="ui-btn__label">${esc(action.label)}</span></button>`) as HTMLButtonElement;
      if (action.busy) {
        button.innerHTML = `<span class="hand-spinner"></span><span class="ui-btn__label">${esc(action.label)}</span>`;
      }
      button.addEventListener('click', () => {
        void action.onClick(handle);
      });
      buttons.push(button);
      foot.appendChild(button);
    }
  }

  document.body.appendChild(root);
  // Focus goes in, stays in, and comes back to the opener -- see focus.ts.
  const trap = trapFocus(root, { initial: opts.initialFocus ?? 'first' });
  // Two frames so the transition runs: a sheet appended with its final class already set
  // is never animated, it just appears.
  requestAnimationFrame(() => requestAnimationFrame(() => root.classList.add('hand-sheet--in')));

  let closed = false;
  const close = (): void => {
    if (closed) return;
    closed = true;
    root.classList.remove('hand-sheet--in');
    if (current === handle) current = null;
    trap.release();
    disposers.forEach((off) => off());
    window.setTimeout(() => root.remove(), 220);
    opts.onClose?.();
  };

  const handle: SheetHandle = {
    root,
    dismissible,
    close,
    setBusy(index, busy) {
      const button = buttons[index];
      if (!button) return;
      const label = button.querySelector('.ui-btn__label')?.textContent ?? '';
      button.innerHTML = busy
        ? `<span class="hand-spinner"></span><span class="ui-btn__label">${esc(label)}</span>`
        : `<span class="ui-btn__label">${esc(label)}</span>`;
      button.toggleAttribute('disabled', busy);
    },
    setDisabled(index, disabled) {
      buttons[index]?.toggleAttribute('disabled', disabled);
    },
  };

  const disposers: Array<() => void> = [];
  if (dismissible) {
    disposers.push(
      on(root.querySelector('.hand-sheet__scrim') as HTMLElement, 'click', close),
      on(root.querySelector('[data-close]') as HTMLElement, 'click', close),
    );
  }

  // The Android back button closes the sheet before it navigates or exits — otherwise back
  // would leave the app with a dialog still on screen.
  disposers.push(
    on(document, 'keydown', (event) => {
      if ((event as KeyboardEvent).key === 'Escape' && dismissible) close();
    }),
  );

  current = handle;
  return handle;
}

// ---------------------------------------------------------------------------
// confirm
// ---------------------------------------------------------------------------
export interface ConfirmOptions {
  title: string;
  message: string;
  /** Extra markup under the message: the figures a decision is made on. */
  detail?: string;
  confirmLabel?: string;
  cancelLabel?: string;
  danger?: boolean;
}

/** Ask a yes/no question. Resolves ``true`` only for an explicit confirmation. */
export function confirmDialog(opts: ConfirmOptions): Promise<boolean> {
  return new Promise((resolve) => {
    let answered = false;
    const handle = openSheet({
      title: opts.title,
      body: `
        <p class="hand-dim">${esc(opts.message)}</p>
        ${opts.detail ? rawBlock(opts.detail) : ''}
      `,
      dismissible: true,
      // A destructive question opens on Cancel: Enter should never clock someone out by
      // reflex. A plain confirmation opens on its first control as usual.
      initialFocus: opts.danger ? 'last' : 'first',
      actions: [
        {
          label: opts.confirmLabel ?? 'Confirm',
          kind: opts.danger ? 'danger' : 'primary',
          onClick: (sheet) => {
            answered = true;
            sheet.close();
            resolve(true);
          },
        },
        {
          label: opts.cancelLabel ?? 'Cancel',
          kind: 'ghost',
          onClick: (sheet) => {
            sheet.close();
          },
        },
      ],
      onClose: () => {
        if (!answered) resolve(false);
      },
    });
    void handle;
  });
}

/** The caller's own markup, passed through deliberately (never user text). */
function rawBlock(markup: string): string {
  return `<div class="hand-stack">${markup}</div>`;
}

/**
 * The early-checkout question, built from the server's own numbers.
 *
 * The API answers a short shift with ``confirm_early_checkout`` plus the figures; the app
 * renders the sentence itself rather than showing the server's English, which is the whole
 * reason those numbers travel.
 */
export function confirmEarlyCheckout(detail: {
  paid_hours?: number;
  regular_hours?: number;
  elapsed_hours?: number;
  break_hours?: number;
}): Promise<boolean> {
  const paid = Number(detail.paid_hours ?? 0).toFixed(2);
  const regular = Number(detail.regular_hours ?? 0).toFixed(2);
  const elapsed = Number(detail.elapsed_hours ?? 0).toFixed(2);
  return confirmDialog({
    title: 'Clock out early?',
    message:
      `You have worked ${paid} h of the ${regular} h paid day. Clocking out now records ` +
      `${paid} h, not the full day.`,
    detail: `
      <div class="hand-card">
        <div class="hand-spread"><span class="hand-dim">On site</span><span class="hand-mono">${esc(elapsed)} h</span></div>
        <div class="hand-spread"><span class="hand-dim">Paid so far</span><span class="hand-mono">${esc(paid)} h</span></div>
        <div class="hand-spread"><span class="hand-dim">Paid day</span><span class="hand-mono">${esc(regular)} h</span></div>
      </div>
    `,
    confirmLabel: 'Clock out anyway',
    cancelLabel: 'Stay clocked in',
    danger: true,
  });
}
