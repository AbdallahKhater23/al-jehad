/**
 * The server-URL sheet.
 *
 * A packaged APK has no "same origin", so the API host is baked at build time — and a
 * self-hosted deployment on a LAN is exactly the case where that baked value is wrong.
 * This sheet is the escape hatch: it validates the address, probes it with a real request
 * before saving, and refuses to store something that does not answer. Saving a dead URL
 * would otherwise brick the app until a reinstall, because every screen would fail at once.
 *
 * Reachable from the sign-in screen's gear and from Profile → Server, so a worker who has
 * already signed in is not trapped by a URL they mistyped.
 *
 * Since a laptop's LAN address is the thing that changes, the sheet can also *find* the
 * server: it probes the addresses the app already knows, then sweeps the network the worker
 * points it at for a server answering as this app (``core/discovery.ts``). The Find button
 * saves what it verified, so the common case -- "the address changed this morning" -- is one
 * tap instead of an IP hunt.
 */

import { getApiBaseOverride, resolveApiBaseUrl, setApiBaseOverride } from '../../core/config.js';
import {
  discoverServers,
  looksLocalHost,
  networkPrefixOf,
  parseNetworkHint,
} from '../../core/discovery.js';
import { request } from '../../core/http.js';
import { el, esc, on } from '../dom.js';
import { icon } from '../icons.js';
import { openSheet, type SheetHandle } from './sheet.js';
import { toastError, toastOk } from './toast.js';

