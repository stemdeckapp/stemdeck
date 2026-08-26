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


def test_health_identifies_the_answering_process():
    # The desktop shell spawns this backend and polls /api/health to know it
    # started. A 200 alone only proves *something* holds the port: a second
    # StemDeck used to adopt the first instance's backend, and with it the first
    # instance's data directory and library (#424). The pid is what the shell
    # names when it reports a port conflict, so it must be the real one.
    import os

    from app.main import app

    with TestClient(app) as client:
        assert client.get("/api/health").json()["pid"] == os.getpid()


def test_health_echoes_the_instance_token(monkeypatch):
    # How the shell recognises its own backend (#457). The pid cannot do it: on
    # the Windows portable build the venv launcher re-execs into
    # python/base/python.exe, so the process that binds the port is a grandchild
    # of the shell and its pid never matches the child that was spawned. The
    # environment survives that re-exec, so identity travels there.
    from app.main import app

    monkeypatch.setenv("STEMDECK_INSTANCE_TOKEN", "deadbeef")
    with TestClient(app) as client:
        assert client.get("/api/health").json()["instance"] == "deadbeef"


def test_health_reports_an_empty_token_when_unset(monkeypatch):
    # Docker, Unraid and source checkouts have no shell to hand them a token.
    # The field is always present so the shell can tell "no token" apart from a
    # backend too old to have the field at all.
    from app.main import app

    monkeypatch.delenv("STEMDECK_INSTANCE_TOKEN", raising=False)
    with TestClient(app) as client:
        assert client.get("/api/health").json()["instance"] == ""


# --- version source precedence (#421) ---------------------------------------
#
# The in-app updater replaces backend/ but never python/, where the installed
# dist metadata lives. If app_version() trusted that metadata, a self-updated
# install would keep reporting the old version and keep offering an update it
# had already applied. So the app-layer marker (static/version.json) wins.


def test_app_version_prefers_the_app_layer_marker(tmp_path, monkeypatch):
    from app import main

    monkeypatch.setattr(main, "STATIC_DIR", tmp_path)
    (tmp_path / "version.json").write_text('{"version": "9.9.9"}\n', encoding="utf-8")
    assert main.app_version() == "9.9.9"


def test_app_version_tolerates_a_bom(tmp_path, monkeypatch):
    # PowerShell's Set-Content -Encoding UTF8 emits a BOM, which json.loads
    # rejects outright; the packaged writer avoids it but the repo-root copy
    # written by the release workflow does not.
    from app import main

    monkeypatch.setattr(main, "STATIC_DIR", tmp_path)
    (tmp_path / "version.json").write_text('{"version": "1.2.3"}\n', encoding="utf-8-sig")
    assert main.app_version() == "1.2.3"


def test_app_version_falls_back_when_marker_is_absent_or_junk(tmp_path, monkeypatch):
    # Docker images and source checkouts have no marker (it is gitignored) and
    # must fall through to the hatch-vcs package metadata, not to a placeholder.
    from app import main

    monkeypatch.setattr(main, "STATIC_DIR", tmp_path)
    assert main.app_version() == main.package_version("stemdeck")

    # "[]" parses fine but has no .get -- the narrowed except must still catch it.
    for junk in ('{"version": ""}', '{"version": null}', "{}", "not json", "[]"):
        (tmp_path / "version.json").write_text(junk, encoding="utf-8")
        assert main.app_version() == main.package_version("stemdeck"), junk
