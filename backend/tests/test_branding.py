"""The company's own name and mark: public to read, administrator-only to write.

WHY THIS SUITE EXISTS
---------------------
Four words and an image used to be a constant in ``frontendjavascript.js`` and an SVG file
beside it, so every screen that says whose payroll this is - and the sheet an administrator
hands to payroll - needed a code change and a redeploy to carry a different name. They are a
settings row now, next door to the shift rules, and this file is what holds the four
properties that make that a feature rather than a source of quiet wrongness:

1. **Reading is public.** The screen that most needs the company's name is the login panel,
   which by definition has no session. What is exposed is the name the company put on its own
   front door and the mark it prints on its own documents - not one account detail.
2. **Writing is not.** A worker, a moallem and an unauthenticated request are all refused;
   the role that may set the company's identity is the same one that may set its shift rules.
3. **``null`` and ``''`` are different all the way out.** ``null`` is "nobody configured
   this" - the shipped lockup prints - and ``''`` is "the company removed this line". A
   reader that flattened them would put a legal suffix back on the sheet of a company that
   deliberately took it off, which is why the round trip is asserted rather than assumed.
4. **The stored bytes are ones this server wrote.** An uploaded mark is decoded, bounded and
   re-encoded, so what a browser can fetch afterwards is never the file that was posted - a
   polyglot, a document with an image header, or a 4000-pixel phone photo of a business card.

The audit trail is asserted too: "who put this logo on our payslips, and when" is the whole
reason the write records the *shape* of the mark rather than its bytes.
"""

from __future__ import annotations

import io
import sqlite3
import struct
import zlib

import pytest
from PIL import Image

import harness
from harness import ADMIN, HEAD_ADMIN, MOALLEM, WORKER, assert_denied, bearer, db_rows, db_scalar

BRANDING = "/api/v1/branding"
LOGO = "/api/v1/branding/logo"
ADMIN_BRANDING = "/api/v1/admin/branding"
ADMIN_LOGO = "/api/v1/admin/branding/logo"

#: The four lines, in the order the console shows them, with the column each one lives in.
LINES = {
    "name": "company_name",
    "legal": "company_legal",
    "est": "company_est",
    "tagline": "company_tagline",
}


def image(size=(600, 600), mode="RGBA") -> bytes:
    """A mark, in the shape a company is most likely to upload one."""
    buffer = io.BytesIO()
    fill = (20, 83, 45, 255) if mode == "RGBA" else (20, 83, 45)
    Image.new(mode, size, fill).save(buffer, format="PNG")
    return buffer.getvalue()


def a_pdf() -> bytes:
    """Something that is not an image, with a name and a type that claim it is."""
    return b"%PDF-1.4\n" + b"0" * 4096


def png_claiming(size=(9000, 9000)) -> bytes:
    """A small PNG whose header claims to be enormous - the decompression bomb, cheaply.

    The bytes are a real 40x40 PNG with its IHDR dimensions rewritten (and the chunk's CRC
    recomputed, because PIL checks it). That is the whole point: a header read alone says
    9000x9000, and eleven kilobytes of file would be two hundred megabytes of bitmap if
    anything decoded it. Writing a real 64-megapixel image instead would make the suite slow
    and would prove the same thing.
    """
    buffer = io.BytesIO()
    Image.new("RGB", (40, 40), (20, 83, 45)).save(buffer, format="PNG")
    raw = bytearray(buffer.getvalue())
    struct.pack_into(">II", raw, 16, size[0], size[1])
    length = struct.unpack(">I", raw[8:12])[0]
    crc = zlib.crc32(bytes(raw[12 : 12 + 4 + length])) & 0xFFFFFFFF
    struct.pack_into(">I", raw, 12 + 4 + length, crc)
    return bytes(raw)


def blob(sql: str, params: tuple = ()) -> bytes | None:
    """A stored image. ``db_scalar`` reads text and happy-path values; a BLOB is its own
    helper because ``bytes`` is what has to be compared with what the endpoint served."""
    conn = sqlite3.connect(harness.DB_PATH)
    try:
        row = conn.execute(sql, params).fetchone()
    finally:
        conn.close()
    return bytes(row[0]) if row and row[0] is not None else None


def write(sql: str, params: tuple = ()) -> None:
    conn = sqlite3.connect(harness.DB_PATH)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. reading: public, and shaped so the caller can tell null from blank
