/**
 * The screen's language, as a document attribute and a stored preference.
 *
 * Four languages, the same four the backend's own tables carry (``ar``, ``en``,
 * ``hi``, ``ur``). The choice is the worker's and it survives a restart: it is kept
 * in ``localStorage`` under ``app_lang`` -- synchronously readable, which is what
 * lets ``index.html`` set ``dir``/``lang`` before the first frame -- and mirrored
 * into Capacitor Preferences, which is the copy a WebView whose site data the OS
 * clears will still find.
 *
 * Two of the four read right to left, so the language is not only a table lookup:
 * it is ``document.documentElement.dir``. The list below is the one place in the
 * app that says which, and ``index.html`` repeats the two codes once, in the boot
 * script that has to run before this module is even parsed.
 *
 * Nothing here knows about a screen. Changing the language applies the document
 * attributes and tells its subscribers; the shell and the sign-in frame repaint
 * themselves from ``t()`` in ``core/strings.ts``.
 */

import { Preferences } from '@capacitor/preferences';

export type LocaleCode = 'ar' | 'en' | 'hi' | 'ur';

export interface LocaleOption {
  code: LocaleCode;
  /** The language's own name, in its own script: what the picker shows. */
  name: string;
  dir: 'ltr' | 'rtl';
}

/** The four languages, in the order the picker lists them. */
export const LOCALES: LocaleOption[] = [
  { code: 'ar', name: 'العربية', dir: 'rtl' },
  { code: 'en', name: 'English', dir: 'ltr' },
  { code: 'hi', name: 'हिन्दी', dir: 'ltr' },
  { code: 'ur', name: 'اردو', dir: 'rtl' },
];

export const DEFAULT_LOCALE: LocaleCode = 'en';

/** The ``localStorage`` key, named once here and repeated in ``index.html``. */
export const LOCALE_STORAGE_KEY = 'app_lang';

/** The Preferences key: the durable copy of the same choice. */
const LOCALE_PREFERENCE_KEY = 'app_lang';

let current: LocaleCode = DEFAULT_LOCALE;

const listeners = new Set<() => void>();

/** Whether ``value`` is one of the four. Anything else is not a language here. */
export function isLocale(value: unknown): value is LocaleCode {
  return LOCALES.some((locale) => locale.code === value);
}

export function localeDirection(code: LocaleCode): 'ltr' | 'rtl' {
  return LOCALES.find((locale) => locale.code === code)?.dir ?? 'ltr';
}

export function localeName(code: LocaleCode): string {
  return LOCALES.find((locale) => locale.code === code)?.name ?? code;
}

/** The language the screen is drawn in right now. Synchronous by design. */
export function getLocale(): LocaleCode {
  return current;
}

/**
 * The value every request carries in ``Accept-Language``.
 *
 * The bare code, not a ranked list: the app draws exactly one language, so a
 * server that localizes a date or a refusal has one right answer to use.
 */
export function acceptLanguage(): string {
  return current;
}

function readStored(): LocaleCode | null {
  try {
    const raw = localStorage.getItem(LOCALE_STORAGE_KEY);
    return isLocale(raw) ? raw : null;
  } catch {
    // Storage disabled (a private mode, a hardened WebView) is not an error here.
    return null;
  }
}

function writeStored(code: LocaleCode): void {
  try {
    localStorage.setItem(LOCALE_STORAGE_KEY, code);
  } catch {
    // See above: the in-memory choice is still applied for this session.
  }
}

/** The phone's own language, when it is one of ours. First run only. */
function systemLocale(): LocaleCode | null {
  const tag = String(
    (typeof navigator !== 'undefined' && (navigator.language || navigator.languages?.[0])) || '',
  ).slice(0, 2).toLowerCase();
  return isLocale(tag) ? tag : null;
}

/** Apply a language to the document, without storing it. */
export function applyLocale(code: LocaleCode): void {
  current = code;
  if (typeof document === 'undefined') return;
  const root = document.documentElement;
  root.lang = code;
  root.dir = localeDirection(code);
}

function remember(code: LocaleCode): void {
  writeStored(code);
  void Preferences.set({ key: LOCALE_PREFERENCE_KEY, value: code }).catch(() => {});
}

/**
 * The stored choice, applied. Called once at boot -- before the first screen is
 * mounted, so nothing paints in the wrong direction.
 */
export async function loadLocale(): Promise<LocaleCode> {
  const fromStorage = readStored();
  const fromPreferences = fromStorage
    ? null
    : await Preferences.get({ key: LOCALE_PREFERENCE_KEY })
        .then(({ value }) => (isLocale(value) ? value : null))
        .catch(() => null);
  const stored = fromStorage ?? fromPreferences;
  const code = stored ?? systemLocale() ?? DEFAULT_LOCALE;
  applyLocale(code);
  // A choice that was already made is written back where both copies can see it. A
  // phone-derived default is not: like the theme, it stays a default until the worker
  // picks a side, so a phone that changes language is followed on the next launch.
  if (stored) remember(code);
  return code;
}

/**
 * Choose a language: applied to the document, stored, and announced.
 *
 * Subscribers repaint; nothing reloads. An unknown code is ignored rather than
 * silently becoming English.
 */
export function setLocale(code: string): LocaleCode {
  const next = isLocale(code) ? code : current;
  if (next === current && typeof document !== 'undefined' && document.documentElement.lang === next) {
    return current;
  }
  applyLocale(next);
  remember(next);
  for (const listener of [...listeners]) listener();
  return next;
}

/** Called when the language changes. One header, one sign-in frame: no leak. */
export function onLocaleChange(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export const __test__ = { LOCALE_STORAGE_KEY, LOCALE_PREFERENCE_KEY, localeDirection, systemLocale };
