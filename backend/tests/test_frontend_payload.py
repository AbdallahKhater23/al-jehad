"""What a phone downloads, and what it fetches only if the session needs it.

WHY THIS EXISTS
---------------
``index.html`` named six scripts (five of them first-party, plus a CDN), and every session
downloaded all of them. Two first-party files most
sessions never opened: ``admin_modules.js`` (255 KB, the console's eight screens, which no
worker screen can reach - ``UI.renderApp`` sends a worker to the handset) and the Arabic
and Hindi tables inside ``i18n.js`` (a further 93 KB for a reader who kept English). On
site cellular, the connection this app is documented to assume is the worst one and the
one its offline queue exists for, that was 348 KB of first-party download that could not
change the screen the worker was looking at.

They are fetched by the session that needs them now, and this suite is what keeps that
true:

1. the document names the files every session uses, and nothing else;
2. the names the two loaders ask for are the names the server serves - a rename that only
   ever shows up as a 404 in somebody's browser is the failure this change introduces;
3. a worker's session renders its handset without either file, and an administrator's
   session paints nothing at all until the console arrives: not a half console, and not
   the sign-in screen they never left;
4. a file that does not arrive is survivable and named - the console offers a way back in,
   the language switch puts the reader back in the language they had, and neither caches
   the failure for the rest of the session;
5. the split did not change the words: the chunks carry the tables that used to live in
   ``i18n.js``, with every key the app asks for resolving in each language.

The last one has a known gap, recorded below rather than hidden: eighteen offline-queue
strings exist in English only, in both other tables. That gap is not this change's doing
and is pinned as a subset, so it can close but not spread.
"""

from __future__ import annotations

import re

import pytest

import frontend_vm
from harness import PROJECT_ROOT

pytestmark = pytest.mark.skipif(frontend_vm.NODE is None, reason="node is not installed")

FRONTEND = PROJECT_ROOT / "frontend"
INDEX = (FRONTEND / "index.html").read_text(encoding="utf-8")

SCRIPT_SRC = re.compile(r"""<script\b[^>]*\bsrc=["']([^"']+)["']""", re.IGNORECASE)

#: What ``index.html`` names: the boot panel, the app, and nothing else. The list is the
#: document's own tags, so a script added back here fails this suite rather than appearing
#: in every session's payload.
SHIPPED = [
    "boot.js",
    "i18n.js",
    "frontendjavascript.js",
    "offline_queue.js",
    "worker_modules.js",
]

#: Fetched by the session that needs them, from the same directory - ``UI.loadConsoleModule``
#: for the console, ``I18n.load`` for a language nobody has read yet.
DEFERRED = ["admin_modules.js", "i18n.ar.js", "i18n.hi.js", "i18n.ur.js"]

#: A worker's phone: the document's list, which is what the harness is given for the
#: sessions below that must not have the console module in them.
PHONE = ["i18n.js", "frontendjavascript.js", "offline_queue.js", "worker_modules.js"]

#: The one place the three tables genuinely disagree, and not part of this change: the
#: offline-queue and device-registration sentences were added in English only. A worker who
#: chose Arabic therefore reads these eighteen in English, at the one moment - no
#: connection, a device that needs re-registering - when nobody can stop and investigate a
#: missing translation. Pinned as a subset so the gap may close but not spread.
ENGLISH_ONLY_STRINGS = {
    "sessionExpiredSignInAgain",
    "loginNoToken",
    "offlineSaved",
    "offlineQueueFailed",
    "offlineNoConnection",
    "offlineModeHint",
    "queuedPunches",
    "queuedPunchesHint",
    "syncNow",
    "nothingToSync",
    "syncDone",
    "syncFailed",
    "lastKnownStatus",
    "sessionExpired",
    "deviceKeyLost",
    "deviceRevoked",
    "reRegisterDevice",
    "deviceRegistered",
}

