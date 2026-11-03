import hashlib
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from unittest.mock import patch

import pytest
import torch.hub

from app.pipeline import warmup

ROOT = Path(__file__).resolve().parents[1]
# The real one, before the fixture below replaces it for every test.
_FREE_BYTES = warmup._free_bytes


@pytest.fixture(autouse=True)
def _model_folders(monkeypatch, tmp_path):
    """The space check reads the model folders and the disk: both point at
    this test's own directory, with room to spare unless a test says not."""
    models = tmp_path / "models"
    monkeypatch.setattr(warmup, "MODELS_DIR", models)
    monkeypatch.setattr(warmup, "_model_folders", lambda: (models, tmp_path / "hf"))
    monkeypatch.setattr(warmup, "_free_bytes", lambda: 1 << 50)
    return models


class _Server:
    """A local HTTP server on a throwaway port. `respond(conn)` answers each
    request; `sent` counts the body bytes it has written."""

    def __init__(self, respond):
        self._respond = respond
        self.sent = 0
        self.released = threading.Event()
        self._sock = socket.create_server(("127.0.0.1", 0))
        self.port = self._sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def url(self, name: str) -> str:
        return f"http://127.0.0.1:{self.port}/models/{name}"

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        request = b""
        while b"\r\n\r\n" not in request:
            chunk = conn.recv(4096)
            if not chunk:
                return
            request += chunk
        try:
            self._respond(self, conn)
        except OSError:
            pass
        finally:
            conn.close()

    def close(self) -> None:
        self.released.set()
        self._sock.close()


@pytest.fixture()
def serve():
    servers: list[_Server] = []

    def start(respond) -> _Server:
        server = _Server(respond)
        servers.append(server)
        return server

    yield start
    for server in servers:
        server.close()


