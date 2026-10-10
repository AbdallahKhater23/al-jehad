import { defineConfig, loadEnv, type Plugin } from 'vite';

import {
  assessApiBase,
  releaseGuardMessage,
  type ApiBaseAssessment,
} from './src/build/api-base-policy';

/**
 * Two profiles, one bundle layout.
 *
 *   npm run build          the debug profile. LAN and emulator addresses are expected here -
 *                          an http:// base on 192.168.x.x or 10.0.2.2 is how the app is
 *                          driven against a developer's own backend (and how the debug APK
 *                          is exercised in the emulator).
 *   npm run build:release  the shipping profile. `--mode release` is what turns the guard on:
 *                          an http:// or private-network API base URL fails the build while
 *                          the config is still being resolved, so nothing is written and no
 *                          stale outDir can be mistaken for a fresh build.
 *
 * The build also records which profile produced the assets in `build-info.json`, which
 * `android/app/build.gradle` reads: `npx cap sync android` copies the same `dist` into both
 * APK variants, so without that file a debug web build could be packaged into a release APK.
 */
export default defineConfig(({ mode }) => ({
  plugins: [apiBaseGuard(mode)],
  build: {
    outDir: './dist',
    emptyOutDir: true,
    target: 'es2022',
  },
  server: {
    port: 5173,
    strictPort: false,
  },
  preview: {
    port: 4173,
  },
}));

/**
 * Judge the API base URL this build will inline, and refuse an unshippable one.
 *
 * The value is read inside the `config` hook, through Vite's own `loadEnv` and the `VITE_`
 * prefix it uses for `import.meta.env`: `.env`, `.env.local` and `.env.release` are all
 * consulted, and an exported VITE_API_BASE_URL wins over all of them - the precedence
 * `config.ts` will see at runtime. Only the hook knows the *directory* to read: it resolves
 * `envDir ?? root ?? cwd` exactly as Vite resolves it, because a guard that read a different
 * `.env` than the bundle inlines would say "safe" with an air of authority while a LAN
 * address rode along.
 *
 * `build-info.json` is written by the same plugin from the same verdict, so the file Gradle
 * trusts cannot disagree with the URL in the bundle.
 */
function apiBaseGuard(mode: string): Plugin {
  const release = mode === 'release';
  let apiBase: ApiBaseAssessment = { raw: '', host: '', kind: 'none', problem: '' };

  return {
    name: 'api-base-guard',
    apply: 'build',
    config(userConfig) {
      const envDir = userConfig.envDir ?? userConfig.root ?? process.cwd();
      const env = loadEnv(mode, envDir, 'VITE_');
      apiBase = assessApiBase(env.VITE_API_BASE_URL);
      if (release && apiBase.kind === 'insecure') {
        throw new Error(releaseGuardMessage(apiBase));
      }
    },
    buildStart() {
      this.info(
        `${release ? 'release' : 'debug'} profile \u00b7 API base ${
          apiBase.kind === 'none'
            ? 'not set (the app must discover or be given its server)'
            : apiBase.raw
        }`,
      );
      if (release && apiBase.kind === 'none') {
        this.warn(
          'no VITE_API_BASE_URL was set for a release build: the app will fall back to the ' +
            'built-in public deployment (DEFAULT_API_BASE in src/core/config.ts). Export ' +
            'VITE_API_BASE_URL to point this deployment somewhere else.',
        );
      }
    },
    generateBundle() {
      // `insecure_api_base` can only be true for the debug profile: the release profile
      // refused to get this far with one. It is the flag android/app/build.gradle reads.
      this.emitFile({
        type: 'asset',
        fileName: 'build-info.json',
        source: `${JSON.stringify(
          {
            profile: release ? 'release' : 'debug',
            api_base: apiBase.raw,
            api_base_host: apiBase.host,
            insecure_api_base: apiBase.kind === 'insecure',
          },
          null,
          2,
        )}\n`,
      });
    },
  };
}