# ---------------------------------------------------------------------------
def test_a_deployment_that_never_opened_the_panel_says_nothing(client):
    """Every key present, every line ``None`` - which is what makes the shipped lockup the
    default rather than something somebody configured."""
    response = client.get(BRANDING)
    assert response.status_code == 200, response.text
    body = response.json()
    for key in LINES:
        assert body[key] is None, f"{key} is configured on a pristine database"
    assert body["logo"] is None
    assert body["logo_url"] is None


def test_the_login_screen_can_read_it_without_a_token(client):
    """No ``Authorization`` header at all: this is the read the sign-in panel makes."""
    assert client.get(BRANDING).status_code == 200


def test_the_mark_is_served_as_a_path_with_the_stored_version_on_it(client):
    """``logo_url`` is a path, because only the client knows which origin it is on, and the
    version in it is what makes a replaced mark a *different* URL rather than a cached one."""
    client.post(
        ADMIN_LOGO, headers=bearer(HEAD_ADMIN), files={"logo": ("mark.png", image(), "image/png")}
    )
    body = client.get(BRANDING).json()
    assert body["logo_url"].startswith("/branding/logo?v=")
    assert body["logo_url"].endswith(str(body["logo"]["version"]))


def test_a_mark_that_was_never_set_is_a_404_not_an_empty_image(client):
    """A screen asking for a logo that is not there has to be able to tell - an empty 200
    would be drawn as a mark that is silently missing."""
    assert client.get(LOGO).status_code == 404


# ---------------------------------------------------------------------------
# 2. writing: an administrator's act, and only an administrator's
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "headers,who",
    [
        (None, "nobody signed in"),
        (bearer(WORKER), "a worker"),
        (bearer(MOALLEM), "a moallem"),
    ],
)
def test_only_an_administrator_may_rename_the_company(client, headers, who):
    assert_denied(
        client.post(ADMIN_BRANDING, headers=headers, json={"name": "Somebody Else Ltd"}),
        endpoint=ADMIN_BRANDING,
        detail=f"the company name must not be writable by {who}",
    )
    assert db_scalar("SELECT company_name FROM company_settings WHERE id = 1") is None, (
        f"{who} was able to rename the company"
    )


def test_the_mark_is_the_administrators_act_too(client):
    """The upload endpoint is the one that writes a blob, so it is checked on its own rather
    than inferred from the endpoint next door."""
    assert_denied(
        client.post(
            ADMIN_LOGO,
            headers=bearer(WORKER),
            files={"logo": ("mark.png", image(), "image/png")},
        ),
        endpoint=ADMIN_LOGO,
        detail="a worker must not be able to set the company's mark",
    )
    assert db_scalar("SELECT logo_bytes FROM company_settings WHERE id = 1") is None


def test_an_ordinary_administrator_may_set_the_name(client):
    """``admin``, not ``head_admin``: this is a deployment setting like the shift rules, not
    an account's own profile - and the person who runs the console day to day is the one who
    knows what the company is called."""
    response = client.post(
        ADMIN_BRANDING, headers=bearer(ADMIN), json={"name": "Al-Jehad Stone Works"}
    )
    assert response.status_code == 200, response.text
    assert db_scalar("SELECT company_name FROM company_settings WHERE id = 1") == "Al-Jehad Stone Works"


def test_saving_one_line_leaves_the_others_alone(client):
    """The panel is one form, but the endpoint is four optional fields: a client that sends
    only what changed must not blank the rest."""
    client.post(ADMIN_BRANDING, headers=bearer(HEAD_ADMIN), json={"name": "First", "legal": "Second"})
    client.post(ADMIN_BRANDING, headers=bearer(HEAD_ADMIN), json={"tagline": "Third"})
    body = client.get(BRANDING).json()
    assert (body["name"], body["legal"], body["tagline"]) == ("First", "Second", "Third")
    assert body["est"] is None


def test_an_empty_request_is_refused_rather_than_treated_as_a_reset(client):
    """``{}`` reaches the endpoint only from a caller that has decided nothing. Answering it
    with a 200 would tell that caller its write landed."""
    response = client.post(ADMIN_BRANDING, headers=bearer(HEAD_ADMIN), json={})
    assert response.status_code == 400
    assert response.json()["detail"] == "Nothing to save."


# ---------------------------------------------------------------------------
# 3. null is "shipped", blank is "not printed"
# ---------------------------------------------------------------------------
def test_an_empty_line_is_stored_and_read_back_as_empty(client):
    """The company removed the line. Nothing prints there, and no default may sneak back."""
    client.post(ADMIN_BRANDING, headers=bearer(HEAD_ADMIN), json={"legal": "", "est": ""})
    body = client.get(BRANDING).json()
    assert body["legal"] == ""
    assert body["est"] == ""
    assert db_scalar("SELECT company_legal FROM company_settings WHERE id = 1") == ""


