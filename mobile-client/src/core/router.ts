import { Capacitor } from '@capacitor/core';
import { App as CapApp } from '@capacitor/app';

export type TabId = 'clock' | 'history' | 'alerts' | 'notes' | 'profile';

export const TAB_IDS: readonly TabId[] = ['clock', 'history', 'alerts', 'notes', 'profile'] as const;

export interface Route {
  tab: TabId;
  path: string;
  params: Record<string, string>;
}

type RouteListener = (route: Route) => void;

function parseHash(hash: string): Route {
  const raw = hash.replace(/^#\/?/, '').trim();
  if (!raw) return { tab: 'clock', path: '/', params: {} };
  const [segment, qs] = raw.split('?');
  const tab = (segment.split('/')[0] as TabId) || 'clock';
  const safeTab: TabId = (TAB_IDS as readonly string[]).includes(tab) ? tab : 'clock';
  const params: Record<string, string> = {};
  if (qs) {
    for (const part of qs.split('&')) {
      const [k, v] = part.split('=');
      if (k) params[decodeURIComponent(k)] = decodeURIComponent(v ?? '');
    }
  }
  return { tab: safeTab, path: `/${segment}`, params };
}

function toHash(tab: TabId, params?: Record<string, string>): string {
  if (!params || Object.keys(params).length === 0) return `#/${tab}`;
  const qs = Object.entries(params)
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`)
    .join('&');
  return `#/${tab}?${qs}`;
}

export class Router {
  private listeners = new Set<RouteListener>();
  private current: Route = parseHash(typeof window !== 'undefined' ? window.location.hash : '');
  private history: TabId[] = [this.current.tab];
  private backHandler: { remove: () => void } | null = null;
  private started = false;

  getRoute(): Route {
    return this.current;
  }

  subscribe(fn: RouteListener): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  private emit(): void {
    for (const fn of this.listeners) {
      try {
        fn(this.current);
      } catch {
        // isolate listener failures
      }
    }
  }

  navigate(tab: TabId, params?: Record<string, string>, opts: { replace?: boolean } = {}): void {
    const hash = toHash(tab, params);
    if (opts.replace) {
      window.location.replace(hash);
    } else {
      window.location.hash = hash;
    }
    // hashchange will update this.current; update eagerly for synchronous callers.
    this.current = parseHash(hash);
    if (this.history[this.history.length - 1] !== tab) this.history.push(tab);
    if (this.history.length > 20) this.history.shift();
    this.emit();
  }

  start(): void {
    if (this.started) return;
    this.started = true;
    this.current = parseHash(window.location.hash);
    if (!window.location.hash) {
      window.location.hash = `#/${this.current.tab}`;
    }
    window.addEventListener('hashchange', () => {
      const next = parseHash(window.location.hash);
      // Track tab history for back handling.
      if (this.current.tab !== next.tab) {
        if (this.history[this.history.length - 1] !== next.tab) this.history.push(next.tab);
        if (this.history.length > 20) this.history.shift();
      }
      this.current = next;
      this.emit();
    });

    // Android hardware back button: pop tab history, otherwise minimize/exit via Capacitor App.
    if (Capacitor.isNativePlatform()) {
      CapApp.addListener('backButton', ({ canGoBack }) => {
        // If we have tab history beyond the root, navigate back within the app.
        if (this.history.length > 1) {
          this.history.pop();
          const prev = this.history[this.history.length - 1] ?? 'clock';
          window.location.hash = `#/${prev}`;
          return;
        }
        if (canGoBack) {
          window.history.back();
          return;
        }
        CapApp.exitApp();
      }).then((h) => {
        this.backHandler = h;
      });
    }
  }

  stop(): void {
    if (this.backHandler) {
      this.backHandler.remove();
      this.backHandler = null;
    }
    this.started = false;
  }

  __test__getHistory(): TabId[] {
    return [...this.history];
  }
}

export const router = new Router();
