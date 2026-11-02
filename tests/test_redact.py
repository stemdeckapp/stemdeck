from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from app.core import redact as redact_mod
from app.core.redact import redact


def test_strips_the_home_directory():
    home = str(Path.home())
    text = f"{home}\\AppData\\Local\\Programs\\Python\\Python312\\Lib\\threads.py"
    result = redact(text)
    assert home not in result
    assert "<home>" in result
    assert "threads.py" in result


def test_strips_a_youtube_url():
    text = "download starting: https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=RDdQw4w9WgXcQ"
    result = redact(text)
    assert "youtube.com" not in result
    assert "dQw4w9WgXcQ" not in result
    assert "<source-url-redacted>" in result


def test_strips_a_youtu_be_url():
    result = redact("source: https://youtu.be/dQw4w9WgXcQ")
    assert "youtu.be" not in result
    assert "<source-url-redacted>" in result


def test_strips_a_soundcloud_url():
    result = redact("source: https://soundcloud.com/artist/track")
    assert "soundcloud.com" not in result
    assert "<source-url-redacted>" in result


def test_does_not_touch_an_unrelated_url():
    """Only the hosts this app actually pulls tracks from are source URLs --
    a link to the app's own site/repo, or anything else, is not personal
    information and must survive (it's often the useful part of a log line)."""
    text = "see https://github.com/stemdeckapp/stemdeck/issues/277 and https://stemdeck.app"
    result = redact(text)
    assert result == text


def test_strips_an_ipv4_address():
    result = redact('192.168.1.14:52341 - "GET /api/jobs HTTP/1.1" 200 OK')
    assert "192.168.1.14" not in result
    assert "<ip>" in result


def test_does_not_mistake_a_version_string_for_an_ip():
    result = redact("StemDeck v0.9.1.dev2+g682ab90d7.d20260816")
    assert "0.9.1" in result
    assert "<ip>" not in result


def test_does_not_mistake_a_timestamp_for_anything():
    """The pipeline's own log format is `YYYY-MM-DD HH:MM:SS ...` -- every
    single line has one, so any redaction pattern with false positives here
    would mangle the entire report, not just the rare real leak."""
    text = "2026-08-17 16:50:02 E stemdeck.pipeline pipeline failed for job abcdefabcdef"
    assert redact(text) == text


def test_composes_all_three_kinds_in_one_pass():
    home = str(Path.home())
    text = f"{home}\\stemdeck  192.168.1.14 requested https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    result = redact(text)
    assert home not in result
    assert "192.168.1.14" not in result
    assert "youtube.com" not in result
    assert "<home>" in result
    assert "<ip>" in result
    assert "<source-url-redacted>" in result


@pytest.mark.parametrize(
    "spell",
    [
        lambda home: home,
        lambda home: home.replace("\\", "/"),
        lambda home: home.upper(),
        lambda home: home.replace("\\", "\\\\"),  # repr() and JSON escape backslashes
    ],
    ids=["as-is", "forward-slashes", "other-case", "escaped"],
)
def test_strips_every_spelling_of_the_home_directory(spell):
    """A log carries the same folder in several spellings: Python writes
    backslashes, a file URI or ffmpeg writes forward slashes, repr() doubles
    the backslashes. An exact string match only catches the first one."""
    text = f'File "{spell(str(Path.home()))}/AppData/x.py", line 3'
    assert redact(text) == 'File "<home>/AppData/x.py", line 3'


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("C:\\Users\\Someone\\AppData\\x.py", "<home>\\AppData\\x.py"),
        ("c:/users/someone/Music/song.flac", "<home>/Music/song.flac"),
        ("'C:\\\\Users\\\\Someone\\\\x.py'", "'<home>\\\\x.py'"),
        ("D:\\Documents and Settings\\Someone\\x", "<home>\\x"),
        ("file:///C:/Users/Someone/x.wav", "file:///<home>/x.wav"),
        ("/Users/someone/Library/Caches/x", "<home>/Library/Caches/x"),
        ("/home/someone/.cache/torch/hub", "<home>/.cache/torch/hub"),
        ("/mnt/c/Users/Someone/x", "<home>/x"),
        ("/c/Users/Someone/x", "<home>/x"),
        ("/cygdrive/c/Users/Someone/x", "<home>/x"),
        ("open \\\\NAS-SOMEONE\\music\\song.wav", "open <unc>\\song.wav"),
        ("open //NAS-SOMEONE/music/song.wav", "open <unc>/song.wav"),
        ("\\\\?\\UNC\\NAS-SOMEONE\\music\\x", "<unc>\\x"),
        ("saved C:\\Users\\Someone.", "saved <home>."),
        (
            '2026-08-17 16:50:02 E stemdeck  File "C:\\Users\\Someone\\x.py", line 3',
            '2026-08-17 16:50:02 E stemdeck  File "<home>\\x.py", line 3',
        ),
    ],
)
def test_strips_any_users_folder_on_any_os(text, expected):
    """A home that is not this process's own: another account, a log copied
    from another machine, a desktop shell running as a different user, or a
    short 8.3 name. The user name is the folder right after Users or home."""
    assert redact(text) == expected


