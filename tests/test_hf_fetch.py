"""Fetching a user model from Hugging Face (app/core/hf_fetch.py): the link
parser, the hosts a request may reach, and the capped, verified download.

The requests go to a local http.server standing in for huggingface.co: the
module's base address, scheme, ports and host allowlist are pointed at it, so
the real urllib code (redirects included) runs and nothing leaves the
machine. "localhost" plays a host that is not allowed."""

from __future__ import annotations

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.core import hf_fetch as hf
from app.core.user_model_format import Refused

COMMIT = "a" * 40


def _refused(code: str, fn, *args, **kwargs):
    with pytest.raises(Refused) as info:
        fn(*args, **kwargs)
    assert info.value.code == code


# ── the link ──


@pytest.mark.parametrize(
    ("text", "parts"),
    [
        ("https://huggingface.co/owner/repo", ("owner", "repo", "main", None)),
        ("https://huggingface.co/owner/repo/", ("owner", "repo", "main", None)),
        ("https://HuggingFace.co/owner/repo?x=1#top", ("owner", "repo", "main", None)),
        ("https://huggingface.co:443/o/r", ("o", "r", "main", None)),
        ("https://huggingface.co/o/r/tree/v1.0", ("o", "r", "v1.0", None)),
        ("https://huggingface.co/o/r/blob/main/m.ckpt", ("o", "r", "main", "m.ckpt")),
        (
            "https://huggingface.co/o/r/resolve/" + COMMIT + "/sub/m%20v2.safetensors?download=1",
            ("o", "r", COMMIT, "sub/m v2.safetensors"),
        ),
    ],
)
def test_a_good_link_parses(text, parts):
    link = hf.parse_link(text)
    assert (link.owner, link.repo, link.rev, link.file) == parts


@pytest.mark.parametrize(
    "text",
    [
        "http://huggingface.co/o/r",
        "https://user@huggingface.co/o/r",
        "https://user:pw@huggingface.co/o/r",
        "https://huggingface.co:8443/o/r",
        "https://huggingface.co./o/r",
        "https://huggingface.co.evil.com/o/r",
        "https://evilhuggingface.co/o/r",
        "https://cdn-lfs.huggingface.co/o/r",
        "https://hf.co/o/r",
        "https://huggingfаce.co/o/r",  # a Cyrillic a
        "https://xn--huggingface-xyz.co/o/r",
        "https://huggingface.co/o",
        "https://huggingface.co/datasets/o/r",
        "https://huggingface.co/spaces/o/r",
        "https://huggingface.co/o/r/blob/main/../../etc/passwd",
        "https://huggingface.co/o/r/blob/main/a/b/c/d.ckpt",
        "https://huggingface.co/o/r/blob/../m.ckpt",
        "https://huggingface.co/o/r/tree/main/sub",
        "https://huggingface.co/o/r/commits/main",
        "https://huggingface.co/o/r/blob/main/.hidden",
        "https://huggingface.co/o/r/blob/main/a%2F..%2Fb",
        "https://huggingface.co/o%2Fx/r",
        "https://huggingface.co/o/r with space",
        "https://huggingface.co\\o\\r",
        "https://huggingface.co/" + "o" * 120 + "/r",
        "https://huggingface.co/o/" + "r" * 300,
        "ftp://huggingface.co/o/r",
        "huggingface.co/o/r",
        "",
        None,
        42,
    ],
)
def test_a_bad_link_is_refused(text):
    _refused("bad_link", hf.parse_link, text)


def test_requests_are_built_from_the_parts_and_pinned_to_the_commit():
    link = hf.parse_link("https://huggingface.co/o/r/blob/main/sub/m%20v2.ckpt?token=x")
    assert hf.api_url(link) == "https://huggingface.co/api/models/o/r/revision/main?blobs=true"
    url = hf.file_url(link, COMMIT, "sub/m v2.ckpt")
    assert url == f"https://huggingface.co/o/r/resolve/{COMMIT}/sub/m%20v2.ckpt"
    assert "token" not in link.canonical()
    _refused("bad_link", hf.file_url, link, "main", "m.ckpt")  # never a branch


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://huggingface.co/x", True),
        ("https://HUGGINGFACE.CO/x", True),
        ("https://hf.co/x", True),
        ("https://cdn-lfs.huggingface.co/x", True),
        ("https://cdn-lfs.hf.co/x", True),
        ("https://cas-bridge.xethub.hf.co/x", True),
        ("https://huggingface.co:443/x", True),
        ("http://cdn-lfs.hf.co/x", False),
        ("https://cdn-lfs.hf.co:8443/x", False),
        ("https://evilhuggingface.co/x", False),
        ("https://evilhf.co/x", False),
        ("https://huggingface.co.evil.com/x", False),
        ("https://huggingface.co./x", False),
        ("https://u@huggingface.co/x", False),
        ("https://.hf.co/x", False),
        ("https://example.com/x", False),
        ("file:///etc/passwd", False),
    ],
)
def test_the_hosts_a_request_may_reach(url, ok):
    assert hf.target_ok(url) is ok


