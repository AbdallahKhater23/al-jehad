import { Preferences } from '@capacitor/preferences';
import { resolveApiBaseUrl } from './config.js';
import { acceptLanguage } from './locale.js';
import { store } from './store.js';

const TOKEN_KEY = 'auth_token';
const TOKEN_EXPIRES_KEY = 'auth_expires_at';
const USER_KEY = 'auth_user';
const TOKEN_VERSION_KEY = 'auth_token_version';

export type HttpMethod = 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';

export interface RequestOptions {
  method?: HttpMethod;
  path: string;
  query?: Record<string, string | number | boolean | null | undefined>;
  body?: unknown;
  formData?: FormData;
  headers?: Record<string, string>;
  timeoutMs?: number;
  auth?: boolean;
  retryOn401?: boolean;
  /** Internal: do not set from call sites — prevents infinite cold-start loops. */
  _coldStartRetried?: boolean;
}

export interface ApiError extends Error {
  status: number;
  code?: string;
  offline?: boolean;
  /**
   * The response's ``detail`` object, kept whole.
   *
   * The server answers a structured refusal with more than a code: the early-checkout
   * question carries ``paid_hours``/``regular_hours``/``elapsed_hours`` so the app can
   * build the sentence itself in the worker's own language, and the off-site checkout
   * refusal carries ``open_hours`` and ``can_request``. Flattening that to a string would
   * throw away exactly the numbers the UI has to show.
   */
  detail?: Record<string, unknown>;
}

function makeApiError(
  message: string,
  status: number,
  code?: string,
  offline?: boolean,
  detail?: Record<string, unknown>,
): ApiError {
  const e = new Error(message) as ApiError;
  e.status = status;
  e.code = code;
  e.offline = offline;
  e.detail = detail;
  return e;
}

function buildUrl(base: string, path: string, query?: RequestOptions['query']): string {
  const p = path.startsWith('/') ? path : `/${path}`;
  const url = new URL(`${base}${p}`);
  if (query) {
    for (const [k, v] of Object.entries(query)) {
      if (v === null || v === undefined || v === '') continue;
      url.searchParams.set(k, String(v));
    }
  }
  return url.toString();
}

async function loadToken(): Promise<string | null> {
  try {
    const { value } = await Preferences.get({ key: TOKEN_KEY });
    return value ?? null;
  } catch {
    return store.getState().session?.token ?? null;
  }
}

async function saveSession(token: string, expiresAt: string | null, userJson: string | null): Promise<void> {
  await Preferences.set({ key: TOKEN_KEY, value: token });
  if (expiresAt) await Preferences.set({ key: TOKEN_EXPIRES_KEY, value: expiresAt });
  else await Preferences.remove({ key: TOKEN_EXPIRES_KEY });
  if (userJson) await Preferences.set({ key: USER_KEY, value: userJson });
}

export async function persistSessionFromLoginResponse(body: Record<string, unknown>): Promise<void> {
  // Backend returns { access_token | token, expires_at, user: { id, name, role }, token_version? }
  const token = (body['access_token'] as string) ?? (body['token'] as string) ?? '';
  if (!token) throw new Error('Login response missing access_token');
  const expiresAt = (body['expires_at'] as string) ?? (body['expiresAt'] as string) ?? null;
  const user = body['user'] as Record<string, unknown> | undefined;
  const userJson = user ? JSON.stringify(user) : null;
  const ver = body['token_version'] ?? body['ver'] ?? 0;
  await saveSession(token, expiresAt, userJson);
  await Preferences.set({ key: TOKEN_VERSION_KEY, value: String(ver) });
  // Mirror into in-memory store.
  if (user) {
    store.setSession({
      user: {
        id: String(user['id'] ?? ''),
        name: String(user['name'] ?? ''),
        role: String(user['role'] ?? 'worker') as never,
        email: (user['email'] as string) ?? null,
        phone: (user['phone'] as string) ?? null,
      },
      token,
      expiresAt,
      tokenVersion: Number(ver) || 0,
    });
  }
}

export async function clearPersistedSession(): Promise<void> {
  await Preferences.remove({ key: TOKEN_KEY });
  await Preferences.remove({ key: TOKEN_EXPIRES_KEY });
  await Preferences.remove({ key: USER_KEY });
  await Preferences.remove({ key: TOKEN_VERSION_KEY });
  store.setSession(null);
}

let refreshPromise: Promise<string | null> | null = null;

async function refreshToken(): Promise<string | null> {
  if (refreshPromise) return refreshPromise;
  refreshPromise = (async () => {
    const token = await loadToken();
    if (!token) return null;
    const base = await resolveApiBaseUrl();
    const url = `${base}/auth/refresh`;
    const controller = new AbortController();
    const t = setTimeout(() => controller.abort(), 10_000);
    try {
      const res = await fetch(url, {
        method: 'POST',
        headers: {
          Accept: 'application/json',
          // The worker's language, on the refresh as on every other call: a refusal
          // refreshes a session the same person is about to read.
          'Accept-Language': acceptLanguage(),
          Authorization: `Bearer ${token}`,
        },
        signal: controller.signal,
      });
      if (!res.ok) {
        await clearPersistedSession();
        return null;
      }
      const body = (await res.json().catch(() => null)) as Record<string, unknown> | null;
      if (!body) return null;
      await persistSessionFromLoginResponse(body);
      const { value } = await Preferences.get({ key: TOKEN_KEY });
      return value ?? null;
    } catch {
      return null;
    } finally {
      clearTimeout(t);
    }
  })();
  try {
    return await refreshPromise;
  } finally {
    refreshPromise = null;
  }
}