def test_null_puts_the_column_back_to_not_configured(client):
    """The undo of the line above: the shipped lockup prints again, which is a different
    answer from an empty string and is stored as a different value."""
    client.post(ADMIN_BRANDING, headers=bearer(HEAD_ADMIN), json={"legal": "INTERNATIONAL CO."})
    client.post(ADMIN_BRANDING, headers=bearer(HEAD_ADMIN), json={"legal": None})
    assert client.get(BRANDING).json()["legal"] is None
    assert db_scalar("SELECT company_legal FROM company_settings WHERE id = 1") is None


def test_the_two_states_are_told_apart_in_the_audit_row(client):
    """``before_json``/``after_json`` are what the log answers an argument with, so the value
    that was there and the one that replaced it have to survive into it as themselves - an
    empty string readable as a decision rather than as a missing value."""
    client.post(ADMIN_BRANDING, headers=bearer(HEAD_ADMIN), json={"legal": "INTERNATIONAL CO."})
    client.post(ADMIN_BRANDING, headers=bearer(HEAD_ADMIN), json={"legal": ""})
    before_json, after_json = db_rows(
        "SELECT before_json, after_json FROM audit_log "
        "WHERE action = 'branding_update' ORDER BY id DESC LIMIT 1"
    )[0]
    assert '"legal": "INTERNATIONAL CO."' in before_json
    assert '"legal": ""' in after_json


def test_a_line_put_back_to_shipped_is_logged_as_null(client):
    """The other half: the reset is a change from a value to ``null``, and a log that wrote
    "" (or omitted it) would be describing a different edit."""
    client.post(ADMIN_BRANDING, headers=bearer(HEAD_ADMIN), json={"legal": "INTERNATIONAL CO."})
    client.post(ADMIN_BRANDING, headers=bearer(HEAD_ADMIN), json={"legal": None})
    _before, after_json = db_rows(
        "SELECT before_json, after_json FROM audit_log "
        "WHERE action = 'branding_update' ORDER BY id DESC LIMIT 1"
    )[0]
    assert '"legal": null' in after_json


# ---------------------------------------------------------------------------
# 4. the lines themselves: one line, bounded, and no markup
# ---------------------------------------------------------------------------
def test_a_line_longer_than_the_limit_is_refused_by_name(client):
    response = client.post(ADMIN_BRANDING, headers=bearer(HEAD_ADMIN), json={"name": "A" * 61})
    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert detail["error_code"] == "branding_field_too_long"
    assert detail["limit"] == 60
    assert detail["field"] == "name"
    assert db_scalar("SELECT company_name FROM company_settings WHERE id = 1") is None


def test_a_line_break_is_refused_rather_than_collapsed(client):
    """A company name is printed on one line and drawn into a header. Silently joining the
    two halves would store a name nobody typed; the refusal says what to do instead."""
    response = client.post(
        ADMIN_BRANDING, headers=bearer(HEAD_ADMIN), json={"name": "Al-Jehad\nInternational"}
    )
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["error_code"] == "branding_field_not_one_line"


@pytest.mark.parametrize(
    "hostile",
    [
        "<script>alert(1)</script>",
        "Al-Jehad &lt;img src=x onerror=alert(1)&gt;",
        "javascript:alert(1)",
    ],
)
def test_markup_is_refused_at_the_door(client, hostile):
    """Every renderer escapes what it interpolates - that is why these would be safe to
    *store* - and this is the other lock: a name that is never markup cannot become markup
    through a renderer that forgets."""
    response = client.post(ADMIN_BRANDING, headers=bearer(HEAD_ADMIN), json={"name": hostile})
    assert response.status_code == 400, response.text
    assert db_scalar("SELECT company_name FROM company_settings WHERE id = 1") is None


@pytest.mark.parametrize(
    "ordinary",
    [
        "Smith & Sons",
        "AL-JEHAD INTERNATIONAL CO.",
        "STONE · MARBLE · GRANITE",
        "شركة الجهاد الدولية",
        "Al-Jehad (Downtown)",
    ],
)
def test_ordinary_company_writing_is_accepted(client, ordinary):
    """The rule has to admit a real company name, or it is a rule against companies with
    ampersands in their name rather than a rule against markup."""
    response = client.post(ADMIN_BRANDING, headers=bearer(HEAD_ADMIN), json={"name": ordinary})
    assert response.status_code == 200, response.text
    assert db_scalar("SELECT company_name FROM company_settings WHERE id = 1") == ordinary