@pytest.fixture
def home_of(monkeypatch):
    """Pin the process's home, so a test does not depend on the runner's
    (a CI container's home is /github/home, a Docker one /root)."""

    def pin(path: str) -> None:
        monkeypatch.setattr(Path, "home", lambda: Path(path))

    return pin


def test_strips_the_user_name_as_a_path_segment_anywhere(home_of):
    home_of("C:/Users/Someone")
    assert redact("D:\\Someone\\Music\\x.wav") == "D:\\<user>\\Music\\x.wav"
    assert redact("/mnt/someone/x.wav") == "/mnt/<user>/x.wav"
    assert redact("saved to D:\\Someone.") == "saved to D:\\<user>."


def test_leaves_the_user_name_alone_inside_other_words(home_of):
    """Only a whole path segment is the user's folder. Replacing the bare
    name would rewrite any word that contains it, and a short or common name
    (`al`, `max`, `music`) would shred every line of the log."""
    home_of("/home/max")
    text = "maxs mix by premax, max said hi"
    assert redact(text) == text


@pytest.mark.parametrize("home", ["/github/home", "/root", "/var/lib/stemdeck"])
def test_a_service_home_name_is_not_a_user_name(home_of, home):
    """A home outside a users folder is named for its role, not a person.
    CI runs in a container whose home is /github/home, and taking "home"
    for the user name rewrote every /home in the log."""
    home_of(home)
    text = "see https://users.example/home/x and /srv/root/stemdeck/backup\n"
    assert redact(text) == text


@pytest.fixture
def folders(tmp_path, monkeypatch):
    data = tmp_path / "StemDeckData"
    monkeypatch.setattr(redact_mod, "DATA_DIR", data)
    monkeypatch.setattr(redact_mod, "JOBS_DIR", data / "jobs")
    monkeypatch.setattr(redact_mod, "CACHE_DIR", data / "cache")
    monkeypatch.setattr(redact_mod, "MODELS_DIR", data / "models")
    return data


@pytest.mark.parametrize(
    ("child", "expected"),
    [
        ("jobs/abc123def456/stems/vocals.wav", "<jobs>/abc123def456/stems/vocals.wav"),
        ("cache/waveforms/x.bin", "<cache>/waveforms/x.bin"),
        ("models/htdemucs.th", "<models>/htdemucs.th"),
        ("settings.json", "<data>/settings.json"),
        ("jobs", "<jobs>"),
    ],
)
def test_strips_the_app_folders(folders, child, expected):
    """The library and data folders name the user (a home path) or their
    drives and disks. A placeholder keeps which folder a path was in, which
    is the part that helps a diagnosis; the nested folder wins over the data
    folder that contains it."""
    path = folders / child
    native = expected.replace("/", os.sep)
    assert redact(path.as_posix()) == expected
    assert redact(str(path)) == native
    assert redact(f"saved to {path}.") == f"saved to {native}."


def test_does_not_take_a_longer_sibling_for_an_app_folder(folders):
    assert redact(str(folders) + "Old") == redact(str(folders.parent)) + os.sep + "StemDeckDataOld"


def test_a_run_of_slashes_stays_linear(monkeypatch):
    """uvicorn logs the request line, so anyone who can reach the server can
    put a URL of slashes into backend.log. A folder pattern that starts on a
    separator took seconds on 16,000 of them, and grows with the square."""
    monkeypatch.setattr(redact_mod, "DATA_DIR", Path("//nas/share/StemDeckData"))
    line = 'INFO: "GET /' + "/" * 50_000 + ' HTTP/1.1" 404'
    started = time.perf_counter()
    redact(line)
    assert time.perf_counter() - started < 1.0


def test_leaves_ordinary_log_lines_alone():
    text = (
        '2026-08-17 16:50:02 I stemdeck  INFO: "GET /api/jobs/abc/stems/vocals.wav HTTP/1.1" 200\n'
        "see /usr/lib/python3/site.py and https://github.com/stemdeckapp/stemdeck/issues/277\n"
        "[1786205373] [stemdeck] torch 2.5.1+cu124 ok\n"
        "see https://users.example/home/x and https://example.com//a/b\n"
    )
    assert redact(text) == text
