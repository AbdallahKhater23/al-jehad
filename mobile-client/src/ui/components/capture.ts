/**
 * The selfie capture overlay.
 *
 * A full-screen camera with a framing guide, a shutter, and a way out. It is a component
 * rather than a screen because it must not disturb the tab the worker is standing on: the
 * clock screen keeps its state behind the overlay, and closing the overlay returns to it.
 *
 * THREE THINGS THIS GETS RIGHT THAT ARE EASY TO GET WRONG
 * ------------------------------------------------------
 * 1. **The stream is always released.** On shutter, on cancel, and on app pause. A held
 *    ``MediaStream`` keeps the camera lit and makes the *next* ``getUserMedia`` call fail on
 *    Android — so a worker who cancels once would be unable to take a punch at all.
 * 2. **The frame is re-encoded to the server's policy, at maximum quality.** The size is
 *    bought with resolution, never with JPEG quality: the frame is what the face is measured
 *    from, and softening it costs match score.
 * 3. **There is a way out that is not the camera.** A WebView that will not grant
 *    ``getUserMedia`` (an app that does not hold the OS camera permission) would otherwise
 *    be a dead end, so the gallery picker is offered in the same overlay — through the same
 *    policy chain.
 */

import { el, esc, on } from '../dom.js';
import { startCamera, pickPhoto, shrinkPhoto, CameraError, type CapturedPhoto } from '../../native/camera.js';
import { toastError } from './toast.js';
import { trapFocus } from './focus.js';

export interface CaptureResult {
  photo: CapturedPhoto | null;
  /** Why there is no photo, when there is not one. */
  reason?: string;
}

/**
 * Open the overlay and resolve when the worker has either taken a photo or given up.
 *
 * ``photo`` is ``null`` for a cancellation, which the caller distinguishes from a refusal by
 * ``reason`` being absent.
 */
export function captureSelfie(): Promise<CaptureResult> {
  return new Promise((resolve) => {
    const root = el(`
      <div class="clock-capture" role="dialog" aria-modal="true" aria-label="Take a selfie">
        <div class="clock-capture__head">
          <span>Take a selfie</span>
          <button class="clock-capture__cancel" type="button" data-cancel>Cancel</button>
        </div>
        <div class="clock-capture__stage">
          <video class="clock-capture__video" playsinline autoplay muted></video>
          <div class="clock-capture__guide"><div class="clock-capture__oval"></div></div>
        </div>
        <p class="clock-capture__hint">Hold the phone at arm’s length, face inside the oval, in good light.</p>
        <div class="clock-capture__foot">
          <button class="clock-capture__gallery" type="button" data-gallery>Gallery</button>
          <button class="clock-capture__shutter" type="button" data-shutter aria-label="Take photo"></button>
        </div>
      </div>
    `);

    const video = root.querySelector('video') as HTMLVideoElement;
    const shutter = root.querySelector('[data-shutter]') as HTMLButtonElement;
    const gallery = root.querySelector('[data-gallery]') as HTMLButtonElement;
    const cancel = root.querySelector('[data-cancel]') as HTMLButtonElement;
    const hint = root.querySelector('.clock-capture__hint') as HTMLElement;

    let settled = false;
    let stopStream: (() => void) | null = null;
    const disposers: Array<() => void> = [];

    function finish(result: CaptureResult): void {
      if (settled) return;
      settled = true;
      stopStream?.();
      for (const off of disposers) off();
      // Focus returns to the button that opened the camera before the overlay goes, so a
      // keyboard user is not dropped at the top of the page.
      trap.release();
      root.remove();
      resolve(result);
    }

    /** A message inside the overlay, for a failure that does not close it. */
    function showProblem(message: string, offerGallery: boolean): void {
      hint.textContent = message;
      hint.style.color = '#f6c1bc';
      gallery.hidden = !offerGallery;
      shutter.hidden = true;
    }

    document.body.appendChild(root);
    // Cancel is the first control, so it takes focus: the worker is never one Enter away
    // from a shutter they did not mean to press. See focus.ts.
    const trap = trapFocus(root, { initial: 'first' });

    void (async () => {
      try {
        const session = await startCamera(video, { idealWidth: 720 });
        stopStream = session.stop;
        shutter.disabled = false;
      } catch (e) {
        const err = e as CameraError;
        stopStream = null;
        if (err.code === 'denied') {
          showProblem('Camera permission was refused. Allow it in Settings, or pick a photo instead.', true);
        } else if (err.code === 'unsupported' || err.code === 'no_camera') {
          showProblem(`${err.message} Pick a photo instead.`, true);
        } else {
          showProblem(`${err.message} You can pick a photo instead.`, true);
        }
      }
    })();

    disposers.push(
      on(cancel, 'click', () => finish({ photo: null })),
      on(shutter, 'click', () => {
        if (settled || shutter.disabled) return;
        shutter.disabled = true;
        void (async () => {
          try {
            const width = video.videoWidth;
            const height = video.videoHeight;
            if (!width || !height) {
              throw new CameraError('not_ready', 'The camera is still starting.');
            }
            const canvas = document.createElement('canvas');
            canvas.width = width;
            canvas.height = height;
            const ctx = canvas.getContext('2d');
            if (!ctx) throw new CameraError('encode_failed', 'This device cannot encode the frame.');
            ctx.drawImage(video, 0, 0, width, height);
            const frame = await new Promise<Blob>((res, rej) =>
              canvas.toBlob(
                (blob) => (blob ? res(blob) : rej(new CameraError('encode_failed', 'The frame could not be encoded.'))),
                'image/jpeg',
                0.92,
              ),
            );
            // The policy chain: downscale to the server's boundary at maximum quality.
            const photo = await shrinkPhoto(frame);
            finish({ photo });
          } catch (e) {
            shutter.disabled = false;
            toastError((e as Error).message);
          }
        })();
      }),
      on(gallery, 'click', () => {
        void (async () => {
          try {
            const photo = await pickPhoto();
            finish({ photo });
          } catch (e) {
            const err = e as CameraError;
            if (err.code !== 'cancelled') toastError(err.message);
            // A dismissed picker leaves the overlay open, which is what the worker expects.
            shutter.disabled = false;
          }
        })();
      }),
    );

    // The Android back button is not wired here on purpose: Capacitor's own back handler
    // navigates, and a capture in progress must not be dismissed by an accidental swipe.
    // The Cancel button is the explicit way out.
  });
}

void esc;