# ---------------------------------------------------------------------------
# The two sessions, driven against the real frontend files
# ---------------------------------------------------------------------------
# ``boot()`` hands back a stubbed DOM that does not run scripts it is handed, so the two
# halves of a deferred file - the tag the app writes, and the file that arrives - are
# observed separately and joined here. That is deliberately not a mock of the loader: the
# tag is read off the real element the app created, with the real src on it, and the file
# handed to ``arrive`` is the real one from ``frontend/``.
HARNESS = r"""
const results = {};

const ADMIN = { id: '5000', name: 'Seed Head Admin', role: 'head_admin', token: 'tok-5000' };
const WORKER = { id: 'w1', name: 'Youssef Adel', role: 'worker', token: 'tok-w1' };

function watchScripts(env) {
    env.evaluate(`(function () {
        window.__scripts = [];
        const create = document.createElement.bind(document);
        document.createElement = (tag) => {
            const el = create(tag);
            if (tag === 'script') window.__scripts.push(el);
            return el;
        };
    })()`);
}

function askedFor(env) { return env.evaluate('window.__scripts.map((el) => el.src)'); }

function arrive(env, file, at) {
    // What the browser does with <script src>: evaluate the file in this page's scope,
    // then fire the load event the app is listening for.
    env.evaluate(fs.readFileSync(path.join(frontend, file), 'utf8'));
    env.evaluate(`window.__scripts[${at || 0}].__handlers.load()`);
}

function signIn(env, who) {
    env.evaluate('State.saveUser(' + JSON.stringify(who) + ')');
}

function markup(env) { return env.evaluate("document.getElementById('app').innerHTML"); }

// --- the worker's session: neither deferred file is even asked for ----------------
{
    const env = boot();
    watchScripts(env);
    signIn(env, WORKER);
    await env.evaluate('UI.renderApp()');
    results.worker = {
        asked_for: askedFor(env),
        // The shell's class is on the host element; the header is in the markup it drew.
        rendered_the_handset: env.evaluate("document.getElementById('app').className").indexOf('hand-app') >= 0
            && markup(env).indexOf('hand-header') >= 0
    };
}

// --- the administrator's session: nothing until the console arrives --------------
{
    const env = boot();
    watchScripts(env);
    signIn(env, ADMIN);
    const painting = env.evaluate('UI.renderApp()');
    // Let every microtask the render queued run: "blank" has to mean waiting for a file,
    // not "has not got round to it yet".
    await Promise.resolve();
    results.console = {
        module_in_this_session: env.evaluate("typeof UI_MODULES !== 'undefined'"),
        blank_while_it_loads: markup(env) === '',
        asked_for: askedFor(env),
        settled: await Promise.race([painting.then(() => 'painted'), Promise.resolve('waiting')]),
        joined_the_same_download: askedFor(env).length
    };
    // The console module arrives.
    arrive(env, 'admin_modules.js', 0);
    await painting;
    results.console.rail = markup(env).indexOf('admin-rail') >= 0;
    results.console.content_host = env.evaluate("!!document.getElementById('adminContent')");
    // A repaint while it is still in flight must join that download, not start another.
    env.evaluate('UI.renderApp()');
    results.console.asked_for_after_a_repaint = askedFor(env);
}

// --- the console's module never arrives ------------------------------------------
{
    const env = boot();
    watchScripts(env);
    signIn(env, ADMIN);
    const painting = env.evaluate('UI.renderApp()');
    env.evaluate('window.__scripts[0].__handlers.error()');
    await painting;
    const page = markup(env);
    results.console_failure = {
        said: page.indexOf(env.evaluate("I18n.__('consoleUnavailable')")) >= 0,
        offered_a_way_back_in: page.indexOf('data-console-failure="reload"') >= 0
            && page.indexOf('data-console-failure="logout"') >= 0,
        not_the_sign_in_screen: page.indexOf('login-shell') < 0
    };
    // Not cached: the next frame asks again, so a connection that comes back is enough.
    env.evaluate('UI.renderApp()');
    results.console_failure.asked_again = askedFor(env).length;
}

// --- a language this session has never read --------------------------------------
{
    const env = boot();
    watchScripts(env);
    const before = {
        table_present: env.evaluate("I18n.has('ar')"),
        title: env.evaluate("I18n.__('title')")
    };
    const switched = env.evaluate("I18n.setLang('ar')");
    const asked = askedFor(env);
    // A second switch, while the first file is still on its way, must not fetch it twice.
    env.evaluate("I18n.load('ar')");
    arrive(env, 'i18n.ar.js', 0);
    await switched;
    results.language = {
        table_present_before: before.table_present,
        english_fallback: before.title,
        asked_for: asked,
        asked_for_once: askedFor(env).length,
        chosen: env.evaluate('I18n.lang'),
        table_present_after: env.evaluate("I18n.has('ar')"),
        title_after: env.evaluate("I18n.__('title')")
    };
    // English is inlined, so switching back to it fetches nothing.
    env.evaluate("I18n.setLang('en')");
    results.language.english_needs_no_file = askedFor(env).length;
}

// --- a language that cannot be fetched -------------------------------------------
{
    const env = boot();
    watchScripts(env);
    const switched = env.evaluate("I18n.setLang('hi')");
    env.evaluate('window.__scripts[0].__handlers.error()');
    await switched;
    results.language_failure = {
        kept_reading: env.evaluate('I18n.lang'),
        stored: env.evaluate("localStorage.getItem('lang')"),
        said: env.evaluate("Array.from(document.getElementById('toastRoot').__children).map((el) => el.textContent).join(' | ')"),
        said_when_it_works: env.evaluate("I18n.__('languageUnavailable')"),
        asked_here: askedFor(env).length
    };
    // The failed attempt is forgotten, so switching back retries it rather than
    // remembering a dead end for the rest of the session.
    env.evaluate("I18n.load('hi')");
    results.language_failure.asked_on_retry = askedFor(env).length;
}
"""