# ── a local stand-in for huggingface.co ──


class _Server:
    def __init__(self) -> None:
        self.routes: dict[str, object] = {}
        self.headers: list[dict[str, str]] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args):
                pass

            def do_GET(self):  # noqa: N802
                server.headers.append(dict(self.headers.items()))
                route = server.routes.get(self.path)
                if route is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                route(self)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def body(self, path: str, data: bytes, *, length: str | None = "real", status: int = 200):
        def answer(h):
            h.send_response(status)
            if length == "real":
                h.send_header("Content-Length", str(len(data)))
            elif length is not None:
                h.send_header("Content-Length", length)
            h.end_headers()
            h.wfile.write(data)

        self.routes[path] = answer

    def redirect(self, path: str, location: str):
        def answer(h):
            h.send_response(302)
            h.send_header("Location", location)
            h.send_header("Content-Length", "0")
            h.end_headers()

        self.routes[path] = answer

    def json(self, path: str, value: object):
        self.body(path, json.dumps(value).encode())

    def status(self, path: str, status: int):
        self.body(path, b"", status=status)


@pytest.fixture
def server(monkeypatch):
    srv = _Server()
    monkeypatch.setattr(hf, "_BASE", f"http://127.0.0.1:{srv.port}")
    monkeypatch.setattr(hf, "_SCHEMES", ("http",))
    monkeypatch.setattr(hf, "_PORTS", (srv.port,))
    monkeypatch.setattr(hf, "ALLOWED_HOSTS", frozenset({"127.0.0.1"}))
    monkeypatch.setattr(hf, "ALLOWED_SUFFIXES", ())
    monkeypatch.setattr(hf, "MODEL_DOWNLOAD_ATTEMPTS", 2)
    monkeypatch.setattr(hf, "MODEL_DOWNLOAD_BACKOFF", (0, 0))
    yield srv
    srv.httpd.shutdown()
    srv.httpd.server_close()


def _link(file: str | None = None) -> hf.HfLink:
    return hf.HfLink("o", "r", "main", file)


def _api(server, siblings, **extra):
    server.json(
        "/api/models/o/r/revision/main?blobs=true", {"sha": COMMIT, "siblings": siblings, **extra}
    )


def _sibling(name, data: bytes | None = None, *, lfs=True, size=None):
    entry: dict = {"rfilename": name}
    if data is not None:
        entry["size"] = len(data) if size is None else size
        if lfs:
            entry["lfs"] = {"sha256": hashlib.sha256(data).hexdigest(), "size": entry["size"]}
    return entry


def _url(server, path: str) -> str:
    return f"http://127.0.0.1:{server.port}{path}"


def _resolve(path: str) -> str:
    return f"/o/r/resolve/{COMMIT}/{path}"


# ── the repository ──


def test_repo_info_reads_the_commit_files_and_licence(server):
    data = b"weights"
    _api(
        server,
        [
            _sibling("m.safetensors", data),
            _sibling("config.yaml", b"a: 1", lfs=False),
            {"rfilename": "../x"},
        ],
        cardData={"license": "<b>cc-by-nc-4.0</b>"},
    )
    info = hf.repo_info(_link(), threading.Event())
    assert info.commit == COMMIT
    assert set(info.files) == {"m.safetensors", "config.yaml"}  # "../x" is never kept
    assert info.files["m.safetensors"].sha256 == hashlib.sha256(data).hexdigest()
    assert info.files["config.yaml"].sha256 is None
    assert info.license == "cc-by-nc-4.0"


@pytest.mark.parametrize(("status", "code"), [(401, "gated"), (403, "gated"), (404, "not_found")])
def test_an_http_refusal_has_its_code(server, status, code):
    server.status("/api/models/o/r/revision/main?blobs=true", status)
    _refused(code, hf.repo_info, _link(), threading.Event())


def test_a_gated_repository_is_gated_before_any_file(server):
    _api(server, [], gated="manual")
    _refused("gated", hf.repo_info, _link(), threading.Event())


def test_an_answer_over_the_json_limit_is_refused(server, monkeypatch):
    monkeypatch.setattr(hf, "HF_API_MAX_BYTES", 100)
    _api(server, [_sibling(f"f{i}.yaml") for i in range(20)])
    _refused("network", hf.repo_info, _link(), threading.Event())


