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


# fetch_verified_file: a checkpoint StemDeck downloads itself (the RoFormer
# vocal model), pinned by size and SHA-256. A local server stands in for the
# host, and a stand-in for its stem_models.ModelFile entry points at it.

SHA = hashlib.sha256(BODY).hexdigest()


def _model_file(server, size: int = len(BODY), sha256: str = SHA):
    from types import SimpleNamespace

    return SimpleNamespace(url=server.url("MelBandRoformer.ckpt"), sha256=sha256, size=size)


def _fetch(spec, dst, attempts: int = 3) -> None:
    model_download.fetch_verified_file(
        spec.url,
        str(dst),
        spec.sha256,
        spec.size,
        read_timeout=5,
        attempts=attempts,
        progress=False,
    )


def test_a_verified_file_that_matches_is_kept_through_the_vocal_model_path(serve, tmp_path):
    from app.pipeline import mel_roformer

    server = serve(lambda conn, n: conn.sendall(_response(BODY)))
    dst = tmp_path / "models" / "roformer" / "MelBandRoformer.ckpt"

    got = mel_roformer.ensure_checkpoint(
        read_timeout=5, attempts=1, model_file=_model_file(server), path=dst
    )

    assert got == dst and dst.read_bytes() == BODY
    assert _leftovers(dst) == []
    # Present and whole: the next job asks for nothing.
    mel_roformer.ensure_checkpoint(
        read_timeout=5, attempts=1, model_file=_model_file(server), path=dst
    )
    assert server.connections == 1


def test_a_verified_file_that_does_not_match_leaves_nothing_and_is_not_asked_twice(serve, tmp_path):
    server = serve(lambda conn, n: conn.sendall(_response(WRONG)))
    dst = tmp_path / "roformer" / "MelBandRoformer.ckpt"

    with pytest.raises(ModelDownloadError) as exc_info:
        _fetch(_model_file(server), dst)

    assert server.connections == 1
    assert list(dst.parent.iterdir()) == []
    message = str(exc_info.value)
    assert message.startswith(f"model download failed: {server.url('MelBandRoformer.ckpt')};")
    assert f"put the file in {dst.parent}" in message
    assert message.endswith("(file did not match its checksum)")


def test_a_verified_file_of_the_wrong_length_is_refused_before_it_is_read(serve, tmp_path):
    server = serve(lambda conn, n: conn.sendall(_response(BODY + b"extra")))
    dst = tmp_path / "dl" / "MelBandRoformer.ckpt"

    with pytest.raises(ModelDownloadError):
        _fetch(_model_file(server), dst)

    assert server.connections == 1
    assert list(dst.parent.iterdir()) == []


