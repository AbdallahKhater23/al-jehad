/**
 * Finding the server on the local network.
 *
 * A packaged APK has no same-origin: the API host is baked in at build time, and on a
 * self-hosted LAN the one thing that changes is the machine's address. This module is the
 * search half of the escape hatch -- it answers "where is it?" instead of asking the worker
 * to know an IP that changed this morning.
 *
 * The search is two-phase: the addresses the app already knows (saved override, built-in
 * default) are probed first and answer in milliseconds; then the /24 the worker points at is
 * swept one ``GET /api/v1/branding`` per address. A 200 carrying the branding shape is the
 * only answer counted as found -- a router's admin page on port 8000 is somebody else's
 * server, and connecting to it would be worse than finding nothing.
 *
 * Every base in and out of this module is an *API* base: it ends in ``/api/v1``, the same
 * shape the config layer stores.
 */

/** The path every route is mounted under. */
const API_SUFFIX = '/api/v1';

export interface FoundServer {
  /** The base URL, including ``/api/v1``. */
  base: string;
  /** The company name the server reports, when it has one configured. */
  name: string | null;
}

const IPV4 = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/;

/** Whether ``192.168.8`` is a network a phone could actually be on. */
export function isLocalNetwork(prefix: string): boolean {
  const octets = prefix.split('.').map(Number);
  if (octets.length !== 3 || octets.some((o) => !Number.isInteger(o) || o < 0 || o > 255)) {
    return false;
  }
  const [a, b] = octets as [number, number, number];
  if (a === 10 || a === 127) return true;
  if (a === 192 && b === 168) return true;
  if (a === 172 && b >= 16 && b <= 31) return true;
  return a === 169 && b === 254;
}

/** ``192.168.8.23`` -> ``192.168.8``; ``null`` for a name or a malformed address. */
export function networkPrefixOf(host: string): string | null {
  const match = IPV4.exec(String(host).trim());
  if (!match) return null;
  const prefix = match.slice(1, 4).join('.');
  return isLocalNetwork(prefix) ? prefix : null;
}

/**
 * What the worker typed into the network field.
 *
 * Accepts ``192.168.8``, ``192.168.8.``, ``192.168.8.0`` and ``192.168.8.0/24``, because
 * all four are how a person writes "the office network". Only a /24 is searched.
 */
export function parseNetworkHint(raw: string): string | null {
  const text = String(raw)
    .trim()
    .replace(/\/\d{1,2}$/, '')
    .replace(/\.+$/, '');
  if (!text) return null;
  const parts = text.split('.');
  if (parts.length === 3) return isLocalNetwork(text) ? text : null;
  if (parts.length === 4) {
    if (!parts.every((p) => /^\d{1,3}$/.test(p) && Number(p) <= 255)) return null;
    const prefix = parts.slice(0, 3).join('.');
    return isLocalNetwork(prefix) ? prefix : null;
  }
  return null;
}

/** A host a LAN server plausibly lives at: a private IP, ``localhost`` or ``*.local``. */
export function looksLocalHost(host: string): boolean {
  const h = String(host).trim().toLowerCase();
  if (!h) return false;
  if (h === 'localhost' || h.endsWith('.local') || h.endsWith('.localhost')) return true;
  const match = IPV4.exec(h);
  return match ? isLocalNetwork(match.slice(1, 4).join('.')) : false;
}

