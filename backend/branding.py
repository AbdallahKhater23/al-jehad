"""The company's own name and mark: one row, read by every screen and every sheet.

WHY THIS MODULE EXISTS
----------------------
The wordmark, the legal suffix, the founding year, the tagline and the mark itself used to
be a JavaScript object and an SVG file in the frontend. That is the correct home for the
*artwork* and the wrong home for the *name*: every surface that says whose payroll this is
- the login panel, the handset header, the console rail, the printed timesheet - read that
constant, so a company that renamed itself, or wanted its own logo at the top of the sheet
it hands to payroll, needed a code change and a redeploy to get one.

So the company's words and its mark are a settings row, next door to ``shift_rules`` and
editable where the company is administered (Admin -> Company). The frontend keeps the
shipped lockup as its *default*, and this module stores only what somebody has deliberately
set: nothing changes for a deployment that never opens the panel.

NULL AND THE EMPTY STRING ARE DIFFERENT, ON PURPOSE
---------------------------------------------------
``None`` in a column means nobody has touched it, and the shipped lockup is what prints. An
empty string means the company decided that line is not printed - a business with no legal
suffix and no founding year has to be able to remove those lines, and a rule of "blank means
the shipped value" would put them straight back. Both states therefore survive the wire, and
``read()`` reports them apart.

WHY THE MARK IS RE-ENCODED RATHER THAN STORED AS SENT
-----------------------------------------------------
Two reasons, and the second is the one that matters:

* **Size.** A phone photo of a logo is several megabytes and 4000 px wide; a printed header
  draws it at roughly 40 mm. It is scaled to ``MAX_LOGO_EDGE`` and written in the format
  its own content calls for - PNG when it has transparency to keep, JPEG when it does not,
  which is a 40 KB upload instead of a 400 KB one for an opaque mark;
* **Safety.** The bytes this server hands back are the bytes it built itself, from a decoded
  and re-encoded image. An uploaded file is never served verbatim, so a file that is a
  polyglot, an HTML document with an image header, or a JPEG with an embedded payload is
  simply not what a browser can end up fetching. The type is one of two this module chose,
  not one the client declared.

The pixel ceiling, the byte ceiling and the type sniffing all come from ``uploads`` rather
than being restated here: an uploaded logo is an uploaded image like every other in this
application, and one policy for those is the whole point of that module.

WHAT THE LINES ARE CHECKED FOR
------------------------------
Two rules, and only one of them is this module's. A lockup line is printed on *one* line -
a company name with a line break in it is a sheet nobody can lay out - so a newline is
refused by name, with the field and the reason. Everything else is ``textguard``'s, the same
policy every stored name in this application goes through: markup, encoded markup and
``javascript:`` URLs are refused, and invisible characters are stripped before storage.

It is checked as *prose* rather than as an identifier on purpose. A company called
"Smith & Sons" and a tagline of "STONE · MARBLE · GRANITE" are ordinary writing, and the
identifier allowlist would refuse both; what makes prose safe here is that every one of
these interpolations is escaped where it is drawn, and the content rules are the second lock
on the door for the renderer that is not.
"""

from __future__ import annotations

import io
import sqlite3
from datetime import datetime
from typing import Any

from fastapi import HTTPException

import textguard
import uploads

#: The company's own words, as the console names them -> the column each one lives in.
FIELDS: dict[str, str] = {
    "name": "company_name",
    "legal": "company_legal",
    "est": "company_est",
    "tagline": "company_tagline",
}

#: What a refusal calls each line. The console's own labels are translated; these name the
#: field in the language the API answers in, like every other endpoint's refusals.
FIELD_LABELS: dict[str, str] = {
    "name": "Company name",
    "legal": "Legal name",
    "est": "Founding year",
    "tagline": "Tagline",
}

#: How long one of those lines may be. A wordmark is a few words: the limit is here rather
#: than only in the console because these strings are printed into a fixed-width sheet and
#: drawn into a header, and a name of ten thousand characters is a layout the reader never
#: gets back. Generous enough that no real company hits it.
MAX_FIELD_CHARS: dict[str, int] = {"name": 60, "legal": 60, "est": 24, "tagline": 80}

#: The longest edge of a stored mark, in pixels. A logo prints at roughly 40 mm, which is
#: 480 px at 300 dpi - so this is the largest edge that can still be sharp on paper, and it
#: is also the point past which the payload stops being worth fetching on site cellular.
MAX_LOGO_EDGE = 512

#: JPEG quality for a mark with no transparency to keep. A 512x512 photographic mark is
#: ~40 KB as JPEG against ~400 KB as PNG, and this image is fetched by phones on site.
JPEG_QUALITY = 88

