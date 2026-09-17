
        // The app toggles `dark` on <html>, so Tailwind must run in class mode
        // (the CDN default is 'media', which silently ignored the theme switch).
        if (window.tailwind) {
            tailwind.config = { darkMode: 'class' };
        }
        // No internet? Warn, but keep the (unstyled) app usable offline on site.
        window.addEventListener('load', function () {
            if (window.tailwind) return;
            var banner = document.createElement('div');
            banner.setAttribute('style',
                'position:fixed;top:0;left:0;right:0;z-index:9998;background:#b45309;color:#fff;' +
                'font:13px system-ui,sans-serif;padding:8px 12px;text-align:center');
            banner.textContent = 'Styles could not be downloaded — the app works but looks plain. ' +
                'Connect to the internet once and reload.';
            document.body.appendChild(banner);
        });
    