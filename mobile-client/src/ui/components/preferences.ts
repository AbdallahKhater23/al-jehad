/**
 * The header's two preference controls: the theme toggle and the language picker.
 *
 * They sit where the server gear used to, and they are the same two controls the
 * web console puts on its own top bar -- a language select and a moon/sun button --
 * so the handset and the desk are one product rather than two.
 *
 * Both are **one instance for the whole app**, handed to whichever header is being
 * built (the shell's, and the sign-in frame's). A header is rebuilt on every route
 * change and on every language change, and a control that subscribed to the theme
 * and locale stores each time it was built would leave a listener behind on every
 * one of those rebuilds. There is only ever one header on screen, so one cached
 * pair of nodes is enough: rebuilding appends the same two elements, and the two
 * module-lifetime subscriptions below keep them true.
 */

import { LOCALES, getLocale, setLocale, onLocaleChange, type LocaleCode } from '../../core/locale.js';
import { t } from '../../core/strings.js';
import { getTheme, isDarkTheme, onThemeChange, saveTheme } from '../theme.js';
import { el, on } from '../dom.js';
import { icon } from '../icons.js';

/**
 * The theme button: the glyph shows what the button *does* (a moon while the screen
 * is light), and the accessible name says the same thing in words.
 */
function buildThemeButton(): HTMLButtonElement {
  const button = el(
    '<button class="icon-button" type="button" data-pref="theme"></button>',
  ) as HTMLButtonElement;
  on(button, 'click', () => {
    void saveTheme(!isDarkTheme());
  });
  paintThemeButton(button);
  return button;
}

function paintThemeButton(button: HTMLButtonElement): void {
  const dark = getTheme() === 'dark';
  button.innerHTML = icon(dark ? 'sun' : 'moon', 20);
  // ``aria-pressed`` carries the state (dark on/off) as well as the colours do, so
  // the one fact this control has is not the one fact a screen reader cannot say.
  button.setAttribute('aria-pressed', dark ? 'true' : 'false');
  button.setAttribute('aria-label', t(dark ? 'theme.switchToLight' : 'theme.switchToDark'));
  button.title = t('theme.label');
}

/**
 * The language picker: a native ``select``, because on a handset the native picker
 * is a list a thumb can scroll in the worker's own script, and it needs no custom
 * menu, no focus trap and no new key handling.
 *
 * Each option is named in its own language -- ``العربية``, not "Arabic" -- which is
 * how someone who cannot read the current language finds their way back.
 */
function buildLanguageSelect(): HTMLElement {
  const wrapper = el('<div class="hand-lang"></div>');
  const select = document.createElement('select');
  select.className = 'hand-lang__select';
  select.setAttribute('data-pref', 'lang');
  for (const locale of LOCALES) {
    const option = document.createElement('option');
    option.value = locale.code;
    option.textContent = locale.name;
    option.lang = locale.code;
    select.appendChild(option);
  }
  on(select, 'change', () => {
    setLocale(select.value);
  });
  wrapper.appendChild(select);
  paintLanguageSelect(select);
  return wrapper;
}

function paintLanguageSelect(select: HTMLSelectElement | HTMLElement): void {
  const node = select as HTMLSelectElement;
  node.value = getLocale();
  node.setAttribute('aria-label', t('language.label'));
  node.title = t('language.label');
}

let cached: HTMLElement[] | null = null;

/** The header's preference controls, in the order the web's top bar shows them. */
export function headerPreferences(): HTMLElement[] {
  if (!cached) {
    const language = buildLanguageSelect();
    const theme = buildThemeButton();
    const select = language.querySelector('select') as HTMLSelectElement;
    const sync = (): void => {
      paintLanguageSelect(select);
      paintThemeButton(theme);
    };
    // Both stores are module-lifetime: see the note at the top of this file.
    onThemeChange(sync);
    onLocaleChange(sync);
    cached = [language, theme];
  }
  return cached;
}

export const __test__ = { LOCALE_CODES: LOCALES.map((locale) => locale.code) as LocaleCode[] };