MIME_PNG = "image/png"
MIME_JPEG = "image/jpeg"

ERR_TOO_LONG = "branding_field_too_long"
ERR_ONE_LINE = "branding_field_not_one_line"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _ensure_row(conn: sqlite3.Connection) -> None:
    """The single settings row exists even on a database that predates this table.

    ``ensure_schema`` creates it, but this module is also called from tests and from a
    two-worker deployment whose migrations ran in the other process, and a settings read
    that raises because a row is missing would take the login screen down with it.
    """
    conn.execute(
        "INSERT OR IGNORE INTO company_settings (id, updated_at) VALUES (1, ?)", (_now(),)
    )


def _row(conn: sqlite3.Connection) -> Any:
    _ensure_row(conn)
    return conn.execute("SELECT * FROM company_settings WHERE id = 1").fetchone()


def clean_text(key: str, value: Any) -> str | None:
    """One line of the lockup, as it will be stored - or a refusal naming what is wrong.

    ``None`` means "nobody has touched this" and is passed through by the caller rather than
    arriving here (the endpoint decides that from which fields the request actually sent);
    a *string* - including an empty one - is what the company set, so it is trimmed of
    surrounding whitespace and checked to be one printable line. Newlines and control
    characters are refused rather than stripped: silently collapsing somebody's company name
    into something they did not type is worse than telling them.

    The line-break rule is this module's own - it is a fact about the *paper* this text ends
    up on, not about text in general - and everything else is ``textguard``'s, the same
    policy every stored name in this application goes through: markup, encoded markup and
    ``javascript:`` URLs are refused outright, and zero-width and bidirectional characters
    are stripped.

    It is ``prose`` rather than ``identifier``, deliberately. These four lines are a legal
    name and a tagline, not a filable identifier: a company called "Smith & Sons" and a
    tagline of "STONE · MARBLE · GRANITE" are both ordinary writing, and the identifier
    allowlist (letters, digits, ``.,-_()/``) would refuse them. What makes that safe is the
    output side, which escapes every one of these interpolations - and the content rules
    above are the second lock on the door for the renderer that does not.
    """
    text = "" if value is None else str(value)
    text = text.strip()
    limit = MAX_FIELD_CHARS.get(key, 60)
    if len(text) > limit:
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": ERR_TOO_LONG,
                "message": f"That is {len(text)} characters; the limit for this line is {limit}.",
                "limit": limit,
                "field": key,
            },
        )
    if any(character < " " or character == "\x7f" for character in text):
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": ERR_ONE_LINE,
                "message": "This line is printed on one line, so it cannot contain a line break.",
                "field": key,
            },
        )
    try:
        return textguard.prose(
            text,
            field=FIELD_LABELS.get(key, key),
            max_length=limit,
            allow_empty=True,
        )
    except ValueError as exc:
        raise textguard.http_error(exc)


def read(conn: sqlite3.Connection) -> dict[str, Any]:
    """What is configured: the four lines and the mark, exactly as they are stored.

    The keys are always present. ``None`` is "not configured" (the caller keeps the shipped
    lockup), ``''`` is "deliberately blank" (the caller prints nothing for that line) - see
    the module docstring for why those two have to stay apart.
    """
    row = _row(conn)
    payload: dict[str, Any] = {key: row[column] for key, column in FIELDS.items()}
    payload["logo"] = _logo_summary(row)
    payload["updated_at"] = row["updated_at"]
    payload["updated_by"] = row["updated_by"]
    return payload


def _logo_summary(row: Any) -> dict[str, Any] | None:
    if row is None or row["logo_bytes"] is None:
        return None
    return {
        #: Bumped on every write, so the URL a screen holds includes the version of the
        #: bytes it was drawn from and a replacement can never be served from a cache.
        "version": int(row["logo_version"] or 0),
        "mime": row["logo_mime"] or MIME_PNG,
        "width": int(row["logo_width"] or 0),
        "height": int(row["logo_height"] or 0),
        "bytes": len(row["logo_bytes"]),
    }


def logo_bytes(conn: sqlite3.Connection) -> tuple[bytes, str, int] | None:
    """The stored mark, for the one endpoint that serves it: ``(bytes, mime, version)``.

    Reading it here rather than in the route keeps the column names in one module - and this
    is also what makes the version that travels in the URL come from the same row as the
    bytes it names.
    """
    row = _row(conn)
    if row["logo_bytes"] is None:
        return None
    return bytes(row["logo_bytes"]), row["logo_mime"] or MIME_PNG, int(row["logo_version"] or 0)


