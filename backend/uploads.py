"""One policy for every uploaded file in this application.

WHY THIS IS A MODULE AND NOT A LINE IN EACH ENDPOINT
---------------------------------------------------
Every upload surface in this app - the clock-in selfie, the admin's enrollment photo, the
self-service capture, the photo that comes with a registration link, and each photo inside
a bulk ZIP - used to make its own decisions, which is another way of saying none of them
did. Two of them read the whole request body into memory and handed it to PIL with no
size limit and no type check at all, so a 2 GB POST was a way to make the server
allocate 2 GB before anybody asked what the file was.

The policy is therefore one function, applied everywhere:

**Size: 5 MB (`UPLOAD_MAX_PHOTO_BYTES`), enforced *while reading*.**
A limit that is checked after ``await upload.read()`` is not a limit - the memory has
already been spent. ``read_photo()`` pulls the body in chunks and refuses it the moment
it crosses the ceiling, so the peak footprint of a hostile upload is one chunk over the
limit rather than the whole payload.

**Type: a real image, decided by the bytes.**
``Content-Type`` is a client-supplied string and so is the filename; anybody can send a
PDF as ``selfie.jpg`` with ``image/jpeg`` set. So the first bytes have to be a JPEG, PNG
or WebP signature, *and* the file then has to decode as an image PIL can hand to the
face pipeline. Both checks are needed: a file can carry a valid JPEG header and still be
truncated garbage (the magic check catches a mislabelled document, the decode catches a
corrupt one).

**Pixels: a ceiling on the decoded size too.**
5 MB of JPEG can be a 300 megapixel image, and decoding that is tens of gigabytes of
RAM - the size limit alone is not the protection it looks like. ``MAX_PIXELS`` is checked
before the pixels are touched.

A refusal is always a small JSON body with a stable ``error_code``
(``photo_too_large`` / ``unsupported_media_type`` / ``empty_upload`` / ``not_an_image`` /
``unreadable_image`` / ``image_too_large``), because every one of these is something the
person holding the phone can act on: retake it smaller, or stop uploading a document.
"""

from __future__ import annotations

import io
from typing import Any

from fastapi import HTTPException, UploadFile

from config import settings

#: How much of the body is held at once. A chunk is a read buffer, not a limit; the
#: limit is ``settings.upload_max_photo_bytes`` and it is enforced across chunks.
CHUNK_BYTES = 256 * 1024

#: Pixel ceiling for a decoded photo. 40 MP is far above any phone camera (a 108 MP
#: sensor still produces a 12 MP default JPEG) and far below the point where a
#: decompression bomb is worth attempting.
MAX_PIXELS = 40_000_000

#: ``(signature, mime)``. Longest first, so a longer signature is never shadowed by a
#: shorter one that happens to be a prefix of it.
SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
)

#: The types this app accepts. Deliberately short: these are the three formats a phone
#: camera and a phone's "save photo" produce, and every one of them is decoded and
#: re-encoded to JPEG before it is stored, so the stored reference is always one format.
ALLOWED_TYPES: tuple[str, ...] = ("image/jpeg", "image/png", "image/webp")

ERR_TOO_LARGE = "photo_too_large"
ERR_EMPTY = "empty_upload"
ERR_UNSUPPORTED = "unsupported_media_type"
ERR_NOT_IMAGE = "not_an_image"
ERR_UNREADABLE = "unreadable_image"
ERR_TOO_MANY_PIXELS = "image_too_large"


def _refuse(status: int, code: str, message: str, **extra: Any) -> HTTPException:
    return HTTPException(status_code=status, detail={"error_code": code, "message": message, **extra})


def max_photo_bytes() -> int:
    """The current ceiling. Read per call so a test or an operator can change it."""
    return int(settings.upload_max_photo_bytes)


def allowed_types() -> tuple[str, ...]:
    return ALLOWED_TYPES


def sniff_photo(data: bytes) -> str | None:
    """The image type the *bytes* say they are, or ``None``.

    The filename extension and the ``Content-Type`` header are both chosen by whoever
    sends the request, so neither is consulted here.
    """
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    for signature, mime in SIGNATURES:
        if data.startswith(signature):
            return mime
    return None


