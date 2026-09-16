"""Legacy batch enrollment over ``worker_photos/``. Superseded - read this first.

``POST /api/v1/admin/enrollment/bulk`` is the supported way to enroll a roster in bulk: it
applies the shared upload policy, runs the liveness policy, records a job and an item per
worker, and can be resumed with ``python -m enrollment --job N``. This script does none of
that - no liveness gate, no job record, no dry run - so it exists only for re-deriving a
template from photos that are already on this machine.

What the biometric ids changed here
-----------------------------------
It used to derive the worker from the *filename*: ``worker_photos/7.jpg`` became worker 7.
The photos are no longer named after accounts (each is named after its account's immutable
biometric id - see ``biometrics``), so a filename is no longer a worker id and cannot be
parsed into one. Each photo's name is looked up against ``users.biometric_id`` instead,
which also means this script can only write a template for an account that already exists,
under that account's own id.

    cd backend && ./venv/Scripts/python.exe enroll_workers.py      # or python3

It needs the environment the app uses (``.env`` at the project root) and reads the database
without modifying anything except the template files it writes.
"""

import io
import os
import sys

from PIL import Image, ImageOps

INPUT_PHOTOS_DIR = "../worker_photos"
VALID_EXTENSIONS = (".jpg", ".jpeg", ".png")


def _accounts_by_photo_stem() -> dict[str, str]:
    """``{photo stem: user id}`` for every account that holds a biometric id."""
    from database import connect

    conn = connect(read_only=True)
    try:
        rows = conn.execute(
            "SELECT id, biometric_id FROM users "
            "WHERE biometric_id IS NOT NULL AND biometric_id <> ''"
        ).fetchall()
    finally:
        conn.close()
    return {str(row["biometric_id"]): str(row["id"]) for row in rows}


def _normalised(image_path: str) -> Image.Image:
    """The photo as a decoded image: EXIF-rotated, RGB, capped at 800 px - in memory.

    It used to be written back to the working directory as ``./temp_enroll_<filename>``, a
    full-size JPEG of a worker's face sitting beside the source tree under a name derived
    from theirs, and removed only on the paths that remembered to.
    """
    with open(image_path, "rb") as handle:
        image = Image.open(io.BytesIO(handle.read()))
    image = ImageOps.exif_transpose(image)
    image = image.convert("RGB")
    image.thumbnail((800, 800))
    return image


def main() -> int:
    import biometrics
    import enrollment
    import face_engine

    if not os.path.isdir(INPUT_PHOTOS_DIR):
        print(f"No '{INPUT_PHOTOS_DIR}' directory to read. Nothing to do.")
        return 1

    photos = sorted(
        name for name in os.listdir(INPUT_PHOTOS_DIR) if name.lower().endswith(VALID_EXTENSIONS)
    )
    if not photos:
        print(f"No photos in '{INPUT_PHOTOS_DIR}'.")
        return 1

    accounts = _accounts_by_photo_stem()
    print(f"Found {len(photos)} photo(s); {len(accounts)} account(s) hold a biometric id.\n")

    succeeded = failed = skipped = 0
    for name in photos:
        user_id = accounts.get(os.path.splitext(name)[0])
        if user_id is None:
            print(f"SKIP  {name}: no account holds that biometric id.")
            print("      A photo named after a worker id belongs to the old scheme - use the")
            print("      bulk enrollment endpoint to enroll a worker by id.")
            skipped += 1
            continue

        print(f"...   {name} (account {user_id})")
        try:
            image = _normalised(os.path.join(INPUT_PHOTOS_DIR, name))
            # Through the engine, like every other caller: even a batch script gets the same
            # bound on how much inference runs at once, and there stays exactly one module in
            # this codebase that knows how to call the model.
            objects = face_engine.ENGINE.represent(enrollment.face_array(image))
            if len(objects) > 1:
                print(f"FAIL  {name}: more than one face - a reference is one person.")
                failed += 1
                continue
            biometrics.write_reference(user_id, image, objects[0]["embedding"])
            print(f"OK    {name} -> account {user_id}")
            succeeded += 1
        except ValueError as exc:
            print(f"FAIL  {name}: no usable face ({exc}).")
            failed += 1
        except Exception as exc:  # noqa: BLE001 - a batch reports, it does not abort
            print(f"FAIL  {name}: {type(exc).__name__}: {exc}")
            failed += 1

    print("\n" + "=" * 40)
    print(f"Enrolled: {succeeded}   Skipped: {skipped}   Failed: {failed}")
    print("=" * 40)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