/** Resolve ``null`` after ``ms`` even if the underlying request is still hanging. */
async function withTimeout<T>(promise: Promise<T | null>, ms: number): Promise<T | null> {
  let timer: ReturnType<typeof setTimeout> | null = null;
  try {
    return await Promise.race([
      promise,
      new Promise<null>((resolve) => {
        timer = setTimeout(() => resolve(null), ms);
      }),
    ]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

/**
 * Ask one address whether it is this app's server.
 *
 * ``/branding`` is public and answers with the company's lockup, which is a shape nothing
 * else on a LAN serves. Its ``name`` is returned so the sheet can say *which* server was
 * found, which is also how the worker confirms they paired with the right one.
 *
 * ``base`` must be an API base (``http://host:port/api/v1``), the shape discovery and the
 * config layer both speak.
 */
export async function probeBranding(
  base: string,
  opts: { timeoutMs?: number; signal?: AbortSignal } = {},
): Promise<FoundServer | null> {
  const timeoutMs = opts.timeoutMs ?? 1200;
  // Accept a bare origin and finish it here: a probe that asks the wrong path 404s, and a
  // 404 looks exactly like "no server there" -- the silent way to search a whole network
  // and report nothing.
  const clean = base.replace(/\/+$/, '');
  const apiBase = clean.endsWith(API_SUFFIX) ? clean : `${clean}${API_SUFFIX}`;
  const ask = (async (): Promise<FoundServer | null> => {
    const res = await fetch(`${apiBase}/branding`, {
      method: 'GET',
      headers: { Accept: 'application/json' },
      cache: 'no-store',
      ...(opts.signal ? { signal: opts.signal } : {}),
    });
    if (!res.ok) return null;
    const body = (await res.json().catch(() => null)) as Record<string, unknown> | null;
    if (!body) return null;
    const hasShape = 'logo_url' in body || ('name' in body && 'updated_at' in body);
    if (!hasShape) return null;
    const name = typeof body['name'] === 'string' && body['name'].trim() ? body['name'].trim() : null;
    return { base: apiBase, name };
  })().catch(() => null);
  return withTimeout(ask, timeoutMs);
}

export interface DiscoverOptions {
  /** First three octets, e.g. ``192.168.8``. */
  prefix: string;
  port: number;
  scheme?: 'http' | 'https';
  /** Absolute bases to try before the sweep, most likely first. */
  known?: string[];
  /** Parallel probes. 24 keeps a /24 under ten seconds without hammering the Wi-Fi. */
  concurrency?: number;
  timeoutMs?: number;
  signal?: AbortSignal;
  onProgress?: (done: number, total: number) => void;
}

/**
 * Sweep ``prefix.1``–``prefix.254`` on ``port`` and return the first server that answers.
 *
 * Known addresses are probed first because the common case is "the address changed but the
 * laptop is still on this network" — the remembered address usually answers, and the sweep
 * never starts. When it does run, the first verified server stops it: a second Freebuff
 * server on one LAN is not a thing worth waiting for.
 */
export async function discoverServers(opts: DiscoverOptions): Promise<FoundServer[]> {
  const scheme = opts.scheme ?? 'http';
  const concurrency = Math.max(1, Math.min(opts.concurrency ?? 24, 48));
  const timeoutMs = opts.timeoutMs ?? 1200;

  const candidates: string[] = [];
  const seen = new Set<string>();
  const push = (base: string): void => {
    const clean = base.replace(/\/+$/, '');
    if (clean && !seen.has(clean)) {
      seen.add(clean);
      candidates.push(clean);
    }
  };
  for (const base of opts.known ?? []) push(base);
  // The API root is part of every candidate: the server mounts under ``/api/v1``, and a
  // bare origin answers 404 on ``/branding`` -- a sweep that asks the wrong path finds
  // nothing while looking like it searched.
  for (let i = 1; i <= 254; i++) {
    push(`${scheme}://${opts.prefix}.${i}:${opts.port}${API_SUFFIX}`);
  }

  // One controller aborts the whole sweep: on a hit, on the sheet closing, on anything.
  const controller = new AbortController();
  const abortAll = (): void => controller.abort();
  opts.signal?.addEventListener('abort', abortAll, { once: true });

  const found: FoundServer[] = [];
  let next = 0;
  let done = 0;
  let stopped = false;

  const worker = async (): Promise<void> => {
    while (!stopped && !opts.signal?.aborted) {
      const index = next++;
      if (index >= candidates.length) return;
      const result = await probeBranding(candidates[index]!, {
        timeoutMs,
        signal: controller.signal,
      });
      done++;
      opts.onProgress?.(done, candidates.length);
      if (result) {
        found.push(result);
        stopped = true;
        abortAll();
        return;
      }
    }
  };

  try {
    await Promise.all(Array.from({ length: concurrency }, () => worker()));
  } finally {
    opts.signal?.removeEventListener('abort', abortAll);
  }
  return found;
}
