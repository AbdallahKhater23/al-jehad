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

**Destination: memory for a one-shot decode, disk for a punch (`spool_photo`).**
``read_photo()`` returns the body as bytes, which is right when the caller decodes it on the
next line and drops it. A punch cannot: its frame is decoded inside the face engine's worker,
so the bytes have to survive a wait for capacity, and megabytes per *waiting* request is what
turned a burst into an out-of-memory event on a 512 MB instance. ``spool_photo()`` applies the
same policy with the body going to a temporary file instead, so what a waiting punch holds is
one chunk - and the file is the caller's to remove (see ``SpooledPhoto.discard``).

**Type: a real image, decided by the bytes.**
``Content-Type`` is a client-supplied string and so is the filename; anybody can send a
PDF as ``selfie.jpg`` with ``image/jpeg`` set. So the first bytes have to be a JPEG, PNG
or WebP signature, *and* the file then has to decode as an image PIL can hand to the
face pipeline. Both checks are needed: a file can carry a valid JPEG header and still be
truncated garbage (the magic check catches a mislabelled document, the decode catches a
corrupt one).

**Pixels: two ceilings, because they answer different questions.**
5 MB of JPEG can be a 300 megapixel image, and decoding that is tens of gigabytes of
RAM - the size limit alone is not the protection it looks like. ``MAX_PIXELS`` is checked
before the pixels are touched, and it is the *bomb* ceiling: the point past which the
request is not a photo of anybody.

The second ceiling is the one a real phone can hit: ``face_frame_max_pixels`` / 2048 px.
A large photo used to be *accepted* and handled with a decode hint, and that was the
quiet bug - the hint makes libjpeg resample on a power-of-two grid, so a 12 MP photo
reached the detector as a ~1008 px frame and a 1.2 MP one as 1280. Two resamplings of the
same face, differing by more than the decision margin the score is compared against
(measured drift up to ~0.0088 against a margin of ~0.0036). So the boundary refuses them
instead, with a message that says what to do about it, and everything inside it decodes
the same way. A phone's own camera app produces 12 MP, which is why the refusal names
downscaling rather than pretending the photo is corrupt.

**Shape: one chain, ``face_frame()``, for every frame a face model is shown.**
The image an enrollment builds a *template* from and the image a punch builds a
*decision* from are compared to each other, so they have to be made the same way. They used
to be made four different ways - 800 px here, 1280 px there, and on three enrollment paths
no resize at all - which meant the same worker photographed the same way could be embedded
at a different resolution depending on which side of the comparison they were on. A
resampler is not neutral: it decides what the detector sees and what the alignment crop is
taken from, and the two sides of a distance comparison have to agree about the pixels. So
the decode hint and the resize live together, in ``face_frame()``, and every path that
feeds the detector goes through it rather than through ``decode_photo`` on its own.

