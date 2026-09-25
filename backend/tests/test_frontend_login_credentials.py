"""What the sign-in form demands, against what the endpoint actually accepts.

THE DEAD END THIS GUARDS
------------------------
``POST /auth/login`` matches the account with::

    WHERE id = ? AND (email = ? OR phone = ?)

so the second credential must equal the email or the phone **stored on the account** - and for
an account whose two columns are both empty, the only value that can match is the empty string.
The console's create-account form offers email and phone as optional fields, so such an account
is one ordinary action away.

The login form marked that field ``required``. The result was a worker who could not sign in at
all: *Sign In* produced "Please fill out this field." pointing at a box with nothing to put in
it, no request was sent, and the screen's own instruction - "the ID and the password your
administrator gave you" - never mentioned a third credential. Nothing on the server was wrong,
which is why no backend test could see it.

A backend test still cannot see it: the block is the browser's constraint validation, which is a
property of the form's markup. So this suite boots the real frontend in the Node VM and asserts
both halves of the contract:

* the field is **not** required, so an empty value can be submitted at all; and
* the request the form sends for a blank field carries ``email_or_phone: ""`` - not a missing
  key, and nothing invented in its place.

Node is optional; without it these skip rather than fail.
"""

from __future__ import annotations

import pytest

import frontend_vm

pytestmark = pytest.mark.skipif(frontend_vm.NODE is None, reason="node is not installed")

HARNESS = r"""
function responders(url) {
    // A refusal, so the submit path runs to its end without a session being created.
    if (url.includes('/auth/login')) return { status: 401, body: { detail: 'Invalid credentials or user ID' } };
    return { status: 200, body: {} };
}

function loginScreen() {
    const env = boot();
    env.setResponder(responders);
    env.evaluate('UI.renderApp()');
    return env;
}

function tagFor(markup, id) {
    const match = markup.match(new RegExp('<input[^>]*id="' + id + '"[^>]*>'));
    return match ? match[0] : null;
}

/** Fill the form and submit it the way the browser does, returning the request it produced. */
async function submit(env, { userId, email, password }) {
    env.evaluate('document.getElementById(' + JSON.stringify('userId') + ').value = ' + JSON.stringify(userId));
    env.evaluate('document.getElementById(' + JSON.stringify('email') + ').value = ' + JSON.stringify(email));
    env.evaluate('document.getElementById(' + JSON.stringify('password') + ').value = ' + JSON.stringify(password));
    const handler = (env.evaluate("document.getElementById('loginForm')").__handlers || {}).submit;
    if (!handler) throw new Error('the login form registered no submit handler');
    await handler({ preventDefault() {}, target: { querySelector: () => ({ disabled: false }) } });
    return {
        posts: env.requests
            .filter((r) => r.url.includes('/auth/login'))
            .map((r) => { try { return JSON.parse(r.body); } catch (err) { return r.body; } })
    };
}

const results = {};

// 1. the markup: the id is required, the email-or-phone is not
{
    const env = loginScreen();
    const markup = env.evaluate("document.getElementById('app').innerHTML");
    results.markup = {
        has_form: markup.indexOf('id="loginForm"') >= 0,
        userid_tag: tagFor(markup, 'userId'),
        email_tag: tagFor(markup, 'email')
    };
}

// 2. the payload: a blank second credential is sent, as an empty string
{
    const env = loginScreen();
    // A worker whose account has neither an email nor a phone: the value that matches the
    // stored columns is "" - which is what an untouched field holds.
    const { posts } = await submit(env, { userId: '480', email: '', password: 'a-password-here-1' });
    results.blank_email = { posts };
}

// 3. ...and a filled one travels unchanged, so the ordinary case is untouched
{
    const env = loginScreen();
    const { posts } = await submit(env, {
        userId: '5000', email: 'admin@siteops.com', password: 'admin-pass-123'
    });
    results.with_email = { posts };
}

// 4. the copy: every table must name the third credential, since the endpoint needs it
{
    const env = loginScreen();
    const tables = env.evaluate('Object.keys(TRANSLATIONS)');
    const intro = {};
    for (const lang of tables) intro[lang] = env.evaluate(`TRANSLATIONS[${JSON.stringify(lang)}].loginIntro`);
    results.copy = {
        languages: tables,
        intro,
        english: env.evaluate("I18n.__('loginIntro')"),
        email_label: env.evaluate("I18n.__('emailOrPhone')")
    };
}
"""


@pytest.fixture(scope="module")
def results() -> dict:
    return frontend_vm.run(HARNESS)


def test_the_second_credential_is_not_required_but_the_id_is(results):
    """``required`` here was the dead end: the accepted value is sometimes the empty string.

    The id still is, and must stay, required: it is the first half of the match on every account
    and there is no account it can be omitted for.
    """
    markup = results["markup"]
    assert markup["has_form"], "the login screen did not render"
    email_tag = markup["email_tag"]
    assert email_tag, f"no #email input in the login form: {markup}"
    assert "required" not in email_tag, (
        "an account with no email and no phone can only match an empty second credential, so a "
        f"'required' here makes signing in impossible: {email_tag}"
    )
    userid_tag = markup["userid_tag"] or ""
    assert "required" in userid_tag, f"the ID is the one field that is always needed: {userid_tag}"


def test_a_blank_second_credential_is_submitted_as_an_empty_string(results):
    """The request has to reach the server at all, carrying ``""`` rather than a missing key.

    A missing key is a 422 from the request model, and a fabricated value is a 401 for an
    account that has neither - so both halves of the field's behaviour are pinned.
    """
    posts = results["blank_email"]["posts"]
    assert len(posts) == 1, f"a blank second credential must still submit: {posts}"
    body = posts[0]
    assert "email_or_phone" in body, f"the request omitted the field the server matches on: {body}"
    assert body["email_or_phone"] == "", body
    assert body["user_id"] == "480", body


def test_a_filled_second_credential_travels_unchanged(results):
    posts = results["with_email"]["posts"]
    assert len(posts) == 1, posts
    assert posts[0]["email_or_phone"] == "admin@siteops.com", posts[0]


def test_the_screen_names_all_three_things_it_is_about_to_ask_for(results):
    """The copy was the other half of the dead end: it named two of the three credentials."""
    copy = results["copy"]
    english = copy["english"]
    assert "email" in english.lower() or "phone" in english.lower(), (
        f"the sign-in copy never mentions the credential the endpoint requires: {english!r}"
    )
    assert copy["email_label"].lower() in english.lower(), (
        f"the label and the sentence disagree about what the field is: {copy['email_label']!r}"
    )
    # Every table has to carry the same sentence - a locale left on the old wording is a locale
    # still telling its readers to bring two credentials.
    missing = [lang for lang, text in copy["intro"].items() if not text]
    assert missing == [], f"these tables have no loginIntro: {missing}"
    untranslated = [lang for lang, text in copy["intro"].items() if text == english and lang != "en"]
    assert untranslated == [], f"these tables were left in English: {untranslated}"
