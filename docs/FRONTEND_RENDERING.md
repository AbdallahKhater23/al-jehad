# Rendering server text in the frontend

This is the audit behind the "escapes everything it interpolates" half of the stored-XSS
defence, the rules that come out of it, and the code changes that were made to `frontend/*.js`.
The other half - refusing markup at the boundary - is `backend/textguard.py`, described in the
README section *"What text the server will store"*.

**Read this before adding a screen that shows a name, a site, or a sentence somebody typed.**

## Why escaping is still the load-bearing defence

Every string in this application that a person wrote is rendered back out as markup: the worker
roster, the live-ops table, the notes inbox, the audit log, a notification, a QR panel, a CSV an
operator opens in Excel. A worker whose name is `<img src=x onerror=…>` is not a person with a
funny name; it is code waiting for the next person who reads a name.

Three locks sit on that door, and only one of them is the renderer:

| Lock | Where | What it stops |
| --- | --- | --- |
| Boundary validation | `backend/textguard.py`, called by every request model | anything *new* being stored as markup |
| Content-Security-Policy | `netguard.CSP_HTML` | an injected `<script>` element from executing at all |
| Output escaping | `frontend/*.js`, `escapeHtml` at every interpolation | everything else, including rows written before the rules existed |

The third is not redundant, because the first two each have a hole by design:

* **Rows written before the rule are still in the database.** They are exactly the rows an
  attacker would have planted, and the read path treats them identically.
* **The CSP still allows event-handler *attributes*** (`script-src-attr 'unsafe-inline'`),
  because the admin console builds its markup as strings and puts its handler in an `onclick=`.
  So an injected `<img onerror=...>` that reaches the DOM *does* run. Escaping is what keeps it
  from reaching the DOM.

## What the audit found

Eight classes of interpolation that carried a value off the wire into `innerHTML` unescaped -
sixteen call sites. All are fixed, and `backend/tests/test_frontend_xss.py` pins them: the ones
with a unique text shape by regex, and every one of them by rendering a hostile value through
the real view and reading back what the DOM received.

| # | Sink | Why it was a bug | Fix |
| --- | --- | --- | --- |
| 1 | `State.user.name` in the header greeting (`frontendjavascript.js:617`) | the signed-in user's own name still came off the wire | `UI.escapeHtml(...)` |
| 2 | Profile card: `State.user.id`, `.name`, `.role` (`:732-740`) | `id` and `role` looked like enums; a server can send any string | `UI.escapeHtml(...)` on all three |
| 3 | `err.message` in an admin tab (`frontendjavascript.js:1528`) | "it is our own error message" stops being true the moment one endpoint interpolates a client value into a message | `this.escapeHtml(err.message)` |
| 4 | `err.message` in the worker dashboard (`worker_modules.js:246`) | same | `this.escapeHtml(...)` |
| 5 | `err.message` in the worker log view (`worker_modules.js:338`) | same | `this.escapeHtml(...)` |
| 6 | Worker history, card layout: `actionLabel(log)`, `log.timestamp`, `log.site`, `log.status` (`:355-360`) | four server fields in one template literal | `escapeHtml` on each |
| 7 | Worker history, table layout: the same four (`:381-385`) | the second layout was written later and copied the first one's omission | `escapeHtml` on each |
| 8 | `res.qr_png_data_uri` in the quick-link panel (`admin_modules.js:1286`) | attribute position, and escaped on the sibling screen further down - a rule with an exception nobody can name | `escapeHtml`, harmless on base64 |

One near-miss worth naming, because it is the reason the *rule* is "escape, then coerce", not
"escape everything": `${log.hours ? Number(log.hours).toFixed(2) + ' h' : '-'}` was already safe -
`Number()` produces a number, and a number cannot be markup. It stays as it is, and the render
test asserts the figure survives (`8` → `8.00`), so the expression cannot be simplified into
something that interpolates the raw field.

