/**
 * Canonical offline signing — byte-for-byte port of backend/offline_sync.py
 * and frontend/offline_queue.js OFFLINE_CRYPTO.
 */

export const SIGNATURE_VERSION = 1;

/**
 * Python's fixed-point formatting, i.e. round-half-EVEN on the *exact* value.
 *
 * WHY THIS IS NOT `(n * factor).toFixed(decimals)`
 * -----------------------------------------------
 * Two traps, and both are reachable with real GPS readings:
 *
 * 1. **Multiplying first invents ties.** ``0.05 * 10`` evaluates to exactly ``0.5`` in
 *    binary floating point, but the true value of the double ``0.05`` is
 *    ``0.05000000000000000277…`` — strictly *above* the midpoint. Python's ``f"{0.05:.1f}"``
 *    therefore answers ``"0.1"`` while a multiply-then-round implementation sees a tie and
 *    answers ``"0.0"``. The same holds for ``1.0000015`` and ``-2.0000005``: the product
 *    lands on the midpoint even though the value never was one.
 * 2. **``toFixed`` breaks ties the wrong way.** ECMAScript resolves a tie toward the larger
 *    ``n``, which is round-half-*up*; Python uses half-*even*. ``(8.25).toFixed(1)`` is
 *    ``"8.3"``, ``f"{8.25:.1f}"`` is ``"8.2"``.
 *
 * A disagreement here is a punch the phone can sign but the server can never verify, because
 * the server re-formats every value before checking the signature. So the rounding is done on
 * the double's *exact* rational value, recovered from its IEEE-754 fields, with BigInt — no
 * intermediate floating point, no invented ties, and half-even applied only to a real tie.
 */
function fixed(value: unknown, decimals: number): string {
  if (value === null || value === undefined || value === '') return '';
  const n = Number(value);
  if (!Number.isFinite(n)) return '';

  // Negative zero keeps its sign, exactly as Python does: f"{-0.0000004:.6f}" is "-0.000000".
  const negative = n < 0 || Object.is(n, -0);
  const scaled = roundHalfEvenScaled(Math.abs(n), decimals);
  return formatScaled(scaled, decimals, negative);
}

/** The double's exact value as ``mantissa * 2^exponent``, read from its own bits. */
function exactParts(abs: number): { mantissa: bigint; exponent: number } {
  const view = new DataView(new ArrayBuffer(8));
  view.setFloat64(0, abs, true);
  const bits = view.getBigUint64(0, true);
  const exponentField = Number((bits >> 52n) & 0x7ffn);
  const fraction = bits & 0xfffffffffffffn;
  if (exponentField === 0) {
    // Subnormal (and zero): no implicit leading bit.
    return { mantissa: fraction, exponent: -1074 };
  }
  return { mantissa: fraction | (1n << 52n), exponent: exponentField - 1075 };
}

/** ``round_half_even(abs * 10^decimals)`` as an integer, computed exactly. */
function roundHalfEvenScaled(abs: number, decimals: number): bigint {
  const { mantissa, exponent } = exactParts(abs);
  if (mantissa === 0n) return 0n;

  // value = numerator / 10^scale, exactly. A negative binary exponent becomes a decimal
  // denominator because 2^-k == 5^k / 10^k.
  let numerator: bigint;
  let scale: number;
  if (exponent < 0) {
    scale = -exponent;
    numerator = mantissa * 5n ** BigInt(scale);
  } else {
    scale = 0;
    numerator = mantissa << BigInt(exponent);
  }

  const shift = scale - decimals;
  if (shift <= 0) {
    // The value already terminates at or before ``decimals`` places: exact, no rounding.
    return numerator * 10n ** BigInt(-shift);
  }

  const divisor = 10n ** BigInt(shift);
  const quotient = numerator / divisor;
  const remainder = numerator % divisor;
  const twice = remainder * 2n;
  if (twice > divisor) return quotient + 1n;
  if (twice < divisor) return quotient;
  return quotient % 2n === 0n ? quotient : quotient + 1n; // a real tie: half to even
}

/** Render an integer scaled by ``10^decimals`` as a fixed-point string. */
function formatScaled(scaled: bigint, decimals: number, negative: boolean): string {
  const sign = negative ? '-' : '';
  const digits = scaled.toString();
  if (decimals === 0) return sign + digits;
  const padded = digits.padStart(decimals + 1, '0');
  const cut = padded.length - decimals;
  return `${sign}${padded.slice(0, cut)}.${padded.slice(cut)}`;
}

export function fmtCoord(value: unknown): string {
  return fixed(value, 6);
}

