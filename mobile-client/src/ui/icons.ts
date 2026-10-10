/**
 * Inline SVG icons.
 *
 * Inline rather than an icon font or a sprite sheet: the app ships as a local bundle inside
 * an APK, so a network request for a glyph is a request that can fail on a site with no
 * signal — and the tab bar is exactly what a worker needs to see when that happens.
 *
 * Every icon is drawn on a 24x24 grid with a 1.8 stroke so they stay legible at the 22-24px
 * the tab bar and the buttons render them at. The markup carries the class ``hand-icon``
 * rather than a size of its own: every glyph is drawn inside something that sizes it --
 * ``.hand-tabs button svg``, ``.ui-btn svg``, ``.hand-alert svg`` -- which is where the
 * size belongs, because the same glyph is 16px in a button and 22px in the bar.
 */

const PATHS: Record<string, string> = {
  clock:
    '<circle cx="12" cy="12" r="9"/><path d="M12 7.5v5l3.2 2"/>',
  history:
    '<path d="M3.5 12a8.5 8.5 0 1 0 2.6-6.1"/><path d="M3.2 4.6v4h4"/><path d="M12 8v4.4l3 1.9"/>',
  alerts:
    '<path d="M6.5 10a5.5 5.5 0 0 1 11 0c0 4 1.5 5.5 1.5 5.5H5S6.5 14 6.5 10Z"/><path d="M10 18.5a2.2 2.2 0 0 0 4 0"/>',
  notes:
    '<path d="M5 4.5h9.5L19 9v10.5H5z"/><path d="M14 4.5V9h5"/><path d="M8.5 13h7M8.5 16.5h4.5"/>',
  profile:
    '<circle cx="12" cy="8.5" r="3.6"/><path d="M4.8 20c0-3.7 3.2-6.2 7.2-6.2s7.2 2.5 7.2 6.2"/>',
  camera:
    '<path d="M3.5 8.5h3l1.5-2.2h8L17.5 8.5h3v10.5h-17z"/><circle cx="12" cy="13.5" r="3.4"/>',
  // The theme toggle's two glyphs, drawn with the console's own moon (its
  // ``ADMIN_ICONS.theme``) so the desk's dark-mode button and the handset's are the
  // same picture. The gear the header used to carry is gone with the server sheet.
  moon:
    '<path d="M20 14.5A8.5 8.5 0 0 1 9.5 4a8.5 8.5 0 1 0 10.5 10.5Z"/>',
  sun:
    '<circle cx="12" cy="12" r="4"/><path d="M12 3.2v2.2M12 18.6v2.2M3.2 12h2.2M18.6 12h2.2M5.8 5.8l1.6 1.6M16.6 16.6l1.6 1.6M5.8 18.2l1.6-1.6M16.6 7.4l1.6-1.6"/>',
  refresh:
    '<path d="M20 11.5a8 8 0 1 0-2.4 6.2"/><path d="M20.3 4.5v4.2h-4.2"/>',
  logout:
    '<path d="M14 4.5H6.5v15H14"/><path d="M11.5 12h9M17.5 8.5 21 12l-3.5 3.5"/>',
  check:
    '<path d="M4.5 12.5 9.5 17.5 19.5 6.5"/>',
  close:
    '<path d="M6 6l12 12M18 6 6 18"/>',
  chevron:
    '<path d="M9 5.5 15.5 12 9 18.5"/>',
  pin:
    '<path d="M12 21s6.5-5.4 6.5-10.2A6.5 6.5 0 0 0 5.5 10.8C5.5 15.6 12 21 12 21Z"/><circle cx="12" cy="10.6" r="2.4"/>',
  key:
    '<circle cx="8" cy="12" r="3.5"/><path d="M11.5 12h9M17.5 12v3.2M20.5 12v2.2"/>',
  shield:
    '<path d="M12 3.5 19 6v6c0 4.2-3 7.3-7 8.5-4-1.2-7-4.3-7-8.5V6z"/><path d="M9 12.2l2.2 2.2L15.5 10"/>',
  wifiOff:
    '<path d="M3.5 8.5A13 13 0 0 1 12 5.2M20.5 8.5a13 13 0 0 0-4.6-2.6"/><path d="M7 12.4a9 9 0 0 1 3.2-1.4M17 12.4a9 9 0 0 0-2.2-1.1"/><path d="M10 15.9a4.5 4.5 0 0 1 4 0"/><path d="M12 19.2h.01"/><path d="M3 3l18 18"/>',
  cloudUp:
    '<path d="M7 18.5a4.2 4.2 0 0 1-.3-8.4A5.5 5.5 0 0 1 17.2 9a3.8 3.8 0 0 1 .6 7.5"/><path d="M12 21v-8M9 16l3-3 3 3"/>',
  alert:
    '<path d="M12 4.5 21 20H3z"/><path d="M12 10v4.2M12 17h.01"/>',
  info:
    '<circle cx="12" cy="12" r="9"/><path d="M12 11v5.5M12 7.8h.01"/>',
  route:
    '<circle cx="6.5" cy="6.5" r="2.5"/><circle cx="17.5" cy="17.5" r="2.5"/><path d="M6.5 9v4.5a4 4 0 0 0 4 4h4.5"/>',
  user:
    '<circle cx="12" cy="8.5" r="3.6"/><path d="M4.8 20c0-3.7 3.2-6.2 7.2-6.2s7.2 2.5 7.2 6.2"/>',
  filter:
    '<path d="M4 6h16M7 12h10M10 18h4"/>',
};

export type IconName = keyof typeof PATHS;

export function icon(name: IconName, size = 24, className = 'hand-icon'): string {
  const body = PATHS[name] ?? '';
  return (
    `<svg class="${className}" width="${size}" height="${size}" viewBox="0 0 24 24" ` +
    `fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" ` +
    `stroke-linejoin="round" aria-hidden="true" focusable="false">${body}</svg>`
  );
}

/** The same glyph as a standalone SVG element, for DOM-built controls. */
export function iconNode(name: IconName, size = 24, className = 'hand-icon'): SVGElement {
  const wrapper = document.createElement('div');
  wrapper.innerHTML = icon(name, size, className);
  return wrapper.firstElementChild as SVGElement;
}
