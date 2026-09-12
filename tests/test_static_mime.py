"""The frontend must be served with correct content types on any host.

StaticFiles asks `mimetypes` for a type, and on Windows `mimetypes` reads
HKEY_CLASSES_ROOT. A machine where some other program registered `.js` as
`text/plain` made StemDeck serve its own ES modules as plain text, which
browsers refuse to execute under strict MIME checking. Every module was
blocked, so nothing wired itself up and the app responded to nothing while
still rendering and still hovering (#617).

Nothing is wrong on a healthy machine, which is exactly why CI and every
developer box missed it. These tests reproduce the broken host instead of
trusting the one they run on.
"""

from __future__ import annotations

import mimetypes

import pytest
from fastapi.testclient import TestClient

from app.main import _pin_static_mime_types, app

JS_TYPES = {"text/javascript", "application/javascript"}


@pytest.fixture
def _restore_mimetypes():
    """`mimetypes` maps are process-global, so a test that edits them must undo it."""
    saved_types = dict(mimetypes.types_map)
    saved_non_standard = dict(mimetypes.common_types)
    yield
    mimetypes.types_map.clear()
    mimetypes.types_map.update(saved_types)
    mimetypes.common_types.clear()
    mimetypes.common_types.update(saved_non_standard)


def test_js_is_javascript_after_import():
    """Importing the app is enough; no request has to happen first.

    Note this cannot fail on a host whose registry is already correct, which is
    every CI runner and most developer machines. It guards the healthy case.
    The tests that actually exercise the fix are the hostile-host ones below,
    which reproduce the broken mapping rather than trusting the host.
    """
    guessed, _ = mimetypes.guess_type("anything.js")
    assert guessed in JS_TYPES, guessed


def test_a_host_that_calls_js_plain_text_is_overridden(_restore_mimetypes):
    """The actual reported failure: a registry mapping .js to text/plain."""
    mimetypes.add_type("text/plain", ".js")
    assert mimetypes.guess_type("x.js")[0] == "text/plain"

    _pin_static_mime_types()

    assert mimetypes.guess_type("x.js")[0] in JS_TYPES


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("x.js", JS_TYPES),
        ("x.mjs", JS_TYPES),
        ("x.css", {"text/css"}),
        ("x.json", {"application/json"}),
        ("x.svg", {"image/svg+xml"}),
        ("x.wasm", {"application/wasm"}),
    ],
)
def test_every_pinned_type_survives_a_hostile_host(name, expected, _restore_mimetypes):
    """A host that calls everything plain text must not change what we serve."""
    for ext in (".js", ".mjs", ".css", ".json", ".svg", ".wasm"):
        mimetypes.add_type("text/plain", ext)

    _pin_static_mime_types()

    assert mimetypes.guess_type(name)[0] in expected


def test_a_real_module_is_served_as_javascript():
    """End to end through StaticFiles, which is what the browser actually sees.

    A module served as text/plain is refused by strict MIME checking, so this
    header is the whole difference between a working app and a dead one.

    Like the import test above, this passes on a healthy host either way. The
    next test is the one that proves the fix reaches StaticFiles.
    """
    with TestClient(app) as c:
        resp = c.get("/js/main.js")

    assert resp.status_code == 200
    ctype = resp.headers["content-type"].split(";")[0].strip()
    assert ctype in JS_TYPES, ctype


def test_a_hostile_host_still_serves_javascript_end_to_end(_restore_mimetypes):
    """The reported failure, reproduced and then fixed, through a real request.

    This is the test that can fail. It puts the broken mapping in place first,
    so without `_pin_static_mime_types` the response really does come back as
    text/plain and the assertion fails on any machine, healthy registry or not.
    """
    mimetypes.add_type("text/plain", ".js")
    with TestClient(app) as c:
        broken = c.get("/js/main.js")
    assert broken.headers["content-type"].split(";")[0].strip() == "text/plain"

    _pin_static_mime_types()

    with TestClient(app) as c:
        fixed = c.get("/js/main.js")

    assert fixed.status_code == 200
    ctype = fixed.headers["content-type"].split(";")[0].strip()
    assert ctype in JS_TYPES, ctype


def test_the_stylesheet_is_served_as_css():
    with TestClient(app) as c:
        resp = c.get("/css/variables.css")

    assert resp.status_code == 200
    assert resp.headers["content-type"].split(";")[0].strip() == "text/css"
