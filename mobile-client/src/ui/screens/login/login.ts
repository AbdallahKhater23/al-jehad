/**
 * Sign-in.
 *
 * The form is the whole screen: three fields, one button. The header carries the
 * theme toggle and the language picker and nothing else -- there is no server to
 * point this app at, because the address is baked into the build and falls back to
 * the public deployment (``core/config.ts``).
 *
 * Every sentence here comes from ``core/strings.ts``, so the screen a worker signs
 * in on is drawn in the language they chose -- which is the one screen where the
 * picker has to be reachable before anybody is signed in.
 */

import { request, persistSessionFromLoginResponse } from '../../../core/http.js';
import { store } from '../../../core/store.js';
import { t } from '../../../core/strings.js';
import type { Screen, ScreenContext } from '../../shell.js';
import { esc, on } from '../../dom.js';
import { icon } from '../../icons.js';
import { toastError, toastOk } from '../../components/toast.js';
import {
  loginRequestBody,
  validateCredentials,
} from '../../credentials.js';

interface LoginResponse {
  status?: string;
  user?: { id: string; name: string; role: string; email?: string; phone?: string };
  approval_status?: string;
  welcome?: string | null;
  access_token?: string;
  token?: string;
  expires_at?: string;
}

export function createLoginScreen(onSignedIn: () => void): Screen {
  return {
    fullHeight: true,
    // The frame names its main landmark from this, so it is the screen's own sentence
    // rather than a second copy of the heading.
    title: () => t('auth.signIn'),
    subtitle: () => t('app.name'),
    mount(host: HTMLElement, ctx: ScreenContext): () => void {
      host.innerHTML = `
        <div class="auth">
          <div class="auth__brand">
            <div class="auth__mark">${icon('shield', 30, 'auth__mark-icon')}</div>
            <h1 class="auth__title">${esc(t('app.name'))}</h1>
            <p class="auth__subtitle">${esc(t('auth.subtitle'))}</p>
          </div>

          <form class="auth__form" id="login-form" novalidate>
            <div class="hand-field-group">
              <label class="hand-field-label" for="login-id">${esc(t('auth.userId'))}</label>
              <input class="ui-field" id="login-id" name="user_id" type="text"
                     inputmode="numeric" autocomplete="username" autocapitalize="off"
                     spellcheck="false" enterkeyhint="next" />
              <span class="ui-field-error" data-error="user_id" hidden></span>
            </div>

            <div class="hand-field-group">
              <label class="hand-field-label" for="login-identity">${esc(t('auth.emailOrPhone'))}</label>
              <input class="ui-field" id="login-identity" name="email_or_phone" type="text"
                     autocomplete="username" autocapitalize="off" autocorrect="off"
                     spellcheck="false" enterkeyhint="next" />
              <span class="ui-field-error" data-error="email_or_phone" hidden></span>
            </div>

            <div class="hand-field-group">
              <label class="hand-field-label" for="login-password">${esc(t('auth.password'))}</label>
              <input class="ui-field" id="login-password" name="password" type="password"
                     autocomplete="current-password" enterkeyhint="go" />
              <span class="ui-field-error" data-error="password" hidden></span>
            </div>

            <div id="login-notice" hidden></div>

            <button class="ui-btn ui-btn-primary is-block" type="submit" id="login-submit">
              <span class="ui-btn__label">${esc(t('auth.signIn'))}</span>
            </button>
          </form>
        </div>
      `;

      const form = host.querySelector('#login-form') as HTMLFormElement;
      const submit = host.querySelector('#login-submit') as HTMLButtonElement;
      const notice = host.querySelector('#login-notice') as HTMLElement;

      const fields = {
        user_id: host.querySelector('#login-id') as HTMLInputElement,
        email_or_phone: host.querySelector('#login-identity') as HTMLInputElement,
        password: host.querySelector('#login-password') as HTMLInputElement,
      };

      const disposers: Array<() => void> = [];
      let busy = false;

      function clearErrors(): void {
        for (const key of Object.keys(fields)) {
          const node = host.querySelector(`[data-error="${key}"]`) as HTMLElement | null;
          if (node) {
            node.hidden = true;
            node.textContent = '';
          }
          fields[key as keyof typeof fields].classList.remove('is-danger');
        }
        notice.hidden = true;
      }

      function showErrors(errors: Record<string, string>): void {
        for (const [key, message] of Object.entries(errors)) {
          const node = host.querySelector(`[data-error="${key}"]`) as HTMLElement | null;
          if (node) {
            node.textContent = message;
            node.hidden = false;
          }
          fields[key as keyof typeof fields]?.classList.add('is-danger');
        }
        const first = Object.keys(errors)[0] as keyof typeof fields | undefined;
        if (first) fields[first]?.focus();
      }

      function setBusy(value: boolean): void {
        busy = value;
        submit.innerHTML = value
          ? `<span class="hand-spinner"></span><span class="ui-btn__label">${esc(t('auth.signingIn'))}</span>`
          : `<span class="ui-btn__label">${esc(t('auth.signIn'))}</span>`;
        submit.toggleAttribute('disabled', value);
      }

      disposers.push(
        on(form, 'submit', (event) => {
          event.preventDefault();
          if (busy) return;
          void submitForm();
        }),
      );

      // Errors clear as soon as the worker edits the field, so a fixed entry does not keep
      // showing the complaint about the old one.
      for (const key of Object.keys(fields) as Array<keyof typeof fields>) {
        disposers.push(
          on(fields[key], 'input', () => {
            const node = host.querySelector(`[data-error="${key}"]`) as HTMLElement | null;
            if (node) node.hidden = true;
            fields[key].classList.remove('is-danger');
            notice.hidden = true;
          }),
        );
      }

      async function submitForm(): Promise<void> {
        clearErrors();
        // Raw, untrimmed: normalization belongs to ``credentials.ts``, so the validator and
        // the request body cannot disagree about what the field actually holds.
        const values = {
          userId: fields.user_id.value,
          emailOrPhone: fields.email_or_phone.value,
          password: fields.password.value,
        };
        const errors = validateCredentials(values);
        if (Object.keys(errors).length) {
          showErrors(errors);
          return;
        }

        setBusy(true);
        try {
          const body = await request<LoginResponse>({
            method: 'POST',
            path: '/auth/login',
            auth: false,
            body: loginRequestBody(values),
          });

          if (!body.access_token && !body.token) {
            throw new Error('The server did not return a session token.');
          }

          await persistSessionFromLoginResponse(body as unknown as Record<string, unknown>);

          // The welcome notice is the one message an account minted by an approval has
          // never seen. It is shown once, on the screen they land on.
          if (body.welcome) {
            notice.hidden = false;
            notice.className = 'auth__welcome';
            notice.textContent = body.welcome;
          }

          const approval = String(body.approval_status ?? '').toLowerCase();
          if (approval === 'pending_approval') {
            toastOk(t('auth.pendingApproval'));
          } else {
            toastOk(t('auth.welcome', { name: store.getState().session?.user.name ?? '' }).trim());
          }
          onSignedIn();
        } catch (e) {
          const err = e as { status?: number; offline?: boolean; message?: string };
          if (err.offline) {
            notice.hidden = false;
            notice.className = 'hand-alert';
            notice.textContent = t('auth.offline');
          } else if (err.status === 401) {
            notice.hidden = false;
            notice.className = 'hand-alert is-danger';
            notice.textContent = t('auth.rejected');
          } else if (err.status === 403) {
            notice.hidden = false;
            notice.className = 'hand-alert is-danger';
            notice.textContent = err.message ?? t('auth.forbidden');
          } else if (err.status === 429) {
            notice.hidden = false;
            notice.className = 'hand-alert';
            notice.textContent = t('auth.tooMany');
          } else {
            notice.hidden = false;
            notice.className = 'hand-alert is-danger';
            notice.textContent = err.message ?? t('auth.failed');
          }
          toastError(t('auth.failed'));
        } finally {
          setBusy(false);
        }
      }

      ctx.main.scrollTop = 0;
      return () => disposers.forEach((off) => off());
    },
  };
}