#: A session that already has the console - every repaint after the first. It must not
#: wait on anything: the module is in memory, so the paint is synchronous.
EAGER_HARNESS = r"""
const results = {};
const env = boot();
env.evaluate('State.saveUser(' + JSON.stringify(
    { id: '5000', name: 'Seed Head Admin', role: 'head_admin', token: 'tok-5000' }) + ')');
env.evaluate('UI.renderApp()');
results.rail = env.evaluate("document.getElementById('app').innerHTML").indexOf('admin-rail') >= 0;
"""

#: The tables, read where the app reads them: the merged ``TRANSLATIONS`` of a session
#: that ended up with all three files. Returned as the disagreements only - the full tables
#: are a quarter of a megabyte of prose, and a test that ships them back through stdout
#: would be measuring its own pipe.
TABLES_HARNESS = r"""
const results = {};
const env = boot();
// Serialised inside the page and parsed here, the way the other frontend suites hand a
// table back: a value that crossed the VM boundary as an object would be compared in the
// wrong realm, and "the same key set" is a claim about strings.
const table = (expression) => JSON.parse(env.evaluate(expression));
const overLanguages = (body) => `(function () {
    const out = {};
    for (const lang of Object.keys(TRANSLATIONS)) { out[lang] = ${body}; }
    return JSON.stringify(out);
})()`;

results.languages = table('JSON.stringify(Object.keys(TRANSLATIONS))');
results.counts = table(overLanguages('Object.keys(TRANSLATIONS[lang]).length'));
// A key the app asks for in a language that does not have it, and a key only that
// language has: the first is a sentence to write, the second is dead weight.
results.english_only = table(overLanguages(
    'Object.keys(TRANSLATIONS.en).filter((key) => !(key in TRANSLATIONS[lang]))'));
results.unknown = table(overLanguages(
    'Object.keys(TRANSLATIONS[lang]).filter((key) => !(key in TRANSLATIONS.en))'));
results.placeholder_mismatch = table(`(function () {
    const placeholders = (text) => (String(text).match(/\\{\\w+\\}/g) || []).sort().join(',');
    const out = {};
    for (const lang of Object.keys(TRANSLATIONS)) {
        out[lang] = Object.keys(TRANSLATIONS[lang]).filter((key) => key in TRANSLATIONS.en
            && placeholders(TRANSLATIONS[lang][key]) !== placeholders(TRANSLATIONS.en[key]));
    }
    return JSON.stringify(out);
})()`);
"""


@pytest.fixture(scope="module")
def sessions() -> dict:
    return frontend_vm.run(HARNESS, scripts=PHONE)


