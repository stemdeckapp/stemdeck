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


# The model mirror. Both hosts are local servers: the "original" one stands in
# for Meta's, and the table is pointed at it for the test.

MIRROR_PATH = f"demucs/test/model-{PREFIX}.th"
GOOD = model_download.MirroredFile(
    path=MIRROR_PATH, sha256=hashlib.sha256(BODY).hexdigest(), size=len(BODY)
)
WRONG = bytes([BODY[0] ^ 1]) + BODY[1:]


class _Recording(_Server):
    """A server that also keeps each request's path."""

    def __init__(self, respond):
        self.paths: list[str] = []
        super().__init__(respond)

    def _handle(self, conn: socket.socket, n: int) -> None:
        request = b""
        while b"\r\n\r\n" not in request:
            chunk = conn.recv(4096)
            if not chunk:
                return
            request += chunk
        self.paths.append(request.split(b" ", 2)[1].decode())
        self._respond(conn, n)


@pytest.fixture()
def hosts(serve, monkeypatch, tmp_path):
    """Start a mirror and an original host, put the test file in the table
    under the original host's address, and return (mirror, origin, url, dst)."""
    started: list[_Server] = []

    def start(mirror_respond, origin_respond=None):
        origin = serve(origin_respond or (lambda conn, n: conn.sendall(_response(BODY))))
        mirror = _Recording(mirror_respond)
        started.append(mirror)
        url = f"http://127.0.0.1:{origin.port}/{MIRROR_PATH}"
        monkeypatch.setattr(model_download, "MIRRORED_FILES", {url: GOOD})
        monkeypatch.setattr(model_download, "MODEL_MIRROR", f"http://127.0.0.1:{mirror.port}")
        folder = tmp_path / "checkpoints"
        folder.mkdir()
        return mirror, origin, url, folder / f"model-{PREFIX}.th"

    yield start
    for server in started:
        server.close()


def _leftovers(dst):
    return sorted(p.name for p in dst.parent.iterdir() if p != dst)


def test_a_tabled_file_comes_from_the_mirror_and_never_asks_the_original_host(hosts, capsys):
    mirror, origin, url, dst = hosts(lambda conn, n: conn.sendall(_response(BODY)))

    with bounded_downloads(5, 3):
        torch.hub.download_url_to_file(url, str(dst), PREFIX, progress=False)

    assert dst.read_bytes() == BODY
    assert mirror.paths == [f"/{MIRROR_PATH}"]
    assert origin.connections == 0
    assert _leftovers(dst) == []
    assert "model mirror failed" not in capsys.readouterr().err


def test_a_mirror_that_cannot_be_reached_falls_back_to_the_original_host(
    hosts, monkeypatch, capsys, _pauses
):
    mirror, origin, url, dst = hosts(lambda conn, n: None)
    with socket.create_server(("127.0.0.1", 0)) as s:
        dead = s.getsockname()[1]
    monkeypatch.setattr(model_download, "MODEL_MIRROR", f"http://127.0.0.1:{dead}")

    with bounded_downloads(5, 2):
        torch.hub.download_url_to_file(url, str(dst), PREFIX, progress=False)

    assert dst.read_bytes() == BODY
    assert origin.connections == 1
    # The mirror had the same tries and pauses as any download.
    assert _pauses == [2]
    err = capsys.readouterr().err.splitlines()
    assert "model mirror failed (connection refused), downloading from the original host" in err
    # Said again so the worker's parent follows the second download.
    assert f'Downloading: "{url}" to {dst}' in err


def test_a_mirror_error_status_falls_back(hosts, capsys):
    mirror, origin, url, dst = hosts(lambda conn, n: conn.sendall(_response(b"x", "404 Not Found")))

    with bounded_downloads(5, 1):
        torch.hub.download_url_to_file(url, str(dst), PREFIX, progress=False)

    assert dst.read_bytes() == BODY
    assert "model mirror failed (HTTP 404)" in capsys.readouterr().err


def test_a_wrong_file_from_the_mirror_is_never_kept_and_not_asked_for_twice(hosts, capsys):
    mirror, origin, url, dst = hosts(lambda conn, n: conn.sendall(_response(WRONG)))

    with bounded_downloads(5, 3):
        torch.hub.download_url_to_file(url, str(dst), PREFIX, progress=False)

    assert dst.read_bytes() == BODY
    assert mirror.connections == 1
    assert origin.connections == 1
    assert _leftovers(dst) == []
    assert "model mirror failed (SHA-256 does not match)" in capsys.readouterr().err


