/**
 * Application bootstrap.
 *
 * Order matters and is deliberate:
 *
 *   1. **Styles** — imported first so the first paint is already the real theme rather than
 *      a flash of unstyled white.
 *   2. **Session** — restored from Preferences before anything asks for a token. A token
 *      that exists but has lapsed is refreshed, not trusted.
 *   3. **Storage** — the offline database is opened early, because a punch captured seconds
 *      after launch must not race the first write.
 *   4. **Device + anchor** — registration and a server-signed anchor are established while
 *      the network is up. This is the only window in which an offline punch becomes
 *      possible, so it happens at boot rather than at the first tap.
 *   5. **Listeners** — connectivity and lifecycle, so a reconnect or a foreground drains the
 *      queue without the worker doing anything.
 *   6. **Shell** — mounted last, because every screen reads the session the steps above
 *      established.
 */

import './styles/tokens.css';
import './styles/shell.css';
import './styles/screens/auth.css';
import './styles/screens/clock.css';
import './styles/screens/notes.css';
import './styles/screens/profile.css';

import { Preferences } from '@capacitor/preferences';
import { resolveApiBaseUrl } from './core/config.js';
import { clearPersistedSession } from './core/http.js';
import { store } from './core/store.js';
import { runSync, syncForCurrentWorker, refreshWorkerState } from './core/sync-loop.js';
import { ensureDevice, refreshAnchor } from './offline/device.js';
import { pendingCount } from './offline/queue.js';
import { startNetworkListener } from './native/network.js';
import { startLifecycle, onLifecycle } from './native/lifecycle.js';
import {
  mountShell,
  registerScreen,
  unmountShell,
  setBadges,
  installBackHandler,
  buildHeaderInner,
} from './ui/shell.js';
import { createLoginScreen } from './ui/screens/login/login.js';
import { createClockScreen } from './ui/screens/clock/clock.js';
import { createHistoryScreen } from './ui/screens/history/history.js';
import { createAlertsScreen } from './ui/screens/alerts/alerts.js';
import { createNotesScreen } from './ui/screens/notes/notes.js';
import { createProfileScreen } from './ui/screens/profile/profile.js';
import { toastError } from './ui/components/toast.js';
import { loadTheme } from './ui/theme.js';

const USER_KEY = 'auth_user';
const TOKEN_KEY = 'auth_token';
const EXPIRES_KEY = 'auth_expires_at';

export interface BootstrapResult {
  apiBase: string | null;
  hasSession: boolean;
  queueDepth: number;
  anchorReady: boolean;
  error: string | null;
}

// ---------------------------------------------------------------------------
// session
// ---------------------------------------------------------------------------
async function restoreSession(): Promise<boolean> {
  const { value: token } = await Preferences.get({ key: TOKEN_KEY }).catch(() => ({ value: null }));
  if (!token) {
    store.setSession(null);
    return false;
  }
  const { value: userJson } = await Preferences.get({ key: USER_KEY }).catch(() => ({ value: null }));
  const { value: expiresAt } = await Preferences.get({ key: EXPIRES_KEY }).catch(() => ({ value: null }));

  let user: Record<string, unknown> | null = null;
  try {
    user = userJson ? (JSON.parse(userJson) as Record<string, unknown>) : null;
  } catch {
    user = null;
  }

  if (!user?.id) {
    // A token with no account beside it is a half-written session: it cannot be rendered,
    // so it is cleared rather than carried.
    await clearPersistedSession();
    return false;
  }

  store.setSession({
    user: {
      id: String(user.id),
      name: String(user.name ?? ''),
      role: String(user.role ?? 'worker') as never,
      email: (user.email as string) ?? null,
      phone: (user.phone as string) ?? null,
    },
    token,
    expiresAt: expiresAt ?? null,
    tokenVersion: 0,
  });
  return true;
}

// ---------------------------------------------------------------------------
// boot
// ---------------------------------------------------------------------------
export async function bootstrap(): Promise<BootstrapResult> {
  const result: BootstrapResult = {
    apiBase: null,
    hasSession: false,
    queueDepth: 0,
    anchorReady: false,
    error: null,
  };

  try {
    result.apiBase = await resolveApiBaseUrl();
  } catch (e) {
    result.error = (e as Error).message;
    store.setInitialized(true);
    return result;
  }

  const hasSession = await restoreSession();
  result.hasSession = hasSession;
  if (!hasSession) {
    store.setInitialized(true);
    return result;
  }

  const workerId = store.getState().session?.user.id ?? '';
  result.queueDepth = await pendingCount(workerId).catch(() => 0);
  store.setOfflineQueueDepth(result.queueDepth);

  // Establish the two things a punch needs before it can be signed. Failure here is not
  // fatal: the worker can still sign in and read, and the next reconnect retries.
  try {
    await ensureDevice(workerId);
    await refreshAnchor(workerId);
    result.anchorReady = true;
  } catch {
    result.anchorReady = false;
  }

  await refreshWorkerState();

  // Drain anything left over from a previous session.
  try {
    const device = await ensureDevice(workerId).catch(() => null);
    if (device) await runSync(workerId, device.device_id);
  } catch {
    // runSync never throws for transport problems; this guards the device lookup.
  }

  store.setInitialized(true);
  return result;
}