### Before / after, as it actually landed

The header greeting - the case that looked trustworthy because it is "the user's own name":

```diff
-                    <p class="font-bold truncate">${State.user.name}</p>
+                    <p class="font-bold truncate">${this.escapeHtml(State.user.name)}</p>
```

The error path, which is the most-copied line in the codebase:

```diff
-            container.innerHTML = `<p class="text-red-500">${I18n.__('error')}: ${err.message}</p>`;
+            container.innerHTML = `<p class="text-red-500">${I18n.__('error')}: ${this.escapeHtml(err.message)}</p>`;
```

The worker's history, in both layouts - note that the *number* is deliberately left alone:

```diff
-                                <p class="font-semibold">${actionLabel(log)}</p>
-                                <p class="text-xs text-gray-500">${log.timestamp}</p>
-                                <p class="text-xs text-gray-500">${log.site}</p>
+                                <p class="font-semibold">${this.escapeHtml(actionLabel(log))}</p>
+                                <p class="text-xs text-gray-500">${this.escapeHtml(log.timestamp)}</p>
+                                <p class="text-xs text-gray-500">${this.escapeHtml(log.site)}</p>
```

### What was already right, and should be copied

* **The admin live-ops rows** put the value in a *quoted attribute* (`data-session="${escapeHtml(...)}"`)
  and the handler is a **delegated listener** on the container, so no handler text is built from
  data at all. That is the shape to reach for: it is also the refactor that removes
  `script-src-attr 'unsafe-inline'`.
* **`Toast` paints with `textContent`**, so nothing there is escaped - and must not be: an escaped
  value would be *displayed* as `&lt;b&gt;`. The test skips `Toast` lines for that reason.
* **`I18n.__()` output is not escaped** anywhere, correctly: translations are in the source file
  `i18n.js`, not in the database. If they ever move to a table they become data and the call
  sites have to change - that is a decision, not a detail.

## The rules

1. **Escape every interpolation that is not a literal, a number you computed, or a value you
   built in the same file.** "It came from our own API" is not one of the categories; the API is
   where the hostile value is stored.

2. **`escapeHtml` belongs to the sink, not to the value.** Escape at the point of interpolation,
   never before storing or on receipt, or the next correct renderer double-escapes and shows
   `&lt;`. Nothing in `frontend/` stores an escaped string.

3. **Quoted attributes, always, and still escaped.** `class="x" data-id="${escapeHtml(v)}"` is
   safe because the quote is escaped *and* the attribute is quoted. An unquoted
   `data-id=${v}` is a breakout even with HTML escaping, because a space ends the attribute.

4. **Inside an event-handler attribute there are two contexts, not one.** HTML-then-JavaScript
   escaping; this repo has the helper for it (`UI_MODULES.liveOpsInlineString`) and its existence
   is a warning sign:

   ```js
   // escapeHtml first (so the attribute cannot be closed), then backslashes and quotes
   // (so the JavaScript string cannot be closed). Two contexts, two passes.
   liveOpsInlineString(value) {
       return this.escapeHtml(String(value ?? '')
           .replace(/\\/g, '\\\\').replace(/'/g, "\\'"));
   }
   ```

   Reaching for it means the better move is usually rule 5.

5. **Prefer not to build markup from data at all.** When there is no formatting to preserve, the
   DOM does the escaping and no string is involved:

   ```js
   // instead of container.innerHTML = `<p>${name}</p>`
   const line = document.createElement('p');
   line.textContent = name;            // text, not markup - nothing to escape
   container.replaceChildren(line);
   ```

   And when a row needs a handler, put the *data* in the element and bind once:

   ```js
   container.innerHTML = list.map((row) =>
       `<button type="button" class="ops-btn" data-force-out="${escapeHtml(row.worker_id)}">`).join('');
   container.onclick = (event) => {
       const button = event.target.closest('[data-force-out]');
       if (button) UI_MODULES.forceOut(button.dataset.forceOut);
   };
   ```

