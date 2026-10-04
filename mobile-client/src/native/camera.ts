/**
 * Selfie capture — ``getUserMedia`` in the WebView, downscaled to the server's policy.
 *
 * WHY THIS IS NOT THE CAPACITOR CAMERA PLUGIN
 * -------------------------------------------
 * The server does not accept "a photo": it accepts a frame that its own detector and
 * embedder can measure. ``uploads.face_frame`` downsizes every upload to
 * ``face_frame_max_px`` (1280 by default) and the model pipeline is calibrated against
 * that crop — so a capture path that hands over a 12 MP camera JPEG produces a *different*
 * crop than the one the template was enrolled with, and the match score drifts by more
 * than the decision margin. ``face_frame_max_edge_px`` (2048) and
 * ``face_frame_max_pixels`` (4 MP) are the ingestion boundary: above them the upload is
 * refused outright with a 422, so the phone has to send something inside it.
 *
 * The web client already had this pipeline (``frontend/capture.js``) and this is the same
 * one: ``getUserMedia`` with ``facingMode: "user"``, a canvas re-encode at **maximum**
 * quality, and the size bought with resolution rather than JPEG compression — lowering the
 * quality instead would soften the very crop the embedding is measured from.
 *
 * RESOURCE DISCIPLINE
 * -------------------
 * A held camera is a lit green dot and a battery drain, and on Android a second
 * ``getUserMedia`` call while the first stream is live can fail outright. So every track is
 * stopped on close, and the app's background/pause event releases the stream too — the
 * capture overlay is not the only way this object can go out of scope.
 */

import { onLifecycle } from './lifecycle.js';

/** The defaults, mirroring ``uploads.policy()`` and ``frontend/capture.js``. */
export const PHOTO_POLICY = {
  max_bytes: 5 * 1024 * 1024,
  accepted: ['image/jpeg', 'image/png', 'image/webp'] as const,
  face_frame_max_pixels: 4_000_000,
  face_frame_max_edge_px: 2048,
} as const;

export type PhotoPolicy = {
  max_bytes: number;
  accepted: readonly string[];
  face_frame_max_pixels: number;
  face_frame_max_edge_px: number;
};

export interface CapturedPhoto {
  blob: Blob;
  width: number;
  height: number;
  /** True when the frame had to be reduced to fit the policy. */
  resized: boolean;
  bytes: number;
}

export class CameraError extends Error {
  code: string;
  constructor(code: string, message: string) {
    super(message);
    this.name = 'CameraError';
    this.code = code;
  }
}

/** The size a frame of ``w``x``h`` must be sent at, and whether that is smaller. */
export function fitToLimits(
  w: number,
  h: number,
  limits: { face_frame_max_pixels: number; face_frame_max_edge_px: number } = PHOTO_POLICY,
): { width: number; height: number; scale: number } {
  const width = Math.max(1, Math.floor(w));
  const height = Math.max(1, Math.floor(h));
  // Both rules apply and the smaller scale wins: a 3000x1200 frame fails the edge rule
  // while sitting under the pixel limit, and a 2048x2048 one fails the pixel rule while
  // sitting exactly on the edge.
  const edgeScale = Math.min(
    1,
    limits.face_frame_max_edge_px / Math.max(width, height),
  );
  const pixelScale = Math.min(
    1,
    Math.sqrt(limits.face_frame_max_pixels / (width * height)),
  );
  const scale = Math.min(edgeScale, pixelScale);
  return {
    width: Math.max(1, Math.round(width * scale)),
    height: Math.max(1, Math.round(height * scale)),
    scale,
  };
}

function canvasToBlob(canvas: HTMLCanvasElement, type: string, quality: number): Promise<Blob> {
  return new Promise((resolve, reject) => {
    canvas.toBlob(
      (blob) => (blob ? resolve(blob) : reject(new CameraError('encode_failed', 'The frame could not be encoded.'))),
      type,
      quality,
    );
  });
}

/**
 * Reduce an image to the policy's boundary.
 *
 * Encoded at quality ``1.0``: the budget being enforced is *pixels*, so the size is bought
 * with resolution and nothing else. Returns the original image when it already fits, and
 * the original rather than nothing when the decoder is unavailable — the server is the
 * authority on what it will accept, and its refusal names the problem.
 */