A refusal is always a small JSON body with a stable ``error_code``
(``photo_too_large`` / ``unsupported_media_type`` / ``empty_upload`` / ``not_an_image`` /
``unreadable_image`` / ``image_too_large`` / ``image_resolution_too_high``), because every
one of these is something the person holding the phone can act on: retake it smaller,
downscale it first, or stop uploading a document.
"""

from __future__ import annotations

import io
import logging
import os
import tempfile
import time
from typing import Any

from fastapi import HTTPException, UploadFile

from config import settings

log = logging.getLogger(__name__)

#: How much of the body is held at once. A chunk is a read buffer, not a limit; the
#: limit is ``settings.upload_max_photo_bytes`` and it is enforced across chunks.
CHUNK_BYTES = 256 * 1024

#: How many leading bytes are needed to recognise a format. 12 is the longest signature
#: this module knows (``RIFF....WEBP``); nothing else is kept from the body in memory.
SNIFF_BYTES = 12

#: Where a punch's upload waits while the face engine gets to it. The OS temp directory,
#: deliberately: a spool file is not data. It is never backed up, never swept by
#: ``retention``, and has no meaning after the request that created it, so a *configurable*
#: path would only invite an operator to point a worker's photograph somewhere persistent -
#: the opposite of what this is. ``sweep_spool_dir`` clears what a crash left behind.
SPOOL_DIR = os.path.join(tempfile.gettempdir(), "attendance-spool")

#: The age at which a spool file is presumed to be a leftover rather than an upload in
#: flight. A punch holds its file for the length of one request (well under a minute), so an
#: hour is far above any live file - which matters because a *concurrent* process shares this
#: directory (the test suite runs several against one temp root) and its live files must not
#: be swept out from under it.
SPOOL_STALE_SECONDS = 3600.0

#: Pixel ceiling for a decoded photo. 40 MP is far above any phone camera (a 108 MP
#: sensor still produces a 12 MP default JPEG) and far below the point where a
#: decompression bomb is worth attempting.
MAX_PIXELS = 40_000_000

#: The ceiling a *face frame* is built under, as whole pixels and as a long edge. Both
#: checked, and the smaller of the two wins for a square photo. See ``decode_photo`` for
#: why accepting a larger one is not merely slower.
FACE_FRAME_MAX_PIXELS = 4_000_000
FACE_FRAME_MAX_EDGE_PX = 2048

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
ERR_RESOLUTION_TOO_HIGH = "image_resolution_too_high"


def _refuse(status: int, code: str, message: str, **extra: Any) -> HTTPException:
    return HTTPException(status_code=status, detail={"error_code": code, "message": message, **extra})


def max_photo_bytes() -> int:
    """The current ceiling. Read per call so a test or an operator can change it."""
    return int(settings.upload_max_photo_bytes)


def allowed_types() -> tuple[str, ...]:
    return ALLOWED_TYPES


def face_frame_max_pixels() -> int:
    """The ingestion boundary, read per call so a test or an operator can move it."""
    return int(getattr(settings, "face_frame_max_pixels", FACE_FRAME_MAX_PIXELS))


def face_frame_max_edge() -> int:
    """The long-edge half of the same boundary."""
    return int(getattr(settings, "face_frame_max_edge_px", FACE_FRAME_MAX_EDGE_PX))


def _resolution_refusal(
    field: str,
    width: int,
    height: int,
    *,
    max_pixels: int | None,
    max_edge: int | None,
) -> HTTPException | None:
    """The refusal for a photo above the face-frame boundary, or ``None`` when it fits.

    One function so the two rules and the sentence they produce cannot drift apart, and so
    that the boundary is answerable from the *declared* size: this is called before a pixel
    is decoded, which is the entire point of having it.
    """
    too_many_pixels = max_pixels is not None and width * height > max_pixels
    too_long_an_edge = max_edge is not None and max(width, height) > max_edge
    if not (too_many_pixels or too_long_an_edge):
        return None
    # Each rule names itself. The two are one boundary, but a 2100x1000 photo is 2.1 MP and
    # telling its owner they exceeded a 4 megapixel limit would be a sentence they cannot
    # check - so the message says which ceiling was crossed and by how much.
    if too_many_pixels:
        limit_mp = (max_pixels or FACE_FRAME_MAX_PIXELS) // 1_000_000
        message = (
            f"The {field} resolution exceeds the {limit_mp} MP limit ({width}x{height}). "
            "Please downscale or capture at standard resolution."
        )
    else:
        message = (
            f"The {field} is {width}x{height}, above the {max_edge} px limit on its longest "
            "edge. Please downscale or capture at standard resolution."
        )
    return _refuse(
        422,
        ERR_RESOLUTION_TOO_HIGH,
        message,
        width=width,
        height=height,
        max_pixels=max_pixels,
        max_edge=max_edge,
    )


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


def check_photo_type(head: bytes, *, field: str = "photo") -> str:
    """The type the leading bytes declare, or a coded refusal.

    Split out of ``validate_photo_bytes`` because the body is not always in one piece: a
    spooled upload (see ``spool_photo``) passes its first ``SNIFF_BYTES`` here and never holds
    the rest at all, and the two callers must answer with exactly the same codes, sentences
    and accepted list - a client cannot tell which destination the server chose, and must not
    have to. What arrives here has already been checked for emptiness: an empty upload is a
    different refusal (``empty_upload``), and answering an empty file with "this is not a
    photo, only JPEG, PNG and WebP are accepted" would send the caller looking at their file
    format when the problem is that the file did not arrive.
    """
    mime = sniff_photo(head)
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
    return check_photo_type(data, field=field)


async def read_photo(upload: UploadFile, *, field: str = "photo", max_bytes: int | None = None) -> bytes:
    """Read one photo upload under the size ceiling, refusing it as soon as it exceeds it.

    Never buffers more than the limit: the body is consumed in chunks and the request is
    refused on the chunk that crosses the ceiling, so an oversized upload costs one
    buffer rather than its own size in memory.

    Holds the body, so this is for a caller that decodes it immediately. A punch wants
    ``spool_photo()``: same policy, but the body lands on disk, which is what lets the frame be
    decoded in the worker rather than held on a request waiting for capacity.
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


