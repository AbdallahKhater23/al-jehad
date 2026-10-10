/**
 * Where the app talks to.
 *
 * A worker never chooses a server. There is no per-device override any more: the
 * gear on the sign-in screen that used to write one is gone, because the address
 * it asked for is the address of a laptop, and a phone at a gate is not standing
 * next to that laptop. What is left is a fixed cascade, highest first:
 *
 *   1. ``VITE_API_BASE_URL``, inlined at build time -- the deployment's own value.
 *      It has to be an absolute ``https://`` public address, because that is the
 *      only shape ``src/build/api-base-policy.ts`` lets a release build ship; a
 *      deployment that serves its own page and API from one origin bakes that
 *      origin here.
 *   2. With none set, a *dev* build (``npm run dev``) uses the emulator's alias for
 *      this machine's loopback (``10.0.2.2``) -- the one place that address is ever
 *      meant to be reached. ``import.meta.env.DEV`` is false in every bundle, so
 *      the branch and its literal are dropped from both build profiles: a release
 *      bundle contains no LAN or loopback address at all.
 *   3. ``DEFAULT_API_BASE``: the public deployment, baked in. This is what makes
 *      the shipped app work with nothing configured, and it is why discovery
 *      (``core/discovery.ts``, deleted) is no longer needed.
 *
 * There is deliberately no same-origin step. The page's own origin in the shipped
 * app is the WebView (``https://localhost``), which fronts nothing, and the page's
 * origin in a browser is a Vite server, which fronts nothing either -- so the step
 * could only ever add a loopback literal to the bundle and a broken base to two
 * workflows that already have a better answer above.
 *
 * ``purgeLegacyApiBaseOverrides()`` runs at boot and throws away anything a
 * previous build left on the device -- the Capacitor Preferences key the old sheet
 * wrote, and the ``localStorage`` keys the sheet and the web console used. A stale
 * ``192.168.x.x`` from the office Wi-Fi must not outlive the build that removed
 * the control which wrote it.
 */

import { Preferences } from '@capacitor/preferences';

const API_SUFFIX = '/api/v1';

/** The public deployment, fronted by the Cloudflare Worker: the shipped default. */
export const DEFAULT_API_BASE = 'https://al-jehad1.abdallahtamet281.workers.dev/api/v1';

/** The Android emulator's alias for the developer's own machine. Dev builds only. */
const DEFAULT_DEV_BASE = 'http://10.0.2.2:8000/api/v1';

/**
 * Keys a previous build (or the web console, sharing a browser profile) may have
 * used for a per-device server address. All of them are discarded at boot.
 */
export const LEGACY_API_BASE_KEYS = [
  'api_base_override',
  'api_base_url',
  'apiBaseURL',
  'apiBaseUrl',
  'server_base_url',
];

/** The same override, where the deleted sheet stored it on the device. */
const LEGACY_API_BASE_PREFERENCE_KEY = 'api_base_override';

function normalizeBaseUrl(raw: string): string {
  let url = raw.trim().replace(/\/+$/, '');
  if (!url) return '';
  if (!/^https?:\/\//i.test(url)) {
    url = `https://${url}`;
  }
  if (!/\/api\/v1\/?$/i.test(url)) {
    url = `${url}${API_SUFFIX}`;
  }
  return url.replace(/\/+$/, '');
}

function viteBaseUrl(): string {
  const env = (import.meta as unknown as { env?: Record<string, string | boolean> }).env;
  const raw = typeof env?.VITE_API_BASE_URL === 'string' ? env.VITE_API_BASE_URL : '';
  if (raw && raw.trim()) return normalizeBaseUrl(raw);
  if (env?.DEV) return DEFAULT_DEV_BASE;
  return '';
}

/**
 * Throw away every per-device server address a previous build could have stored.
 *
 * Called once at boot, before the first request. Neither the value nor its absence
 * changes what the app connects to -- the cascade above is the only rule -- so this
 * is housekeeping, not a migration: it is what stops a stale LAN address from being
 * read back by an older build someone reinstalls later.
 */
export function purgeLegacyApiBaseOverrides(): void {
  if (typeof localStorage !== 'undefined') {
    for (const key of LEGACY_API_BASE_KEYS) {
      try {
        localStorage.removeItem(key);
      } catch {
        // Storage disabled: nothing was stored under these keys either.
      }
    }
  }
  void Preferences.remove({ key: LEGACY_API_BASE_PREFERENCE_KEY }).catch(() => {});
}

let cachedResolved: string | null = null;

/** The API base this build talks to. Resolved once, then from memory. */
export async function resolveApiBaseUrl(): Promise<string> {
  if (cachedResolved) return cachedResolved;
  cachedResolved = viteBaseUrl() || DEFAULT_API_BASE;
  return cachedResolved;
}

/** Drop the memoised base. For tests and for a build-time value that changed. */
export function invalidateApiBaseCache(): void {
  cachedResolved = null;
}

export const __test__ = {
  normalizeBaseUrl,
  viteBaseUrl,
  DEFAULT_API_BASE,
  DEFAULT_DEV_BASE,
  LEGACY_API_BASE_KEYS,
  LEGACY_API_BASE_PREFERENCE_KEY,
};
