"""The separation model's download is bounded (#125).

A network that opens the connection to the model host and then sends nothing
held the first Split for 30 minutes at 0% CPU, then failed as "exit status 1".
Every test here runs torch.hub's real download code against a local server
that misbehaves on purpose: no real network, no real model.
"""

from __future__ import annotations

import errno
import hashlib
import socket
import ssl
import threading
import time

import pytest
import torch.hub

from app.core import model_download
from app.core.model_download import ModelDownloadError, bounded_downloads, fetch_with_retry


class _Server:
    """A local HTTP server on a throwaway port that answers each connection
    with `respond(conn, n)`, n counting connections from 1."""

    def __init__(self, respond):
        self._respond = respond
        self.released = threading.Event()
        self.connections = 0
        self._sock = socket.create_server(("127.0.0.1", 0))
        self._conns: list[socket.socket] = []
        self.port = self._sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def url(self, name: str = "model-deadbeef.th") -> str:
        return f"http://127.0.0.1:{self.port}/demucs/{name}"

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            self.connections += 1
            self._conns.append(conn)
            threading.Thread(
                target=self._handle, args=(conn, self.connections), daemon=True
            ).start()

    def _handle(self, conn: socket.socket, n: int) -> None:
        request = b""
        while b"\r\n\r\n" not in request:
            chunk = conn.recv(4096)
            if not chunk:
                return
            request += chunk
        self._respond(conn, n)

    def close(self) -> None:
        self.released.set()
        self._sock.close()
        for conn in self._conns:
            conn.close()


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


@pytest.fixture(autouse=True)
def _pauses(monkeypatch):
    """The pauses between tries, recorded instead of waited out."""
    waited: list[float] = []
    monkeypatch.setattr(model_download, "sleep", waited.append)
    return waited


def _response(body: bytes, status: str = "200 OK") -> bytes:
    head = f"HTTP/1.1 {status}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n"
    return head.encode() + body


BODY = b"checkpoint bytes " * 64
PREFIX = hashlib.sha256(BODY).hexdigest()[:8]


def test_a_stalled_host_fails_after_every_try_and_names_the_file_and_folder(
    serve, tmp_path, _pauses
):
    server = serve(lambda conn, n: server.released.wait())
    dst = tmp_path / "checkpoints" / "model-deadbeef.th"
    dst.parent.mkdir()

    started = time.monotonic()
    with pytest.raises(ModelDownloadError) as exc_info, bounded_downloads(1, 3):
        torch.hub.download_url_to_file(server.url(), str(dst), PREFIX, progress=False)
    elapsed = time.monotonic() - started

    assert server.connections == 3
    assert _pauses == [2, 5]
    # Three one-second reads; the pauses are recorded, not waited.
    assert elapsed < 10
    message = str(exc_info.value)
    assert message.startswith("model download failed:")
    assert server.url() in message
    assert "no data for 1 s" in message
    assert str(dst.parent) in message
    assert not dst.exists()


def test_the_block_leaves_the_socket_timeout_and_torch_hub_as_they_were(serve, tmp_path):
    server = serve(lambda conn, n: server.released.wait())
    original = torch.hub.download_url_to_file
    before = socket.getdefaulttimeout()

    with pytest.raises(ModelDownloadError), bounded_downloads(1, 1):
        assert socket.getdefaulttimeout() == 1
        torch.hub.download_url_to_file(server.url(), str(tmp_path / "x.th"), progress=False)

    assert socket.getdefaulttimeout() == before
    assert torch.hub.download_url_to_file is original


def test_a_load_through_load_state_dict_from_url_is_bounded(serve, tmp_path):
    """The call demucs makes (demucs/repo.py), with its check_hash=True."""
    server = serve(lambda conn, n: server.released.wait())

    with pytest.raises(ModelDownloadError) as exc_info, bounded_downloads(1, 1):
        torch.hub.load_state_dict_from_url(
            server.url("5c90dfd2-34c22ccb.th"), model_dir=str(tmp_path), check_hash=True
        )

    assert "5c90dfd2-34c22ccb.th" in str(exc_info.value)
    assert str(tmp_path) in str(exc_info.value)


