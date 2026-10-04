/**
 * Action buttons with a loading state that does not move the layout.
 *
 * The state machine is the point: ``idle -> busy -> idle`` with a scroll-lock so a
 * successful tap does not activate whatever is at the worker's thumb position once the
 * screen redraws under it. The lock is short and mechanical, and it is what stops the
 * oldest mobile bug in the book — the double submit.
 */

import { el, esc, on } from '../dom.js';

export interface ActionButtonOptions {
  label: string;
  /** A second line under the label, for a button that is also a summary. */
  sub?: string;
  variant?: 'primary' | 'danger' | 'transit' | 'plain' | 'ghost';
  block?: boolean;
  disabled?: boolean;
  onClick: (button: ActionButton) => void | Promise<void>;
}

/** How long a button refuses a second tap after a successful one. */
const RELEASE_MS = 350;

export class ActionButton {
  readonly root: HTMLButtonElement;
  private busy = false;
  private locked = false;
  private label: string;
  private sub: string | undefined;

  constructor(opts: ActionButtonOptions) {
    this.label = opts.label;
    this.sub = opts.sub;
    const variant =
      opts.variant === 'primary'
        ? 'ui-btn-primary'
        : opts.variant === 'danger'
          ? 'ui-btn-danger'
          : opts.variant === 'ghost'
            ? 'ui-btn-quiet'
            : '';
    this.root = el(
      `<button class="ui-btn ${variant} ${opts.block ? 'is-block' : ''}" type="button"></button>`,
    ) as HTMLButtonElement;
    this.paint();
    if (opts.disabled) this.root.setAttribute('disabled', '');

    on(this.root, 'click', () => {
      if (this.busy || this.locked) return;
      const result = opts.onClick(this);
      if (result instanceof Promise) {
        // A handler that manages its own busy state is left alone; one that returns a
        // promise without setting it gets the automatic treatment.
        if (!this.busy) {
          this.setBusy(true);
          result.finally(() => this.setBusy(false));
        }
      }
    });
  }

  private paint(): void {
    this.root.innerHTML =
      (this.busy ? '<span class="hand-spinner"></span>' : '') +
      `<span class="ui-btn__label">${esc(this.label)}</span>` +
      (this.sub ? `<span class="ui-btn__sub">${esc(this.sub)}</span>` : '');
  }

  setLabel(label: string, sub?: string): void {
    this.label = label;
    this.sub = sub;
    this.paint();
  }

  setBusy(busy: boolean): void {
    if (this.busy === busy) return;
    this.busy = busy;
    this.root.toggleAttribute('aria-busy', busy);
    this.paint();
    if (!busy) {
      this.locked = true;
      window.setTimeout(() => {
        this.locked = false;
      }, RELEASE_MS);
    }
  }

  setDisabled(disabled: boolean): void {
    this.root.toggleAttribute('disabled', disabled);
  }

  get isBusy(): boolean {
    return this.busy;
  }
}

/** A plain button element with the shared classes, for one-off actions. */
export function button(
  label: string,
  onClick: () => void,
  variant: 'primary' | 'danger' | 'ghost' | 'plain' = 'plain',
): HTMLButtonElement {
  const cls =
    variant === 'primary'
      ? 'ui-btn ui-btn-primary'
      : variant === 'danger'
        ? 'ui-btn ui-btn-danger'
        : variant === 'ghost'
          ? 'ui-btn ui-btn-quiet'
          : 'ui-btn';
  const node = el(`<button class="${cls}" type="button">${esc(label)}</button>`) as HTMLButtonElement;
  on(node, 'click', onClick);
  return node;
}
