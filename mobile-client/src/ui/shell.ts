/**
 * The app shell: a sticky header, one scrollable region, and the fixed bottom tab bar.
 *
 * Only ``.hand-main`` scrolls. That is what keeps the header and the tab bar put without
 * ``position: fixed``, which on Android fights the on-screen keyboard and the safe-area
 * insets at the same time.
 *
 * Screens are mounted into ``#hand-view`` and torn down on the way out: a screen that has
 * started a polling timer or a camera stream has to be able to stop it, so every screen
 * returns a disposer and the shell calls it before mounting the next one.
 */

import { router, TAB_IDS, type Route, type TabId } from '../core/router.js';
import { store } from '../core/store.js';
import { t } from '../core/strings.js';
import { el, esc, on, raw } from './dom.js';
import { icon, type IconName } from './icons.js';
import { closeSheet, sheetIsOpen } from './components/sheet.js';
import { headerPreferences } from './components/preferences.js';

export interface ScreenContext {
  /** Re-render this screen in place. */
  refresh: () => void;
  /** The scrollable region, for a screen that owns its own list. */
  main: HTMLElement;
}

export interface Screen {
  /** Called once per mount. Returns a disposer. */
  mount(host: HTMLElement, ctx: ScreenContext): void | (() => void);
  /** The header's title and subtitle for this screen. */
  title(): string;
  subtitle?(): string;
  /** Header actions, rendered to the right of the title. */
  actions?(): HTMLElement[];
  /** Screens that own their scroll region set this. */
  fullHeight?: boolean;
}

type ScreenFactory = (route: Route) => Screen;

const factories = new Map<TabId, ScreenFactory>();

/**
 * The tab bar's own table.
 *
 * ``label`` is a getter rather than a string: it is drawn from the app's string
 * table (``core/strings.ts``), and the language can change while the app is running,
 * so a label captured at module load would be the one thing on screen that stayed in
 * the language it started in. Every read goes through ``t()``, which answers in the
 * language chosen *at that moment*.
 */
const TAB_META: Record<TabId, { readonly label: string; icon: IconName }> = {
  clock: { get label() { return t('nav.clock'); }, icon: 'clock' },
  history: { get label() { return t('nav.history'); }, icon: 'history' },
  alerts: { get label() { return t('nav.alerts'); }, icon: 'alerts' },
  notes: { get label() { return t('nav.notes'); }, icon: 'notes' },
  profile: { get label() { return t('nav.profile'); }, icon: 'profile' },
};

let mounted: { screen: Screen; dispose: (() => void) | null } | null = null;
let shellRoot: HTMLElement | null = null;
let viewHost: HTMLElement | null = null;
let headerHost: HTMLElement | null = null;
let tabsHost: HTMLElement | null = null;
let unreadAlerts = 0;
let unreadNotes = 0;
/** The shell's polite announcer: badge increases and route changes, read but never drawn. */
let liveRegion: HTMLElement | null = null;
let badgesSeen = false;
let lastTab: TabId | null = null;

function announce(message: string): void {
  if (liveRegion) liveRegion.textContent = message;
}

export function registerScreen(tab: TabId, factory: ScreenFactory): void {
  factories.set(tab, factory);
}

/** The badge counts the tab bar shows. Set from the screens that fetch the data. */
export function setBadges(counts: { alerts?: number; notes?: number }): void {
  const before = { alerts: unreadAlerts, notes: unreadNotes };
  if (counts.alerts !== undefined) unreadAlerts = Math.max(0, counts.alerts);
  if (counts.notes !== undefined) unreadNotes = Math.max(0, counts.notes);

  // A count that goes *up* is announced, politely. The first fetch after the shell mounts
  // is the state the reader is already looking at, not news. A count that goes down is
  // something the worker just did on the screen in front of them, so it is not read out.
  if (badgesSeen) {
    if (counts.alerts !== undefined && unreadAlerts > before.alerts) {
      announce(`${TAB_META.alerts.label}: ${unreadAlerts} unread`);
    }
    if (counts.notes !== undefined && unreadNotes > before.notes) {
      announce(`${TAB_META.notes.label}: ${unreadNotes} unread`);
    }
  }
  badgesSeen = true;
  paintTabs();
}

export function getBadges(): { alerts: number; notes: number } {
  return { alerts: unreadAlerts, notes: unreadNotes };
}