def test_a_wrong_file_with_the_original_host_down_leaves_the_folder_empty(hosts):
    mirror, origin, url, dst = hosts(
        lambda conn, n: conn.sendall(_response(WRONG)),
        lambda conn, n: conn.sendall(_response(b"no", "403 Forbidden")),
    )

    with pytest.raises(ModelDownloadError) as exc_info, bounded_downloads(5, 1):
        torch.hub.download_url_to_file(url, str(dst), PREFIX, progress=False)

    # The error names the original address, as before the mirror existed.
    assert str(exc_info.value).startswith(f"model download failed: {url};")
    assert str(exc_info.value).endswith("(HTTP 403)")
    assert list(dst.parent.iterdir()) == []


def test_a_mirror_file_of_the_wrong_length_is_refused_before_it_is_read(hosts, capsys):
    mirror, origin, url, dst = hosts(lambda conn, n: conn.sendall(_response(BODY + b"extra")))

    with bounded_downloads(5, 3):
        torch.hub.download_url_to_file(url, str(dst), PREFIX, progress=False)

    assert dst.read_bytes() == BODY
    assert mirror.connections == 1
    assert _leftovers(dst) == []
    assert f"expected {len(BODY)}" in capsys.readouterr().err


def test_a_mirror_body_longer_than_the_table_says_is_refused(hosts):
    head = b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n"

    def respond(conn, n):
        conn.sendall(head + BODY + b"extra")
        conn.close()

    mirror, origin, url, dst = hosts(respond)

    with bounded_downloads(5, 3):
        torch.hub.download_url_to_file(url, str(dst), PREFIX, progress=False)

    assert dst.read_bytes() == BODY
    assert mirror.connections == 1
    assert _leftovers(dst) == []


