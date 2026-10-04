/**
 * What a *release* build is allowed to bake in as the API base URL.
 *
 * WHY THIS EXISTS
 * ---------------
 * ``VITE_API_BASE_URL`` is inlined into the bundle at build time, and the value that makes a
 * LAN deployment work at a desk - ``http://192.168.8.23:8000/api/v1``, or the emulator's
 * ``http://10.0.2.2:8000/api/v1`` - is exactly the value that cannot work in a shipped app:
 *
 *   * the WebView page is ``https://localhost`` (see capacitor.config.ts), so an http:// base
 *     is mixed content; and a release APK keeps Android's secure defaults, because the
 *     cleartext permission and the network security config live under ``android/app/src/debug``
 *     and are not merged into a release build. The request would be blocked twice over,
 *     long after the build looked green.
 *   * a private address (10/8, 172.16/12, 192.168/16, 169.254/16, loopback, a ``*.local``
 *     name) only exists on the network the developer was standing in, so the installed app
 *     reaches nothing at all - and the failure surfaces on a worker's phone, at a gate.
 *
 * So the release profile refuses both, at the moment of the build, with the offending value
 * in the message (see vite.config.ts). The debug profile keeps both - that is what the LAN and
 * emulator workflows need - and the build writes ``build-info.json`` recording which profile
 * produced the assets, so the Android release build can refuse a debug ``dist`` too (see
 * android/app/build.gradle).
 *
 * The app may still *discover* a LAN server at runtime - that is the "Find on network" flow -
 * and ``setApiBaseOverride()`` may point it at one. This module is only about what is compiled
 * in, never about what the worker chooses later on the phone.
 */

/** How an API base URL fares against the release rule. */
export type ApiBaseKind =
  /** Nothing configured: the shipped app is expected to find its server (or be pointed at one). */
  | 'none'
  /** https on a public host: shippable. */
  | 'secure'
  /** http://, a private-network host, or not a URL at all: refused by the release profile. */
  | 'insecure';

export interface ApiBaseAssessment {
  /** The value as configured, trimmed. Empty when nothing is set. */
  raw: string;
  /** Lower-cased hostname, without IPv6 brackets. Empty when there is no usable URL. */
  host: string;
  kind: ApiBaseKind;
  /** Empty when nothing is wrong; otherwise one clause naming the problem. */
  problem: string;
}

/** mDNS and other names that only resolve on a local network. */
const PRIVATE_NAME_SUFFIXES = ['.localhost', '.local', '.internal', '.home.arpa'];

function ipv4Parts(host: string): number[] | null {
  const match = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(host);
  if (!match) return null;
  const parts = match.slice(1, 5).map(Number);
  return parts.some((part) => part > 255) ? null : parts;
}

/**
 * The private IPv4 block this address sits in, or ``null`` for a public one.
 *
 * Ranges, not a regex per block, because the boundaries are the whole point: 172.16.0.0/12
 * ends at 172.31.255.255 (172.32 is public), and 100.64.0.0/10 - carrier-grade NAT, the space
 * a Tailscale address lands in - is just as unreachable from a phone on mobile data.
 */
function privateIpv4Reason(host: string): string | null {
  const parts = ipv4Parts(host);
  if (!parts) return null;
  const [a, b] = parts;
  if (a === 0) return '0.0.0.0/8 (this network)';
  if (a === 10) return '10.0.0.0/8 (private network)';
  if (a === 100 && b >= 64 && b <= 127) return '100.64.0.0/10 (carrier-grade NAT)';
  if (a === 127) return '127.0.0.0/8 (loopback)';
  if (a === 169 && b === 254) return '169.254.0.0/16 (link-local)';
  if (a === 172 && b >= 16 && b <= 31) return '172.16.0.0/12 (private network)';
  if (a === 192 && b === 168) return '192.168.0.0/16 (private network)';
  return null;
}

/**
 * Expand an IPv6 literal into its eight 16-bit groups, or ``null`` if it is not one.
 *
 * The URL parser has already canonicalised the spelling, so ``::1`` and ``::ffff:c0a8:105``
 * arrive in their ``::``-shortened hex form rather than the dotted-quad the developer typed.
 * Working in groups - not off the leading text - is what lets the two spellings of one address
 * be judged the same way.
 */
function ipv6Groups(bare: string): number[] | null {
  const halves = bare.split('::');
  if (halves.length > 2) return null;
  const head = halves[0] ? halves[0].split(':') : [];
  const tail = halves.length === 2 && halves[1] ? halves[1].split(':') : [];
  const filler = new Array(Math.max(0, 8 - head.length - tail.length)).fill('0');
  const groups = [...head, ...filler, ...tail];
  if (groups.length !== 8) return null;
  const numbers = groups.map((g) => (/^[0-9a-f]{1,4}$/.test(g) ? Number.parseInt(g, 16) : NaN));
  return numbers.some(Number.isNaN) ? null : numbers;
}

