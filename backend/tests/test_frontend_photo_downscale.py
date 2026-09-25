"""The browser half of the ingestion boundary: a phone's own photo is resized, not refused.

WHY THIS EXISTS
---------------
``backend/uploads.py`` refuses a face photo above 4 MP / 2048 px (``image_resolution_too_high``),
because a large upload used to arrive at the detector resampled differently from a small one -
up to ~0.0088 of embedding drift against a decision margin of ~0.0036. That boundary is correct
and it would also have broken the ordinary way a worker is enrolled: a phone's camera app writes
12 MP, ``enroll.html``/``quick.html`` ask for exactly that (``<input type="file" capture="user">``),
and the console's pickers take a photograph off the admin's own phone. Every one of those is
above the boundary, so the browser has to *resize* before it uploads.

Three things are pinned here, and they are the three that can silently rot:

* **the arithmetic.** ``fitToLimits`` is pure, so it can be tested with numbers: both rules
  apply, the smaller scale wins, and it never enlarges. A browser that upscaled a 320 px photo
  to the ceiling would change what the detector sees for every old phone in the field.
* **the resize actually runs.** ``shrinkPhoto`` is driven here with a modelled 12 MP photo and
  a modelled decoder, and the encoded result is checked - including that a file that already
  fits comes back *unchanged* rather than re-encoded, and that an undecodable one comes back as
  the original so the server can name the problem.
* **the numbers are read, not guessed.** Two page worlds share no code file (the link pages
  load ``capture.js``, the app shell loads i18n plus its three modules), so the boundary is
  written down once per world - and every copy is compared against ``backend/config.py`` here.
  A drift between them is a phone photo refused on one screen and accepted on another.

The link pages read the numbers from the server's own answer (``photo_policy``, which is
``uploads.policy()``); the shell has no such response, hence the mirror and this test.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import frontend_vm
from harness import BACKEND_DIR, PROJECT_ROOT

FRONTEND = PROJECT_ROOT / "frontend"
pytestmark = pytest.mark.skipif(frontend_vm.NODE is None, reason="node is not installed")


def _source(name: str) -> str:
    return (FRONTEND / name).read_text(encoding="utf-8")


def _squashed(name: str) -> str:
    """The file with whitespace collapsed, for the source pins below.

    A wiring check on a frontend file has to be forgiving about layout and unforgiving about
    absence: the alternative is a test that fails when somebody wraps a line.
    """
    return re.sub(r"\s+", " ", _source(name))


def _limits_in(name: str) -> dict[str, int]:
    """The boundary numbers as that file writes them."""
    text = _source(name)
    return {
        key: int(value.replace("_", ""))
        for key, value in re.findall(r"(face_frame_max_(?:pixels|edge_px))[:\s]*[=:]\s*([0-9_]+)", text)
    }


# --------------------------------------------------------------------------- #
# 1. the numbers, in every file that has a copy
# --------------------------------------------------------------------------- #
def test_every_mirror_of_the_boundary_matches_the_backend():
    """The one assertion that keeps three copies of a number from becoming three policies."""
    import config

    expected = {
        "face_frame_max_pixels": config.settings.face_frame_max_pixels,
        "face_frame_max_edge_px": config.settings.face_frame_max_edge_px,
    }

    for name in ("capture.js", "admin_modules.js", "frontendjavascript.js"):
        assert _limits_in(name) == expected, f"{name} no longer mirrors backend/config.py"


def test_the_link_pages_read_the_boundary_from_the_server_not_from_a_mirror():
    """``photo_policy`` is ``uploads.policy()``, and it now carries the two numbers.

    This is the request's other half: the pages that take a photo with the phone's *own*
    camera app must be told what the deployment accepts, rather than shipping a constant that
    agrees with the server on the day it was written.
    """
    import uploads

    policy = uploads.policy()
    assert policy["face_frame_max_pixels"] == uploads.face_frame_max_pixels()
    assert policy["face_frame_max_edge_px"] == uploads.face_frame_max_edge()

    # Both endpoints that hand a policy to a page publish that object, unedited.
    for name in ("enrollment.py", "quick_links.py"):
        assert '"photo_policy": uploads.policy()' in (BACKEND_DIR / name).read_text(encoding="utf-8"), name

    # And ``capture.js`` copies both fields out of whatever the server sent.
    squashed = _squashed("capture.js")
    assert "policy.face_frame_max_pixels = Number(next.face_frame_max_pixels)" in squashed
    assert "policy.face_frame_max_edge_px = Number(next.face_frame_max_edge_px)" in squashed


# --------------------------------------------------------------------------- #
# 2. the wiring: every path that can carry a phone photo
# --------------------------------------------------------------------------- #
def test_the_shutter_draws_the_frame_the_boundary_allows():
    """A 4K track would otherwise put an 8 MP selfie on the wire and be refused."""
    assert "var fit = fitToLimits(video.videoWidth || 720, video.videoHeight || 960)" in _squashed("capture.js")


def test_a_chosen_file_is_resized_before_it_is_held():
    """The gallery/native-camera path: 12 MP in, a frame the server accepts out."""
    squashed = _squashed("capture.js")
    assert "shrinkPhoto(file, function (small) { photo = small;" in squashed, (
        "accept() holds whatever it was handed; a phone photo above the boundary would be refused"
    )


def test_the_app_shell_caps_its_capture_and_the_console_its_pickers():
    shell = _squashed("frontendjavascript.js")
    assert "const fit = this.fitToLimits(video.videoWidth, video.videoHeight)" in shell

    admin = _squashed("admin_modules.js")
    # Both face-photo pickers, and the shared helper they go through.
    assert admin.count("this.shrinkFacePhoto(chosen,") == 2, "a picker stopped resizing"
    assert "return UI.shrinkPhoto(file, done);" in admin
    # The logo is not a face frame and must not be squeezed through the ingestion boundary.
    assert 'id="companyLogoInput"' in admin


def test_the_console_reads_the_same_boundary_the_shrink_helper_enforces():
    """``PHOTO_POLICY`` is passed straight to the resize, so the mirror is load-bearing."""
    assert "face_frame_max_pixels: 4000000" in _source("admin_modules.js")


# --------------------------------------------------------------------------- #
# 3. the behaviour, against the real file in a modelled browser
# --------------------------------------------------------------------------- #
HARNESS = r"""
const results = {};
const env = boot();