def write(
    conn: sqlite3.Connection,
    supplied: dict[str, Any],
    *,
    actor_id: str | None = None,
) -> dict[str, Any]:
    """Store the lines the caller sent: ``{before, after, branding}``.

    Only the keys present in ``supplied`` are touched, so a panel that saves one box cannot
    silently clear the others. A value of ``None`` puts the column back to "not configured"
    (the shipped lockup prints again), and a string - empty or not - is stored as given.

    ``before`` and ``after`` cover exactly the fields that were touched, which is the shape
    the audit trail records: the value that was there and the value that replaced it.
    """
    before = read(conn)
    changes: dict[str, Any] = {}
    updates: dict[str, Any] = {}
    for key, value in supplied.items():
        if key not in FIELDS:
            continue
        stored = None if value is None else clean_text(key, value)
        updates[FIELDS[key]] = stored
        changes[key] = stored
    if updates:
        _ensure_row(conn)
        assignments = ", ".join(f"{column} = ?" for column in updates)
        conn.execute(
            f"UPDATE company_settings SET {assignments}, updated_at = ?, updated_by = ? WHERE id = 1",
            (*updates.values(), _now(), actor_id),
        )
    return {
        "before": {key: before[key] for key in changes},
        "after": changes,
        "branding": read(conn),
    }


def set_logo(
    conn: sqlite3.Connection, data: bytes, *, field: str = "logo", actor_id: str | None = None
) -> dict[str, Any]:
    """Store an uploaded mark, after re-encoding it into something this server drew.

    The validation is ``uploads``': the byte ceiling is enforced while the body is read (by
    the endpoint, before this is called), and the decode here checks the bytes really are an
    image and bounds the pixel count before any pixels are touched.
    """
    uploads.validate_photo_bytes(data, field=field)
    image = uploads.decode_photo(data, field=field, keep_alpha=True)
    image.thumbnail((MAX_LOGO_EDGE, MAX_LOGO_EDGE))
    encoded, mime = encode_logo(image)
    if not encoded:
        raise HTTPException(
            status_code=422,
            detail={"error_code": "unreadable_image", "message": f"The {field} could not be re-encoded."},
        )
    _ensure_row(conn)
    conn.execute(
        "UPDATE company_settings SET logo_bytes = ?, logo_mime = ?, logo_width = ?, logo_height = ?, "
        "logo_version = COALESCE(logo_version, 0) + 1, updated_at = ?, updated_by = ? WHERE id = 1",
        (encoded, mime, image.width, image.height, _now(), actor_id),
    )
    return read(conn)


def clear_logo(conn: sqlite3.Connection, *, actor_id: str | None = None) -> bool:
    """Remove the mark, so the shipped one is used again. Returns whether one was there."""
    row = _row(conn)
    had = row["logo_bytes"] is not None
    conn.execute(
        "UPDATE company_settings SET logo_bytes = NULL, logo_mime = NULL, logo_width = NULL, "
        "logo_height = NULL, logo_version = COALESCE(logo_version, 0) + 1, updated_at = ?, "
        "updated_by = ? WHERE id = 1",
        (_now(), actor_id),
    )
    return had


def encode_logo(image: Any) -> tuple[bytes, str]:
    """The stored form of a mark: PNG when it has transparency, JPEG when it does not.

    Transparency is the deciding factor and not the source format: a mark drawn on a
    transparent ground has to keep it, because the sheet it is printed on is white and the
    login panel it is drawn on is green - a JPEG would give it a box of one colour that is
    wrong on the other. Everything else is a picture, and JPEG is a tenth of the size.
    """
    buffer = io.BytesIO()
    if uploads.has_alpha(image):
        image.convert("RGBA").save(buffer, format="PNG", optimize=True)
        return buffer.getvalue(), MIME_PNG
    image.convert("RGB").save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return buffer.getvalue(), MIME_JPEG


def payload_for_api(conn: sqlite3.Connection, *, base_path: str = "/branding/logo") -> dict[str, Any]:
    """What a screen needs: the lines, and a URL for the mark when there is one.

    The URL is a *path* rather than an absolute one - the page may be served from a LAN
    address, a port of its own or an HTTPS tunnel, and only the client knows which origin it
    is talking to (``API.baseURL``). ``?v=`` is the stored version, so a replaced mark is a
    different URL and no cache anywhere can keep serving the old one.
    """
    data = read(conn)
    logo = data["logo"]
    return {
        **{key: data[key] for key, _ in FIELDS.items()},
        "logo_url": f"{base_path}?v={logo['version']}" if logo else None,
        "logo": logo,
        "updated_at": data["updated_at"],
    }