function isNetworkFailure(err: unknown): boolean {
  if (err instanceof DOMException && err.name === 'AbortError') return false;
  if (err instanceof TypeError) return true;
  const msg = String((err as Error)?.message ?? err ?? '').toLowerCase();
  return msg.includes('networkerror') || msg.includes('failed to fetch') || msg.includes('load failed');
}

const DEFAULT_TIMEOUT_MS = 15_000;
const RAILWAY_COLD_START_RETRY_MS = 1_200;

function isRailwayColdStartStatus(status: number): boolean {
  return status === 502 || status === 503 || status === 504;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** Map an ApiError / network error into a user-facing string (no stack, no raw payload). */
export function friendlyErrorMessage(err: unknown): string {
  const e = err as ApiError & { offline?: boolean; status?: number; code?: string };
  if (e?.offline) return 'You appear to be offline. Check your connection and try again.';
  if (e?.code === 'timeout') return 'The server is taking too long to respond. Please try again.';
  if (e?.status === 422) return e.message || 'Some fields need attention. Please check and try again.';
  if (e?.status === 429) return 'Too many attempts. Please wait a moment and try again.';
  if (e?.status && e.status >= 500) return 'The server is temporarily unavailable. Please try again shortly.';
  if (e?.status === 401) return 'Your session has expired. Please sign in again.';
  if (e?.status === 403) return e.message || 'You do not have permission to do that.';
  return (e?.message as string) || 'Something went wrong. Please try again.';
}

async function doFetchWithTimeout(
  url: string,
  init: RequestInit,
  timeoutMs: number,
): Promise<Response> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...init, signal: controller.signal });
  } finally {
    clearTimeout(timer);
  }
}

export async function request<T = unknown>(opts: RequestOptions): Promise<T> {
  const base = await resolveApiBaseUrl();
  const url = buildUrl(base, opts.path, opts.query);
  const needsAuth = opts.auth !== false;
  let token: string | null = null;
  if (needsAuth) token = await loadToken();

  const timeoutMs = opts.timeoutMs ?? DEFAULT_TIMEOUT_MS;

  const headers: Record<string, string> = {
    Accept: 'application/json',
    // The language the screen is drawn in. Safe to send unconditionally: it is a
    // CORS-safelisted header, so it adds no preflight, and a server that does not
    // localize anything reads past it.
    'Accept-Language': acceptLanguage(),
    ...(opts.headers ?? {}),
  };
  if (token) headers['Authorization'] = `Bearer ${token}`;
  let body: BodyInit | undefined;
  if (opts.formData) {
    body = opts.formData;
    // Let fetch set the multipart boundary.
    delete headers['Content-Type'];
  } else if (opts.body !== undefined) {
    headers['Content-Type'] = headers['Content-Type'] ?? 'application/json';
    body = JSON.stringify(opts.body);
  }

  let res: Response;
  try {
    res = await doFetchWithTimeout(url, { method: opts.method ?? 'GET', headers, body }, timeoutMs);
  } catch (err) {
    if (isNetworkFailure(err)) {
      throw makeApiError('Network unavailable', 0, 'offline', true);
    }
    if (err instanceof DOMException && err.name === 'AbortError') {
      throw makeApiError('Request timed out', 0, 'timeout');
    }
    throw makeApiError(String((err as Error).message ?? 'Network error'), 0, 'network_error', true);
  }

  // Railway rollout / cold-start: the proxy returns 502/503/504 for ~1-2s while
  // the new instance boots. One bounded retry avoids surfacing a transient 5xx
  // as a hard failure on a button the user just tapped. Mutations are safe to
  // retry once here because Railway only returns these during boot — not as
  // application errors — and the backend's punch paths are idempotent where it
  // matters: offline POST /attendance/sync is deduped on client_punch_id UNIQUE
  // + (device_id, nonce) UNIQUE (migrations.py, offline_sync.py INSERT OR IGNORE),
  // and online POST /attendance/punch is gated by single-active-session
  // (main.py) so a proxy-level retry cannot double-create an active session.
  if (isRailwayColdStartStatus(res.status) && !opts._coldStartRetried) {
    await sleep(RAILWAY_COLD_START_RETRY_MS);
    return request<T>({ ...opts, _coldStartRetried: true });
  }

  if (res.status === 401 && needsAuth && opts.retryOn401 !== false) {
    const next = await refreshToken();
    if (next) {
      // Retry once with refreshed token.
      return request<T>({ ...opts, retryOn401: false });
    }
    throw makeApiError('Not authenticated', 401, 'unauthorized');
  }

  if (!res.ok) {
    let detail: unknown = null;
    let code: string | undefined;
    let structured: Record<string, unknown> | undefined;
    try {
      const j = (await res.json()) as Record<string, unknown>;
      detail = (j['detail'] as unknown) ?? j;
      if (typeof detail === 'object' && detail !== null) {
        structured = detail as Record<string, unknown>;
        code = structured['error_code'] as string | undefined;
        if (!code) code = (j['error_code'] as string) ?? (j['code'] as string) ?? undefined;
      } else if (typeof detail === 'string') {
        code = detail;
      }
    } catch {
      // ignore json parse failure
    }
    const msg =
      typeof detail === 'string'
        ? detail
        : (structured?.['message'] as string | undefined) ??
          (structured?.['error_code'] as string | undefined) ??
          `Request failed (${res.status})`;
    throw makeApiError(msg, res.status, code, false, structured);
  }

  if (res.status === 204) return undefined as T;
  const text = await res.text();
  if (!text) return undefined as T;
  try {
    return JSON.parse(text) as T;
  } catch {
    return text as unknown as T;
  }
}

export async function getStoredToken(): Promise<string | null> {
  return loadToken();
}

export const __test__ = { buildUrl, isNetworkFailure, TOKEN_KEY };
