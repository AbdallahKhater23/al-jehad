import { Network } from '@capacitor/network';

export type NetworkStatus = { connected: boolean; connectionType: string };

type Listener = (status: NetworkStatus) => void;

const listeners = new Set<Listener>();
let current: NetworkStatus = { connected: true, connectionType: 'unknown' };
let started = false;
let handle: { remove: () => Promise<void> } | null = null;

function emit(status: NetworkStatus): void {
  current = status;
  for (const fn of listeners) {
    try {
      fn(status);
    } catch {
      // isolate
    }
  }
}

export function getNetworkStatus(): NetworkStatus {
  return current;
}

export function isOnline(): boolean {
  return current.connected;
}

export function subscribeNetwork(fn: Listener): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export async function startNetworkListener(opts?: {
  onOnline?: () => void;
  onOffline?: () => void;
}): Promise<void> {
  if (started) return;
  started = true;
  try {
    const s = await Network.getStatus();
    emit({ connected: s.connected, connectionType: s.connectionType });
  } catch {
    // Fallback to browser online signal.
    emit({ connected: navigator.onLine !== false, connectionType: 'unknown' });
  }

  try {
    handle = await Network.addListener('networkStatusChange', (s) => {
      const next: NetworkStatus = { connected: s.connected, connectionType: s.connectionType };
      const wasOnline = current.connected;
      emit(next);
      if (!wasOnline && next.connected) opts?.onOnline?.();
      if (wasOnline && !next.connected) opts?.onOffline?.();
    });
  } catch {
    // Browser fallback.
    const onOnline = () => {
      const wasOnline = current.connected;
      emit({ connected: true, connectionType: 'wifi' });
      if (!wasOnline) opts?.onOnline?.();
    };
    const onOffline = () => {
      emit({ connected: false, connectionType: 'none' });
      opts?.onOffline?.();
    };
    window.addEventListener('online', onOnline);
    window.addEventListener('offline', onOffline);
    handle = {
      remove: async () => {
        window.removeEventListener('online', onOnline);
        window.removeEventListener('offline', onOffline);
      },
    };
  }
}

export async function stopNetworkListener(): Promise<void> {
  if (handle) {
    await handle.remove().catch(() => {});
    handle = null;
  }
  started = false;
}