def test_a_cut_off_mirror_body_is_tried_again_on_the_mirror(hosts, _pauses):
    def respond(conn, n):
        if n == 1:
            conn.sendall(_response(BODY)[: -len(BODY) // 2])
            conn.close()
        else:
            conn.sendall(_response(BODY))

    mirror, origin, url, dst = hosts(respond)

    with bounded_downloads(5, 3):
        torch.hub.download_url_to_file(url, str(dst), PREFIX, progress=False)

    assert dst.read_bytes() == BODY
    assert mirror.connections == 2
    assert origin.connections == 0
    assert _pauses == [2]
    assert _leftovers(dst) == []


def test_a_mirror_redirect_is_not_followed(hosts, serve, capsys):
    elsewhere = serve(lambda conn, n: conn.sendall(_response(BODY)))
    location = f"http://127.0.0.1:{elsewhere.port}/{MIRROR_PATH}"
    redirect = (
        f"HTTP/1.1 302 Found\r\nLocation: {location}\r\nContent-Length: 0\r\n"
        "Connection: close\r\n\r\n"
    ).encode()
    mirror, origin, url, dst = hosts(lambda conn, n: conn.sendall(redirect))

    with bounded_downloads(5, 1):
        torch.hub.download_url_to_file(url, str(dst), PREFIX, progress=False)

    assert elsewhere.connections == 0
    assert origin.connections == 1
    assert dst.read_bytes() == BODY
    assert "model mirror failed (HTTP 302)" in capsys.readouterr().err


def test_no_mirror_setting_uses_the_original_host_only(hosts, monkeypatch):
    mirror, origin, url, dst = hosts(lambda conn, n: conn.sendall(_response(BODY)))
    monkeypatch.setattr(model_download, "MODEL_MIRROR", None)

    with bounded_downloads(5, 1):
        torch.hub.download_url_to_file(url, str(dst), PREFIX, progress=False)

    assert mirror.connections == 0
    assert origin.connections == 1
    assert dst.read_bytes() == BODY


def test_a_file_not_in_the_table_is_never_mirrored(hosts):
    mirror, origin, url, dst = hosts(lambda conn, n: conn.sendall(_response(BODY)))
    other = url.replace("/demucs/test/", "/demucs/other/")

    with bounded_downloads(5, 1):
        torch.hub.download_url_to_file(other, str(dst), PREFIX, progress=False)

    assert mirror.connections == 0
    assert origin.connections == 1


def test_a_destination_with_another_name_is_never_mirrored(hosts):
    mirror, origin, url, dst = hosts(lambda conn, n: conn.sendall(_response(BODY)))

    with bounded_downloads(5, 1):
        torch.hub.download_url_to_file(url, str(dst.with_name("x.th")), PREFIX, progress=False)

    assert mirror.connections == 0
    assert origin.connections == 1


def test_the_demucs_load_path_takes_the_mirror(serve, monkeypatch, tmp_path):
    """load_state_dict_from_url with check_hash=True, as demucs/repo.py calls it."""
    import io

    buf = io.BytesIO()
    torch.save({"w": torch.zeros(3)}, buf)
    body = buf.getvalue()
    digest = hashlib.sha256(body).hexdigest()
    path = f"demucs/test/model-{digest[:8]}.th"
    origin = serve(lambda conn, n: conn.sendall(_response(b"no", "403 Forbidden")))
    mirror = serve(lambda conn, n: conn.sendall(_response(body)))
    url = f"http://127.0.0.1:{origin.port}/{path}"
    entry = model_download.MirroredFile(path=path, sha256=digest, size=len(body))
    monkeypatch.setattr(model_download, "MIRRORED_FILES", {url: entry})
    monkeypatch.setattr(model_download, "MODEL_MIRROR", f"http://127.0.0.1:{mirror.port}")

    checkpoints = tmp_path / "checkpoints"
    with bounded_downloads(5, 1):
        state = torch.hub.load_state_dict_from_url(url, model_dir=str(checkpoints), check_hash=True)

    assert torch.equal(state["w"], torch.zeros(3))
    assert origin.connections == 0
    assert [p.name for p in checkpoints.iterdir()] == [f"model-{digest[:8]}.th"]


def test_the_table_holds_the_file_demucs_asks_for_for_htdemucs_6s():
    from demucs import pretrained

    remote = pretrained._parse_remote_files(pretrained.REMOTE_ROOT / "files.txt")
    bag = (pretrained.REMOTE_ROOT / "htdemucs_6s.yaml").read_text()
    # One model, so one file.
    assert "models: ['5c90dfd2']" in bag
    entry = model_download.MIRRORED_FILES[remote["5c90dfd2"]]
    assert entry.size == 54_996_327
    assert entry.sha256.startswith("34c22ccb")
    assert all(
        u.startswith("https://dl.fbaipublicfiles.com/") for u in model_download.MIRRORED_FILES
    )


@pytest.mark.parametrize(
    "path",
    [
        "../demucs/x-34c22ccb.th",
        "demucs/../x-34c22ccb.th",
        "demucs/Upper/x-34c22ccb.th",
        "demucs\\x-34c22ccb.th",
        "x-34c22ccb.th",
        "demucs/x-34c22ccb.pt",
    ],
)
def test_a_table_path_that_could_escape_or_is_not_a_checkpoint_is_refused(path):
    with pytest.raises(ValueError):
        model_download.MirroredFile(path=path, sha256="34c22ccb" + "0" * 56, size=1)


def test_a_table_hash_that_does_not_fit_the_file_name_is_refused():
    with pytest.raises(ValueError):
        model_download.MirroredFile(path="demucs/x-34c22ccb.th", sha256="0" * 64, size=1)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, "https://files.stemdeck.app"),
        ("", None),
        ("   ", None),
        ("https://mirror.example", "https://mirror.example"),
        ("https://mirror.example/models/", "https://mirror.example/models"),
        ("https://mirror.example:8443", "https://mirror.example:8443"),
        ("http://mirror.example", None),
        ("HTTP://mirror.example", None),
        ("ftp://mirror.example", None),
        ("file:///etc/passwd", None),
        ("mirror.example", None),
        ("https://", None),
        ("https://user:pw@mirror.example", None),
        ("https://mirror.example/?x=1", None),
        ("https://mirror.example/#x", None),
        ("https://mirror.example/../x", None),
        ("https://mirror.example:99999", None),
        ("https://mirror.example\\x", None),
        ("https://mirror .example", None),
    ],
)
def test_the_mirror_setting_takes_https_only(raw, expected):
    from app.core.config import parse_model_mirror

    assert parse_model_mirror(raw) == expected