def validate_photo_bytes(data: bytes, *, field: str = "photo", max_bytes: int | None = None) -> str:
    """Size + type check for bytes that are already in hand (a ZIP member, a test).

    Returns the sniffed mime. Raises the same coded refusals as ``read_photo``, so a
    caller can record one error vocabulary whether the photo arrived in a request body
    or inside an archive.
    """
    limit = int(max_bytes if max_bytes is not None else max_photo_bytes())
    if not data:
        raise _refuse(400, ERR_EMPTY, f"No {field} received (the file was empty).")
    if len(data) > limit:
        raise _refuse(
            413,
            ERR_TOO_LARGE,
            f"The {field} is {len(data) / (1024 * 1024):.1f} MB; the limit is "
            f"{limit // (1024 * 1024)} MB. Retake the photo at a lower resolution.",
            limit_bytes=limit,
            received_bytes=len(data),
        )
    mime = sniff_photo(data)
    if mime is None:
        raise _refuse(
            415,
            ERR_NOT_IMAGE,
            f"The {field} is not a photo. Only JPEG, PNG and WebP images are accepted - "
            "no documents, archives or videos.",
            accepted=list(ALLOWED_TYPES),
        )
    if mime not in ALLOWED_TYPES:
        raise _refuse(
            415,
            ERR_UNSUPPORTED,
            f"{mime} is not accepted. Only JPEG, PNG and WebP images are accepted.",
            accepted=list(ALLOWED_TYPES),
        )
    return mime


async def read_photo(upload: UploadFile, *, field: str = "photo", max_bytes: int | None = None) -> bytes:
    """Read one photo upload under the size ceiling, refusing it as soon as it exceeds it.

    Never buffers more than the limit: the body is consumed in chunks and the request is
    refused on the chunk that crosses the ceiling, so an oversized upload costs one
    buffer rather than its own size in memory.
    """
    limit = int(max_bytes if max_bytes is not None else max_photo_bytes())
    collected: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise _refuse(
                413,
                ERR_TOO_LARGE,
                f"The {field} is larger than {limit // (1024 * 1024)} MB. Retake the photo at "
                "a lower resolution.",
                limit_bytes=limit,
            )
        collected.append(chunk)
    data = b"".join(collected)
    validate_photo_bytes(data, field=field, max_bytes=limit)
    return data


def has_alpha(image: Any) -> bool:
    """Whether this image carries transparency, in any of the three ways PIL means it.

    ``RGBA``/``LA`` are the channels; a palette image keeps it in ``info['transparency']``,
    which is how a PNG with a transparent ground is stored. Asked here rather than at each
    call site because getting it wrong is silent: an image flattened to RGB loses its
    transparency and prints a coloured box where the paper should show through.
    """
    mode = getattr(image, "mode", "")
    return mode in ("RGBA", "LA") or (mode == "P" and "transparency" in (getattr(image, "info", None) or {}))


def decode_photo(data: bytes, *, field: str = "photo", keep_alpha: bool = False):
    """Decode validated bytes into an EXIF-corrected ``PIL.Image``.

    The pixel ceiling is checked before the pixels are touched: five megabytes of JPEG
    can describe a 300 megapixel image, and decoding that is where the memory actually
    goes.

    ``keep_alpha`` is off for every *photo* in this application, and on for the one image
    that is not one: a company logo. A face is a photograph of a person and has no
    transparency to preserve, but a mark is drawn on a transparent ground so it can sit on
    the login panel's green or on white paper - and ``convert("RGB")``, which is right for
    a selfie, would fill that ground with black.
    """
    from PIL import Image, ImageOps

    try:
        image = Image.open(io.BytesIO(data))
        width, height = image.size
    except Exception:
        raise _refuse(
            422,
            ERR_UNREADABLE,
            f"The {field} could not be read as an image. It may be corrupt - take another one.",
        ) from None
    if width * height > MAX_PIXELS:
        raise _refuse(
            422,
            ERR_TOO_MANY_PIXELS,
            f"The {field} is {width}x{height} pixels, above the {MAX_PIXELS // 1_000_000} megapixel "
            "limit. Retake it at a lower resolution.",
            width=width,
            height=height,
        )
    try:
        # ``exif_transpose`` is what makes a portrait phone photo landscape-correct; a
        # face reference stored at the wrong rotation rejects its owner forever.
        upright = ImageOps.exif_transpose(image)
        if keep_alpha and has_alpha(upright):
            return upright.convert("RGBA")
        return upright.convert("RGB")
    except Exception:
        raise _refuse(
            422,
            ERR_UNREADABLE,
            f"The {field} could not be processed as an image - take another one.",
        ) from None


def policy() -> dict[str, Any]:
    """The policy, for ``/status/detail`` and for the pages that explain it."""
    return {
        "max_bytes": max_photo_bytes(),
        "max_megapixels": MAX_PIXELS // 1_000_000,
        "accepted": list(ALLOWED_TYPES),
    }