@pytest.fixture(scope="module")
def every_session() -> dict:
    return frontend_vm.run(EAGER_HARNESS)


@pytest.fixture(scope="module")
def tables() -> dict:
    return frontend_vm.run(TABLES_HARNESS)


# ---------------------------------------------------------------------------
# What the document asks for
# ---------------------------------------------------------------------------
@pytest.mark.regression
def test_no_string_is_declared_twice_in_a_table():
    """A duplicated key is a string silently replaced, and the tables cannot show it.

    An object literal with the same key twice is valid JavaScript: the *last* one wins, no
    warning is printed, and everything that reads the first one keeps its old value - which is
    how a new screen's wording once replaced a different screen's. ``Object.keys`` cannot see
    it after the fact (the duplicate is gone by then), so this reads the source: every
    ``"key":`` line, per table, with the key named in the failure.
    """
    key = re.compile(r'^\s*"([A-Za-z0-9_]+)":', re.MULTILINE)
    for name in ("i18n.js", "i18n.ar.js", "i18n.hi.js", "i18n.ur.js"):
        source = (FRONTEND / name).read_text(encoding="utf-8")
        found = key.findall(source)
        duplicates = sorted({entry for entry in found if found.count(entry) > 1})
        assert duplicates == [], (
            f"{name} declares {duplicates} more than once. The last declaration wins and the "
            f"first one's screen silently changes - give one of them its own key, in all four "
            f"tables"
        )


def test_the_document_ships_only_the_files_every_session_uses():
    """Six names, and the next eager tag is a phone paying for an office again."""
    shipped = [src for src in SCRIPT_SRC.findall(INDEX) if "://" not in src]
    assert shipped == SHIPPED, f"index.html loads {shipped}"
    for name in DEFERRED:
        assert name not in shipped, (
            f"{name} is in index.html again. It is fetched by the session that needs it "
            "(UI.loadConsoleModule / I18n.load); a tag here hands it to every worker's "
            "phone, which cannot use it."
        )
    # No remote script at all. The Tailwind Play CDN used to be here, compiling the
    # frontend's utility classes in the browser on every load - including on the phone at a
    # gate whose connection this whole suite exists for. It is gone, along with the script
    # that apologised when it could not be downloaded; a browser now needs nothing but this
    # origin to render the app.
    assert [s for s in SCRIPT_SRC.findall(INDEX) if "://" in s] == []


@pytest.mark.regression
def test_the_deferred_files_are_named_where_the_app_asks_for_them():
    """The loader's own string, read off the source that ships.

    A rename on one side of this is invisible in every other test in this repo - the tag
    would simply 404 in a browser - so the two names are pinned against each other here.
    """
    console = (FRONTEND / "frontendjavascript.js").read_text(encoding="utf-8")
    i18n = (FRONTEND / "i18n.js").read_text(encoding="utf-8")
    assert "script.src = 'admin_modules.js'" in console
    assert "CHUNKS: ['ar', 'hi', 'ur']" in i18n
    assert "script.src = 'i18n.' + code + '.js'" in i18n


@pytest.mark.regression
def test_every_deferred_file_is_served_from_the_site_root(client):
    """On demand still means one round trip to the same directory, revalidated."""
    for name in DEFERRED:
        response = client.get(f"/{name}")
        assert response.status_code == 200, name
        assert "javascript" in response.headers["content-type"], name
        assert "no-cache" in response.headers.get("cache-control", ""), (
            f"{name} must be revalidated on every load: a stale console module is the "
            "same bug class as a stale frontendjavascript.js, on the screen that can "
            "change other people's hours"
        )


# ---------------------------------------------------------------------------
# What a session does without them
# ---------------------------------------------------------------------------
@pytest.mark.regression
def test_a_workers_session_renders_the_handset_without_the_console(sessions):
    worker = sessions["worker"]
    assert worker["rendered_the_handset"], "the worker's screen is the handset"
    assert worker["asked_for"] == [], (
        f"a worker's session fetched {worker['asked_for']}. The point of the deferral is "
        "that this session never asks for the console module or for a language it is not "
        "reading."
    )