export async function shrinkPhoto(
  image: Blob | HTMLImageElement | ImageBitmap,
  limits: PhotoPolicy = PHOTO_POLICY,
): Promise<CapturedPhoto> {
  const source =
    typeof ImageBitmap !== 'undefined' && image instanceof ImageBitmap
      ? { bitmap: image, width: image.width, height: image.height, release: true }
      : image instanceof HTMLImageElement
        ? { element: image, width: image.naturalWidth || image.width, height: image.naturalHeight || image.height, release: false }
        : null;

  if (!source) {
    // A Blob: decode it, then treat it as an element.
    const url = URL.createObjectURL(image as Blob);
    try {
      const element = await new Promise<HTMLImageElement>((resolve, reject) => {
        const el = new Image();
        el.onload = () => resolve(el);
        el.onerror = () => reject(new CameraError('decode_failed', 'The image could not be read.'));
        el.src = url;
      });
      return await shrinkPhoto(element, limits);
    } finally {
      URL.revokeObjectURL(url);
    }
  }

  const fit = fitToLimits(source.width, source.height, limits);
  const alreadyFits = fit.scale >= 1;
  const originalBytes =
    image instanceof Blob ? image.size : 0;

  if (alreadyFits && originalBytes > 0 && originalBytes <= limits.max_bytes) {
    return {
      blob: image as Blob,
      width: source.width,
      height: source.height,
      resized: false,
      bytes: originalBytes,
    };
  }

  const canvas = document.createElement('canvas');
  canvas.width = fit.width;
  canvas.height = fit.height;
  const ctx = canvas.getContext('2d');
  if (!ctx) {
    if (image instanceof Blob) {
      return { blob: image, width: source.width, height: source.height, resized: false, bytes: image.size };
    }
    throw new CameraError('encode_failed', 'This device cannot encode the frame.');
  }
  ctx.imageSmoothingEnabled = true;
  ctx.imageSmoothingQuality = 'high';
  ctx.drawImage(
    (source.bitmap ?? source.element) as CanvasImageSource,
    0,
    0,
    fit.width,
    fit.height,
  );

  let blob = await canvasToBlob(canvas, 'image/jpeg', 1);
  // Only if the *pixel* budget was already satisfied does the byte ceiling get enforced
  // with quality — the same order the server's own chain uses, so the frame that arrives
  // is the frame that was measured.
  if (blob.size > limits.max_bytes) {
    let quality = 0.92;
    while (blob.size > limits.max_bytes && quality > 0.4) {
      quality -= 0.1;
      blob = await canvasToBlob(canvas, 'image/jpeg', quality);
    }
  }

  if (source.release && source.bitmap) source.bitmap.close();

  return { blob, width: fit.width, height: fit.height, resized: !alreadyFits || blob.size < originalBytes, bytes: blob.size };
}

export interface CameraSession {
  video: HTMLVideoElement;
  stop: () => void;
  capture: () => Promise<CapturedPhoto>;
}

/**
 * Start the front camera and attach it to ``video``.
 *
 * The stream is owned by the returned session; ``stop()`` is idempotent and is also wired
 * to the app's pause event, so a worker who backgrounds the app mid-capture does not leave
 * the camera lit.
 */
export async function startCamera(
  video: HTMLVideoElement,
  opts: { idealWidth?: number; limits?: PhotoPolicy } = {},
): Promise<CameraSession> {
  if (typeof navigator === 'undefined' || !navigator.mediaDevices?.getUserMedia) {
    throw new CameraError(
      'unsupported',
      'This device does not expose a camera to the app. Use the file picker instead.',
    );
  }

  const limits = opts.limits ?? PHOTO_POLICY;
  let stream: MediaStream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      video: {
        facingMode: 'user',
        width: { ideal: opts.idealWidth ?? 720 },
      },
      audio: false,
    });
  } catch (e) {
    const err = e as { name?: string; message?: string };
    if (err.name === 'NotAllowedError' || err.name === 'SecurityError') {
      throw new CameraError('denied', 'Camera permission was refused. Allow it in Settings and try again.');
    }
    if (err.name === 'NotFoundError' || err.name === 'OverconstrainedError') {
      throw new CameraError('no_camera', 'No front camera is available on this device.');
    }
    throw new CameraError('failed', err.message ?? 'The camera could not be started.');
  }

  video.srcObject = stream;
  video.setAttribute('playsinline', 'true');
  video.muted = true;
  try {
    await video.play();
  } catch {
    // Autoplay refusal is not fatal: the first frame still renders once the user taps.
  }

  let stopped = false;
  const stop = (): void => {
    if (stopped) return;
    stopped = true;
    for (const track of stream.getTracks()) {
      try {
        track.stop();
      } catch {
        // A track that is already dead cannot be stopped twice.
      }
    }
    if (video.srcObject === stream) video.srcObject = null;
  };

  // The app going to the background releases the camera even if the overlay is still on
  // screen: a held track is what makes the *next* getUserMedia call fail.
  const off = onLifecycle('pause', () => stop());

  const capture = async (): Promise<CapturedPhoto> => {
    const w = video.videoWidth;
    const h = video.videoHeight;
    if (!w || !h) {
      throw new CameraError('not_ready', 'The camera is still starting. Try again in a moment.');
    }
    const canvas = document.createElement('canvas');
    canvas.width = w;
    canvas.height = h;
    const ctx = canvas.getContext('2d');
    if (!ctx) throw new CameraError('encode_failed', 'This device cannot encode the frame.');
    ctx.drawImage(video, 0, 0, w, h);

    const frame = await canvasToBlob(canvas, 'image/jpeg', 0.92);
    const shrunk = await shrinkPhoto(frame, limits);
    stop();
    off();
    return shrunk;
  };

  return {
    video,
    stop: () => {
      stop();
      off();
    },
    capture,
  };
}

/**
 * Pick a photo from the gallery, through the same policy chain.
 *
 * This is the fallback for a device whose WebView will not grant ``getUserMedia`` — an
 * Android WebView only satisfies it when the *app* already holds the OS camera permission,
 * so a deployment without the permission in its manifest has no other route.
 */
export async function pickPhoto(limits: PhotoPolicy = PHOTO_POLICY): Promise<CapturedPhoto> {
  const file = await new Promise<File>((resolve, reject) => {
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = limits.accepted.join(',');
    input.style.display = 'none';
    input.addEventListener('change', () => {
      const chosen = input.files?.[0];
      input.remove();
      if (chosen) resolve(chosen);
      else reject(new CameraError('cancelled', 'No photo was chosen.'));
    });
    // Android fires no event when the picker is dismissed, so the node is left in the
    // document for the browser to reap rather than leaking a listener per attempt.
    document.body.appendChild(input);
    input.click();
  });

  if (!limits.accepted.includes(file.type)) {
    throw new CameraError('bad_type', `That file is a ${file.type || 'unknown type'}; send a JPEG, PNG or WebP.`);
  }
  return shrinkPhoto(file, limits);
}

export const __test__ = { fitToLimits };
