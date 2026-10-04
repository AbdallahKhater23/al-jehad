/**
 * The screen's own theme, as a token swap.
 *
 * ``tokens.css`` ships the light palette and the dark one behind ``html.dark`` -- the same
 * two blocks the web app has. This module is the only thing that sets that class, so the
 * choice is the worker's and it survives a restart: the value is kept in Preferences beside
 * the session rather than in memory, because a phone that flips back to white on every
 * launch is not a setting, it is a flicker.
 *
 * Default is light, deliberately: the web console ships light, the screen a worker was
 * trained on is light, and a handset reporting a dark system theme must not silently change
 * it. Dark is something the worker asks for.
 */

import { Preferences } from '@capacitor/preferences';

const THEME_KEY = 'ui_theme_dark';

/** Apply a theme to the document, without persisting it. */
function apply(dark: boolean): void {
  if (typeof document === 'undefined') return;
  document.documentElement.classList.toggle('dark', dark);
}

/** Whether the dark block is currently applied. */
export function isDarkTheme(): boolean {
  return typeof document !== 'undefined' && document.documentElement.classList.contains('dark');
}

/** The stored choice, applied. Called once at boot, before the shell is mounted. */
export async function loadTheme(): Promise<boolean> {
  const { value } = await Preferences.get({ key: THEME_KEY }).catch(() => ({ value: null }));
  const dark = value === 'true';
  apply(dark);
  return dark;
}

/** Remember a theme and apply it. */
export async function saveTheme(dark: boolean): Promise<void> {
  apply(dark);
  await Preferences.set({ key: THEME_KEY, value: dark ? 'true' : 'false' }).catch(() => {});
}
