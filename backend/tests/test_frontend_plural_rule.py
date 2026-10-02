"""The plural rule behind counted strings - ``I18n.pluralCategory`` and ``I18n.__p``.

WHY THIS EXISTS
---------------
``I18n.__`` renders one sentence per key. That is exactly right for the hundreds of strings
with no number in them, and it is wrong for the handful that carry one: the notes screens
label their rows and their day markers with how long ago something happened, and the string
that reads "``{count} h ago``" in English has no single Hindi or Arabic form. Hindi writes
घंटा for one hour and घंटे for the rest; Arabic writes a dual that takes no numeral at all
("قبل ساعتين"), then a plural noun for three-to-ten and a singular noun for eleven-to-99.
The old code pasted the number into one English-shaped sentence, so a reader saw
"1 घंटे पहले" and "قبل 1 دقيقة" - the grammar of a language the string was not written in.

The rule is CLDR's cardinal categories, trimmed to the whole numbers this app counts. What
can only be checked here is that the four languages pick the categories they should and
that the counted keys actually render through it - the tables themselves, and their
placeholder parity, are ``test_frontend_payload.py``'s job.

Node is optional; without it these skip rather than fail.
"""

from __future__ import annotations

import pytest

import frontend_vm

pytestmark = pytest.mark.skipif(frontend_vm.NODE is None, reason="node is not installed")

HARNESS = r"""
const results = {};
const env = boot();

// --- the rule itself: one category per count, per language -----------------------
const COUNTS = [0, 1, 2, 3, 10, 11, 99, 100, 101];
results.category = {};
for (const code of ['en', 'ar', 'hi', 'ur']) {
    results.category[code] = COUNTS.map((n) => ({
        n: n,
        category: env.evaluate("I18n.pluralCategory(" + n + ", '" + code + "')")
    }));
}

// --- what the notes' own counted keys render to -----------------------------------
const inLang = (code, key, n) => {
    env.evaluate("I18n.lang = '" + code + "'");
    return env.evaluate("I18n.__p('" + key + "', " + n + ")");
};
results.rendered = {};
for (const code of ['en', 'ar', 'hi', 'ur']) {
    results.rendered[code] = {
        hours_1: inLang(code, 'timeHoursAgo', 1),
        hours_2: inLang(code, 'timeHoursAgo', 2),
        hours_5: inLang(code, 'timeHoursAgo', 5),
        hours_15: inLang(code, 'timeHoursAgo', 15),
        minutes_1: inLang(code, 'timeMinutesAgo', 1),
        minutes_5: inLang(code, 'timeMinutesAgo', 5),
        days_3: inLang(code, 'timeDaysAgo', 3)
    };
}

// --- a language with no forms, and the plain accessor, still answer ----------------
env.evaluate("I18n.lang = 'fr'");
results.fallback = env.evaluate("I18n.__p('timeHoursAgo', 3)");
env.evaluate("I18n.lang = 'ar'");
results.plain_key = env.evaluate("I18n.__('timeHoursAgo')");

// --- and the screen goes through it: UI.timeAgo on a real stamp --------------------
const pad = (value) => String(value).padStart(2, '0');
const stamp = (msAgo) => {
    const d = new Date(Date.now() - msAgo);
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' '
        + pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
};
results.time_ago = {};
for (const code of ['en', 'ar', 'hi', 'ur']) {
    env.evaluate("I18n.lang = '" + code + "'");
    results.time_ago[code] = {
        one_hour: env.evaluate('UI.timeAgo(' + JSON.stringify(stamp(3600 * 1000 + 2000)) + ')'),
        two_hours: env.evaluate('UI.timeAgo(' + JSON.stringify(stamp(2 * 3600 * 1000 + 2000)) + ')'),
        five_hours: env.evaluate('UI.timeAgo(' + JSON.stringify(stamp(5 * 3600 * 1000 + 2000)) + ')'),
        fifteen_hours: env.evaluate('UI.timeAgo(' + JSON.stringify(stamp(15 * 3600 * 1000 + 2000)) + ')'),
        minutes: env.evaluate('UI.timeAgo(' + JSON.stringify(stamp(5 * 60 * 1000 + 2000)) + ')'),
        days: env.evaluate('UI.timeAgo(' + JSON.stringify(stamp(3 * 86400 * 1000 + 7200 * 1000)) + ')')
    };
}
"""


@pytest.fixture(scope="module")
def plural() -> dict:
    return frontend_vm.run(HARNESS)


def _categories(plural, code) -> dict:
    return {row["n"]: row["category"] for row in plural["category"][code]}


@pytest.mark.regression
def test_english_and_urdu_split_at_one(plural):
    """The languages with a two-form rule: exactly one is ``one``, everything else ``other``."""
    expected = {0: "other", 1: "one", 2: "other", 3: "other", 10: "other",
                11: "other", 99: "other", 100: "other", 101: "other"}
    assert _categories(plural, "en") == expected
    assert _categories(plural, "ur") == expected


