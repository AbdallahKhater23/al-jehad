import { App as CapApp } from '@capacitor/app';
import { Capacitor } from '@capacitor/core';

export type LifecycleEvent = 'pause' | 'resume' | 'foreground' | 'background';

type Handler = () => void | Promise<void>;

const handlers: Record<LifecycleEvent, Set<Handler>> = {
  pause: new Set(),
  resume: new Set(),
  foreground: new Set(),
  background: new Set(),
};

let started = false;
let capHandles: Array<{ remove: () => Promise<void> }> = [];

function emit(event: LifecycleEvent): void {
  for (const fn of handlers[event]) {
    try {
      const r = fn();
      if (r instanceof Promise) r.catch(() => {});
    } catch {
      // isolate
    }
  }
}

export function onLifecycle(event: LifecycleEvent, fn: Handler): () => void {
  handlers[event].add(fn);
  return () => handlers[event].delete(fn);
}

export async function startLifecycle(): Promise<void> {
  if (started) return;
  started = true;

  // Browser fallbacks — always wired so sync-on-foreground works in dev.
  const onVisibility = () => {
    if (document.visibilityState === 'visible') {
      emit('foreground');
      emit('resume');
    } else {
      emit('background');
      emit('pause');
    }
  };
  document.addEventListener('visibilitychange', onVisibility);

  const onPageShow = () => emit('resume');
  const onPageHide = () => emit('pause');
  window.addEventListener('pageshow', onPageShow);
  window.addEventListener('pagehide', onPageHide);

  // Keep references for stopLifecycle.
  capHandles.push({
    remove: async () => {
      document.removeEventListener('visibilitychange', onVisibility);
      window.removeEventListener('pageshow', onPageShow);
      window.removeEventListener('pagehide', onPageHide);
    },
  });

  if (Capacitor.isNativePlatform()) {
    try {
      const h1 = await CapApp.addListener('appStateChange', ({ isActive }) => {
        if (isActive) {
          emit('foreground');
          emit('resume');
        } else {
          emit('background');
          emit('pause');
        }
      });
      const h2 = await CapApp.addListener('pause', () => emit('pause'));
      const h3 = await CapApp.addListener('resume', () => emit('resume'));
      capHandles.push(h1, h2, h3);
    } catch {
      // Native listeners are best-effort.
    }
  }
}

export async function stopLifecycle(): Promise<void> {
  for (const h of capHandles) {
    await h.remove().catch(() => {});
  }
  capHandles = [];
  started = false;
}
