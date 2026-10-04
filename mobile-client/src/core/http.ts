import { Preferences } from '@capacitor/preferences';
import { resolveApiBaseUrl } from './config.js';
import { store } from './store.js';

const TOKEN_KEY = 'auth_token';
const TOKEN_EXPIRES_KEY = 'auth_expires_at';
const USER_KEY = 'auth_user';
const TOKEN_VERSION_KEY = 'auth_token_version';

export type HttpMethod = 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';

export interface RequestOptions {
  method?: HttpMethod;
  path: string;
  /**
   * Talk to this base instead of the one this device is configured with.
   *
   * Only for asking a *candidate* server whether it is there, before anything is saved:
   * the server-address sheet probes the address the worker typed, not the one the app is
   * still configured with. Regular calls never pass it, so an override cannot be bypassed
   * by accident.
   */
  base?: string;
  query?: Record<string, string | number | boolean | null | undefined>;
  body?: unknown;
  formData?: FormData;
  headers?: Record<string, string>;
  timeoutMs?: number;
  auth?: boolean;
  retryOn401?: boolean;
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

export async function request<T = unknown>(opts: RequestOptions): Promise<T> {
  const base = opts.base ?? (await resolveApiBaseUrl());
  const url = buildUrl(base, opts.path, opts.query);
  const needsAuth = opts.auth !== false;
  let token: string | null = null;
  if (needsAuth) token = await loadToken();

  const timeoutMs = opts.timeoutMs ?? 15_000;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);

  const headers: Record<string, string> = {
    Accept: 'application/json',
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
    res = await fetch(url, {
      method: opts.method ?? 'GET',
      headers,
      body,
      signal: controller.signal,
    });
  } catch (err) {
    clearTimeout(timer);
    if (isNetworkFailure(err)) {
      throw makeApiError('Network unavailable', 0, 'offline', true);
    }
    if (err instanceof DOMException && err.name === 'AbortError') {
      throw makeApiError('Request timed out', 0, 'timeout');
    }
    throw makeApiError(String((err as Error).message ?? 'Network error'), 0, 'network_error', true);
  } finally {
    clearTimeout(timer);
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