def _send_slowly(body: bytes, chunk: int, pause: float):
    def respond(server: _Server, conn: socket.socket) -> None:
        conn.sendall(f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\n\r\n".encode())
        for start in range(0, len(body), chunk):
            if server.released.is_set():
                return
            conn.sendall(body[start : start + chunk])
            server.sent += min(chunk, len(body) - start)
            time.sleep(pause)

    return respond


def test_section_warmup_loads_cpu_model():
    with patch("allin1_infer.models.load_pretrained_model") as load:
        warmup._warm_sections()

    load.assert_called_once_with(model_name=warmup.SECTION_MODEL, device="cpu")


def test_warmup_continues_after_individual_failure(monkeypatch, capsys):
    calls = []

    def fail():
        calls.append("fail")
        raise RuntimeError("bad checkpoint")

    def succeed():
        calls.append("succeed")

    monkeypatch.setattr(warmup, "_STEPS", (("sections", fail), ("demucs", succeed)))

    assert warmup.main() == 0
    assert calls == ["fail", "succeed"]
    # Each model is announced before it runs, so the setup screen can say
    # which one it is on (#405). A failure that is not the network's is not
    # reported as one.
    assert capsys.readouterr().out.splitlines() == [
        "WARMUP_START sections 1 2",
        "WARMUP_FAILED sections bad checkpoint",
        "WARMUP_START demucs 2 2",
        "WARMUP_OK demucs",
    ]


def test_no_network_is_told_apart_from_other_failures(monkeypatch, capsys):
    """Offline: no name resolves. The step's error is whatever its library
    wraps that in, here urllib's URLError."""

    def no_dns(*args, **kwargs):
        raise socket.gaierror(11001, "getaddrinfo failed")

    monkeypatch.setattr(socket, "getaddrinfo", no_dns)
    monkeypatch.setattr(
        warmup,
        "_STEPS",
        (("beat_this", lambda: urllib.request.urlopen("http://models.example/beat.ckpt")),),
    )

    warmup.main()

    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "WARMUP_START beat_this 1 1"
    assert lines[1].startswith("WARMUP_FAILED beat_this ")
    assert lines[2:] == ["WARMUP_OFFLINE beat_this"]


def test_a_host_that_answers_with_an_error_is_not_offline(serve, monkeypatch, capsys):
    def not_found(server, conn):
        conn.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")

    server = serve(not_found)
    monkeypatch.setattr(
        warmup, "_STEPS", (("demucs", lambda: urllib.request.urlopen(server.url("x.th"))),)
    )

    warmup.main()

    lines = capsys.readouterr().out.splitlines()
    assert lines[1].startswith("WARMUP_FAILED demucs HTTP Error 404")
    assert len(lines) == 2


def test_a_stalled_model_host_ends_the_model_instead_of_the_step(
    serve, monkeypatch, tmp_path, capsys
):
    """The host takes the connection and sends nothing. Before #405 the warmup
    had no read timeout at all, so this one model held the whole step until
    the shell's 30-minute deadline. Now it fails like the separation model's
    download does (#125), and the next model still gets its turn."""
    server = serve(lambda server, conn: server.released.wait(20))
    dst = tmp_path / "checkpoints" / "model-deadbeef.th"
    dst.parent.mkdir()
    monkeypatch.setattr(warmup, "TIMEOUT_MODEL_DOWNLOAD_READ", 1)
    monkeypatch.setattr(warmup, "MODEL_DOWNLOAD_ATTEMPTS", 1)
    monkeypatch.setattr(
        warmup,
        "_STEPS",
        (
            (
                "demucs",
                lambda: torch.hub.download_url_to_file(
                    server.url(dst.name), str(dst), progress=False
                ),
            ),
            ("beat_this", lambda: None),
        ),
    )

    started = time.monotonic()
    warmup.main()
    elapsed = time.monotonic() - started

    lines = capsys.readouterr().out.splitlines()
    assert elapsed < 10, f"the stalled download held the step for {elapsed:.0f} s"
    assert lines[1].startswith("WARMUP_FAILED demucs model download failed: ")
    assert "(no data for 1 s)" in lines[1]
    assert lines[2:] == [
        "WARMUP_OFFLINE demucs",
        "WARMUP_START beat_this 2 2",
        "WARMUP_OK beat_this",
    ]
    assert not dst.exists()


def test_not_enough_space_downloads_nothing_and_says_how_much(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(warmup, "_STEPS", (("demucs", lambda: calls.append("demucs")),))
    monkeypatch.setattr(warmup, "_free_bytes", lambda: 1_000)

    assert warmup.main() == 0

    assert calls == []
    needed = warmup._DOWNLOAD_BYTES["demucs"]
    assert capsys.readouterr().out.splitlines() == [f"WARMUP_NO_SPACE {needed} 1000"]


def test_models_already_on_disk_need_no_space(monkeypatch, capsys, _model_folders):
    """A second setup run (a new version) on a nearly full disk still checks
    the models it has instead of refusing for want of room it does not need."""
    monkeypatch.setattr(warmup, "_DOWNLOAD_BYTES", {"demucs": 4_000})
    monkeypatch.setattr(warmup, "_STEPS", (("demucs", lambda: None),))
    monkeypatch.setattr(warmup, "_free_bytes", lambda: 1_000)
    checkpoints = _model_folders / "torch" / "hub" / "checkpoints"
    checkpoints.mkdir(parents=True)
    (checkpoints / "demucs.th").write_bytes(b"\0" * 4_000)

    warmup.main()

    assert capsys.readouterr().out.splitlines() == ["WARMUP_START demucs 1 1", "WARMUP_OK demucs"]


def test_free_space_is_read_before_the_models_folder_exists(tmp_path):
    folder = tmp_path / "data" / "models"
    with patch.object(warmup, "MODELS_DIR", folder):
        assert _FREE_BYTES() > 0
    assert not folder.exists()


def test_whisper_counts_only_when_lyrics_would_be_transcribed(monkeypatch):
    monkeypatch.setattr(warmup, "_STEPS", (("whisper", lambda: None),))
    monkeypatch.setattr(warmup, "_whisper_model", lambda: None)
    assert warmup._bytes_needed() == 0

    monkeypatch.setattr(warmup, "_whisper_model", lambda: "turbo")
    assert warmup._bytes_needed() == warmup._DOWNLOAD_BYTES["turbo"]


_KILL_CHILD = """
import sys, torch.hub
from app.pipeline import warmup
url, dst = sys.argv[1], sys.argv[2]
warmup._bytes_needed = lambda: 0
warmup._STEPS = (("demucs", lambda: torch.hub.download_url_to_file(url, dst, progress=False)),)
sys.exit(warmup.main())
"""


def test_a_download_stopped_midway_leaves_no_file_that_looks_complete(serve, tmp_path):
    """The wizard's Skip, and closing the window, end the warmup mid-file
    with a hard kill (#405). What decides whether that is safe is where the
    half file sits: torch.hub, which fetches the separation and beat models,
    writes to a temporary name and moves the file into place only once it is
    whole, so the next run, or the first Split, downloads it again instead of
    loading half a checkpoint."""
    body = os.urandom(4 << 20)
    server = serve(_send_slowly(body, 64 << 10, 0.05))
    checkpoints = tmp_path / "models" / "torch" / "hub" / "checkpoints"
    checkpoints.mkdir(parents=True)
    dst = checkpoints / "htdemucs-0123abcd.th"
    env = {
        **os.environ,
        "STEMDECK_DATA_DIR": str(tmp_path),
        "TORCH_HOME": str(tmp_path / "models" / "torch"),
        "PYTHONUNBUFFERED": "1",
    }
    child = subprocess.Popen(
        [sys.executable, "-c", _KILL_CHILD, server.url(dst.name), str(dst)],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 60
        while server.sent < (1 << 20) and time.monotonic() < deadline:
            assert child.poll() is None, "the download ended before it could be stopped"
            time.sleep(0.05)
        assert server.sent >= (1 << 20), "the download never started"
    finally:
        child.kill()
        child.wait(timeout=30)
        assert child.stdout is not None
        child.stdout.close()

    assert not dst.exists(), "a half download sits where a whole checkpoint would"
    leftovers = [p.name for p in checkpoints.iterdir()]
    assert all(name.endswith(".partial") for name in leftovers), leftovers

    # And the next try is a fresh, whole download.
    server.released.set()
    whole = serve(_send_slowly(body, 1 << 20, 0))
    torch.hub.download_url_to_file(
        whole.url(dst.name), str(dst), hashlib.sha256(body).hexdigest()[:8], progress=False
    )
    assert dst.read_bytes() == body


def test_section_warmup_disables_hugging_face_symlinks(monkeypatch):
    """Unelevated Windows cannot create the cache symlinks the hub prefers.

    Stubbed rather than patched through the real package so the guarantee is
    checked even where the optional inference dependency is not installed.
    """
    import types

    monkeypatch.delenv("HF_HUB_DISABLE_SYMLINKS", raising=False)
    package = types.ModuleType("allin1_infer")
    models = types.ModuleType("allin1_infer.models")
    seen = {}
    models.load_pretrained_model = lambda **kwargs: seen.update(kwargs)
    package.models = models
    monkeypatch.setitem(sys.modules, "allin1_infer", package)
    monkeypatch.setitem(sys.modules, "allin1_infer.models", models)

    warmup._warm_sections()

    assert os.environ["HF_HUB_DISABLE_SYMLINKS"] == "1"
    assert seen == {"model_name": warmup.SECTION_MODEL, "device": "cpu"}


def test_section_worker_disables_hugging_face_symlinks():
    """The worker downloads the same checkpoints in its own process."""
    import importlib

    module = importlib.import_module("app.pipeline.section_worker")

    assert "HF_HUB_DISABLE_SYMLINKS" in module.os.environ