def test_an_answer_without_a_commit_is_refused(server):
    server.json("/api/models/o/r/revision/main?blobs=true", {"sha": "main", "siblings": []})
    _refused("network", hf.repo_info, _link(), threading.Event())


def test_no_credentials_are_ever_sent(server, monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "hf_secret")
    _api(server, [])
    hf.repo_info(_link(), threading.Event())
    assert server.headers
    for headers in server.headers:
        assert not {k.lower() for k in headers} & {"authorization", "cookie"}
        assert "hf_secret" not in json.dumps(headers)


def test_an_unreachable_host_is_network(monkeypatch):
    monkeypatch.setattr(hf, "_BASE", "http://127.0.0.1:9")
    monkeypatch.setattr(hf, "_SCHEMES", ("http",))
    monkeypatch.setattr(hf, "_PORTS", (9,))
    monkeypatch.setattr(hf, "ALLOWED_HOSTS", frozenset({"127.0.0.1"}))
    monkeypatch.setattr(hf, "MODEL_DOWNLOAD_ATTEMPTS", 2)
    monkeypatch.setattr(hf, "MODEL_DOWNLOAD_BACKOFF", (0, 0))
    _refused("network", hf.repo_info, _link(), threading.Event())


# ── which files ──


def _info(*names: str) -> hf.RepoInfo:
    return hf.RepoInfo(COMMIT, {n: hf.RemoteFile(n, 10, None) for n in names})


@pytest.mark.parametrize(
    ("names", "file", "expected"),
    [
        (("m.safetensors", "config.yaml"), None, ("m.safetensors", "config.yaml")),
        (("m.safetensors", "m.ckpt", "c.yml"), None, ("m.safetensors", "c.yml")),
        (("m.ckpt", "config.yaml"), None, ("m.ckpt", "config.yaml")),
        (("a.ckpt", "b.ckpt", "a.yaml", "b.yaml"), "b.ckpt", ("b.ckpt", "b.yaml")),
        (("m.ckpt", "x.yaml", "config.yaml"), None, ("m.ckpt", "config.yaml")),
        (("m.pth", "s/c.yaml"), None, ("m.pth", "s/c.yaml")),
    ],
)
def test_the_files_chosen(names, file, expected):
    weights, cfg = hf.choose_files(_info(*names), _link(file))
    assert (weights.path, cfg.path) == expected


@pytest.mark.parametrize(
    ("names", "file", "code"),
    [
        (("a.safetensors", "b.safetensors", "c.yaml"), None, "ambiguous"),
        (("a.ckpt", "b.pt", "c.yaml"), None, "ambiguous"),
        (("m.ckpt", "x.yaml", "y.yaml"), None, "ambiguous"),
        (("m.ckpt",), None, "config_missing"),
        (("m.onnx", "c.yaml"), None, "not_found"),
        (("c.yaml",), "m.ckpt", "not_found"),
        (("m.onnx", "c.yaml"), "m.onnx", "bad_format"),
    ],
)
def test_a_choice_that_cannot_be_made_is_refused(names, file, code):
    _refused(code, hf.choose_files, _info(*names), _link(file))


# ── one file ──


@pytest.fixture
def folder(tmp_path):
    """An empty folder of its own (tmp_path holds the suite's isolated dirs)."""
    path = tmp_path / "dl"
    path.mkdir()
    return path


def _download(server, path: str, folder, **kwargs):
    defaults = {"max_bytes": 1024, "cancel": threading.Event()}
    return hf.download(_url(server, path), folder, "model", **{**defaults, **kwargs})


def test_a_file_is_downloaded_synced_and_hashed(server, folder, monkeypatch):
    data = b"x" * 300
    server.body(_resolve("m.ckpt"), data)
    synced: list[int] = []
    real_fsync = hf.os.fsync
    monkeypatch.setattr(hf.os, "fsync", lambda fd: (synced.append(fd), real_fsync(fd)))
    seen: list[int] = []
    tmp, size, sha = _download(
        server,
        _resolve("m.ckpt"),
        folder,
        expected_size=300,
        expected_sha256=hashlib.sha256(data).hexdigest(),
        on_bytes=seen.append,
    )
    assert tmp.read_bytes() == data and size == 300 and sha == hashlib.sha256(data).hexdigest()
    assert tmp.name.startswith("model.") and tmp.name.endswith(".partial")
    assert synced, "the bytes are synced before the caller renames the file"
    assert seen[-1] == 300


def test_a_file_with_no_length_is_stopped_at_the_first_byte_over(server, folder):
    server.body(_resolve("m.ckpt"), b"x" * 2000, length=None)
    _refused("too_large", _download, server, _resolve("m.ckpt"), folder, max_bytes=1000)
    assert list(folder.iterdir()) == []