async function wireListeners(): Promise<void> {
  await startNetworkListener({
    onOnline: () => {
      const session = store.getState().session;
      if (!session) return;
      // A reconnect is exactly when authority is renewed, so the anchor is refreshed
      // *before* the queue is drained — a punch queued after this gets the fresh window.
      void refreshAnchor(session.user.id)
        .catch(() => {})
        .then(() => syncForCurrentWorker());
    },
  });

  await startLifecycle();
  onLifecycle('resume', () => {
    void syncForCurrentWorker();
  });
  onLifecycle('foreground', () => {
    if (!store.getState().session) return;
    void refreshWorkerState();
  });
}

// ---------------------------------------------------------------------------
// shell wiring
// ---------------------------------------------------------------------------
function registerScreens(): void {
  registerScreen('clock', () => createClockScreen());
  registerScreen('history', () => createHistoryScreen());
  registerScreen('alerts', () => createAlertsScreen());
  registerScreen('notes', () => createNotesScreen());
  registerScreen('profile', () => createProfileScreen(() => showLogin()));
}

let root: HTMLElement | null = null;

/** Show the shell for the signed-in worker. */
function showApp(): void {
  if (!root) return;
  registerScreens();
  mountShell(root);
}

/** Show the sign-in screen. Tears the shell down so no screen keeps a timer alive. */
function showLogin(): void {
  if (!root) return;
  unmountShell();
  setBadges({ alerts: 0, notes: 0 });
  // The login screen is registered as the clock tab's stand-in: it is the only screen the
  // router can show when there is no session, and it replaces the whole frame.
  mountLoginScreen();
}

function mountLoginScreen(): void {
  if (!root) return;
  root.replaceChildren();
  const app = document.createElement('div');
  app.className = 'hand-app';
  const header = document.createElement('header');
  header.className = 'hand-header';
  const view = document.createElement('main');
  view.className = 'hand-main';
  app.append(header, view);
  root.appendChild(app);

  const screen = createLoginScreen(() => {
    void (async () => {
      await bootstrap();
      // The same callback serves a successful sign-in and a saved server address. A saved
      // address is not a session: without one the worker stays on the sign-in screen, now
      // redrawn against the address that was just found or tested.
      if (store.getState().session) {
        showApp();
        void syncForCurrentWorker();
      } else {
        mountLoginScreen();
      }
    })();
  });

  // The same header band the shell draws, so the frame does not change shape when the
  // session arrives -- only the name and role inside it do.
  header.appendChild(buildHeaderInner(screen.actions?.() ?? []));
  // The shell names its main landmark from the screen's own title; this frame draws its
  // own main, so it has to do the same instead of leaving an unnamed landmark behind.
  view.setAttribute('aria-label', screen.title());

  const host = document.createElement('div');
  host.className = 'hand-screen';
  view.appendChild(host);
  screen.mount(host, { refresh: mountLoginScreen, main: view });
}

/** Decide which of the two frames to show. */
async function renderEntry(): Promise<void> {
  if (store.getState().session) showApp();
  else mountLoginScreen();
}

export async function start(): Promise<void> {
  root = document.getElementById('app');
  if (!root) {
    throw new Error('The app root element is missing.');
  }

  // The worker's theme choice, applied before the first frame the shell paints.
  await loadTheme();
  installBackHandler();
  const result = await bootstrap();
  await wireListeners();
  await renderEntry();

  if (result.error) {
    toastError(`Server address is not configured: ${result.error}`);
  } else if (result.hasSession && !result.anchorReady) {
    toastError('Could not reach the server. Offline punches will be available once it answers.');
  }
}

// ---------------------------------------------------------------------------
// the sign-out / session-loss path
// ---------------------------------------------------------------------------
// Tracked outside the subscription so the callback can tell a *loss* of session from the
// steady state: only the transition from signed-in to signed-out replaces the frame.
let lastHadSession = false;

store.subscribe((state) => {
  const current = Boolean(state.session);
  const wasSignedIn = lastHadSession;
  lastHadSession = current;
  if (wasSignedIn && !current) showLogin();
});

if (typeof document !== 'undefined') {
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => {
      void start();
    });
  } else {
    void start();
  }
}
