from __future__ import annotations

from fastapi.testclient import TestClient


def test_health_endpoints_report_ok():
    from app.main import app

    with TestClient(app) as client:
        for path in ("/health", "/api/health"):
            r = client.get(path)
            assert r.status_code == 200
            body = r.json()
            assert body["name"] == "StemDeck"
            assert body["status"] == "ok"
            assert body["version"]
            assert "ffmpeg_configured" in body
            assert "jobs_dir" not in body
            assert "data_dir" not in body


# --- version source precedence (#421) ---------------------------------------
#
# The in-app updater replaces backend/ but never python/, where the installed
# dist metadata lives. If app_version() trusted that metadata, a self-updated
# install would keep reporting the old version and keep offering an update it
# had already applied. So the app-layer marker (static/version.json) wins.


def test_app_version_prefers_the_app_layer_marker(tmp_path, monkeypatch):
    import app.main as main

    monkeypatch.setattr(main, "STATIC_DIR", tmp_path)
    (tmp_path / "version.json").write_text('{"version": "9.9.9"}\n', encoding="utf-8")
    assert main.app_version() == "9.9.9"


def test_app_version_tolerates_a_bom(tmp_path, monkeypatch):
    # PowerShell's Set-Content -Encoding UTF8 emits a BOM, which json.loads
    # rejects outright; the packaged writer avoids it but the repo-root copy
    # written by the release workflow does not.
    import app.main as main

    monkeypatch.setattr(main, "STATIC_DIR", tmp_path)
    (tmp_path / "version.json").write_text('{"version": "1.2.3"}\n', encoding="utf-8-sig")
    assert main.app_version() == "1.2.3"


def test_app_version_falls_back_when_marker_is_absent_or_junk(tmp_path, monkeypatch):
    # Docker images and source checkouts have no marker (it is gitignored) and
    # must fall through to the hatch-vcs package metadata, not to a placeholder.
    import app.main as main

    monkeypatch.setattr(main, "STATIC_DIR", tmp_path)
    assert main.app_version() == main.package_version("stemdeck")

    for junk in ('{"version": ""}', '{"version": null}', "{}", "not json"):
        (tmp_path / "version.json").write_text(junk, encoding="utf-8")
        assert main.app_version() == main.package_version("stemdeck"), junk