@pytest.mark.regression
def test_hindi_counts_zero_with_one(plural):
    """Hindi says "एक घंटा" for one hour and takes the plural noun for the rest.

    Zero shares the singular form: "0 घंटे" is not what a Hindi reader writes. That is the
    one place Hindi's rule differs from English's, and the reason a shared two-form rule
    would be wrong here rather than merely unfinished.
    """
    assert _categories(plural, "hi") == {
        0: "one", 1: "one", 2: "other", 3: "other", 10: "other",
        11: "other", 99: "other", 100: "other", 101: "other"
    }


@pytest.mark.regression
def test_arabic_uses_the_whole_six_category_set(plural):
    """Arabic is the only one of the four that needs more than one/other.

    The dual is its own form, three-to-ten takes a plural noun and eleven-to-99 a singular
    one - all of it read off ``n % 100``, so the categories repeat every hundred.
    """
    assert _categories(plural, "ar") == {
        0: "zero", 1: "one", 2: "two", 3: "few", 10: "few",
        11: "many", 99: "many", 100: "other", 101: "other"
    }


@pytest.mark.regression
def test_arabic_relative_times_read_as_arabic(plural):
    """The three shapes Arabic needs, on the hours key: singular, dual, plural, singular.

    The dual and the singular carry no numeral - "قبل ساعتين" is two hours, and a "2" in
    front of it is the English sentence again. Three-to-ten and eleven-to-99 do carry it,
    with the noun changing between them.
    """
    hours = plural["rendered"]["ar"]
    assert hours["hours_1"] == "قبل ساعة"
    assert hours["hours_2"] == "قبل ساعتين"
    assert hours["hours_5"] == "قبل 5 ساعات"
    assert hours["hours_15"] == "قبل 15 ساعة"
    assert "1" not in hours["hours_1"] and "2" not in hours["hours_2"], (
        "the Arabic singular and dual are written without the numeral"
    )
    assert plural["rendered"]["ar"]["minutes_5"] == "قبل 5 دقائق"
    assert plural["rendered"]["ar"]["days_3"] == "قبل 3 أيام"


@pytest.mark.regression
def test_hindi_and_urdu_inflect_the_hour_noun(plural):
    """The noun is the whole difference: एक घंटा पहले for one, घंटे पहले for the rest.

    Minutes and days do not inflect in either language, which is why their one-forms
    restate the plain string - and why the rule still has to be consulted rather than the
    number simply concatenated.
    """
    assert plural["rendered"]["hi"]["hours_1"] == "एक घंटा पहले"
    assert plural["rendered"]["hi"]["hours_2"] == "2 घंटे पहले"
    assert plural["rendered"]["hi"]["minutes_1"] == "एक मिनट पहले"
    assert plural["rendered"]["ur"]["hours_1"] == "ایک گھنٹہ پہلے"
    assert plural["rendered"]["ur"]["hours_2"] == "2 گھنٹے پہلے"
    assert plural["rendered"]["ur"]["minutes_1"] == "ایک منٹ پہلے"


@pytest.mark.regression
def test_english_keeps_its_own_words(plural):
    """English's one-forms are written words, not the numeral glued to the plural: it is
    "an hour ago", not "1 h ago".
    """
    assert plural["rendered"]["en"]["hours_1"] == "an hour ago"
    assert plural["rendered"]["en"]["minutes_1"] == "a minute ago"
    assert plural["rendered"]["en"]["hours_2"] == "2 h ago"
    assert plural["rendered"]["en"]["hours_15"] == "15 h ago"


@pytest.mark.regression
def test_a_language_without_forms_falls_back_to_english(plural):
    """The rule may be new; a caller may not be, and no table may leave it blank.

    A key with no form for the chosen category falls back to the plain key (in the reader's
    language when it has one, in English when it does not) - which is what keeps every
    string already written working, and what a language whose table is still loading gets.
    """
    assert plural["fallback"] == "3 h ago"
    assert plural["plain_key"] == "قبل {count} ساعة", (
        "the plain accessor is unchanged: it still hands back the other-form string, "
        "placeholder and all"
    )


@pytest.mark.regression
def test_the_notes_screens_go_through_the_rule(plural):
    """``UI.timeAgo`` is the only caller, and it must ask for the reader's form.

    The notes rows and their day markers are the strings this exists for, so the check is
    the rendered answer, not the table entry: what a screen shows for five hours ago in
    Arabic is the Arabic three-to-ten noun.
    """
    assert plural["time_ago"]["ar"]["five_hours"] == "قبل 5 ساعات"
    assert plural["time_ago"]["ar"]["one_hour"] == "قبل ساعة"
    assert plural["time_ago"]["ar"]["two_hours"] == "قبل ساعتين"
    assert plural["time_ago"]["ar"]["days"] == "قبل 3 أيام"
    assert plural["time_ago"]["ar"]["minutes"] == "قبل 5 دقائق"
    assert plural["time_ago"]["hi"]["one_hour"] == "एक घंटा पहले"
    assert plural["time_ago"]["ur"]["one_hour"] == "ایک گھنٹہ پہلے"
    assert plural["time_ago"]["en"]["one_hour"] == "an hour ago"
