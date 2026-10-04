import type { CapacitorConfig } from '@capacitor/cli';

const config: CapacitorConfig = {
  appId: 'com.freebuff.mobile',
  appName: 'Freebuff',
  webDir: 'dist',
  server: {
    androidScheme: 'https',
  },
  android: {
    // The app reaches its backend with plain `fetch()` from the WebView (see
    // src/core/http.ts), and `androidScheme: 'https'` makes the page's origin
    // https://localhost - so a call to an http:// LAN address is *mixed content*, which the
    // WebView blocks on its own account, before the network security policy is ever
    // consulted. This is the switch that allows it: it is read by the Android runtime and
    // becomes WebSettings.MIXED_CONTENT_ALWAYS_ALLOW in Bridge.java.
    //
    // The other two layers a LAN deployment needs are not Capacitor options:
    //   * android:usesCleartextTraffic="true" - in the debug manifest, next to this one;
    //   * the cleartext policy itself - android/app/src/debug/res/xml/network_security_config.xml.
    // Both live under src/debug, so a release APK keeps Android's secure defaults and never
    // accepts plaintext traffic.
    allowMixedContent: true,
  },
  plugins: {
    CapacitorHttp: {
      enabled: true,
    },
  },
};

export default config;