@pytest.mark.regression
def test_an_administrators_session_waits_for_the_console_rather_than_half_drawing_it(sessions):
    console = sessions["console"]
    assert console["module_in_this_session"] is False, "this session is meant not to have it"
    assert console["asked_for"] == ["admin_modules.js"], (
        "the console is fetched from the site root by the name index.html used to carry"
    )
    assert console["blank_while_it_loads"], (
        "the console painted before its module arrived. A half console - or the sign-in "
        "screen an administrator never left - is worse than a moment of nothing"
    )
    assert console["settled"] == "waiting", "the render finished without the module"
    assert console["rail"] and console["content_host"], (
        "once the module lands, the console is the console: rail and content host"
    )
    assert console["asked_for_after_a_repaint"] == ["admin_modules.js"], (
        "a repaint while the module was in flight started a second download of the same "
        "255 KB file"
    )


@pytest.mark.regression
def test_a_repaint_in_a_session_that_already_has_the_console_waits_for_nothing(every_session):
    """The deferral is one fetch per session, not one per frame.

    With the module in memory the console is painted in the same turn the render is asked
    for. If this fails, every repaint - a theme toggle, a phone rotating - is now waiting
    a microtask on a promise that is already resolved, and things that were ordered are
    not any more.
    """
    assert every_session["rail"], "the console paints synchronously once it is loaded"


@pytest.mark.regression
def test_the_console_says_so_and_offers_a_way_back_in_when_its_module_never_arrives(sessions):
    failure = sessions["console_failure"]
    assert failure["said"], "the administrator is told what could not be loaded"
    assert failure["offered_a_way_back_in"], "and can reload or sign out from there"
    assert failure["not_the_sign_in_screen"], (
        "they are still signed in: their session is valid and re-entering a password "
        "that was never the problem is a dead end"
    )
    assert failure["asked_again"] == 2, (
        "the failure was cached, so a connection that comes back still needs a sign-out"
    )


@pytest.mark.regression
def test_a_language_this_session_never_read_is_fetched_by_name_and_is_the_app_table(sessions):
    language = sessions["language"]
    assert language["table_present_before"] is False, "this session starts without Arabic"
    assert language["english_fallback"] == "Site Attendance", (
        "a reader must never see a key name where a sentence belongs, even for the moment "
        "before their language arrives"
    )
    assert language["asked_for"] == ["i18n.ar.js"], "the file is named i18n.<code>.js"
    assert language["asked_for_once"] == 1, (
        "a second switch during the first fetch started a second download"
    )
    assert language["chosen"] == "ar"
    assert language["table_present_after"] is True, (
        "the file the loader fetched is not the table it expects: i18n.ar.js has to "
        "assign TRANSLATIONS.ar"
    )
    assert language["title_after"] == "نظام حضور الموقع", (
        "after the file arrives the reader is reading it, not English with the direction "
        "flipped"
    )
    assert language["english_needs_no_file"] == 1, (
        "English is inlined in i18n.js; switching back to it must not fetch anything"
    )


@pytest.mark.regression
def test_a_language_that_cannot_be_fetched_leaves_the_reader_where_they_were(sessions):
    failure = sessions["language_failure"]
    assert failure["kept_reading"] == "en", (
        "the selector said Hindi over English text, which reads as a bad translation "
        "rather than as a connection problem"
    )
    assert failure["stored"] == "en", "and the choice is not remembered for next time"
    assert failure["said"] == failure["said_when_it_works"], (
        "the reader is told the language could not be downloaded"
    )
    assert failure["asked_here"] == 1
    assert failure["asked_on_retry"] == 2, (
        "the failed attempt was cached, so the reader never gets that language without "
        "clearing the app's storage"
    )


