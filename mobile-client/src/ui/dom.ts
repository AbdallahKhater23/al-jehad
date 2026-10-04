/**
 * Small DOM helpers shared by every screen.
 *
 * The rendering model is vanilla template strings, which means every value that reaches the
 * markup has to be escaped — a worker's note subject or a server's error message is
 * untrusted text, and an APK with a script injection in it is a stolen session on a device
 * that signs punches. ``esc()`` is therefore used at every interpolation that is not a
 * literal, and ``html`` is deliberately a tagged template so the escaping is visible at the
 * call site rather than hidden in a helper that takes a raw string.
 */

const ENTITIES: Record<string, string> = {
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&#39;',
};

/** Escape text for interpolation into markup. */
export function esc(value: unknown): string {
  if (value === null || value === undefined) return '';
  return String(value).replace(/[&<>"']/g, (ch) => ENTITIES[ch] ?? ch);
}

/**
 * Tagged template that escapes every interpolation.
 *
 * ``html`<p>${userNote}</p>` `` is safe by construction; a value that is already markup has
 * to be wrapped in ``raw()``, which is a deliberate act a reviewer can grep for.
 */
export function html(strings: TemplateStringsArray, ...values: unknown[]): string {
  let out = strings[0] ?? '';
  for (let i = 0; i < values.length; i++) {
    const v = values[i];
    out += (v && typeof v === 'object' && '__raw' in (v as object)
      ? (v as { __raw: string }).__raw
      : esc(v)) + (strings[i + 1] ?? '');
  }
  return out;
}

/** Mark a string as already-safe markup. Use only for strings this app built. */
export function raw(markup: string): { __raw: string } {
  return { __raw: markup };
}

/** Build an element from markup. */
export function el(markup: string): HTMLElement {
  const wrapper = document.createElement('div');
  wrapper.innerHTML = markup.trim();
  return wrapper.firstElementChild as HTMLElement;
}

/** Build a fragment from markup (may contain several top-level nodes). */
export function frag(markup: string): DocumentFragment {
  const template = document.createElement('template');
  template.innerHTML = markup;
  return template.content;
}

/** Replace an element's children with markup. */
export function render(host: Element, markup: string): void {
  host.replaceChildren(frag(markup));
}

/** ``YYYY-MM-DD HH:MM:SS`` -> a Date, read as UTC like the server writes it. */
export function parseServerTs(value: string | null | undefined): Date | null {
  if (!value) return null;
  const m = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})/.exec(String(value));
  if (!m) return null;
  return new Date(
    Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]),
  );
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/** ``2026-10-03 14:05:00`` -> ``03 Oct, 14:05``. */
export function fmtDateTime(value: string | null | undefined): string {
  const d = parseServerTs(value);
  if (!d) return '—';
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${pad(d.getUTCDate())} ${MONTHS[d.getUTCMonth()]} ${d.getUTCFullYear()}, ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
}

/** ``2026-10-03 14:05:00`` -> ``14:05``. */
export function fmtTime(value: string | null | undefined): string {
  const d = parseServerTs(value);
  if (!d) return '—';
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
}

/**
 * ``2026-10-03`` or ``2026-10-03 14:05:00`` -> ``Sat, 03 Oct``.
 *
 * Both forms, because the two callers pass different things: the record groups its rows by
 * ``dayKey``, which is the bare date, and a day heading is exactly what this formats. Read
 * only by the full-timestamp parser, a bare date matched nothing and the heading rendered an
 * em dash -- a day label that never said which day.
 */
export function fmtDay(value: string | null | undefined): string {
  const raw = String(value ?? '');
  const d = parseServerTs(/^\d{4}-\d{2}-\d{2}$/.test(raw) ? `${raw} 00:00:00` : raw);
  if (!d) return '—';
  const days = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${days[d.getUTCDay()]}, ${pad(d.getUTCDate())} ${MONTHS[d.getUTCMonth()]}`;
}

/** The date part of a server timestamp, for grouping. */
export function dayKey(value: string | null | undefined): string {
  if (!value) return 'unknown';
  const m = /^(\d{4}-\d{2}-\d{2})/.exec(String(value));
  return m ? m[1] : 'unknown';
}

/** ``8.25`` -> ``8h 15m``. */
export function fmtHours(hours: number | null | undefined): string {
  const n = Number(hours);
  if (!Number.isFinite(n)) return '—';
  const totalMinutes = Math.round(n * 60);
  const h = Math.floor(totalMinutes / 60);
  const m = totalMinutes % 60;
  if (h === 0) return `${m}m`;
  if (m === 0) return `${h}h`;
  return `${h}h ${m}m`;
}

/** ``3925`` -> ``1:05:25``, for the live shift timer. */
export function fmtClock(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${h}:${pad(m)}:${pad(sec)}`;
}

/** ``8.25`` -> ``8.25 h``; two decimals, which is what the server reports. */
export function fmtHoursDecimal(hours: number | null | undefined): string {
  const n = Number(hours);
  return Number.isFinite(n) ? `${n.toFixed(2)} h` : '—';
}

/** A relative age, for "synced 4 minutes ago". */
export function fmtAgo(from: Date | null): string {
  if (!from) return 'never';
  const seconds = Math.max(0, Math.round((Date.now() - from.getTime()) / 1000));
  if (seconds < 45) return 'just now';
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  return `${Math.round(hours / 24)} d ago`;
}

/** Today as ``YYYY-MM-DD`` in the same UTC terms the server uses. */
export function todayIso(): string {
  return new Date().toISOString().slice(0, 10);
}

/** ``n`` days before today, as ``YYYY-MM-DD``. */
export function daysAgoIso(days: number): string {
  const d = new Date();
  d.setUTCDate(d.getUTCDate() - days);
  return d.toISOString().slice(0, 10);
}

/** The first letter of a name, for an avatar. */
export function initial(name: string | null | undefined): string {
  const trimmed = String(name ?? '').trim();
  return trimmed ? trimmed[0]!.toUpperCase() : '?';
}

/** Attach a listener and return a disposer, so screens can clean up after themselves. */
export function on<K extends keyof HTMLElementEventMap>(
  host: EventTarget,
  type: K | string,
  handler: (event: Event) => void,
  options?: AddEventListenerOptions,
): () => void {
  host.addEventListener(type, handler, options);
  return () => host.removeEventListener(type, handler, options);
}

/** A delegated click handler: ``data-action`` on the target or an ancestor. */
export function delegate(
  host: EventTarget,
  action: string,
  handler: (target: HTMLElement, event: Event) => void,
): () => void {
  return on(host, 'click', (event) => {
    const start = event.target as HTMLElement | null;
    const match = start?.closest?.(`[data-action="${action}"]`) as HTMLElement | null;
    if (match && host instanceof Node && host.contains(match)) handler(match, event);
  });
}

/** Debounce, for the location input and search fields. */
export function debounce<T extends (...args: never[]) => void>(fn: T, ms: number): T {
  let timer: ReturnType<typeof setTimeout> | null = null;
  return ((...args: never[]) => {
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  }) as T;
}
