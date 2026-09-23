"""The downscaled frame stored with every punch, and the one route that serves it.

WHY THIS EXISTS
---------------
A pending review used to arrive as numbers only: a distance, a liveness verdict, a flag
sentence. The frame the numbers were measured from was decoded, used and dropped, so the
administrator making the decision - approve hours, refuse them, or re-enroll a face - could
not see what the camera actually saw. "Matched at 0.52" is a claim; the frame is the evidence
the claim is about, and a decision with no evidence is a coin flip that writes to payroll.

The frame is a *copy*, deliberately: the punch already has the full-size upload in memory,
and writing that would double the storage cost of every punch and hand retention a JPEG the
size of what the phone sent. It is downscaled to ``FRAME_MAX_PX`` (256px on the long edge,
which is what a review card shows at 96-160px) and re-encoded, so one frame is a few
kilobytes and a site's year of them is small against the database it sits beside.

The conventions here are ``quick_links``' punch-photo store, on purpose - that module went
through this twice already:

* the file is named ``secrets.token_hex(16)`` and **nothing else**. Names like
  ``{log id}_{timestamp}`` filed a worker's face under a number anybody can count upwards;
  the frame is looked up by log id over the API, never by filename, so the name only has to
  be unique;
* the directory lives beside the database and is repointable from a test, which must not
  write faces into the checkout - the same convention ``main.WORKER_PHOTOS_DIR`` and
  ``quick_links.PHOTOS_DIR`` use;
* the serving route resolves the stored name against ``os.path.basename`` *and* checks the
  resolved file is inside the directory before it is served. The path comes from our own
  database, but a path read from a database is still a path, and a future bug that lets one
  be edited must not turn the route into a file-read primitive;
* the sweep is in ``retention`` (``_sweep_punch_frames``), not here: every deletion in this
  application has exactly one implementation, in the module that writes the compliance event
  describing it.

What a frame is *for* is narrower than what a quick-link photo is for: it is evidence on a
review card. It is not a second copy of the worker's face for general browsing, so it is
served to administrators only, one log id at a time, and rows whose evidence has been wiped
by retention answer 404 rather than pretending to have a picture.
"""

from __future__ import annotations

import os
import secrets
from typing import Any

from PIL import Image

from config import settings

#: Where the frames are kept, from the settings so that a child process inherits the answer in
#: ``PUNCH_FRAMES_DIR`` instead of writing into the checkout - the same convention
#: ``quick_links.PHOTOS_DIR`` and ``main.WORKER_PHOTOS_DIR`` use. Repointable from a test, which
#: must not write faces into the repository either; here that is a repoint of this attribute.
FRAMES_DIR = str(settings.punch_frames_dir)

#: The long edge of the stored frame. A review card draws the evidence at 96-160px, so 256
#: keeps it readable at 2x on a phone's screen while holding a frame to a few kilobytes -
#: which is what makes storing one with *every* punch affordable rather than a policy
#: decision an operator has to think about.
FRAME_MAX_PX = 256


def frames_dir() -> str:
    """The frame directory, created on first use."""
    os.makedirs(FRAMES_DIR, exist_ok=True)
    return FRAMES_DIR


def frame_name() -> str:
    """The name of one frame: random, and nothing to do with the punch's row.

    See the module docstring: a filename is not an identifier this app exposes, so it only
    has to be unique - and a worker's face must not be filed under a countable number.
    """
    return f"{secrets.token_hex(16)}.jpg"


def store_frame(image: Image.Image, name: str | None = None) -> str:
    """Write the downscaled copy of ``image`` and return the name it was stored under.

    ``image`` is the already-decoded punch upload (the same object the liveness and face
    stages consumed), so this is one thumbnail call and one encode on the way past - no
    second decode of the request body, no second read of a file on disk. ``thumbnail`` is
    in-place and only ever shrinks, so a frame the phone already sent small is stored as it
    arrived.

    Raises ``OSError`` on disk failure, which the caller turns into its own refusal: a punch
    that could not keep its evidence is still a punch, and refusing it would lose an honest
    worker's hours over a disk.
    """
    name = name or frame_name()
    copy = image.copy()
    copy.thumbnail((FRAME_MAX_PX, FRAME_MAX_PX))
    copy.convert("RGB").save(os.path.join(frames_dir(), name), format="JPEG", quality=80)
    return name


def discard_frame(name: str | None) -> None:
    """Best-effort removal of a frame that no row will claim.

    A punch that fails inside its transaction (already clocked in, no open shift) leaves a
    file no row points at; ``retention``'s residue pass would collect it at the next sweep,
    and this removes it now instead - the same two-layer cleanup the quick-link punch uses.
    Never raises: cleanup failing must not fail the request that is being refused anyway.
    """
    if not name:
        return
    try:
        path = os.path.join(frames_dir(), os.path.basename(str(name)))
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def resolve_stored(name: str | None) -> str | None:
    """The absolute path of a stored frame, or ``None`` when there is none to serve.

    The double check is the point: ``basename`` takes the stored value apart, and the
    directory comparison proves the result is inside the frame directory. A row edited to
    name ``../../times.db`` resolves to ``None`` here, so the serving route in ``main`` can
    answer 404 without ever opening a path the database did not put under this directory.
    """
    if not name:
        return None
    directory = os.path.abspath(frames_dir())
    candidate = os.path.abspath(os.path.join(directory, os.path.basename(str(name))))
    if os.path.dirname(candidate) != directory or not os.path.exists(candidate):
        return None
    return candidate


def referenced_names(conn: Any) -> set[str]:
    """Every frame name any row still claims - what retention must not sweep away.

    Two tables claim frames: the attendance log (the review-card evidence) and the refused
    punch (the triage surface). A claimant added in a second place and not listed here is how
    retention deletes a live card's evidence, so the set is the union, by construction.
    """
    names: set[str] = {
        os.path.basename(str(row["punch_frame"]))
        for row in conn.execute(
            "SELECT punch_frame FROM attendance_logs WHERE punch_frame IS NOT NULL AND punch_frame <> ''"
        ).fetchall()
    }
    try:
        names |= {
            os.path.basename(str(row["punch_frame"]))
            for row in conn.execute(
                "SELECT punch_frame FROM refused_punches WHERE punch_frame IS NOT NULL AND punch_frame <> ''"
            ).fetchall()
        }
    except Exception:  # noqa: BLE001 - a database without migration 22 claims nothing here
        pass
    return names
