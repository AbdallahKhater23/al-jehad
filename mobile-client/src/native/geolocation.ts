import { Geolocation, type Position } from '@capacitor/geolocation';
import { roundCoord, fmtCoord, fmtAccuracy } from '../offline/signing.js';

export interface CapturedLocation {
  lat: number | null;
  lon: number | null;
  accuracy: number | null;
  /** "lat,lon" at signed precision, or null. */
  coords: string | null;
  /** Formatted strings matching the signature (%.6f / %.1f). */
  formatted: { lat: string; lon: string; accuracy: string };
  timestamp: number;
}

export async function captureLocation(timeoutMs = 10_000): Promise<CapturedLocation> {
  let pos: Position;
  try {
    pos = await Geolocation.getCurrentPosition({
      enableHighAccuracy: true,
      timeout: timeoutMs,
      maximumAge: 0,
    });
  } catch (e) {
    throw new Error(`Location unavailable: ${String((e as Error).message ?? e)}`);
  }

  const rawLat = pos.coords.latitude;
  const rawLon = pos.coords.longitude;
  const rawAcc = pos.coords.accuracy;

  const lat = roundCoord(rawLat);
  const lon = roundCoord(rawLon);
  const accuracy = Number.isFinite(rawAcc) ? Math.round(rawAcc * 10) / 10 : null;

  const coords = lat !== null && lon !== null ? `${fmtCoord(lat)},${fmtCoord(lon)}` : null;

  return {
    lat,
    lon,
    accuracy,
    coords,
    formatted: { lat: fmtCoord(lat), lon: fmtCoord(lon), accuracy: fmtAccuracy(accuracy) },
    timestamp: pos.timestamp ?? Date.now(),
  };
}

export async function checkLocationPermission(): Promise<'granted' | 'denied' | 'prompt'> {
  try {
    const r = await Geolocation.checkPermissions();
    const v = (r as unknown as { location?: string; coarseLocation?: string }).location
      ?? (r as unknown as { location?: string }).location
      ?? 'prompt';
    if (v === 'granted') return 'granted';
    if (v === 'denied') return 'denied';
    return 'prompt';
  } catch {
    return 'prompt';
  }
}

export async function requestLocationPermission(): Promise<'granted' | 'denied'> {
  try {
    const r = await Geolocation.requestPermissions();
    const v = (r as unknown as { location?: string }).location ?? 'prompt';
    return v === 'granted' ? 'granted' : 'denied';
  } catch {
    return 'denied';
  }
}
