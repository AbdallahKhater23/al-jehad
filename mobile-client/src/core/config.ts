import { Preferences } from '@capacitor/preferences';

const API_BASE_OVERRIDE_KEY = 'api_base_override';
const DEFAULT_DEV_BASE = 'http://10.0.2.2:8000/api/v1';

function normalizeBaseUrl(raw: string): string {
  let url = raw.trim().replace(/\/+$/, '');
  if (!url) return '';
  if (!/^https?:\/\//i.test(url)) {
    url = `https://${url}`;
  }
  if (!/\/api\/v1\/?$/i.test(url)) {
    url = `${url}/api/v1`;
  }
  return url.replace(/\/+$/, '');
}

function viteBaseUrl(): string {
  const env = (import.meta as unknown as { env?: Record<string, string> }).env;
  const raw = env?.VITE_API_BASE_URL ?? '';
  if (raw && raw.trim()) return normalizeBaseUrl(raw);
  // In dev, fall back to emulator loopback; in prod the build must set VITE_API_BASE_URL.
  if (env?.DEV) return DEFAULT_DEV_BASE;
  return '';
}

let cachedOverride: string | null | undefined;
let cachedResolved: string | null = null;

export async function getApiBaseOverride(): Promise<string | null> {
  if (cachedOverride !== undefined) return cachedOverride;
  try {
    const { value } = await Preferences.get({ key: API_BASE_OVERRIDE_KEY });
    cachedOverride = value ? normalizeBaseUrl(value) : null;
    return cachedOverride;
  } catch {
    cachedOverride = null;
    return null;
  }
}

export async function setApiBaseOverride(url: string | null): Promise<void> {
  const normalized = url ? normalizeBaseUrl(url) : null;
  cachedOverride = normalized;
  cachedResolved = null;
  if (normalized) {
    await Preferences.set({ key: API_BASE_OVERRIDE_KEY, value: normalized });
  } else {
    await Preferences.remove({ key: API_BASE_OVERRIDE_KEY });
  }
}

export async function resolveApiBaseUrl(): Promise<string> {
  if (cachedResolved) return cachedResolved;
  const override = await getApiBaseOverride();
  if (override) {
    cachedResolved = override;
    return override;
  }
  const vite = viteBaseUrl();
  if (vite) {
    cachedResolved = vite;
    return vite;
  }
  // Last resort: same-origin (works when served via server.url in Capacitor, or in browser dev).
  if (typeof window !== 'undefined' && window.location?.origin) {
    const origin = window.location.origin.replace(/\/+$/, '');
    // Capacitor WebView origin is https://localhost — not useful as API host.
    if (origin && origin !== 'https://localhost' && origin !== 'capacitor://localhost') {
      cachedResolved = `${origin}/api/v1`;
      return cachedResolved;
    }
  }
  throw new Error(
    'API base URL is not configured. Set VITE_API_BASE_URL at build time or call setApiBaseOverride().',
  );
}

export function invalidateApiBaseCache(): void {
  cachedOverride = undefined;
  cachedResolved = null;
}

export const __test__ = { normalizeBaseUrl, viteBaseUrl, API_BASE_OVERRIDE_KEY };