# ---------------------------------------------------------------------------
# The tables the split produced
# ---------------------------------------------------------------------------
@pytest.mark.regression
def test_the_three_tables_are_one_vocabulary(tables):
    """The chunks are the tables that were in ``i18n.js``, not a paraphrase of them."""
    assert tables["languages"] == ["en", "ar", "hi", "ur"], (
        "the loader builds TRANSLATIONS from four files; a session that ends up with "
        "four languages is what proves they all merged"
    )
    counts = tables["counts"]
    assert counts["en"] >= 508, f"the English table lost strings: {counts}"
    assert counts["ar"] == counts["hi"], f"the two translations drifted apart: {counts}"
    # Urdu was written after the split, straight from the English table, so it carries every
    # key. The eighteen-string gap below is Arabic's and Hindi's; it stays theirs rather than
    # being inherited by a table that had no reason to arrive short.
    assert counts["ur"] == counts["en"], (
        f"the Urdu table is incomplete ({counts['ur']} of {counts['en']} keys): a reader who "
        "chooses Urdu must not fall back to English anywhere, least of all offline"
    )

    for lang in ("ar", "hi", "ur"):
        assert tables["unknown"][lang] == [], (
            f"{lang} has keys English does not: {tables['unknown'][lang]}. A key nobody "
            "asks for in English is a string the app can never show."
        )
        assert tables["placeholder_mismatch"][lang] == [], (
            f"{lang} substitutes different placeholders than English for "
            f"{tables['placeholder_mismatch'][lang]}: the sentence renders with a brace "
            "in it, or drops the value it was given"
        )
        assert set(tables["english_only"][lang]) <= ENGLISH_ONLY_STRINGS, (
            f"{lang} is missing strings outside the known offline gap: "
            f"{sorted(set(tables['english_only'][lang]) - ENGLISH_ONLY_STRINGS)}. Either "
            "translate them here or name the offline family in ENGLISH_ONLY_STRINGS - "
            "do not let a new English-only string reach a reader silently."
        )


DIRECTION_HARNESS = r"""
const results = {};
const env = boot();
// ``applyDirection`` is what every repaint and every language switch goes through, so it
// is the one place the layout's direction is decided.
const directionFor = (code) => env.evaluate(
    "(function () { I18n.lang = '" + code + "'; I18n.applyDirection();" +
    " return document.documentElement.__attrs.dir + '/' + document.documentElement.__attrs.lang; })()");
results.direction = { en: directionFor('en'), ar: directionFor('ar'), hi: directionFor('hi'), ur: directionFor('ur') };
results.rtl_list = env.evaluate('I18n.RTL.join(",")');
results.is_rtl = {
    ar: env.evaluate("I18n.isRtl('ar')"), ur: env.evaluate("I18n.isRtl('ur')"),
    hi: env.evaluate("I18n.isRtl('hi')"), en: env.evaluate("I18n.isRtl('en')")
};
"""


@pytest.fixture(scope="module")
def direction() -> dict:
    return frontend_vm.run(DIRECTION_HARNESS)


@pytest.mark.regression
def test_urdu_mirrors_the_layout_like_arabic(direction):
    """Urdu reads right to left, and the app decides that from one list, not a branch.

    A second RTL language is the one thing about adding Urdu that no string test can see:
    get it wrong and every screen renders left to right in a right-to-left script, which
    looks broken to the reader and correct to the suite.
    """
    assert direction["rtl_list"] == "ar,ur", (
        f"the right-to-left languages are {direction['rtl_list']!r}: Urdu belongs in this "
        "list, and nothing else does"
    )
    assert direction["is_rtl"] == {"ar": True, "ur": True, "hi": False, "en": False}
    assert direction["direction"] == {
        "en": "ltr/en", "ar": "rtl/ar", "hi": "ltr/hi", "ur": "rtl/ur"
    }, direction["direction"]


def test_the_english_table_really_is_all_that_i18n_js_carries():
    """The move, stated as a property: no other language's table is in the shipped file.

    ``every_session`` above loads all three files, so the split could be undone by pasting
    the tables back into ``i18n.js`` and every other assertion here would still pass -
    except this one, which is about the file every session downloads.
    """
    shipped = (FRONTEND / "i18n.js").read_text(encoding="utf-8") + (FRONTEND / "frontendjavascript.js").read_text(encoding="utf-8")
    for lang, marker in (
        ("ar", '"title": "نظام حضور الموقع"'),
        ("hi", '"title": "साइट उपस्थिति"'),
        ("ur", '"title": "حاضری الموقع"'),
    ):
        assert marker not in shipped, (
            f"the {lang} table is back in a file every session downloads. It belongs in "
            f"frontend/i18n.{lang}.js"
        )
