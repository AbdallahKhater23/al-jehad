export type UserRole = 'worker' | 'moallem' | 'off_office' | 'admin' | 'head_admin' | 'developer';

export interface SessionUser {
  id: string;
  name: string;
  role: UserRole;
  email?: string | null;
  phone?: string | null;
}

export interface Session {
  user: SessionUser;
  token: string;
  expiresAt: string | null;
  tokenVersion: number;
}

export interface ShiftStatus {
  active: boolean;
  siteName: string | null;
  clockInTime: string | null;
  startSource: string | null;
}

export interface GeofenceWindow {
  siteName: string | null;
  onSite: boolean;
  windowLabel: string | null;
  distanceM: number | null;
}

export interface StoreState {
  session: Session | null;
  shift: ShiftStatus | null;
  geofence: GeofenceWindow | null;
  offlineQueueDepth: number;
  lastSyncAt: string | null;
  initialized: boolean;
}

type Listener = (state: Readonly<StoreState>) => void;

function deepEqual(a: unknown, b: unknown): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

export class Store {
  private state: StoreState = {
    session: null,
    shift: null,
    geofence: null,
    offlineQueueDepth: 0,
    lastSyncAt: null,
    initialized: false,
  };

  private listeners = new Set<Listener>();

  getState(): Readonly<StoreState> {
    return this.state;
  }

  subscribe(listener: Listener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  private emit(): void {
    const snapshot = this.state;
    for (const fn of this.listeners) {
      try {
        fn(snapshot);
      } catch {
        // Listener errors must not break the store.
      }
    }
  }

  setSession(session: Session | null): void {
    if (deepEqual(this.state.session, session)) return;
    this.state = { ...this.state, session };
    this.emit();
  }

  setShift(shift: ShiftStatus | null): void {
    if (deepEqual(this.state.shift, shift)) return;
    this.state = { ...this.state, shift };
    this.emit();
  }

  setGeofence(geofence: GeofenceWindow | null): void {
    if (deepEqual(this.state.geofence, geofence)) return;
    this.state = { ...this.state, geofence };
    this.emit();
  }

  setOfflineQueueDepth(depth: number): void {
    const n = Math.max(0, Math.floor(depth));
    if (this.state.offlineQueueDepth === n) return;
    this.state = { ...this.state, offlineQueueDepth: n };
    this.emit();
  }

  setLastSyncAt(iso: string | null): void {
    if (this.state.lastSyncAt === iso) return;
    this.state = { ...this.state, lastSyncAt: iso };
    this.emit();
  }

  setInitialized(v: boolean): void {
    if (this.state.initialized === v) return;
    this.state = { ...this.state, initialized: v };
    this.emit();
  }

  patch(partial: Partial<StoreState>): void {
    let changed = false;
    const next = { ...this.state };
    for (const k of Object.keys(partial) as (keyof StoreState)[]) {
      const nv = partial[k] as unknown;
      const cv = this.state[k] as unknown;
      if (!deepEqual(cv, nv)) {
        (next as Record<string, unknown>)[k] = nv;
        changed = true;
      }
    }
    if (!changed) return;
    this.state = next;
    this.emit();
  }

  select<T>(selector: (s: Readonly<StoreState>) => T): T {
    return selector(this.state);
  }
}

export const store = new Store();
