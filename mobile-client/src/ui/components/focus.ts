/**
 * Keyboard focus for the app's overlays.
 *
 * A sheet or the capture overlay is a modal in everything but the browser's eyes: the page
 * behind it is still in the tab order, and without this a keyboard user can tab straight
 * out of a dialog into controls they cannot see. This helper does the three things a modal
 * owes the keyboard -- move focus in when it opens, keep Tab inside while it is open, and
 * put focus back on the control that opened it when it closes.
 */

import { on } from '../dom.js';

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), ' +
  'textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

export interface FocusTrapHandle {
  /** Remove the trap and return focus to whatever was focused when it opened. */
  release: () => void;
}

/**
 * Trap Tab inside ``root``.
 *
 * ``initial: 'last'`` is for a destructive confirmation: the safe answer should be the one
 * focus starts on, so Enter on a phone with a keyboard cannot clock the worker out.
 */
export function trapFocus(
  root: HTMLElement,
  opts: { initial?: 'first' | 'last' } = {},
): FocusTrapHandle {
  const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;

  const focusable = (): HTMLElement[] =>
    (Array.from(root.querySelectorAll(FOCUSABLE)) as HTMLElement[]).filter(
      (node) => node.getClientRects().length > 0,
    );

  // The overlay is appended in the same frame it is built, so the first frame is not laid
  // out yet; focusing in the next one is what actually takes on Android.
  const raf = requestAnimationFrame(() => {
    const list = focusable();
    if (list.length) (opts.initial === 'last' ? list[list.length - 1]! : list[0]!).focus();
    else {
      root.tabIndex = -1;
      root.focus();
    }
  });

  const stop = on(document, 'keydown', (event) => {
    if ((event as KeyboardEvent).key !== 'Tab') return;
    const list = focusable();
    if (!list.length) {
      event.preventDefault();
      return;
    }
    const first = list[0]!;
    const last = list[list.length - 1]!;
    const active = document.activeElement;
    if ((event as KeyboardEvent).shiftKey) {
      if (active === first || !root.contains(active)) {
        event.preventDefault();
        last.focus();
      }
    } else if (active === last || !root.contains(active)) {
      event.preventDefault();
      first.focus();
    }
  });

  let released = false;
  return {
    release: () => {
      if (released) return;
      released = true;
      cancelAnimationFrame(raf);
      stop();
      // The opener can be gone by now -- a sign-out row removes itself -- so focus only
      // moves to something still on screen.
      if (previous?.isConnected) previous.focus();
    },
  };
}