def test_a_cut_off_body_fails_its_checksum_and_is_tried_again(serve, tmp_path, _pauses):
    """http.client returns a short body without an error when the host closes
    early; the checksum in the file name is what catches it."""

    def respond(conn, n):
        if n == 1:
            conn.sendall(_response(BODY)[: -len(BODY) // 2])
            conn.close()
        else:
            conn.sendall(_response(BODY))

    server = serve(respond)
    dst = tmp_path / "model-deadbeef.th"

    with bounded_downloads(5, 3):
        torch.hub.download_url_to_file(server.url(), str(dst), PREFIX, progress=False)

    assert server.connections == 2
    assert _pauses == [2]
    assert dst.read_bytes() == BODY


def test_a_body_that_never_matches_its_checksum_is_a_download_failure(serve, tmp_path):
    server = serve(lambda conn, n: conn.sendall(_response(b"<html>captive portal</html>")))

    with pytest.raises(ModelDownloadError) as exc_info, bounded_downloads(5, 2):
        torch.hub.download_url_to_file(server.url(), str(tmp_path / "m.th"), PREFIX, progress=False)

    assert server.connections == 2
    assert str(exc_info.value).endswith("(file did not match its checksum)")


@pytest.mark.parametrize(
    ("respond", "reason"),
    [
        (lambda conn, n: conn.sendall(_response(b"no", "403 Forbidden")), "HTTP 403"),
        (lambda conn, n: conn.close(), "connection closed early"),
    ],
)
def test_each_failure_says_what_happened(serve, tmp_path, respond, reason):
    server = serve(respond)

    with pytest.raises(ModelDownloadError) as exc_info, bounded_downloads(5, 1):
        torch.hub.download_url_to_file(server.url(), str(tmp_path / "m.th"), progress=False)

    assert str(exc_info.value).endswith(f"({reason})")


def test_a_refused_connection_says_so(tmp_path):
    with socket.create_server(("127.0.0.1", 0)) as s:
        port = s.getsockname()[1]
    url = f"http://127.0.0.1:{port}/m.th"

    with pytest.raises(ModelDownloadError) as exc_info, bounded_downloads(5, 1):
        torch.hub.download_url_to_file(url, str(tmp_path / "m.th"), progress=False)

    assert str(exc_info.value).endswith("(connection refused)")


def test_a_tls_connection_cut_mid_file_is_tried_again(_pauses):
    calls = []

    def fn():
        calls.append(1)
        if len(calls) == 1:
            raise ssl.SSLEOFError(8, "EOF occurred in violation of protocol")

    fetch_with_retry(fn, url="https://h/m.th", folder="f", attempts=3, read_timeout=1)

    assert calls == [1, 1]
    assert _pauses == [2]


def test_a_full_disk_is_not_retried_or_called_a_download_failure(_pauses):
    calls = []

    def fn():
        calls.append(1)
        raise OSError(errno.ENOSPC, "No space left on device")

    with pytest.raises(OSError) as exc_info:
        fetch_with_retry(fn, url="http://h/m.th", folder="f", attempts=3, read_timeout=1)

    assert not isinstance(exc_info.value, ModelDownloadError)
    assert calls == [1]
    assert _pauses == []


def test_each_failed_try_is_said_on_stderr(capsys, _pauses):
    def fn():
        raise TimeoutError("timed out")

    with pytest.raises(ModelDownloadError):
        fetch_with_retry(fn, url="http://h/m.th", folder="f", attempts=3, read_timeout=30)

    err = capsys.readouterr().err.splitlines()
    assert err == [
        "model download try 1 of 3 failed (no data for 30 s), trying again in 2 s",
        "model download try 2 of 3 failed (no data for 30 s), trying again in 5 s",
    ]