def test_invisible_characters_are_stripped_rather_than_stored(client):
    """A right-to-left override in a wordmark reverses the rest of the line it is drawn on,
    which is a rendering failure rather than a name."""
    response = client.post(
        ADMIN_BRANDING, headers=bearer(HEAD_ADMIN), json={"name": "Al-\u202eJehad\u200b"}
    )
    assert response.status_code == 200, response.text
    assert db_scalar("SELECT company_name FROM company_settings WHERE id = 1") == "Al-Jehad"


# ---------------------------------------------------------------------------
# 5. the mark: re-encoded by this server, and never served as sent
# ---------------------------------------------------------------------------
def test_an_uploaded_mark_becomes_a_png_this_server_drew(client):
    uploaded = image(size=(1200, 800), mode="RGBA")
    response = client.post(
        ADMIN_LOGO, headers=bearer(HEAD_ADMIN), files={"logo": ("mark.png", uploaded, "image/png")}
    )
    assert response.status_code == 200, response.text
    stored = response.json()["branding"]["logo"]
    # Scaled to the printable ceiling, and the type is one this module chose.
    assert max(stored["width"], stored["height"]) == 512
    assert stored["mime"] == "image/png"

    served = client.get(LOGO)
    assert served.status_code == 200
    assert served.headers["content-type"] == "image/png"
    assert served.content == blob("SELECT logo_bytes FROM company_settings WHERE id = 1")
    assert served.content != uploaded, "the uploaded bytes were served back verbatim"


def test_an_opaque_mark_is_stored_as_a_jpeg(client):
    """A tenth of the size, and there is no transparency to lose: the sheet it prints on is
    white and the login panel is green, so a PNG here is several hundred KB of nothing."""
    response = client.post(
        ADMIN_LOGO,
        headers=bearer(HEAD_ADMIN),
        files={"logo": ("mark.png", image(size=(900, 900), mode="RGB"), "image/png")},
    )
    assert response.status_code == 200, response.text
    assert response.json()["branding"]["logo"]["mime"] == "image/jpeg"
    assert client.get(LOGO).headers["content-type"] == "image/jpeg"


def test_transparency_survives_the_round_trip(client):
    """A mark drawn on a transparent ground keeps it, or it prints as a coloured box."""
    client.post(
        ADMIN_LOGO,
        headers=bearer(HEAD_ADMIN),
        files={"logo": ("mark.png", image(size=(300, 300), mode="RGBA"), "image/png")},
    )
    with Image.open(io.BytesIO(client.get(LOGO).content)) as decoded:
        assert decoded.mode in ("RGBA", "LA"), f"the ground was flattened to {decoded.mode}"


def test_a_document_renamed_as_an_image_is_refused(client):
    """The type is decided from the bytes, not from the name or the ``Content-Type`` - and
    it is the same refusal every other upload in this application makes (415, from
    ``uploads``), because a logo is not a looser kind of file than a face reference."""
    response = client.post(
        ADMIN_LOGO, headers=bearer(HEAD_ADMIN), files={"logo": ("mark.png", a_pdf(), "image/png")}
    )
    assert response.status_code == 415, response.text
    assert response.json()["detail"]["error_code"] == "not_an_image"
    assert db_scalar("SELECT logo_bytes FROM company_settings WHERE id = 1") is None


def test_the_pixel_ceiling_is_enforced_before_the_pixels_are_touched(client):
    """Five megabytes of PNG can describe a 300-megapixel image, and decoding that is where
    the memory goes - the refusal has to come from the header, not from a decoded bitmap.
    The file under the header here is 40x40, and it is refused all the same."""
    response = client.post(
        ADMIN_LOGO,
        headers=bearer(HEAD_ADMIN),
        files={"logo": ("huge.png", png_claiming(), "image/png")},
    )
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["error_code"] == "image_too_large"
    assert (detail["width"], detail["height"]) == (9000, 9000)
    assert db_scalar("SELECT logo_bytes FROM company_settings WHERE id = 1") is None


