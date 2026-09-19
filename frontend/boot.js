
        // --- Diagnostics first: a blank page must never be silent again. ---
        window.__bootErrors = [];
        (function () {
            function panel(title, lines) {
                var existing = document.getElementById('bootError');
                if (existing) existing.remove();
                var box = document.createElement('div');
                box.id = 'bootError';
                box.setAttribute('style',
                    'position:fixed;inset:0;z-index:9999;overflow:auto;background:#fff;color:#111;' +
                    'font:14px/1.5 system-ui,-apple-system,sans-serif;padding:20px');
                box.innerHTML =
                    '<h2 style="margin:0 0 8px;color:#b91c1c;font-size:18px">' + title + '</h2>' +
                    '<p style="margin:0 0 12px;color:#374151">Something stopped the app from starting. ' +
                    'Send this text to whoever set the app up:</p>' +
                    '<pre style="white-space:pre-wrap;background:#f3f4f6;border:1px solid #e5e7eb;' +
                    'border-radius:8px;padding:12px;font-size:12px">' + lines.join('\n') + '</pre>' +
                    '<p style="color:#374151">This page URL: <b>' + location.href + '</b></p>';
                document.body.appendChild(box);
            }
            window.__showBootPanel = panel;

            window.addEventListener('error', function (event) {
                var where = (event.filename || '').split('/').pop();
                window.__bootErrors.push(
                    (event.message || 'Script error') + (where ? '  (' + where + ':' + event.lineno + ')' : ''));
            }, true);
            window.addEventListener('unhandledrejection', function (event) {
                var reason = event.reason;
                window.__bootErrors.push('Unhandled promise rejection: ' +
                    ((reason && (reason.message || reason.detail)) || String(reason)));
            });

            window.addEventListener('load', function () {
                setTimeout(function () {
                    var app = document.getElementById('app');
                    if (app && app.children.length > 0) return; // app rendered fine
                    // Each probe names its global directly rather than handing a string to the
                    // Function constructor. A document policy grants no 'unsafe-eval', so the
                    // old string-built probe threw, the catch read that as "not loaded", and
                    // this panel - which only ever appears once the app has already failed -
                    // reported every script as missing and sent whoever read it after the
                    // wrong problem. A top-level `const` is not a property of `window` either,
                    // so `window['UI']` would say the same untrue thing; `typeof` on the name
                    // sees the global lexical binding.
                    // Exactly the files index.html loads. admin_modules.js and the two
                    // non-English translation tables are fetched later, by the session
                    // that needs them, and are deliberately absent here: an administrator
                    // whose console failed gets UI's own message on a rendered page (so
                    // this panel never runs), and listing a file nobody has asked for yet
                    // as "missing" would send whoever reads this after the wrong problem -
                    // the mistake the string-built probe used to make below.
                    var files = [
                        ['i18n.js', function () { return typeof I18n; }],
                        ['frontendjavascript.js', function () { return typeof UI; }],
                        ['worker_modules.js', function () { return typeof WORKER_MODULES; }],
                        ['offline_queue.js', function () { return typeof OFFLINE; }]
                    ];
                    var missing = [];
                    files.forEach(function (pair) {
                        var loaded = false;
                        // `typeof` on a name that was never declared is 'undefined' rather than
                        // a ReferenceError; a script that threw while evaluating leaves the
                        // binding in its temporal dead zone, which is a ReferenceError.
                        try { loaded = pair[1]() !== 'undefined'; } catch (e) { loaded = false; }
                        if (!loaded) missing.push(pair[0]);
                    });
                    panel('The app did not start',
                        ['Missing scripts: ' + (missing.length ? missing.join(', ') : 'none')]
                        .concat(window.__bootErrors.length ? ['Errors: ' + window.__bootErrors.join(' | ')] : ['No JavaScript error was reported.'])
                        // The stylesheet, not a CDN: if this reads false, the page is
                        // unstyled - which is the difference between a missing file and
                        // a missing connection, and the two need different answers.
                        .concat(['Stylesheet applied: ' + (function () {
                                     try { return getComputedStyle(document.body).boxSizing === 'border-box'; }
                                     catch (e) { return 'unknown'; }
                                 })(),
                                 'Secure context (GPS/camera allowed): ' + (window.isSecureContext === true),
                                 'Scripts expected next to: ' + location.href.replace(/[?#].*$/, '')]));
                }, 2500);
            });
        })();
    