def spool_dir() -> str:
    """The directory spooled uploads live in, created on first use."""
    os.makedirs(SPOOL_DIR, exist_ok=True)
    return SPOOL_DIR


class SpooledPhoto:
    """One uploaded photo, on disk, owned by whoever spooled it.

    Deliberately not a context manager: the owner is a request, and a request's cleanup points
    are not all indented inside one block. ``discard`` never raises - a punch that cannot
    remove its own temporary file has lost nothing (the startup sweep collects it) and must not
    fail a worker's clock-in over it.
    """

    __slots__ = ("path", "size", "mime")

    def __init__(self, path: str, size: int, mime: str) -> None:
        self.path = path
        self.size = size
        self.mime = mime

    def discard(self) -> None:
        """Remove the file. Never raises: a refusal must not become a 500 on the way out.

        A removal that *fails* is logged rather than swallowed. It used to be silent, and
        that is how a real leak stayed invisible: on Windows a file another handle still has
        open cannot be unlinked at all, so a refusal raised while the decoder held the spool
        left the upload on disk, and this method said nothing - while ``sweep_spool_dir`` would
        have collected it an hour later, which made the leak survivable and therefore quiet.
        On Linux the bug is not even reachable (a file with an open handle can be unlinked),
        which is why it survived this long. Anything that leaks now leaves a line in the log.
        """
        try:
            os.remove(self.path)
        except FileNotFoundError:
            return
        except OSError as exc:
            log.warning(
                "could not remove the spooled upload %s (%s); the startup sweep will collect it",
                self.path,
                exc,
            )

    def __repr__(self) -> str:
        # A path in a traceback is fine; the bytes of somebody's face are not, and neither is
        # anything derived from them. Nothing here reads the file.
        return f"SpooledPhoto(path={self.path!r}, size={self.size}, mime={self.mime!r})"