/**
 * The private IPv6 block this address sits in, or ``null``.
 *
 * ``hostname`` keeps the brackets, and an IPv4-mapped literal (``::ffff:192.168.1.5``, what a
 * dual-stack socket reports for a v4 peer, canonicalised by the parser to ``::ffff:c0a8:105``)
 * is unwrapped to the v4 rule so the two spellings of the same address cannot disagree.
 */
function privateIpv6Reason(host: string): string | null {
  const bare = host.replace(/^\[/, '').replace(/\]$/, '').toLowerCase();
  if (!bare.includes(':')) return null;
  if (bare === '::1') return '::1 (loopback)';
  if (bare === '::') return ':: (unspecified)';
  const groups = ipv6Groups(bare);
  if (!groups) return null;

  const leadingZeros = groups.slice(0, 6).every((g) => g === 0);
  const mapped = groups.slice(0, 5).every((g) => g === 0) && groups[5] === 0xffff;
  if (leadingZeros || mapped) {
    const dotted = [
      (groups[6] >> 8) & 0xff, groups[6] & 0xff, (groups[7] >> 8) & 0xff, groups[7] & 0xff,
    ].join('.');
    const reason = privateIpv4Reason(dotted);
    if (reason) return reason;
  }

  const first = groups[0];
  if ((first & 0xfe00) === 0xfc00) return 'fc00::/7 (unique local)';
  if ((first & 0xffc0) === 0xfe80) return 'fe80::/10 (link-local)';
  return null;
}

function privateHostReason(host: string): string | null {
  if (host === 'localhost') return 'localhost (loopback)';
  for (const suffix of PRIVATE_NAME_SUFFIXES) {
    if (host.endsWith(suffix)) return `${suffix} (local-network name)`;
  }
  return privateIpv4Reason(host) ?? privateIpv6Reason(host);
}

/**
 * Judge one configured value.
 *
 * A value with no scheme is read the way the app reads it (``config.ts`` prepends ``https://``),
 * so ``api.example.com`` passes and ``192.168.8.23`` fails for being private rather than for
 * being malformed. A value that is neither, like ``ftp://host`` or a stray space in the middle,
 * fails as unparseable - it was never going to produce a working base.
 */
export function assessApiBase(raw: string | null | undefined): ApiBaseAssessment {
  const trimmed = (raw ?? '').trim();
  if (!trimmed) return { raw: '', host: '', kind: 'none', problem: '' };

  const schemeMatch = /^([a-z][a-z0-9+.-]*):\/\//i.exec(trimmed);
  const scheme = schemeMatch ? `${schemeMatch[1].toLowerCase()}:` : 'https:';
  const candidate = schemeMatch ? trimmed : `https://${trimmed}`;
  let url: URL;
  try {
    url = new URL(candidate);
  } catch {
    return {
      raw: trimmed,
      host: '',
      kind: 'insecure',
      problem: 'not a usable URL',
    };
  }
  const host = url.hostname.toLowerCase();

  // http:// is the one that matters in practice, but nothing else belongs here either: the
  // app only ever prepends https, and the WebView's own page is https.
  const schemeProblem =
    scheme === 'https:' ? '' : scheme === 'http:' ? 'plaintext http://' : `${scheme} (not https)`;
  const hostReason = privateHostReason(host);
  if (schemeProblem || hostReason) {
    // Both are named when both apply - "http:// on 192.168.0.0/16" is one address with two
    // independent reasons it cannot ship, and fixing only one of them fixes nothing.
    const problem = [schemeProblem, hostReason].filter(Boolean).join(' on ');
    return { raw: trimmed, host, kind: 'insecure', problem };
  }
  return { raw: trimmed, host, kind: 'secure', problem: '' };
}

/** The loud refusal a release build prints instead of producing an unshippable bundle. */
export function releaseGuardMessage(assessment: ApiBaseAssessment): string {
  return [
    'Refusing to build the RELEASE profile with an API base URL that can never work in a',
    'shipped app.',
    '',
    `    VITE_API_BASE_URL = ${assessment.raw}`,
    `    problem           = ${assessment.problem}`,
    '',
    'Why this is fatal rather than a warning: a release APK keeps Android\'s secure defaults',
    "(usesCleartextTraffic is off, and the network security config lives under src/debug, which",
    'a release build does not merge), so an http:// punch could never be uploaded. The WebView',
    'page itself is https://localhost, which makes an http:// base mixed content on top of',
    'that. And a private address is not a security preference - it is a name that resolves to',
    'nothing on the worker\'s phone.',
    '',
    'Either point the release build at the public deployment:',
    '    VITE_API_BASE_URL=https://<public-host>/api/v1 npm run build:release',
    'or, to test against a LAN or emulator server, build the debug profile:',
    '    npm run build        (and install the debug APK)',
  ].join('\n');
}