// A decoder the boundary arithmetic can be driven through. The VM has a DOM but no image
// decoder, which is exactly the platform boundary: everything inside ``Image``'s contract is
// the app's own code, and the sandbox is otherwise the browser the file already ships to.
// ``DIMS`` is what the modelled camera app wrote that file at.
env.evaluate(`
    globalThis.DIMS = { width: 0, height: 0, fail: false };
    globalThis.Image = function () {
        const self = this;
        this.naturalWidth = 0;
        this.naturalHeight = 0;
        this.onload = null;
        this.onerror = null;
        let src = '';
        Object.defineProperty(this, 'src', {
            get() { return src; },
            set(value) {
                src = value;
                setTimeout(() => {
                    if (DIMS.fail) { if (self.onerror) self.onerror(); return; }
                    self.naturalWidth = DIMS.width;
                    self.naturalHeight = DIMS.height;
                    if (self.onload) self.onload();
                }, 0);
            }
        });
        return this;
    };
`);

// --- 1. the arithmetic, and 2. the numbers coming from the server ----------
results.fits = await env.evaluate(`(async () => {
    // window.Capture: the shipped file assigns it to the modelled window, which is the
    // browser's own object here rather than the harness's global - and going through window
    // is how the page's own code reaches it too.
    const fit = (w, h) => {
        const box = window.Capture.fitToLimits(w, h);
        return { w: box.width, h: box.height, scale: Number(box.scale.toFixed(4)) };
    };
    const out = {
        '12mp_phone': fit(4032, 3024),
        'wide': fit(3000, 1200),
        'square': fit(2048, 2048),
        'canvas': fit(1280, 960),
        'tiny': fit(320, 240),
        'nonsense': fit(0, 0)
    };
    window.Capture.setPolicy({ face_frame_max_pixels: 1000000, face_frame_max_edge_px: 1000 });
    out.from_server = fit(4032, 3024);
    window.Capture.setPolicy({ max_bytes: 5 * 1024 * 1024 });
    out.ignored_when_absent = fit(4032, 3024);
    return out;
})()`);