async def spool_photo(
    upload: UploadFile, *, field: str = "photo", max_bytes: int | None = None
) -> SpooledPhoto:
    """Read one photo upload **onto disk** under the same policy, a chunk at a time.

    WHY THIS EXISTS
    ---------------
    ``read_photo`` returns the body as bytes, which is right for a photo that is decoded and
    discarded on the next line, and wrong for a punch: the punch's frame is decoded *inside the
    face engine's worker*, so its bytes have to survive a queue wait. Held as bytes, that is up
    to 5 MB alive for every request waiting for capacity, plus the ~10 MB of decoded pixels the
    old shape built before submitting and could not release until the job ended. That is what
    turned a burst of punches into a memory event on a 512 MB instance rather than into a slow
    answer.

    So this is the same policy with a different destination: the body is pulled in
    ``CHUNK_BYTES`` pieces and written straight out, so what a waiting punch holds is one
    chunk. The refusals are identical - same status, same ``error_code``, same sentence - for
    the reason given on ``check_photo_type``.

    The file is the caller's to remove; nothing here cleans up after the request that owns it,
    because a second lifetime for the same file is how a temporary file ends up in a directory
    that exists to hold faces.
    """
    limit = int(max_bytes if max_bytes is not None else max_photo_bytes())
    target = tempfile.NamedTemporaryFile(
        prefix="punch-", suffix=".jpg", dir=spool_dir(), delete=False
    )
    path = target.name
    head = b""
    total = 0
    try:
        with target:
            while True:
                chunk = await upload.read(CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise _refuse(
                        413,
                        ERR_TOO_LARGE,
                        f"The {field} is larger than {limit // (1024 * 1024)} MB. Retake the "
                        "photo at a lower resolution.",
                        limit_bytes=limit,
                    )
                if len(head) < SNIFF_BYTES:
                    # The format lives in the first bytes; the rest is never inspected in
                    # memory, which is the whole point of writing as we read.
                    head += chunk[: SNIFF_BYTES - len(head)]
                target.write(chunk)
        if not total:
            raise _refuse(400, ERR_EMPTY, f"No {field} received (the file was empty).")
        mime = check_photo_type(head, field=field)
    except BaseException:
        # Every refusal - and every cancellation - takes the partial file with it. A refused
        # upload that left a file behind would make the refusal its own kind of success.
        _unlink(path)
        raise
    return SpooledPhoto(path=path, size=total, mime=mime)


def _unlink(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def sweep_spool_dir(*, max_age_seconds: float = SPOOL_STALE_SECONDS) -> int:
    """Remove spool files a crash left behind, and say how many.

    A punch removes its own spool file on every path it can (see ``SpooledPhoto.discard``), so
    what is left for this is a request that died mid-flight: a killed worker, a redeploy, the
    OOM killer. One pass at startup is enough, because the file has no meaning after the
    request that made it. The age floor is what keeps a *live* file safe - several processes
    share this directory in a test run - and it is set an order of magnitude above the length
    of any real request.
    """
    cutoff = time.time() - max_age_seconds
    try:
        names = os.listdir(SPOOL_DIR)
    except OSError:
        return 0
    removed = 0
    for name in names:
        path = os.path.join(SPOOL_DIR, name)
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
                removed += 1
        except OSError:
            continue
    if removed:
        log.info("removed %s spooled upload(s) left by an interrupted request", removed)
    return removed


def has_alpha(image: Any) -> bool:
    """Whether this image carries transparency, in any of the three ways PIL means it.

    ``RGBA``/``LA`` are the channels; a palette image keeps it in ``info['transparency']``,
    which is how a PNG with a transparent ground is stored. Asked here rather than at each
    call site because getting it wrong is silent: an image flattened to RGB loses its
    transparency and prints a coloured box where the paper should show through.
    """
    mode = getattr(image, "mode", "")
    return mode in ("RGBA", "LA") or (mode == "P" and "transparency" in (getattr(image, "info", None) or {}))


def _open_photo(source: bytes | str | os.PathLike[str]):
    """Open a photo for reading without decoding a pixel of it.

    Two sources, and the difference matters beyond convenience. Bytes are handed to PIL through
    a ``BytesIO``; a *path* is handed over as a filename so PIL opens the descriptor itself and
    closes it again the moment the pixels are loaded - which is what lets a spooled upload be
    removed while the decoded frame lives on, on an operating system that will not unlink a
    file somebody still has open.
    """
    from PIL import Image

    if isinstance(source, (bytes, bytearray, memoryview)):
        return Image.open(io.BytesIO(bytes(source)))
    return Image.open(os.fspath(source))


def decode_photo(
    source: bytes | str | os.PathLike[str],
    *,
    field: str = "photo",
    keep_alpha: bool = False,
    max_pixels: int | None = None,
    max_edge: int | None = None,
):
    """Decode validated bytes (or a file holding them) into an EXIF-corrected ``PIL.Image``.

    The pixel ceiling is checked before the pixels are touched: five megabytes of JPEG
    can describe a 300 megapixel image, and decoding that is where the memory actually
    goes.

    ``keep_alpha`` is off for every *photo* in this application, and on for the one image
    that is not one: a company logo. A face is a photograph of a person and has no
    transparency to preserve, but a mark is drawn on a transparent ground so it can sit on
    the login panel's green or on white paper - and ``convert("RGB")``, which is right for
    a selfie, would fill that ground with black.

    ``source`` may be the bytes or a path to a file holding them. A punch passes the path,
    because its bytes are only ever on disk (see ``spool_photo``) - and PIL keeps the file open
    only until the pixels are in memory, so decoding a spooled upload does not pin the file.

    ``max_pixels`` / ``max_edge`` are the caller's ingestion boundary - for a face frame,
    ``settings.face_frame_max_pixels`` and ``settings.face_frame_max_edge_px``. Both are
    measured against the *declared* size, before libjpeg is asked for a pixel, so an
    oversized upload costs a header read rather than a 12 MB bitmap.

    This function deliberately does **not** apply a decode hint (``Image.draft``). It used
    to: the hint let libjpeg decode a 12 MP JPEG at 1/4 scale instead of allocating ~36 MB.
    What it also did was make the resampling depend on the *source size* - ``draft`` rounds
    down to a power of two, so a 12 MP photo arrived at the detector near 1008 px and a
    1.2 MP one at 1280 - and the two are compared to each other by a cosine distance whose
    margin is ~0.0036, against a measured drift of up to ~0.0088. That is a punch crossing
    the line because of how big the uploaded file was.

    With the boundary above in place the hint buys nothing worth that: everything accepted
    here is at most 4 MP, which is at most 12 MB as an RGB bitmap. So every accepted upload
    is decoded the same way, and the only resize in the pipeline is the one explicit
    ``thumbnail`` in ``face_frame``.

    Callers that are feeding a face model want ``face_frame()`` instead, which applies the
    boundary *and* the resize, so no call site has to remember both halves.
    """
    from PIL import ImageOps

    try:
        image = _open_photo(source)
        width, height = image.size
    except Exception:
        raise _refuse(
            422,
            ERR_UNREADABLE,
            f"The {field} could not be read as an image. It may be corrupt - take another one.",
        ) from None

    try:
        if width * height > MAX_PIXELS:
            raise _refuse(
                422,
                ERR_TOO_MANY_PIXELS,
                f"The {field} is {width}x{height} pixels, above the "
                f"{MAX_PIXELS // 1_000_000} megapixel limit. Retake it at a lower resolution.",
                width=width,
                height=height,
            )
        refusal = _resolution_refusal(
            field, width, height, max_pixels=max_pixels, max_edge=max_edge
        )
        if refusal is not None:
            raise refusal
        # ``exif_transpose`` is what makes a portrait phone photo landscape-correct; a
        # face reference stored at the wrong rotation rejects its owner forever.
        upright = ImageOps.exif_transpose(image)
        if keep_alpha and has_alpha(upright):
            return upright.convert("RGBA")
        return upright.convert("RGB")
    except HTTPException:
        # A coded refusal is not a decode failure. Both are ours to write, and only one of
        # them describes the photo as unreadable.
        raise
    except Exception:
        raise _refuse(
            422,
            ERR_UNREADABLE,
            f"The {field} could not be processed as an image - take another one.",
        ) from None
    finally:
        # The source handle is released *before* any refusal can reach a caller that deletes
        # the file it came from. ``Image.open`` is lazy, so a refusal raised on the declared
        # size - the whole point of checking it early - leaves PIL holding the descriptor, and
        # on Windows an open file cannot be unlinked: a routine refusal would leak the upload
        # (and ``SpooledPhoto.discard``, which must not raise, would report nothing). The
        # returned frame is a ``convert`` copy, so nothing downstream reads this descriptor.
        image.close()


def face_frame(
    source: bytes | str | os.PathLike[str],
    *,
    field: str = "photo",
    max_long_edge: int | None = None,
):
    """The one way this application turns photo bytes into the frame a face model sees.

    Every path that embeds a face goes through here - a punch, a quick link, an offline
    batch, a registration link, the console's own capture, a bulk ZIP, the bulk CLI - and
    that is the point rather than tidiness: a *stored template* and a *live frame* are
    compared to each other by cosine distance, so producing them with two different
    resamplers makes the comparison depend on which side of it a face was on. Same photo,
    same chain, same pixels.

    ``thumbnail`` is now the *only* resample in the chain. ``decode_photo`` used to take a
    libjpeg hint as well, and the two together were the point - but the hint resamples on a
    power-of-two grid, so its output depended on how big the uploaded file was. The
    ingestion boundary (``face_frame_max_pixels`` / ``face_frame_max_edge_px``, applied by
    ``decode_photo``) replaces it: a photo that fits is decoded whole and resampled exactly
    once, and a photo that does not fit is refused instead of quietly entering a second
    vector space. Separating the halves - which is how this was written before the chain
    was one function - is how an enrollment path came to embed a frame at a *different*
    size than the punch that would later be measured against it, and how three of them came
    to resize not at all.

    ``thumbnail`` only ever shrinks, so a photo already smaller than the ceiling is passed
    through untouched, and the hint never enlarges what the camera sent.

    ``source`` is bytes or a path (see ``decode_photo``). The punch passes the *path* of its
    spooled upload, which is what lets the whole decode happen inside the face engine's worker
    instead of on the request path: nothing about the frame - not the bytes, not the pixels -
    exists while a punch waits for capacity.
    """
    edge = int(max_long_edge if max_long_edge is not None else settings.face_frame_max_px)
    image = decode_photo(
        source,
        field=field,
        max_pixels=face_frame_max_pixels(),
        max_edge=face_frame_max_edge(),
    )
    image.thumbnail((edge, edge))
    return image


def policy() -> dict[str, Any]:
    """The policy, for ``/status/detail`` and for the pages that explain it.

    The two face-frame numbers are published, not just enforced: a browser cannot downscale
    a photo to a limit it does not know, and a client that finds out by being refused has
    already spent the worker's upload budget finding out.
    """
    return {
        "max_bytes": max_photo_bytes(),
        "max_megapixels": MAX_PIXELS // 1_000_000,
        "accepted": list(ALLOWED_TYPES),
        "face_frame_max_pixels": face_frame_max_pixels(),
        "face_frame_max_edge_px": face_frame_max_edge(),
    }