export function fmtAccuracy(value: unknown): string {
  return fixed(value, 1);
}

export function roundCoord(value: unknown): number | null {
  if (value === null || value === undefined || value === '') return null;
  const n = Number(value);
  if (!Number.isFinite(n)) return null;
  return Math.round(n * 1e6) / 1e6;
}

export interface CanonicalFields {
  device_id: string;
  worker_id: string;
  action: string;
  client_punch_id: string;
  nonce: string;
  anchor_id: string;
  effective_timestamp: string;
  lat: number | null;
  lon: number | null;
  accuracy: number | null;
  photo_sha256: string | null;
}

export function canonicalPunch(fields: CanonicalFields): string {
  return [
    `v${SIGNATURE_VERSION}`,
    fields.device_id,
    fields.worker_id,
    fields.action,
    fields.client_punch_id,
    fields.nonce,
    fields.anchor_id,
    fields.effective_timestamp,
    fmtCoord(fields.lat),
    fmtCoord(fields.lon),
    fmtAccuracy(fields.accuracy),
    fields.photo_sha256 || '',
  ].join('\n');
}

export function b64urlToBytes(token: string): Uint8Array<ArrayBuffer> {
  const normalized = String(token).trim().replace(/-/g, '+').replace(/_/g, '/');
  const padded = normalized + '='.repeat((4 - (normalized.length % 4)) % 4);
  const binary = atob(padded);
  const buffer = new ArrayBuffer(binary.length);
  const out = new Uint8Array(buffer);
  for (let i = 0; i < binary.length; i++) out[i] = binary.charCodeAt(i);
  return out;
}

/**
 * Normalise any accepted input to a plain ``ArrayBuffer``.
 *
 * ``crypto.subtle`` takes ``BufferSource``, which a ``Uint8Array`` backed by a
 * ``SharedArrayBuffer`` does not satisfy — and TypeScript models every view as potentially
 * shared. Copying into a fresh buffer both satisfies the type and removes the aliasing the
 * subtle API refuses.
 */
async function toArrayBuffer(input: Blob | ArrayBuffer | Uint8Array | string): Promise<ArrayBuffer> {
  if (typeof Blob !== 'undefined' && input instanceof Blob) return await input.arrayBuffer();
  if (input instanceof ArrayBuffer) return input;
  if (input instanceof Uint8Array) {
    const copy = new ArrayBuffer(input.byteLength);
    new Uint8Array(copy).set(input);
    return copy;
  }
  const encoded = new TextEncoder().encode(String(input));
  const copy = new ArrayBuffer(encoded.byteLength);
  new Uint8Array(copy).set(encoded);
  return copy;
}

export function bytesToHex(bytes: Uint8Array): string {
  let hex = '';
  for (let i = 0; i < bytes.length; i++) hex += bytes[i].toString(16).padStart(2, '0');
  return hex;
}

export async function sha256Hex(input: Blob | ArrayBuffer | Uint8Array | string): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', await toArrayBuffer(input));
  return bytesToHex(new Uint8Array(digest));
}

export async function signPunch(keyToken: string, fields: CanonicalFields): Promise<string> {
  const raw = b64urlToBytes(keyToken);
  const key = await crypto.subtle.importKey('raw', raw, { name: 'HMAC', hash: 'SHA-256' }, false, [
    'sign',
  ]);
  const sig = await crypto.subtle.sign('HMAC', key, new TextEncoder().encode(canonicalPunch(fields)));
  return bytesToHex(new Uint8Array(sig));
}

export function parseTs(value: string): number {
  const m = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})/.exec(String(value ?? ''));
  if (!m) return NaN;
  return Date.UTC(
    Number(m[1]),
    Number(m[2]) - 1,
    Number(m[3]),
    Number(m[4]),
    Number(m[5]),
    Number(m[6]),
  );
}

export function formatTs(epochMs: number): string {
  const d = new Date(epochMs);
  if (Number.isNaN(d.getTime())) return '';
  const pad = (n: number) => String(n).padStart(2, '0');
  return (
    `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ` +
    `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}`
  );
}

export function monotonicNow(): number {
  if (typeof performance !== 'undefined' && typeof performance.now === 'function' && typeof (performance as unknown as { timeOrigin?: number }).timeOrigin === 'number') {
    return (performance as unknown as { timeOrigin: number }).timeOrigin + performance.now();
  }
  return Date.now();
}

export function randomHex(bytes: number): string {
  const buf = new Uint8Array(new ArrayBuffer(bytes));
  crypto.getRandomValues(buf);
  return bytesToHex(buf);
}

export function newPunchId(): string {
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  return randomHex(16);
}
