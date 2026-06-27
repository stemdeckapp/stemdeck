from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import _is_mobile_ua, app

IPHONE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)
ANDROID_UA = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
)
DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
# Modern iPadOS Safari reports a Mac desktop UA — tablets fall through to the DAW.
IPAD_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)


@pytest.mark.parametrize("ua", [IPHONE_UA, ANDROID_UA])
def test_is_mobile_ua_true_for_phones(ua: str):
    assert _is_mobile_ua(ua) is True


@pytest.mark.parametrize("ua", [DESKTOP_UA, IPAD_UA, "", "curl/8.0"])
def test_is_mobile_ua_false_for_non_phones(ua: str):
    assert _is_mobile_ua(ua) is False


def test_root_serves_mobile_shell_to_phones():
    with TestClient(app) as c:
        resp = c.get("/", headers={"user-agent": IPHONE_UA})
    assert resp.status_code == 200
    assert "/mobile/app.js" in resp.text


def test_root_serves_daw_to_desktop():
    with TestClient(app) as c:
        resp = c.get("/", headers={"user-agent": DESKTOP_UA})
    assert resp.status_code == 200
    assert "/css/daw.css" in resp.text


def test_ui_query_override_forces_mobile_on_desktop():
    with TestClient(app) as c:
        resp = c.get("/", params={"ui": "mobile"}, headers={"user-agent": DESKTOP_UA})
    assert "/mobile/app.js" in resp.text


def test_ui_query_override_forces_desktop_on_phone():
    with TestClient(app) as c:
        resp = c.get("/", params={"ui": "desktop"}, headers={"user-agent": IPHONE_UA})
    assert "/css/daw.css" in resp.text


def test_mobile_assets_are_served():
    with TestClient(app) as c:
        assert c.get("/mobile/app.js").status_code == 200
        assert c.get("/mobile/styles.css").status_code == 200
