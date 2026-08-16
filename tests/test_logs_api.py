from __future__ import annotations

import io
import time
import zipfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    from app import main as main_mod

    monkeypatch.setattr(main_mod, "LOGS_DIR", tmp_path / "logs")
    return TestClient(main_mod.app)


@pytest.fixture
def logs_dir(tmp_path):
    d = tmp_path / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


# --- GET /api/logs ---------------------------------------------------------


def test_reports_the_log_directory(client, logs_dir):
    body = client.get("/api/logs").json()
    assert body["dir"] == str(logs_dir.resolve())
    assert body["dir_exists"] is True


def test_reports_a_missing_directory_without_failing(client, tmp_path):
    """A fresh install has written nothing yet; the tab must still render."""
    body = client.get("/api/logs").json()
    assert body["dir_exists"] is False
    assert all(f["exists"] is False for f in body["files"])


def test_marks_existing_files_with_size(client, logs_dir):
    (logs_dir / "stemdeck.log").write_text("hello", encoding="utf-8")
    files = {f["name"]: f for f in client.get("/api/logs").json()["files"]}
    assert files["stemdeck.log"]["exists"] is True
    assert files["stemdeck.log"]["size"] == 5
    assert files["stemdeck.log"]["modified"] is not None
    assert files["stemdeck.log.1"]["exists"] is False


def test_every_file_is_described(client, logs_dir):
    for f in client.get("/api/logs").json()["files"]:
        assert f["description"], f["name"]


def test_covers_rotations_and_the_desktop_logs(client, logs_dir):
    names = {f["name"] for f in client.get("/api/logs").json()["files"]}
    assert {"stemdeck.log", "stemdeck.log.1", "stemdeck.log.2", "stemdeck.log.3"} <= names
    # Written by the Tauri shell, so absent on server deployments but still
    # worth listing so a desktop user knows where to look.
    assert {"backend.log", "backend.log.1", "backend.log.2", "setup.log"} <= names


def test_never_returns_log_contents(client, logs_dir):
    """Metadata only: a traceback can capture anything, and serving it over
    HTTP would widen that to whoever can reach the app."""
    (logs_dir / "stemdeck.log").write_text("SECRET-TOKEN-abc123", encoding="utf-8")
    assert "SECRET-TOKEN" not in client.get("/api/logs").text


# --- GET /api/logs.zip -----------------------------------------------------


def _names(resp):
    with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
        return set(z.namelist())


def test_zip_bundles_every_present_log(client, logs_dir):
    (logs_dir / "stemdeck.log").write_text("current", encoding="utf-8")
    (logs_dir / "stemdeck.log.1").write_text("older", encoding="utf-8")
    (logs_dir / "setup.log").write_text("setup", encoding="utf-8")
    r = client.get("/api/logs.zip")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert _names(r) == {"stemdeck.log", "stemdeck.log.1", "setup.log"}


def test_zip_preserves_contents(client, logs_dir):
    (logs_dir / "stemdeck.log").write_text("line one\nline two\n", encoding="utf-8")
    with zipfile.ZipFile(io.BytesIO(client.get("/api/logs.zip").content)) as z:
        assert z.read("stemdeck.log").decode() == "line one\nline two\n"


def test_zip_only_includes_known_log_names(client, logs_dir):
    """The name set is fixed, so anything else dropped in the directory -- a
    stray dump, an editor swap file -- can never be swept into a download."""
    (logs_dir / "stemdeck.log").write_text("ok", encoding="utf-8")
    (logs_dir / "credentials.txt").write_text("do not ship me", encoding="utf-8")
    (logs_dir / "notes.log").write_text("nor me", encoding="utf-8")
    assert _names(client.get("/api/logs.zip")) == {"stemdeck.log"}


def test_zip_explains_itself_when_there_is_nothing_to_send(client, logs_dir):
    """An empty zip reads as a broken download; say why instead."""
    names = _names(client.get("/api/logs.zip"))
    assert names == {"README.txt"}


def test_zip_survives_a_missing_directory(client, tmp_path):
    r = client.get("/api/logs.zip")
    assert r.status_code == 200
    assert _names(r) == {"README.txt"}


def test_zip_filename_is_timestamped(client, logs_dir):
    (logs_dir / "stemdeck.log").write_text("x", encoding="utf-8")
    cd = client.get("/api/logs.zip").headers["content-disposition"]
    assert cd.startswith('attachment; filename="stemdeck-logs-')
    assert cd.endswith('.zip"')


def test_zip_skips_an_unreadable_file_rather_than_failing(client, logs_dir, monkeypatch):
    """One bad file must not lose the rest of the bundle."""
    (logs_dir / "stemdeck.log").write_text("good", encoding="utf-8")
    (logs_dir / "setup.log").write_text("bad", encoding="utf-8")

    real = type(logs_dir).read_bytes

    def _boom(self):
        if self.name == "setup.log":
            raise OSError("permission denied")
        return real(self)

    monkeypatch.setattr(type(logs_dir), "read_bytes", _boom)
    assert _names(client.get("/api/logs.zip")) == {"stemdeck.log"}


# --- GET /api/logs/{view} --------------------------------------------------


def _stamp(offset_min: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - offset_min * 60))


def test_tail_keeps_only_the_requested_window(client, logs_dir):
    (logs_dir / "stemdeck.log").write_text(
        f"{_stamp(180)} I stemdeck ancient\n"
        f"{_stamp(90)} I stemdeck too old\n"
        f"{_stamp(10)} I stemdeck recent\n",
        encoding="utf-8",
    )
    body = client.get("/api/logs/application?minutes=60").text
    assert "recent" in body
    assert "too old" not in body
    assert "ancient" not in body


