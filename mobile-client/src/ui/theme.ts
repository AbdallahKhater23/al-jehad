/**
 * The screen's own theme, as a token swap.
 *
 * ``tokens.css`` ships the light palette and the dark one behind ``html.dark`` /
 * ``data-theme="dark"``. This module is the only thing that sets those, so the
 * choice is the worker's and it survives a restart: the value is kept in
 * ``localStorage`` under ``app_theme`` -- synchronously readable, which is what
 * lets ``index.html`` paint the right theme on the first frame instead of flashing
 * white -- and mirrored into Capacitor Preferences, the copy the OS cannot clear
 * out from under a WebView.
 *
 * With no stored choice the *device's* preference decides
 * (``prefers-color-scheme``): a worker whose phone is dark at night gets a dark
 * screen without being asked. Nothing is written for that decision -- it is a
 * default, not a preference -- so a phone that flips to light at sunrise is
 * followed on the next launch, until the worker picks a side with the header
 * toggle (or Profile's switch, which is the same switch).
 *
 * The choice the previous build kept in Preferences under ``ui_theme_dark`` (a
 * boolean string) is read once and carried over, so this is not a reset for
 * anyone who had already set it.
 */

import { Preferences } from '@capacitor/preferences';

export type Theme = 'light' | 'dark';

/** The ``localStorage`` key, named once here and repeated in ``index.html``. */
export const THEME_STORAGE_KEY = 'app_theme';

/** The Preferences key: the durable copy of the same choice. */
const THEME_PREFERENCE_KEY = 'app_theme';

/** The key the previous build used, read once and then dropped. */
const LEGACY_THEME_PREFERENCE_KEY = 'ui_theme_dark';

const listeners = new Set<() => void>();

function systemPrefersDark(): boolean {
  return (
    typeof window !== 'undefined' &&
    typeof window.matchMedia === 'function' &&
    window.matchMedia('(prefers-color-scheme: dark)').matches
  );
}

function readStored(): Theme | null {
  try {
    const raw = localStorage.getItem(THEME_STORAGE_KEY);
    return raw === 'dark' || raw === 'light' ? raw : null;
  } catch {
    // Storage disabled (a private mode, a hardened WebView): the default stands.
    return null;
  }
}

/** Apply a theme to the document, without storing it. */
export function applyTheme(theme: Theme): void {
  if (typeof document === 'undefined') return;
  const root = document.documentElement;
  // Both spellings, exactly as ``tokens.css`` accepts them: the class the web app
  // toggles and the attribute a page can set before any script runs.
  root.classList.toggle('dark', theme === 'dark');
  root.setAttribute('data-theme', theme);
}

/** The theme the document is wearing right now. */
export function getTheme(): Theme {
  if (typeof document === 'undefined') return 'light';
  return document.documentElement.classList.contains('dark') ? 'dark' : 'light';
}

/** Whether the dark block is currently applied. */
export function isDarkTheme(): boolean {
  return getTheme() === 'dark';
}

/** Called when the theme changes, whoever changed it. */
export function onThemeChange(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function remember(theme: Theme): void {
  try {
    localStorage.setItem(THEME_STORAGE_KEY, theme);
  } catch {
    // See ``readStored``: the theme still applies for this session.
  }
  void Preferences.set({ key: THEME_PREFERENCE_KEY, value: theme }).catch(() => {});
  void Preferences.remove({ key: LEGACY_THEME_PREFERENCE_KEY }).catch(() => {});
}

/** Set a theme: applied, stored, and announced. */
export function setTheme(theme: Theme): void {
  applyTheme(theme);
  remember(theme);
  for (const listener of [...listeners]) listener();
}

/** Light to dark and back, for the header's one-button control. */
export function toggleTheme(): Theme {
  const next: Theme = isDarkTheme() ? 'light' : 'dark';
  setTheme(next);
  return next;
}

/**
 * The stored choice, applied. Called once at boot, before the shell is mounted --
 * and before that, in effect, by the inline script in ``index.html``.
 */
export async function loadTheme(): Promise<Theme> {
  let theme = readStored();
  let chosen = theme !== null;
  if (!theme) {
    const { value } = await Preferences.get({ key: THEME_PREFERENCE_KEY }).catch(() => ({
      value: null,
    }));
    if (value === 'dark' || value === 'light') {
      theme = value;
      chosen = true;
    }
  }
  if (!theme) {
    const { value } = await Preferences.get({ key: LEGACY_THEME_PREFERENCE_KEY }).catch(() => ({
      value: null,
    }));
    if (value === 'true' || value === 'false') {
      theme = value === 'true' ? 'dark' : 'light';
      chosen = true;
    }
  }
  if (!theme) theme = systemPrefersDark() ? 'dark' : 'light';
  applyTheme(theme);
  // A migrating (or already stored) choice is written back under the new key; a
  // system-derived default is not, so the phone keeps deciding until the worker does.
  if (chosen) remember(theme);
  return theme;
}

/** Remember a theme and apply it. */
export async function saveTheme(dark: boolean): Promise<void> {
  setTheme(dark ? 'dark' : 'light');
}

export const __test__ = {
  THEME_STORAGE_KEY,
  THEME_PREFERENCE_KEY,
  LEGACY_THEME_PREFERENCE_KEY,
  systemPrefersDark,
};