def test_removing_the_mark_puts_the_shipped_one_back(client):
    """And bumps the version, so a screen holding the removed mark's URL is holding a URL
    that answers 404 rather than a cached image of a logo the company just took down."""
    client.post(
        ADMIN_LOGO, headers=bearer(HEAD_ADMIN), files={"logo": ("mark.png", image(), "image/png")}
    )
    before = int(db_scalar("SELECT logo_version FROM company_settings WHERE id = 1"))
    response = client.delete(ADMIN_LOGO, headers=bearer(HEAD_ADMIN))
    assert response.status_code == 200, response.text
    assert response.json()["removed"] is True
    body = response.json()["branding"]
    assert body["logo"] is None and body["logo_url"] is None
    assert int(db_scalar("SELECT logo_version FROM company_settings WHERE id = 1")) > before
    assert client.get(LOGO).status_code == 404


def test_removing_a_mark_that_is_not_there_is_an_answer_not_an_error(client):
    """The undo of an act somebody else already undid. It says ``removed: false`` instead of
    failing, because the state the caller asked for is the state that holds."""
    response = client.delete(ADMIN_LOGO, headers=bearer(HEAD_ADMIN))
    assert response.status_code == 200, response.text
    assert response.json()["removed"] is False


# ---------------------------------------------------------------------------
# 6. the audit trail, and the one row these settings live in
# ---------------------------------------------------------------------------
def test_the_log_says_who_changed_what(client):
    client.post(ADMIN_BRANDING, headers=bearer(ADMIN), json={"name": "Recorded Ltd"})
    actor_id, action, entity, entity_id = db_rows(
        "SELECT actor_id, action, entity, entity_id FROM audit_log "
        "WHERE action = 'branding_update' ORDER BY id DESC LIMIT 1"
    )[0]
    assert actor_id == ADMIN
    assert entity == "company_settings"
    assert str(entity_id) == "1"


def test_the_log_records_the_shape_of_the_mark_and_not_its_bytes(client):
    """A few hundred kilobytes of base64 in every audit row would make the log unreadable,
    and the question it answers is who put this logo on our payslips - not what it was."""
    client.post(
        ADMIN_LOGO,
        headers=bearer(HEAD_ADMIN),
        files={"logo": ("mark.png", image(size=(400, 400)), "image/png")},
    )
    after = str(
        db_rows("SELECT after_json FROM audit_log WHERE action = 'branding_logo_set' "
                "ORDER BY id DESC LIMIT 1")[0][0]
    )
    assert '"mime"' in after and '"width"' in after and '"bytes"' in after
    assert "logo_bytes" not in after
    assert len(after) < 500, "the audit row carries the image itself"


def test_clearing_the_mark_is_only_logged_when_there_was_one(client):
    """An undo of nothing is not an event, and a log full of them buries the one that was."""
    client.delete(ADMIN_LOGO, headers=bearer(HEAD_ADMIN))
    assert db_rows("SELECT id FROM audit_log WHERE action = 'branding_logo_removed'") == []
    client.post(
        ADMIN_LOGO, headers=bearer(HEAD_ADMIN), files={"logo": ("mark.png", image(), "image/png")}
    )
    client.delete(ADMIN_LOGO, headers=bearer(HEAD_ADMIN))
    assert len(db_rows("SELECT id FROM audit_log WHERE action = 'branding_logo_removed'")) == 1


def test_there_is_exactly_one_company_row(client):
    """The table is a single row by schema (``CHECK (id = 1)``) and by endpoint, so a second
    company cannot exist to be read by one process and not another."""
    client.post(ADMIN_BRANDING, headers=bearer(HEAD_ADMIN), json={"name": "Only One"})
    assert db_scalar("SELECT COUNT(*) FROM company_settings") == 1
    assert client.get(BRANDING).json()["name"] == "Only One"


def test_the_updated_by_column_records_the_actor(client):
    """Beside the audit row, because "who set this" is the first question anybody asks of a
    settings value they disagree with."""
    client.post(ADMIN_BRANDING, headers=bearer(ADMIN), json={"name": "Attributed Ltd"})
    assert db_scalar("SELECT updated_by FROM company_settings WHERE id = 1") == ADMIN


def test_the_row_survives_being_deleted_from_under_it(client):
    """``_ensure_row`` exists for the database that predates this table: a settings read that
    raised would take the login screen down with it, and that screen is the one surface that
    cannot afford to fail."""
    write("DELETE FROM company_settings")
    assert client.get(BRANDING).status_code == 200
    assert (
        client.post(ADMIN_BRANDING, headers=bearer(HEAD_ADMIN), json={"name": "Recreated Ltd"}).status_code
        == 200
    )
    assert client.get(BRANDING).json()["name"] == "Recreated Ltd"