def test_a_cut_off_verified_file_is_tried_again(serve, tmp_path, _pauses):
    def respond(conn, n):
        if n == 1:
            conn.sendall(_response(BODY)[: -len(BODY) // 2])
            conn.close()
        else:
            conn.sendall(_response(BODY))

    server = serve(respond)
    dst = tmp_path / "dl" / "MelBandRoformer.ckpt"

    _fetch(_model_file(server), dst)

    assert server.connections == 2
    assert _pauses == [2]
    assert dst.read_bytes() == BODY
    assert _leftovers(dst) == []


def test_a_verified_file_that_never_arrives_whole_fails_with_the_folder(serve, tmp_path):
    def respond(conn, n):
        conn.sendall(_response(BODY)[: -len(BODY) // 2])
        conn.close()

    server = serve(respond)
    dst = tmp_path / "dl" / "MelBandRoformer.ckpt"

    with pytest.raises(ModelDownloadError) as exc_info:
        _fetch(_model_file(server), dst, attempts=2)

    assert server.connections == 2
    assert str(exc_info.value).endswith("(connection closed early)")
    assert list(dst.parent.iterdir()) == []


def test_a_redirect_to_plain_http_is_not_followed(serve, tmp_path):
    elsewhere = serve(lambda conn, n: conn.sendall(_response(BODY)))
    location = f"http://127.0.0.1:{elsewhere.port}/MelBandRoformer.ckpt"
    redirect = (
        f"HTTP/1.1 302 Found\r\nLocation: {location}\r\nContent-Length: 0\r\n"
        "Connection: close\r\n\r\n"
    ).encode()
    server = serve(lambda conn, n: conn.sendall(redirect))
    dst = tmp_path / "dl" / "MelBandRoformer.ckpt"

    with pytest.raises(ModelDownloadError) as exc_info:
        _fetch(_model_file(server), dst, attempts=1)

    assert elsewhere.connections == 0
    assert str(exc_info.value).endswith("(HTTP 302)")
    assert list(dst.parent.iterdir()) == []


def test_a_redirect_to_https_is_followed():
    """Hugging Face answers a pinned file with a redirect to its CDN."""
    from urllib.request import Request

    handler = model_download._HttpsRedirectOnly()
    req = Request("https://huggingface.co/x/resolve/abc/m.ckpt")
    followed = handler.redirect_request(req, None, 302, "Found", {}, "https://cdn-lfs.hf.co/m")
    assert followed is not None and followed.full_url == "https://cdn-lfs.hf.co/m"
    for scheme in ("http://cdn/m", "ftp://cdn/m", "file:///etc/passwd"):
        assert handler.redirect_request(req, None, 302, "Found", {}, scheme) is None


def test_a_verified_file_is_on_the_disk_before_it_takes_its_name(serve, tmp_path, monkeypatch):
    """A crash right after the move must not leave a truncated file under the
    final name: the data is fsynced first, the folder after the move."""
    import os

    from app.core import settings

    events: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(os, "fsync", lambda fd: (events.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(
        os, "replace", lambda a, b: (events.append("replace"), real_replace(a, b))[1]
    )
    monkeypatch.setattr(settings, "fsync_dir", lambda d: events.append(f"fsync_dir {d}"))
    server = serve(lambda conn, n: conn.sendall(_response(BODY)))
    dst = tmp_path / "dl" / "MelBandRoformer.ckpt"

    _fetch(_model_file(server), dst)

    assert events == ["fsync", "replace", f"fsync_dir {dst.parent}"]


def test_the_mirror_download_is_on_the_disk_before_it_takes_its_name(hosts, monkeypatch):
    import os

    from app.core import settings

    events: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(os, "fsync", lambda fd: (events.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(
        os, "replace", lambda a, b: (events.append("replace"), real_replace(a, b))[1]
    )
    monkeypatch.setattr(settings, "fsync_dir", lambda d: events.append("fsync_dir"))
    mirror, origin, url, dst = hosts(lambda conn, n: conn.sendall(_response(BODY)))

    with bounded_downloads(5, 1):
        torch.hub.download_url_to_file(url, str(dst), PREFIX, progress=False)

    assert dst.read_bytes() == BODY
    assert events == ["fsync", "replace", "fsync_dir"]


def test_a_verified_download_needs_a_real_checksum_and_size(tmp_path):
    with pytest.raises(ValueError):
        model_download.fetch_verified_file(
            "https://h/m", str(tmp_path / "m"), "abc", 5, read_timeout=1, attempts=1
        )
    with pytest.raises(ValueError):
        model_download.fetch_verified_file(
            "https://h/m", str(tmp_path / "m"), SHA, 0, read_timeout=1, attempts=1
        )


def test_a_verified_download_follows_only_https_redirects(serve, tmp_path, monkeypatch):
    """The handler is the https-only one, not the mirror's refuse-all one: a
    Hugging Face CDN redirect must be followed."""
    seen: list[type] = []
    real = model_download.build_opener

    def spy(*handlers):
        seen.extend(handlers)
        return real(*handlers)

    monkeypatch.setattr(model_download, "build_opener", spy)
    server = serve(lambda conn, n: conn.sendall(_response(BODY)))
    _fetch(_model_file(server), tmp_path / "dl" / "MelBandRoformer.ckpt")
    assert seen == [model_download._HttpsRedirectOnly]


# Partials a hard kill left behind.


def _age(path, seconds: float) -> None:
    import os

    then = time.time() - seconds
    os.utime(path, (then, then))


def test_old_partials_of_the_file_are_swept_before_a_verified_download(serve, tmp_path):
    folder = tmp_path / "dl"
    folder.mkdir()
    name = "MelBandRoformer.ckpt"
    stale = folder / f"{name}.{'a' * 32}.partial"
    fresh = folder / f"{name}.{'b' * 32}.partial"
    other = folder / f"other.ckpt.{'c' * 32}.partial"
    short_hex = folder / f"{name}.{'d' * 16}.partial"
    not_partial = folder / f"{name}.{'e' * 32}.part"
    a_dir = folder / f"{name}.{'f' * 32}.partial"
    for p in (stale, fresh, other, short_hex, not_partial):
        p.write_bytes(b"x")
    a_dir.mkdir()
    for p in (stale, other, short_hex, not_partial, a_dir):
        _age(p, model_download.PARTIAL_MAX_AGE_SEC + 60)
    server = serve(lambda conn, n: conn.sendall(_response(BODY)))

    _fetch(_model_file(server), folder / name)

    left = sorted(p.name for p in folder.iterdir())
    assert stale.name not in left
    assert {fresh.name, other.name, short_hex.name, not_partial.name, a_dir.name, name} == set(left)


def test_a_partial_that_is_a_link_is_never_swept(tmp_path):
    import os

    folder = tmp_path / "dl"
    folder.mkdir()
    target = tmp_path / "precious.bin"
    target.write_bytes(b"keep")
    link = folder / f"m.ckpt.{'a' * 32}.partial"
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("this machine cannot make a symlink")
    _age(target, model_download.PARTIAL_MAX_AGE_SEC + 60)
    assert model_download.sweep_partials(folder, "m.ckpt") == []
    assert link.is_symlink() and target.read_bytes() == b"keep"


def test_a_sweep_of_a_missing_folder_is_nothing():
    assert model_download.sweep_partials("Z:/no/such/folder/at/all", "m.ckpt") == []


# Demucs' checkpoints, pinned by their full SHA-256 on the original host too.


def test_every_demucs_checkpoint_is_pinned_in_full_and_fits_its_name():
    from app.core import stem_models

    hub = stem_models.hub_files()
    names = {f.relpath for f in hub.values()}
    assert names == {
        "5c90dfd2-34c22ccb.th",
        "f7e0c4bc-ba3fe64a.th",
        "d12395a8-e57c48e6.th",
        "92cfc3b6-ef3bcb9c.th",
        "04573f0d-f3cf25b2.th",
    }
    for url, f in hub.items():
        assert url == "https://dl.fbaipublicfiles.com/demucs/hybrid_transformer/" + f.relpath
        # torch.hub's own check is this prefix; the pin is the whole hash.
        assert f.sha256.startswith(f.relpath.split("-")[1].split(".")[0])
        assert f.root == "torch_hub"
    # The mirror's table and the registry agree on htdemucs_6s's file.
    for url, entry in model_download.MIRRORED_FILES.items():
        assert (hub[url].sha256, hub[url].size) == (entry.sha256, entry.size)


def test_a_pinned_checkpoint_from_the_original_host_is_verified_in_full(
    serve, tmp_path, monkeypatch
):
    """torch.hub would keep this file: no hash prefix was asked for, and it
    checks only 8 hex characters when one is. The pin is the full SHA-256."""
    from types import SimpleNamespace

    server = serve(lambda conn, n: conn.sendall(_response(WRONG)))
    url = server.url(f"model-{PREFIX}.th")
    monkeypatch.setattr(
        model_download.stem_models,
        "hub_files",
        lambda: {url: SimpleNamespace(sha256=SHA, size=len(BODY))},
    )
    dst = tmp_path / "checkpoints" / f"model-{PREFIX}.th"
    dst.parent.mkdir()

    with pytest.raises(ModelDownloadError) as exc_info, bounded_downloads(5, 3):
        torch.hub.download_url_to_file(url, str(dst), None, progress=False)

    assert server.connections == 1  # the same wrong file is not asked for again
    assert str(exc_info.value).endswith("(file did not match its checksum)")
    assert list(dst.parent.iterdir()) == []


def test_a_pinned_checkpoint_that_matches_is_kept(serve, tmp_path, monkeypatch):
    from types import SimpleNamespace

    server = serve(lambda conn, n: conn.sendall(_response(BODY)))
    url = server.url(f"model-{PREFIX}.th")
    monkeypatch.setattr(
        model_download.stem_models,
        "hub_files",
        lambda: {url: SimpleNamespace(sha256=SHA, size=len(BODY))},
    )
    dst = tmp_path / "checkpoints" / f"model-{PREFIX}.th"
    dst.parent.mkdir()

    with bounded_downloads(5, 1):
        torch.hub.download_url_to_file(url, str(dst), PREFIX, progress=False)

    assert dst.read_bytes() == BODY
    assert _leftovers(dst) == []


def test_a_cached_checkpoint_that_is_not_its_pin_is_removed_before_a_load(tmp_path, monkeypatch):
    """A copy from before the pins (checked by 8 hex characters only), or one
    cut short: removed, so torch.hub fetches it again through the check. A
    matching one, someone's own copy included, is kept."""
    from types import SimpleNamespace

    from app.core import stem_models

    hub = tmp_path / "hub" / "checkpoints"
    hub.mkdir(parents=True)
    monkeypatch.setattr(stem_models, "hub_checkpoints_dir", lambda: hub)
    good = SimpleNamespace(root="torch_hub", relpath="good-aaaa.th", sha256=SHA, size=len(BODY))
    bad = SimpleNamespace(root="torch_hub", relpath="bad-bbbb.th", sha256=SHA, size=len(BODY))
    absent = SimpleNamespace(root="torch_hub", relpath="gone-cccc.th", sha256=SHA, size=1)
    monkeypatch.setattr(stem_models, "MODELS", {"x": SimpleNamespace(files=(good, bad, absent))})
    (hub / "good-aaaa.th").write_bytes(BODY)
    (hub / "bad-bbbb.th").write_bytes(WRONG)

    removed = model_download.drop_unverified("x")

    assert removed == [str(hub / "bad-bbbb.th")]
    assert sorted(p.name for p in hub.iterdir()) == ["good-aaaa.th"]
    assert model_download.drop_unverified("no_such_model") == []