def test_tail_keeps_untimestamped_continuation_lines(client, logs_dir):
    """A traceback is one event across many lines; dropping the ones without a
    timestamp of their own would shred it."""
    (logs_dir / "stemdeck.log").write_text(
        f"{_stamp(5)} E stemdeck job failed\n"
        "Traceback (most recent call last):\n"
        '  File "x.py", line 1\n'
        "ValueError: boom\n",
        encoding="utf-8",
    )
    body = client.get("/api/logs/application?minutes=60").text
    assert "Traceback (most recent call last):" in body
    assert "ValueError: boom" in body


def test_tail_drops_continuations_of_old_entries(client, logs_dir):
    (logs_dir / "stemdeck.log").write_text(
        f"{_stamp(200)} E stemdeck old failure\n"
        "  old traceback line\n"
        f"{_stamp(2)} I stemdeck fresh\n",
        encoding="utf-8",
    )
    body = client.get("/api/logs/application?minutes=60").text
    assert "old traceback line" not in body
    assert "fresh" in body


def test_tail_reads_the_previous_rotation_too(client, logs_dir):
    """A rotation inside the window would otherwise make a busy log look empty."""
    (logs_dir / "stemdeck.log.1").write_text(f"{_stamp(20)} I stemdeck before rotation\n", "utf-8")
    (logs_dir / "stemdeck.log").write_text(f"{_stamp(5)} I stemdeck after rotation\n", "utf-8")
    body = client.get("/api/logs/application?minutes=60").text
    assert "before rotation" in body
    assert body.index("before rotation") < body.index("after rotation"), (
        "must read forwards in time"
    )


def test_tail_parses_the_setup_log_epoch_format(client, logs_dir):
    """setup.log is written by the Tauri shell with epoch seconds, because the
    crate has no date library."""
    now = int(time.time())
    (logs_dir / "setup.log").write_text(
        f"[{now - 7200}] [stemdeck] old entry\n[{now - 60}] [stemdeck] new entry\n",
        encoding="utf-8",
    )
    body = client.get("/api/logs/setup?minutes=60").text
    assert "new entry" in body
    assert "old entry" not in body


def test_tail_serves_the_backend_log(client, logs_dir):
    """backend.log was listed in Settings and shipped in the zip, but had no
    view -- the one log holding what killed a backend before its own logging
    was up was the one log you could not read in the app."""
    (logs_dir / "backend.log").write_text(
        f"{_stamp(120)} I stemdeck ancient\n{_stamp(3)} I stemdeck recent crash\n",
        encoding="utf-8",
    )
    body = client.get("/api/logs/backend?minutes=60").text
    assert "recent crash" in body
    assert "ancient" not in body


def test_tail_reads_the_backend_rotations_in_order(client, logs_dir):
    (logs_dir / "backend.log.2").write_text(f"{_stamp(30)} I stemdeck oldest\n", encoding="utf-8")
    (logs_dir / "backend.log.1").write_text(f"{_stamp(20)} I stemdeck middle\n", encoding="utf-8")
    (logs_dir / "backend.log").write_text(f"{_stamp(5)} I stemdeck newest\n", encoding="utf-8")
    body = client.get("/api/logs/backend?minutes=60").text
    assert body.index("oldest") < body.index("middle") < body.index("newest")


def test_every_listed_log_file_is_reachable_through_some_view(client, logs_dir):
    """The Settings pane lists files and offers views; a file in the first list
    with no view is a dead end for the user, which is how backend.log ended up
    invisible."""
    from app.main import _LOG_FILES, _LOG_VIEWS

    viewable = {name for names in _LOG_VIEWS.values() for name in names}
    listed = {name for name, _ in _LOG_FILES}
    # Rotations beyond the first are covered by the zip, not by a live view.
    unreachable = {n for n in listed - viewable if not n.endswith((".2", ".3"))}
    assert not unreachable, f"listed but not viewable: {sorted(unreachable)}"


def test_tail_says_so_when_the_window_is_empty(client, logs_dir):
    (logs_dir / "stemdeck.log").write_text(f"{_stamp(500)} I stemdeck ancient\n", encoding="utf-8")
    assert "No entries in the last 60 minutes" in client.get("/api/logs/application").text


def test_tail_says_so_when_the_file_is_missing(client, logs_dir):
    assert "No log file yet" in client.get("/api/logs/setup").text


def test_tail_rejects_an_unknown_view(client, logs_dir):
    assert client.get("/api/logs/nope").status_code == 404


@pytest.mark.parametrize(
    "view",
    [
        "..%2F..%2Fetc%2Fpasswd",
        "%2e%2e%2fsettings",
        "stemdeck.log",  # a real filename is still not a view name
        "logs.zip",
    ],
)
def test_tail_only_serves_named_views_not_paths(client, logs_dir, view):
    """The view name maps to a fixed file set rather than being joined onto a
    path, so nothing outside that set is reachable. (A literal "../x" is
    normalised away by the client before it reaches the route, so the encoded
    forms are the ones worth asserting.)"""
    assert client.get(f"/api/logs/{view}").status_code == 404


def test_tail_clamps_an_absurd_window(client, logs_dir):
    (logs_dir / "stemdeck.log").write_text(f"{_stamp(1)} I stemdeck hi\n", encoding="utf-8")
    assert client.get("/api/logs/application?minutes=999999").status_code == 200


def test_tail_truncates_a_flood(client, logs_dir):
    (logs_dir / "stemdeck.log").write_text(
        "".join(f"{_stamp(1)} I stemdeck line {i}\n" for i in range(6000)), encoding="utf-8"
    )
    body = client.get("/api/logs/application").text
    assert "earlier lines not shown" in body
    assert len(body.splitlines()) < 4200