/** Normalise what a person types into a base URL. Returns ``null`` when unusable. */
export function normaliseServerUrl(raw: string): string | null {
  const trimmed = raw.trim();
  if (!trimmed) return null;
  let url = trimmed.replace(/\/+$/, '');
  if (!/^https?:\/\//i.test(url)) {
    // A private LAN address or a ``*.local`` name is almost never serving TLS: typing
    // ``192.168.8.23:8000`` has to mean http, or the scheme guess alone fails the save.
    // Public names keep https, which is what they mean.
    const host = url.split(/[/?#]/)[0]!.split(':')[0]!;
    url = `${looksLocalHost(host) ? 'http' : 'https'}://${url}`;
  }
  if (!/\/api\/v1$/i.test(url)) url = `${url}/api/v1`;
  try {
    const parsed = new URL(url);
    if (!parsed.hostname) return null;
    return `${parsed.origin}${parsed.pathname.replace(/\/+$/, '')}`;
  } catch {
    return null;
  }
}

/**
 * Ask the server whether it is there. A 401 is a *good* answer: the host is alive.
 *
 * The request goes to ``url``, not to the base this device currently has configured --
 * testing the address the worker typed is the entire point of the button. (Before this it
 * probed the old base, so a wrong-but-reachable old server made a wrong new address save.)
 */
export async function probeServer(url: string): Promise<{ ok: boolean; status: number; message?: string }> {
  try {
    await request<unknown>({ path: '/branding', auth: false, timeoutMs: 8000, base: url });
    return { ok: true, status: 200 };
  } catch (e) {
    const err = e as { status?: number; offline?: boolean; message?: string };
    // A reachable server that refuses the request still proves the address is right —
    // and `/branding` is public, so anything but a network failure means it answered.
    if (err.status && err.status > 0) return { ok: true, status: err.status };
    return { ok: false, status: 0, message: err.message ?? 'No answer' };
  }
}

export function openServerSheet(opts: { onSaved?: (url: string) => void } = {}): SheetHandle {
  // A scan that outlives its sheet would keep probing a network nobody is watching, so the
  // controller is created here and closing the sheet stops the sweep.
  const scan = new AbortController();
  const handle = openSheet({
    title: 'Server address',
    onClose: () => scan.abort(),
    body: `
      <div class="hand-field-group">
        <label class="hand-field-label" for="server-url">API base URL</label>
        <input id="server-url" class="ui-field" type="url" inputmode="url"
               autocomplete="off" autocapitalize="off" spellcheck="false"
               placeholder="https://example.com/api/v1" />
        <span class="ui-hint" id="server-hint">Loading…</span>
      </div>
      <div class="hand-field-group">
        <label class="hand-field-label" for="server-net">Network to search</label>
        <div class="ui-row is-tight">
          <input id="server-net" class="ui-field is-flex" type="text" inputmode="decimal"
                 autocomplete="off" autocapitalize="off" spellcheck="false"
                 placeholder="192.168.8" aria-describedby="find-hint" />
          <button class="ui-btn" type="button" data-find>
            <span class="ui-btn__label">Find</span>
          </button>
        </div>
        <span class="ui-hint" id="find-hint">Searches this network for the server — useful
        when its address has changed.</span>
      </div>
      <div class="hand-alert is-info">
        ${icon('info', 16)}
        <p>The address must be reachable from this phone. A server on your own
        network usually needs <strong>http://</strong> and its LAN address.</p>
      </div>
    `,
    actions: [
      {
        label: 'Test and save',
        kind: 'primary',
        onClick: () => {
          void save();
        },
      },
      {
        label: 'Use default',
        kind: 'ghost',
        onClick: () => {
          void reset();
        },
      },
    ],
  });

  const input = handle.root.querySelector('#server-url') as HTMLInputElement;
  const hint = handle.root.querySelector('#server-hint') as HTMLElement;
  const netInput = handle.root.querySelector('#server-net') as HTMLInputElement;
  const findHint = handle.root.querySelector('#find-hint') as HTMLElement;
  const findButton = handle.root.querySelector('[data-find]') as HTMLButtonElement;

  void (async () => {
    let current = '';
    const override = await getApiBaseOverride();
    if (override) {
      current = override;
      input.value = override;
      hint.textContent = 'Overridden on this device.';
    } else {
      try {
        current = await resolveApiBaseUrl();
        input.value = current;
        hint.textContent = 'Built-in default for this app.';
      } catch (e) {
        hint.textContent = (e as Error).message;
      }
    }
    // The network field starts at the /24 the app already talks to -- the laptop moving
    // inside the same network is the ordinary case, and this is the only guess we can make
    // without asking Android for the phone's own subnet.
    netInput.value = networkPrefixOf(hostOf(current)) ?? '';
  })();

  on(input, 'input', () => {
    const normalised = normaliseServerUrl(input.value);
    hint.textContent = normalised
      ? `Will be saved as ${normalised}`
      : 'Enter an address such as https://example.com';
  });

  on(findButton, 'click', () => {
    void find();
  });

  /** The host of a base URL, or an empty string when it cannot be parsed. */
  function hostOf(url: string): string {
    try {
      return new URL(url).hostname;
    } catch {
      return '';
    }
  }

  /**
   * Search the typed network for a server that answers as this app, and save what answers.
   *
   * The address that is already configured is probed first: when only the laptop's address
   * changed, or when the saved override is still good, this returns in milliseconds and no
   * sweep runs at all.
   */
  async function find(): Promise<void> {
    const normalised = normaliseServerUrl(input.value);
    const prefix =
      parseNetworkHint(netInput.value) ?? (normalised ? networkPrefixOf(hostOf(normalised)) : null);
    if (!prefix) {
      findHint.textContent = 'Enter the network to search, such as 192.168.8.';
      toastError('Enter the network to search.');
      return;
    }

    let scheme: 'http' | 'https' = 'http';
    let port = 8000;
    if (normalised) {
      try {
        const parsed = new URL(normalised);
        scheme = parsed.protocol === 'https:' ? 'https' : 'http';
        port = parsed.port ? Number(parsed.port) : scheme === 'https' ? 443 : 80;
      } catch {
        // The defaults stand: a LAN server on its shipped port.
      }
    }

    const known: string[] = [];
    if (normalised) known.push(normalised);
    const override = await getApiBaseOverride();
    if (override) known.push(override);
    try {
      known.push(await resolveApiBaseUrl());
    } catch {
      // Nothing configured yet; the sweep is still worth a try.
    }

    netInput.value = prefix;
    const label = findButton.innerHTML;
    findButton.innerHTML = '<span class="hand-spinner"></span><span class="ui-btn__label">Searching…</span>';
    findButton.toggleAttribute('disabled', true);
    handle.setDisabled(0, true);
    findHint.textContent = `Searching ${prefix}.1-254 on port ${port}…`;
    const started = Date.now();
    try {
      const results = await discoverServers({
        prefix,
        port,
        scheme,
        known,
        signal: scan.signal,
        onProgress: (done, total) => {
          findHint.textContent = `Searching ${prefix}.1-254 on port ${port}… ${done}/${total}`;
        },
      });
      if (scan.signal.aborted) return;
      const server = results[0];
      if (!server) {
        findHint.textContent = `No server answered on ${prefix}.1-254 (port ${port}). Check the network, then try again.`;
        toastError('No server found on that network.');
        return;
      }
      input.value = server.base;
      hint.textContent = server.name ? `Found “${server.name}” and saved it.` : 'Found the server and saved it.';
      findHint.textContent = `Found ${server.base} in ${Math.max(1, Math.round((Date.now() - started) / 1000))} s.`;
      await setApiBaseOverride(server.base);
      toastOk(`Server found: ${server.base}`);
      opts.onSaved?.(server.base);
      handle.close();
    } finally {
      findButton.innerHTML = label;
      findButton.toggleAttribute('disabled', false);
      handle.setDisabled(0, false);
    }
  }

  async function save(): Promise<void> {
    const normalised = normaliseServerUrl(input.value);
    if (!normalised) {
      hint.textContent = 'That is not a usable address.';
      toastError('Enter a valid server address.');
      return;
    }
    handle.setBusy(0, true);
    handle.setDisabled(1, true);
    hint.textContent = 'Contacting the server…';
    const probe = await probeServer(normalised);
    handle.setBusy(0, false);
    handle.setDisabled(1, false);

    if (!probe.ok) {
      hint.textContent = probe.message ?? 'No answer from that address.';
      // The URL is NOT saved on a failed probe: every screen would fail at once, and the
      // worker would have no way back to a working address from a broken sign-in screen.
      toastError('That address did not answer. Nothing was changed.');
      return;
    }

    await setApiBaseOverride(normalised);
    hint.textContent = `Saved. Server answered (HTTP ${probe.status}).`;
    toastOk('Server address saved.');
    opts.onSaved?.(normalised);
    handle.close();
  }

  async function reset(): Promise<void> {
    await setApiBaseOverride(null);
    toastOk('Back to the built-in default.');
    handle.close();
    opts.onSaved?.('');
  }

  return handle;
}

/** The gear button the header renders. */
export function serverGearButton(onClick: () => void): HTMLButtonElement {
  const node = el(
    `<button class="ui-btn ui-btn-quiet is-icon" type="button" aria-label="Server settings">${icon('gear', 22)}</button>`,
  ) as HTMLButtonElement;
  on(node, 'click', onClick);
  return node;
}

export const __test__ = { normaliseServerUrl };
void esc;