6. **URLs need `encodeURIComponent`, and a scheme check.** A value interpolated into `href` or
   `src` is not markup but it is still an instruction: `javascript:` and content-carrying `data:`
   are refused server-side by `textguard`, and the client must not paste a raw value into a URL:

   ```js
   link.href = `https://wa.me/?text=${encodeURIComponent(message)}`;
   ```

7. **Numbers are not strings.** `Number(log.hours).toFixed(2)` is safe *because* `Number()`
   coerces - if it is removed, the safe step is gone with it. That is why the coercion stays even
   where escaping would also work.

## Sinks worth grepping for

Every place data can become markup or code. Current counts in `frontend/`:

```bash
cd frontend
grep -c "innerHTML" *.js                 # admin_modules 28, frontendjavascript 18, worker_modules 18, boot/enroll/quick 1 each
grep -n "insertAdjacentHTML\|outerHTML\|document.write\|\beval(\|new Function\|setAttribute('on" *.js   # 0 today
grep -n "on[a-z]*=\"\${" *.js   # handler text built from data - the biggest remaining surface
grep -n "href=\"\${" *.js        # a URL built from data
```

`insertAdjacentHTML`, `outerHTML`, `document.write` and `eval` are unused. `innerHTML` is used
heavily on purpose - the app paints whole views from strings - so the guard is not "do not use
it", it is "every `${…}` in it is a decision".

One real bug came out of exactly this grep: `frontend/boot.js` probed for its own globals with
`new Function('return typeof ' + name)`, which a policy without `'unsafe-eval'` **refuses**. The
probe threw, the surrounding `catch` read the throw as "not loaded", and the panel that exists to
explain a blank page named every script as missing. It now names each global directly
(`typeof I18n`), which sees a top-level `const` that `window['I18n']` cannot.

## The guards

```bash
cd backend
./venv/Scripts/python.exe -m pytest tests/test_frontend_xss.py -q      # render-level, hostile values
./venv/Scripts/python.exe -m pytest tests/test_text_safety.py -q       # boundary rules, Arabic included
```

* `test_frontend_xss.py` renders `<img onerror>`, a `<script>`-named site and an `svg/onload`
  status through the app's own rendering functions in the Node VM the other frontend suites use,
  in both the card and the table layout, and asserts the DOM received text.
* The same file pins the nine interpolations above by regex (a revert fails), the count of inline
  event handlers per file (may fall, never rise), the absence of inline `<script>` blocks, and
  the boot probe's freedom from `new Function`/`eval`.
* `GET /api/v1/readiness` reports `stored_text`: rows already in the database that today's rules
  would refuse, per table and column. It is advisory and never repaired automatically - rewriting
  a person's name is a decision for a person.

## Known cleanup, in the order it is worth doing

1. **`escapeHtml` exists three times** (`UI`, `WORKER_MODULES`, `UI_MODULES`) because the pages
   load three scripts that cannot see each other's globals. One file, loaded first, is the fix -
   and the triplication is itself a risk: three copies drift.
2. **96 inline event handlers** (`admin_modules.js` 67, `frontendjavascript.js` 19,
   `worker_modules.js` 10). Converting them to delegated listeners is what removes
   `script-src-attr 'unsafe-inline'`, which is the last thing an injected `<img onerror>` needs.
3. **`https://cdn.tailwindcss.com` in `script-src`.** The frontend has no build step, so its
   utility CSS is compiled in the browser by that script; committing a Tailwind build (or
   vendoring the browser build same-origin) is what makes `script-src 'self'` literal. Until
   then the allowance is deliberate, documented, and reported per scrape.
4. **CSV formula injection** is closed at the boundary rather than in the writer: the identifier
   profile allows only ` .,-_()/`, so a stored name cannot begin with `=` or `+`. The CSV writer
   itself does no prefixing - worth knowing before a new column is exported from a *prose* field.
