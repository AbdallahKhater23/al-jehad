"""The text this application is willing to store, and the text it refuses.

WHY THIS EXISTS
---------------
Every string a client sent was stored exactly as it arrived: a worker's name, a site's name,
the reason an administrator gave for rejecting a punch, the free-text note attached to a
clock link. Those strings are not inert. They are rendered back out - into the console's
tables, into an administrator's notification, into the audit log, into a CSV somebody opens
in a spreadsheet - and the rendering side is ordinary string interpolation. A worker whose
name is ``<img src=x onerror=fetch('/admin/users/delete',...)>`` is therefore not a worker
with a funny name; it is code that runs in the browser of whichever administrator reads the
roster next, holding that administrator's token.

The escaping that stops that belongs on the *output* side, and it is there:
``frontend/*.js`` escapes every value it interpolates that it did not build itself. This
module is the other half, and it is deliberately not a replacement for it. What it buys:

* a stored row cannot hurt a reader *even when the reader does not escape* - which is the
  situation for the CSV export, the WhatsApp message, an operator's ``sqlite3`` shell, and
  any future view of these rows that has not been written yet;
* the rejection happens once, at the boundary, instead of in every consumer;
* a row already in the database from a version that did not validate is still caught by the
  consumers that do escape, so the two halves overlap rather than depend on each other.

WHAT IS REFUSED, AND WHY EACH RULE IS HERE
------------------------------------------
1. **Markup.** ``<`` followed by a letter or ``/``, in any spacing - the shape of every tag,
   including the ones nobody writes by hand (``<svg/onload=``). Refused in prose, refused in
   names by the allowlist.
2. **Entity-encoded markup.** ``&lt;script&gt;`` and friends decode back into a tag the moment
   anything passes the text through an HTML parser, so the entity is refused as firmly as the
   tag it encodes. This is the rule that catches an attacker who has read the first rule.
3. **Schemes.** ``javascript:``/``vbscript:`` always, and ``data:`` only when it carries
   content (``data:text/html``, ``data:image/svg+xml``, ``data:;base64``). A navigation to
   either is script execution with extra steps - and a note that says "the data: nothing
   arrived" keeps working, which is the point of narrowing it to a content-type shape.
4. **Bidirectional overrides and invisible characters.** ``U+202E`` makes everything after it
   display in reverse: a stored name can render as a *different* name, and a file extension
   can display as another one. These are stripped rather than refused, because a client that
   sends them is not attacking anything - it is usually a paste from a document editor - and
   a name that is silently made readable beats a save that fails for no visible reason.
   ``U+200C``/``U+200D`` are explicitly **kept**: they are real orthography in Arabic and
   Persian, and stripping them would mangle legitimate names.
5. **Control characters, NUL, and repeated whitespace.** Stripped. A NUL in a name is a
   truncated comparison waiting to happen (SQLite's ``length()`` and Python's ``len`` disagree
   about a NUL, which is exactly how a check and a query end up seeing different strings).

WHAT IS ALLOWED
---------------
Names, site names and labels take an **allowlist**: Latin letters, Arabic letters in every
block an Arabic keyboard can produce, Arabic and Western digits, the harakat, spaces, and
``. , - _ ( ) /``. Two deliberate consequences:

* **Apostrophes are refused in identifiers.** ``O'Brien`` cannot be stored as a name. That is
  a real cost and it is paid on purpose: an apostrophe is the one character that both breaks
  an unquoted SQL literal *and* escapes an HTML attribute, and it is the only rule here that
  a person can notice. A deployment that needs it widens ``IDENTIFIER_PUNCTUATION`` below -
  one line, one place - rather than the allowlist being loose for everyone.
* **Arabic is a first-class citizen, not an exception.** The ranges are listed explicitly
  instead of using ``\\w``, so what is accepted is a decision that can be read and tested
  rather than whatever the current Unicode database says a word character is.

Prose (note bodies, replies, rejection reasons, invite notes) takes everything except the
four refusals above, because a worker writing "the lift did not work (again!) — 2 days lost"
must not be told their message contains an illegal character. Prose is where the escaping on
the output side is load-bearing; identifiers are where it is a second lock on the door.

WHAT THIS IS NOT
----------------
* **Not HTML escaping.** Nothing is turned into ``&lt;``. Storing escaped text double-escapes
  the moment it is rendered by something correct, and produces ``&amp;lt;`` in a CSV.
* **Not a length policy for passwords.** A password is never validated here: it is hashed,
  never rendered, and constraining its characters would only make it weaker. Passwords are
  checked for *strength* (``security.validate_password_strength``), which is a different
  question, and ``LoginRequest`` is left alone for the same reason - it is a credential
  comparison, not a stored write, and refusing a character there locks a person out of their
  own account.
* **Not authorization.** A perfectly clean string may still be a lie; who may write it is
  answered by the token, the role and the endpoints.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Final

# ---------------------------------------------------------------------------
# limits
# ---------------------------------------------------------------------------
#: A person's name. Long enough for four-part Arabic names with the family prefixes some
#: families keep, short enough that a name cannot be used as a note field.
MAX_NAME: Final = 80
#: A construction site. Sites are named after a compound plus a phase or a block.
MAX_SITE_NAME: Final = 120
#: One line of context: a note subject, a flag reason, a link note.
MAX_LABEL: Final = 160
#: An email address or a phone number. Both may carry punctuation a name cannot.
MAX_CONTACT: Final = 160
#: A note body or a reply. Deliberately generous: the point of prose validation is the
#: *content* rules, not the length, and the endpoints keep their own configurable ceilings.
MAX_NOTE: Final = 4000

# ---------------------------------------------------------------------------
# character classes
# ---------------------------------------------------------------------------
#: Arabic letters, block by block, so that "does an Arabic name pass?" is answerable by
#: reading this constant. ``\u0640`` (tatweel) is included by range below and kept: it is the
#: calligraphic elongation people type to justify a line, and it is not a security problem.
_ARABIC_LETTERS: Final = (
    "\u0621-\u063a"  # ء .. غ
    "\u0641-\u064a"  # ف .. ي
    "\u066e-\u06d3"  # the extended block: peh, tcheh, gaf, heh/yeh variants
    "\u06fa-\u06fc"
    "\u0750-\u077f"  # Arabic Supplement
    "\u08a0-\u08ff"  # Arabic Extended-A
    "\ufb50-\ufdfd"  # Presentation Forms-A (NFKC folds these; kept for idempotence)
    "\ufe70-\ufefc"  # Presentation Forms-B
)
#: The harakat and the dagger alef. Refusing these would refuse something like "مُحَمَّد"
#: written with its vowels.
_ARABIC_MARKS: Final = "\u064b-\u065f\u0670"
_LATIN: Final = "A-Za-z"
#: Western digits plus both Arabic-Indic sets. A phone number typed on an Arabic keyboard is
#: a phone number; refusing ``٠١٠`` would be a bug reported as "the app hates Arabic".
_DIGITS: Final = "0-9\u0660-\u0669\u06f0-\u06f9"

#: Everything an identifier may contain *besides* letters, marks and digits. See the module
#: docstring for why the apostrophe and the double quote are absent.
IDENTIFIER_PUNCTUATION: Final = " .,-_()/"

_IDENTIFIER_CLASS: Final = (
    _LATIN + _ARABIC_LETTERS + _ARABIC_MARKS + _DIGITS + re.escape(IDENTIFIER_PUNCTUATION)
)
#: ``fullmatch`` against this is the entire identifier rule.
IDENTIFIER_RE: Final = re.compile(rf"[{_IDENTIFIER_CLASS}]+")

#: Contact punctuation: what an email or a phone number legitimately carries, and nothing
#: else. ``+`` and parentheses are dialling notation; ``@``, ``.``, ``_`` and ``-`` are email.
_CONTACT_PUNCTUATION: Final = " @._+(),/-"
_CONTACT_CLASS: Final = (
    _LATIN + _ARABIC_LETTERS + _ARABIC_MARKS + _DIGITS + re.escape(_CONTACT_PUNCTUATION)
)
CONTACT_RE: Final = re.compile(rf"[{_CONTACT_CLASS}]+")

#: Characters that change how text reads without adding a glyph. Stripped, never refused:
#: a bidirectional override is a spoofing tool (``U+202E`` reverses everything after it) and
#: the zero-width space and BOM are paste damage. ``U+200C``/``U+200D`` are NOT here - they
#: are orthography in Arabic and Persian - and neither is the tatweel.
INVISIBLE: Final = (
    "\u200b\u200e\u200f"
    "\u202a\u202b\u202c\u202d\u202e"
    "\u2066\u2067\u2068\u2069"
    "\u061c\ufeff"
)
#: Kept after the strip above, and named so that the exception is visible rather than implied.
_ORTHOGRAPHIC_INVISIBLE: Final = "\u200c\u200d"

# ---------------------------------------------------------------------------
# active content
# ---------------------------------------------------------------------------
#: A tag opening, however it is spaced: ``<a``, ``< a``, ``</a``, ``<!doctype``, ``<?php`` -
#: and ``<%2F``, the percent-encoded ``</`` that survives a trip through a URL and comes back
#: as a tag the moment anything decodes it.
MARKUP_RE: Final = re.compile(r"<\s*/?\s*[A-Za-z!?%\u00a1]")
#: An entity that decodes into something structural. ``&amp;lt;`` is caught because the
#: reader that decodes twice is the one this rule exists for.
ENTITY_RE: Final = re.compile(
    r"&(?:lt|gt|amp|quot|apos|colon|sol|NewLine|tab|#\d{1,7}|#x[0-9a-fA-F]{1,6})\s*;",
    re.IGNORECASE,
)
#: Script schemes always; ``data:`` only when it carries a content type, so that ordinary
#: prose ("the data: it never arrived") is not refused for containing a word and a colon.
SCHEME_RE: Final = re.compile(
    r"(?:javascript|vbscript|mocha|livescript)\s*:"
    r"|data\s*:\s*(?:text|image|audio|video|application|;|font|\w+/|,)",
    re.IGNORECASE,
)
#: ``onclick=`` without a tag is inert, but it is exactly what a payload looks like, and a
#: quarantine that only catches the tag has to be right about spacing to work at all.
EVENT_RE: Final = re.compile(r"\bon[a-z]{3,}\s*=\s*[\"']?", re.IGNORECASE)

_REASONS: Final = (
    (MARKUP_RE, "an HTML tag"),
    (ENTITY_RE, "an encoded HTML entity"),
    (SCHEME_RE, "a javascript: or data: URL"),
)


# ---------------------------------------------------------------------------
# normalisation
# ---------------------------------------------------------------------------
def normalise(value: Any, *, multiline: bool = False) -> str:
    """Fold the ways one string can be written into the one way it is stored.

    Four things happen, in this order, and the order matters:

    1. **NFKC** - so ``Ａ`` (fullwidth) and ``A`` are the same name, ``ﻣ`` (presentation form)
       and ``م`` are the same letter, and a stored value compares equal to itself after a
       round trip through any client. Without this, two rows can look identical on screen and
       be different keys in the database.
    2. **Invisible characters dropped** - the bidirectional overrides and the zero-width
       paste damage. See ``INVISIBLE``.
    3. **Control characters dropped** - ``Cc`` and the remaining ``Cf``, with tabs and
       newlines converted to whitespace first. A NUL is the important one: SQLite truncates a
       string at a NUL for ``length()`` while Python does not, so a value that passes a
       Python check can be a different value by the time a query sees it.
    4. **Whitespace collapsed** - runs of spaces and tabs become one space, and (in multiline
       mode) at most one blank line survives. A name is padded with five spaces often enough
       that storing those spaces makes a roster look ragged and a "client_id" comparison fail.
    """
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value))

    kept: list[str] = []
    for char in text:
        if char in INVISIBLE:
            continue
        if char in _ORTHOGRAPHIC_INVISIBLE:
            kept.append(char)
            continue
        if char in "\r\n":
            kept.append("\n" if multiline else " ")
            continue
        if char == "\t":
            kept.append(" ")
            continue
        if unicodedata.category(char) in {"Cc", "Cf"}:
            continue
        kept.append(char)
    text = "".join(kept)

    if multiline:
        text = re.sub(r"[^\S\n]+", " ", text)
        text = re.sub(r"\n{2,}", "\n", text)
        text = "\n".join(line.strip() for line in text.split("\n"))
    else:
        text = re.sub(r"\s+", " ", text)
    return text.strip()


def active_content_reason(value: str) -> str | None:
    """The first thing in ``value`` that would be code in a browser, or ``None``.

    Returned as a phrase rather than a boolean so that the error a person sees can say what
    was found ("contains an HTML tag") instead of naming a regex they did not write.
    """
    for pattern, reason in _REASONS:
        if pattern.search(value):
            return reason
    if EVENT_RE.search(value):
        return "an inline event handler"
    return None


def describe_char(char: str) -> str:
    """``U+003C '<' `` - a message a person can act on, in a language they may not read."""
    name = unicodedata.name(char, "UNNAMED CHARACTER")
    return f"U+{ord(char):04X} {char!r} ({name.title()})"


def _refuse_disallowed(text: str, pattern: re.Pattern[str], *, field: str, allowed: str) -> None:
    """Raise on the first character the allowlist does not contain, naming it."""
    for char in text:
        if pattern.fullmatch(char):
            continue
        raise ValueError(
            f"{field} may contain {allowed}. It contains {describe_char(char)}, which is not "
            f"one of them."
        )


# ---------------------------------------------------------------------------
# the three profiles
# ---------------------------------------------------------------------------
def identifier(
    value: Any,
    *,
    field: str,
    max_length: int = MAX_NAME,
    allow_empty: bool = False,
) -> str:
    """An allowlisted single-line identifier: a name, a site, a label, a category.

    Raises ``ValueError``, which is what a Pydantic ``field_validator`` wants: the message
    becomes the 422 the client reads. Form-based endpoints catch it and re-raise as a 400
    (``http_error``).

    ``allow_empty`` exists for the optional fields (``site_name`` on a forced punch in, the
    name on an enrollment invite) where "" means "not given" rather than "invalid".
    """
    text = normalise(value)
    if not text:
        if allow_empty:
            return ""
        raise ValueError(f"{field} must not be empty.")
    if len(text) > max_length:
        raise ValueError(
            f"{field} must be at most {max_length} characters; this one is {len(text)}."
        )
    allowed = (
        "letters (English or Arabic), digits, spaces and any of "
        f"{' '.join(IDENTIFIER_PUNCTUATION.strip())}"
    )
    _refuse_disallowed(text, IDENTIFIER_RE, field=field.capitalize(), allowed=allowed)
    reason = active_content_reason(text)
    if reason is not None:
        # Unreachable through the allowlist above, and kept anyway: if somebody widens
        # IDENTIFIER_PUNCTUATION to admit a character, the content rules still apply.
        raise ValueError(f"{field} contains {reason}. Please send the name as plain text.")
    return text


def contact(value: Any, *, field: str, allow_empty: bool = True) -> str:
    """An email address or a phone number: the punctuation those carry, nothing else."""
    text = normalise(value)
    if not text:
        if allow_empty:
            return ""
        raise ValueError(f"{field} must not be empty.")
    if len(text) > MAX_CONTACT:
        raise ValueError(
            f"{field} must be at most {MAX_CONTACT} characters; this one is {len(text)}."
        )
    _refuse_disallowed(
        text,
        CONTACT_RE,
        field=field.capitalize(),
        allowed="letters, digits, spaces and any of @ . _ + ( ) , - /",
    )
    return text


def prose(
    value: Any,
    *,
    field: str,
    max_length: int = MAX_NOTE,
    allow_empty: bool = False,
) -> str:
    """Free text with the dangerous shapes removed and everything else left alone.

    Everything except markup, encoded markup and script URLs is *kept*: apostrophes,
    ampersands and semicolons are ordinary writing, and refusing them in a note about a
    broken lift would be the validator inventing a problem. Those characters are harmless
    here precisely because every renderer escapes what it interpolates; the two rules that
    remain are the ones that survive a renderer that does not.
    """
    text = normalise(value, multiline=True)
    if not text:
        if allow_empty:
            return ""
        raise ValueError(f"{field} must not be empty.")
    if len(text) > max_length:
        raise ValueError(
            f"{field} must be at most {max_length} characters; this one is {len(text)}."
        )
    reason = active_content_reason(text)
    if reason is not None:
        raise ValueError(
            f"{field} contains {reason}. Please write it as plain text: no HTML tags, no "
            f"encoded entities, no javascript:/data: links."
        )
    return text


# ---------------------------------------------------------------------------
# http glue
# ---------------------------------------------------------------------------
def http_error(exc: ValueError) -> Exception:
    """Wrap a refusal as the 400 an HTTP endpoint should answer with.

    Imported lazily so that this module stays importable without FastAPI (it is pure string
    policy, and the tests exercise it that way).
    """
    from fastapi import HTTPException

    return HTTPException(status_code=400, detail=str(exc))


def policy() -> dict[str, Any]:
    """What this module enforces, for ``/status/detail`` and the operations README."""
    return {
        "identifier_punctuation": IDENTIFIER_PUNCTUATION,
        "identifier_scripts": "Latin, Arabic (all blocks a keyboard produces), Western and Arabic-Indic digits",
        "prose_refused": [
            "HTML tags and tag openings",
            "HTML entities that decode into markup (&lt;script&gt;)",
            "javascript: / vbscript: URLs, data: URLs carrying a content type",
            "inline event handlers (onclick=)",
        ],
        "stripped": "bidirectional overrides, zero-width space, BOM, control characters, NUL",
        "kept": "Arabic orthographic joiners (U+200C/U+200D), tatweel, harakat, all ordinary punctuation",
        "max_lengths": {
            "name": MAX_NAME,
            "site_name": MAX_SITE_NAME,
            "label": MAX_LABEL,
            "contact": MAX_CONTACT,
            "note": MAX_NOTE,
        },
    }