def test_a_length_that_lies_low_never_brings_more_than_it_says(server, folder):
    server.body(_resolve("m.ckpt"), b"x" * 2000, length="10")
    tmp, size, _sha = _download(server, _resolve("m.ckpt"), folder, max_bytes=1000)
    assert size == 10 and tmp.stat().st_size == 10
    # Against the size the repository listed, a length other than it is refused.
    tmp.unlink()
    _refused("bad_download", _download, server, _resolve("m.ckpt"), folder, expected_size=500)
    assert list(folder.iterdir()) == []


def test_a_declared_length_over_the_cap_is_refused_before_reading(server, folder):
    server.body(_resolve("m.ckpt"), b"x" * 2000)
    _refused("too_large", _download, server, _resolve("m.ckpt"), folder, max_bytes=1000)
    _refused("too_large", _download, server, _resolve("m.ckpt"), folder, expected_size=5000)


def test_a_wrong_sha_is_bad_download_and_leaves_nothing(server, folder):
    server.body(_resolve("m.ckpt"), b"x" * 300)
    _refused(
        "bad_download",
        _download,
        server,
        _resolve("m.ckpt"),
        folder,
        expected_size=300,
        expected_sha256="0" * 64,
    )
    assert list(folder.iterdir()) == []


def test_a_length_other_than_the_listed_size_is_bad_download(server, folder):
    server.body(_resolve("m.ckpt"), b"x" * 300)
    _refused("bad_download", _download, server, _resolve("m.ckpt"), folder, expected_size=299)


def test_a_redirect_to_an_allowed_host_is_followed(server, folder):
    server.body("/cdn/m.ckpt", b"ok")
    server.redirect(_resolve("m.ckpt"), _url(server, "/cdn/m.ckpt"))
    tmp, size, _sha = _download(server, _resolve("m.ckpt"), folder)
    assert tmp.read_bytes() == b"ok" and size == 2


@pytest.mark.parametrize(
    "location",
    [
        "http://localhost:{port}/cdn/m.ckpt",  # a host that is not allowed
        "https://127.0.0.1:{port}/cdn/m.ckpt",  # a scheme that is not allowed here
        "http://127.0.0.1:1/cdn/m.ckpt",  # a port that is not allowed
        "http://user@127.0.0.1:{port}/cdn/m.ckpt",
    ],
)
def test_a_redirect_elsewhere_is_refused(server, folder, location):
    server.body("/cdn/m.ckpt", b"ok")
    server.redirect(_resolve("m.ckpt"), location.format(port=server.port))
    _refused("network", _download, server, _resolve("m.ckpt"), folder)
    assert list(folder.iterdir()) == []
    assert len(server.headers) == 1  # the target was never asked


def test_a_fourth_redirect_is_refused(server, folder):
    server.body("/hop/4", b"ok")
    server.redirect(_resolve("m.ckpt"), _url(server, "/hop/1"))
    for i in range(1, 4):
        server.redirect(f"/hop/{i}", _url(server, f"/hop/{i + 1}"))
    _refused("network", _download, server, _resolve("m.ckpt"), folder)
    server.redirect(_resolve("m.ckpt"), _url(server, "/hop/2"))
    tmp, _size, _sha = _download(server, _resolve("m.ckpt"), folder)  # three hops are fine
    assert tmp.read_bytes() == b"ok"


def test_cancel_mid_stream_leaves_nothing(server, folder):
    server.body(_resolve("m.ckpt"), b"x" * (3 * 1024 * 1024))
    cancel = threading.Event()
    with pytest.raises(hf.Cancelled):
        _download(
            server,
            _resolve("m.ckpt"),
            folder,
            max_bytes=10 * 1024 * 1024,
            cancel=cancel,
            on_bytes=lambda n: cancel.set(),
        )
    assert list(folder.iterdir()) == []


def test_a_disk_that_cannot_be_written_is_disk_full_and_not_tried_again(
    server, folder, monkeypatch
):
    server.body(_resolve("m.ckpt"), b"x" * 300)

    def full(fd):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(hf.os, "fsync", full)
    _refused("disk_full", _download, server, _resolve("m.ckpt"), folder)
    assert len(server.headers) == 1
    assert list(folder.iterdir()) == []


def test_a_server_error_is_tried_again_then_network(server, folder):
    server.status(_resolve("m.ckpt"), 503)
    _refused("network", _download, server, _resolve("m.ckpt"), folder)
    assert len(server.headers) == 2  # MODEL_DOWNLOAD_ATTEMPTS in this fixture


def test_an_empty_file_is_refused(server, folder):
    server.body(_resolve("m.ckpt"), b"")
    _refused("empty", _download, server, _resolve("m.ckpt"), folder)