// --- 3. the resize ---------------------------------------------------------
// A policy is sticky by design (``setPolicy`` merges: a response that omits a field must not
// reset it), so the deployment's own numbers are put back before the resize cases below.
window_setPolicy(4000000, 2048);

function window_setPolicy(pixels, edge) {
    return env.evaluate(`window.Capture.setPolicy(${JSON.stringify({ face_frame_max_pixels: pixels, face_frame_max_edge_px: edge })})`);
}

function photoCase(name, dims) {
    return env.evaluate(`(async () => {
        const file = new Blob([new Uint8Array(64)], { type: 'image/jpeg' });
        file.name = ${JSON.stringify(name)};
        DIMS = ${JSON.stringify(dims)};
        const out = await new Promise((resolve) => {
            window.Capture.shrinkPhoto(file, (small, resized) => resolve({ small, resized }));
        });
        return {
            resized: out.resized,
            type: out.small.type,
            same_object: out.small === file,
            frame: await out.small.text()
        };
    })()`);
}

results.big = await photoCase('IMG_4821.jpg', { width: 4032, height: 3024, fail: false });
results.kept = await photoCase('IMG_1001.jpg', { width: 1280, height: 960, fail: false });
results.broken = await photoCase('broken.jpg', { width: 0, height: 0, fail: true });
"""


@pytest.fixture(scope="module")
def browser() -> dict:
    return frontend_vm.run(HARNESS, scripts=["capture.js"])


def test_the_arithmetic_resizes_a_phone_photo_and_never_enlarges_anything(browser):
    fits = browser["fits"]

    # 4032x3024 (12.19 MP): the long edge binds first, at ~0.508 - and the area rule alone
    # would have allowed 0.573, so this pins which rule wins rather than that one exists.
    assert fits["12mp_phone"] == {"w": 2048, "h": 1536, "scale": 0.5079}
    assert fits["12mp_phone"]["w"] * fits["12mp_phone"]["h"] <= 4_000_000

    # 3000x1200 is 3.6 MP - under the area limit - and 3000 px on its edge, so only the edge
    # rule can be what resized it.
    assert fits["wide"] == {"w": 2048, "h": 819, "scale": 0.6827}

    # 2048x2048 is exactly on the edge and 4.19 MP of area: only the *pixel* rule can fire.
    assert fits["square"] == {"w": 2000, "h": 2000, "scale": 0.9766}
    assert fits["square"]["w"] * fits["square"]["h"] <= 4_000_000

    # The shutter's own frame, and a photo from an old phone: untouchable either way.
    assert fits["canvas"] == {"w": 1280, "h": 960, "scale": 1}
    assert fits["tiny"] == {"w": 320, "h": 240, "scale": 1}
    assert fits["nonsense"] == {"w": 0, "h": 0, "scale": 1}


def test_the_boundary_the_pages_use_is_the_one_the_server_sent_them(browser):
    """A link whose policy landed resizes to *that* deployment's numbers, not to a constant."""
    fits = browser["fits"]

    assert fits["from_server"] == {"w": 1000, "h": 750, "scale": 0.248}
    # A later policy response *without* the fields leaves them as they were: merging rather
    # than replacing, so a partial answer cannot silently re-arm the compiled-in default on a
    # deployment that has configured a smaller boundary.
    assert fits["ignored_when_absent"] == {"w": 1000, "h": 750, "scale": 0.248}


def test_a_phone_photo_is_resized_and_re_encoded_as_a_named_jpeg(browser):
    """The modelled canvas reports the size it was asked to encode, so this is exact."""
    big = browser["big"]

    assert big["resized"] is True
    assert big["same_object"] is False
    assert big["type"] == "image/jpeg"
    assert big["frame"] == "modelled frame 2048x1536 image/jpeg q0.92", big["frame"]


def test_a_photo_that_already_fits_is_handed_back_untouched(browser):
    """Not re-encoded: a re-encode of a photo that fits is loss for nothing, and it would
    change the bytes the punch path's own decode sees."""
    assert browser["kept"]["resized"] is False
    assert browser["kept"]["same_object"] is True


def test_a_photo_that_will_not_decode_is_left_for_the_server_to_refuse(browser):
    """The browser is not the authority. Its refusal cannot say *why*, in the worker's
    language, with a code the client can act on - so the original goes and the server answers."""
    assert browser["broken"]["resized"] is False
    assert browser["broken"]["same_object"] is True