function paintTabs(): void {
  if (!tabsHost) return;
  // The bar's own name is a string too: it is what a screen reader announces when the
  // worker reaches the bar, and it has to answer in the language the rest of it does.
  tabsHost.setAttribute('aria-label', t('nav.sections'));
  const route = router.getRoute();
  for (const tab of TAB_IDS) {
    const button = tabsHost.querySelector(`[data-tab="${tab}"]`) as HTMLButtonElement | null;
    if (!button) continue;
    // ``aria-current`` rather than a class is what marks the tab, exactly as the web
    // marks it: it is the attribute a screen reader answers "where am I" with, and the
    // styling hangs off it, so the two can never disagree.
    if (tab === route.tab) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');

    // Relabelled on every paint, so a language change relabels the bar without
    // rebuilding it (and without moving any focus).
    const label = button.querySelector('.hand-tab-label');
    if (label) label.textContent = TAB_META[tab].label;

    const badgeCount = tab === 'alerts' ? unreadAlerts : tab === 'notes' ? unreadNotes : 0;
    let badge = button.querySelector('.hand-tab-count');
    let status = button.querySelector<HTMLElement>('.hand-tab-status');
    if (badgeCount > 0) {
      if (!badge) {
        // The numeral is decoration -- ``aria-hidden`` -- but the count is not: the hidden
        // span beside it puts "3 unread" into the tab's own accessible name, so the one
        // fact a worker needs from this bar is not the one fact a screen reader cannot say.
        badge = el('<span class="hand-tab-count" aria-hidden="true"></span>');
        button.appendChild(badge);
      }
      badge.setAttribute('data-count', String(badgeCount));
      badge.textContent = badgeCount > 99 ? '99+' : String(badgeCount);
      if (!status) {
        status = el('<span class="hand-sr-only hand-tab-status"></span>');
        button.appendChild(status);
      }
      status.textContent = badgeCount > 99 ? 'more than 99 unread' : `${badgeCount} unread`;
    } else {
      badge?.remove();
      status?.remove();
    }
  }
}

function buildTabs(): HTMLElement {
  // The web's own bar: one button per section, the icon over the word, and the unread
  // count over the icon's shoulder. The class names are the web's, so what styles this
  // is the same rule the desk uses rather than a copy of it.
  const bar = el(`<nav class="hand-tabs" aria-label="${esc(t('nav.sections'))}"></nav>`);
  for (const tab of TAB_IDS) {
    const meta = TAB_META[tab];
    const button = el(`
      <button type="button" data-tab="${tab}">
        ${icon(meta.icon)}
        <span class="hand-tab-label">${esc(meta.label)}</span>
      </button>
    `);
    on(button, 'click', () => {
      if (router.getRoute().tab === tab) {
        // Tapping the current tab scrolls back to the top, which is what every native
        // tab bar does and what a worker expects when they have scrolled a long list.
        viewHost?.scrollTo({ top: 0, behavior: 'smooth' });
        return;
      }
      router.navigate(tab);
    });
    bar.appendChild(button);
  }
  return bar;
}

/** The company's role names, as the web spells them (frontend/i18n.js). */
const ROLE_LABELS: Record<string, string> = {
  worker: 'Worker',
  moallem: 'Moallem',
  off_office: 'Off-Office Worker',
  admin: 'Administrator',
  head_admin: 'Head administrator',
  developer: 'Developer',
};

/**
 * The header's inner band -- the mark on the company's green, who is signed in, and the
 * screen's controls. The same three blocks as the web's ``workerHeaderHtml``, in the same
 * order, and exported so the sign-in frame draws the same header the shell does.
 */
export function buildHeaderInner(actions: HTMLElement[] = []): HTMLElement {
  const session = store.getState().session;
  const name = session?.user.name || t('app.name');
  const role = session ? (ROLE_LABELS[session.user.role] ?? session.user.role) : t('header.roleSignedOut');
  const band = el(`
    <div class="hand-header-inner">
      <span class="hand-brand" aria-hidden="true"><img src="/logo-mark.svg" alt="" /></span>
      <div class="hand-who">
        <p class="hand-who-name">${esc(name)}</p>
        <p class="hand-who-role">${esc(role)}</p>
      </div>
    </div>
  `);
  // The screen's own controls, then the two preference controls that belong to every
  // screen -- and to the sign-in frame, which draws this same header. The gear that
  // used to sit at the end of this row is gone: there is no server to configure.
  const host = el('<div class="hand-actions"></div>');
  for (const action of actions) host.appendChild(action);
  for (const control of headerPreferences()) host.appendChild(control);
  band.appendChild(host);
  return band;
}

function paintHeader(screen: Screen): void {
  if (!headerHost) return;
  headerHost.replaceChildren(buildHeaderInner(screen.actions?.() ?? []));
}

function unmount(): void {
  if (mounted?.dispose) {
    try {
      mounted.dispose();
    } catch {
      // A screen's own cleanup must not stop the next screen from mounting.
    }
  }
  mounted = null;
}

