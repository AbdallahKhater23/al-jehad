"""A person's name in every language the deployment reads, and the fallback when it is missing.

WHAT IS STORED
--------------
``users.name_i18n`` is one JSON object keyed by language code - ``{"en": "Ahmed Ali",
"ar": "أحمد علي"}`` - and ``users.name`` stays exactly what it always was: the single value
every existing screen displays. Nothing here writes into ``name``; a caller decides what the
canonical value is (``primary``) and stores it beside the map.

WHY NOT TWO COLUMNS
-------------------
Four languages are in use (``LANGUAGES``), and the applicant types their name in whichever one
they are reading. Two fixed columns would give Hindi and Urdu nowhere to go, and would have to
be re-migrated when a fifth language appears; a map carries however many the deployment reads.

WHY NOTHING IS COPIED INTO THE MISSING SLOTS
--------------------------------------------
Reading a name in a language it was not given in has to fall back *somewhere*, and the fallback
belongs at the read (``display``), not at the write. Copying the typed name into the other three
slots would make the stored row claim four names when the applicant gave one - and it would make
a filler copy indistinguishable from a real translation, so the translate pass could never tell
which slots still need one. Storage keeps only what was given or translated; ``display`` always
answers.

THE TRANSLATION PIPELINE
------------------------
``translate`` fills missing slots through a caller-supplied ``translate_one(text, source,
target)``. The callable is a parameter rather than a client this module owns, because the
translation provider is a deployment's decision and because the serving process must not make an
outbound call inside a registration: ``tools/translate_names.py`` is the caller that talks to a
provider (an operator runs it, or the cron does), and it is the *only* place a provider is
configured. A read in a language that has not been translated yet falls back - see ``display`` -
so an untranslated deployment is degraded, never broken.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Iterable, Mapping

#: The languages a name can be stored in, in the order a fallback walks them. The console and
#: the registration page both read their language codes from here rather than keeping a second
#: list that could drift.
LANGUAGES: tuple[str, ...] = ("en", "ar", "hi", "ur")

#: What a ``translate_one`` callable is: the text, the language it is in, and the language
#: wanted. It returns the translation, or ``None`` when it has none to give.
Translator = Callable[[str, str, str], "str | None"]


def parse(value: Any) -> dict[str, str]:
    """Read a stored ``name_i18n`` - JSON text, a mapping, or ``None`` - as ``{lang: name}``.

    Only the languages in ``LANGUAGES`` survive, and only when they carry something: a map
    written by a newer build, a hand-edited row or a half-filled form all read as the subset
    this build can display rather than as an error.
    """
    if value is None or value == "":
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    if not isinstance(value, Mapping):
        return {}
    names: dict[str, str] = {}
    for language in LANGUAGES:
        text = value.get(language)
        if isinstance(text, str) and text.strip():
            names[language] = " ".join(text.split())
    return names


def serialise(names: Mapping[str, Any] | None) -> str | None:
    """The JSON text for ``users.name_i18n``, or ``None`` when there is no name to store.

    ``None`` rather than ``"{}"``: an account with no per-language name is a NULL column, the
    state every account created before this column existed is in, and one shape is easier to
    read than two that mean the same.
    """
    cleaned = parse(names)
    if not cleaned:
        return None
    return json.dumps(cleaned, ensure_ascii=False, sort_keys=True)


def primary(names: Mapping[str, Any] | None) -> str:
    """The one name to keep in ``users.name``: the first language that has one.

    ``users.name`` is required and every existing screen reads it, so a caller that has a map
    needs one value from it. The order is ``LANGUAGES`` rather than the map's own order, so the
    same form filled in two languages stores the same canonical name wherever it is read.
    """
    cleaned = parse(names)
    for language in LANGUAGES:
        if language in cleaned:
            return cleaned[language]
    return ""


def display(names: Any, language: str, fallback: str = "") -> str:
    """The name to show in ``language``: what it has, else what the map has, else ``fallback``.

    The chain is deliberate and is the whole of "fall back gracefully": the language asked for,
    then any language the map carries (in ``LANGUAGES`` order, so a reader does not get a
    different name depending on how the JSON was serialised), then the account's own ``name`` -
    which is where every account created before the map existed keeps its name.
    """
    cleaned = parse(names)
    if language in cleaned:
        return cleaned[language]
    for candidate in LANGUAGES:
        if candidate in cleaned:
            return cleaned[candidate]
    return fallback or ""


def missing(names: Mapping[str, Any] | None, *, skip: Iterable[str] = ()) -> list[str]:
    """The languages ``names`` has no name for, in ``LANGUAGES`` order.

    ``skip`` is for a caller that translates from the language it already has: a translation of
    a name into the language the name is already written in is a round trip that can come back
    different from the text it started with.
    """
    cleaned = parse(names)
    return [language for language in LANGUAGES if language not in cleaned and language not in skip]


def translate(
    names: Mapping[str, Any] | None,
    *,
    translate_one: Translator | None = None,
) -> dict[str, str]:
    """Fill every missing language from the one(s) that are there. Never raises.

    The source is the first language the map carries - the language the applicant typed - and
    each missing slot is asked for once. A provider that answers nothing (unconfigured, offline,
    refusing the text) leaves the slot empty rather than inventing a value: ``display`` falls
    back for it, which is the difference between a name shown in the wrong language and a name
    shown as nothing at all.
    """
    filled = parse(names)
    if translate_one is None or not filled:
        return filled
    source = next(language for language in LANGUAGES if language in filled)
    text = filled[source]
    for language in missing(filled, skip=(source,)):
        try:
            answer = translate_one(text, source, language)
        except Exception:  # noqa: BLE001 - a provider's failure is not the caller's
            continue
        if isinstance(answer, str) and answer.strip():
            filled[language] = " ".join(answer.split())
    return filled


def label(row: Mapping[str, Any], language: str) -> str:
    """``display`` for a database row: its ``name_i18n`` when the column is there, else ``name``.

    A convenience for the three read surfaces that all show a person's name (the roster, the
    timesheet, the live board) and all join a second row for an assignment - they call this with
    either row rather than repeating the chain.
    """
    stored = row["name_i18n"] if "name_i18n" in row.keys() else None
    try:
        fallback = row["name"]
    except (IndexError, KeyError):
        fallback = ""
    return display(stored, language, fallback or "")