/** Mount the screen for the current route. */
export function mountRoute(route: Route): void {
  if (!viewHost || !headerHost) return;
  const factory = factories.get(route.tab);
  if (!factory) {
    // Unreachable in this build: every tab is registered before the shell mounts. Stated
    // as a fault rather than as "coming soon", because a screen that silently does not
    // exist is a bug, not a roadmap -- and a worker reading "coming soon" on the tab they
    // use every day would report it as one.
    // The heading is not decoration even here: a screen reader that lands on the fault state
    // should still be told which section it is in, exactly as it would be on a working one.
    viewHost.innerHTML = `
      <h1 class="hand-sr-only">${esc(TAB_META[route.tab].label)}</h1>
      <div class="ui-empty">
        <p class="ui-empty-title">This section did not load</p>
        <p class="ui-empty-body">Close and reopen the app. If it stays empty, tell an administrator.</p>
      </div>
    `;
    return;
  }

  unmount();
  const screen = factory(route);
  paintHeader(screen);

  viewHost.replaceChildren();
  viewHost.className = screen.fullHeight ? 'hand-main hand-main--flush' : 'hand-main';
  // The screen knows its own name (``Screen.title``); the header deliberately shows the
  // worker instead. Naming the main landmark with it is what lets a screen reader answer
  // "which part of the app am I in" without sighted context.
  const title = screen.title();
  if (title) viewHost.setAttribute('aria-label', title);
  else viewHost.removeAttribute('aria-label');
  // The screen owns the document's one **h1**, drawn from its own name so the two can never
  // disagree. It is read, never seen: the header deliberately shows the worker, and a visible
  // title band would be a second header the web does not have. What it buys is the outline -
  // without it every card below announced its title as an orphan "heading level 3", with no
  // level 1 or 2 to place it under (the cards' own section titles are h2).
  //
  // Exactly one screen is mounted at a time, so exactly one h1 is in the document; the
  // sign-in frame is not this shell and carries its own visible h1.
  if (title) {
    const heading = el('<h1 class="hand-sr-only"></h1>');
    heading.textContent = title;
    viewHost.appendChild(heading);
  }
  const host = el('<div class="hand-screen"></div>');
  viewHost.appendChild(host);

  const ctx: ScreenContext = {
    main: viewHost,
    refresh: () => mountRoute(router.getRoute()),
  };
  const dispose = screen.mount(host, ctx);
  mounted = { screen, dispose: typeof dispose === 'function' ? dispose : null };
  paintTabs();

  // A tab bar that silently swaps the page leaves a screen reader with no confirmation the
  // tap did anything, so the new section is announced. The first mount is not an
  // announcement: that is the screen the worker opened the app to reach.
  if (lastTab !== null && lastTab !== route.tab) {
    announce(`${TAB_META[route.tab].label} section`);
  }
  lastTab = route.tab;
}

/** Build the frame and start listening to the router. */
export function mountShell(root: HTMLElement): void {
  root.replaceChildren();
  const app = el('<div class="hand-app"></div>');
  const header = el('<header class="hand-header"></header>');
  const view = el('<main class="hand-main" id="hand-view"></main>');
  const tabs = buildTabs();

  app.append(header, view, tabs);
  // The live region is deliberately outside ``.hand-app``: an extra child of that grid
  // would add a row, and the region has to exist before the first message is put in it --
  // a live region created in the same task as its content is not announced by every
  // reader.
  liveRegion = el('<p class="hand-sr-only" role="status" aria-live="polite"></p>');
  root.append(app, liveRegion);

  headerHost = header;
  viewHost = view;
  tabsHost = tabs;
  shellRoot = app;

  router.subscribe((route) => mountRoute(route));
  mountRoute(router.getRoute());
}

/** Re-render the mounted screen without a route change (after a store update, say). */
export function refreshScreen(): void {
  mountRoute(router.getRoute());
}

export function isShellMounted(): boolean {
  return shellRoot !== null;
}

/** The Android back button: a sheet first, then the tab history, then exit. */
export function installBackHandler(): void {
  on(document, 'keydown', (event) => {
    if ((event as KeyboardEvent).key === 'Escape' && sheetIsOpen()) {
      closeSheet();
    }
  });
}

/** Sign-out tears the frame down so no screen keeps a timer alive behind the login form. */
export function unmountShell(): void {
  unmount();
  shellRoot?.remove();
  shellRoot = null;
  headerHost = null;
  viewHost = null;
  tabsHost = null;
  unreadAlerts = 0;
  unreadNotes = 0;
  badgesSeen = false;
  lastTab = null;
  liveRegion = null;
}

/** A subtitle for the header built from the signed-in worker and the queue depth. */
export function sessionSubtitle(): string {
  const state = store.getState();
  if (!state.session) return '';
  const queued = state.offlineQueueDepth;
  return queued > 0
    ? `${state.session.user.name} · ${queued} punch${queued === 1 ? '' : 'es'} waiting`
    : state.session.user.name;
}

void raw;
