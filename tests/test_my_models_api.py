"""My models over /api/my-models (app/api/my_models.py).

The Hugging Face side is a stand-in for hf_fetch._open (no network), and the
test run is a stand-in for user_model_check.run_check (its real worker has
tests/test_user_model_check.py), so these tests are about the routes: their
validation order, the work they start, what they write and where, and that
no answer carries a path, an exception or the link that was sent."""

from __future__ import annotations

import hashlib
import io
import json
import struct
import threading
import time
from urllib.error import HTTPError

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

import app.main as main
from app.api import my_models
from app.core import hf_fetch as hf
from app.core import settings as _settings
from app.core import user_model_check as checker
from app.core import user_models as um
from app.core.config import MAX_USER_MODELS
from app.core.models import Job
from app.core.registry import _jobs
from app.main import app
from app.pipeline import jobqueue

COMMIT = "c" * 40
CONFIG = {
    "audio": {"chunk_size": 44_100, "num_channels": 2, "sample_rate": 44_100},
    "model": {
        "dim": 16,
        "depth": 1,
        "stereo": True,
        "num_stems": 2,
        "num_bands": 8,
        "stft_n_fft": 256,
        "stft_win_length": 256,
        "stft_hop_length": 64,
        "heads": 1,
        "dim_head": 8,
        "time_transformer_depth": 1,
        "freq_transformer_depth": 1,
    },
    "training": {"instruments": ["vocals", "drums"]},
}
CONFIG_BYTES = yaml.safe_dump(CONFIG).encode()
RESULT = {"params": 4321, "cpu_rtf": 0.2, "torch": "2.6.0", "separator": "0.44.5", "keys": 9}


def _weights(seed: bytes = b"a") -> bytes:
    """A small, valid safetensors file (the format sniff reads it for real)."""
    raw = (seed * 16)[:16]
    header = json.dumps({"w": {"dtype": "F32", "shape": [4], "data_offsets": [0, 16]}}).encode()
    return struct.pack("<Q", len(header)) + header + raw


@pytest.fixture(autouse=True)
def _state(monkeypatch):
    monkeypatch.setattr(um, "requirements_met", lambda: True)
    monkeypatch.setattr(hf, "MODEL_DOWNLOAD_BACKOFF", (0, 0))
    my_models.reset_state()
    _jobs.clear()
    yield
    my_models.cancel_all()
    deadline = time.monotonic() + 10
    while my_models._work and time.monotonic() < deadline:
        time.sleep(0.02)
    my_models.reset_state()
    _jobs.clear()


@pytest.fixture
def check(monkeypatch):
    """The test run: passes with RESULT, or answers ``code``; with ``gate``
    set it waits for it (or for cancel)."""
    state: dict = {"gate": None, "code": None, "calls": []}

    def fake(folder, stems, *, on_step, on_pct, cancel, register, **_kw):
        state["calls"].append((folder, list(stems)))
        on_step("run")
        on_pct(50)
        gate = state["gate"]
        while gate is not None and not gate.is_set():
            if cancel.is_set():
                return checker.CheckRun(None, "interrupted", [])
            time.sleep(0.02)
        if state["code"]:
            return checker.CheckRun(None, state["code"], ["Traceback: secret /path"])
        return checker.CheckRun(dict(RESULT), None, [])

    monkeypatch.setattr(checker, "run_check", fake)
    return state


class _Response:
    def __init__(self, body: bytes, *, delay: float = 0.0) -> None:
        self._body = io.BytesIO(body)
        self.headers = {"Content-Length": str(len(body))}
        self._delay = delay

    def read(self, n: int = -1) -> bytes:
        if self._delay:
            time.sleep(self._delay)
            n = min(n, 1024) if n and n > 0 else 1024
        return self._body.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def hub(monkeypatch):
    """A stand-in Hugging Face: {path: bytes | int status}, and every URL asked."""
    routes: dict[str, object] = {}
    asked: list[str] = []
    slow: set[str] = set()

    def fake_open(url: str):
        assert hf.target_ok(url), url
        asked.append(url)
        path = url.removeprefix("https://huggingface.co")
        answer = routes.get(path, 404)
        if isinstance(answer, int):
            raise HTTPError(url, answer, "refused", {}, None)
        return _Response(answer, delay=0.01 if path in slow else 0.0)  # type: ignore[arg-type]

    monkeypatch.setattr(hf, "_open", fake_open)

    def repo(files: dict[str, bytes], **extra) -> None:
        siblings = []
        for name, data in files.items():
            entry = {"rfilename": name, "size": len(data)}
            if name.endswith((".ckpt", ".safetensors")):
                entry["lfs"] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
            siblings.append(entry)
            routes[f"/o/r/resolve/{COMMIT}/{name}"] = data
        routes["/api/models/o/r/revision/main?blobs=true"] = json.dumps(
            {"sha": COMMIT, "siblings": siblings, **extra}
        ).encode()

    return {"routes": routes, "asked": asked, "repo": repo, "slow": slow}


@pytest.fixture
def client(check):
    with TestClient(app) as c:
        yield c


def _wait(c, model_id: str, *, until=("ready", "refused", "missing"), timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        entry = next(
            (m for m in c.get("/api/my-models").json()["models"] if m["id"] == model_id), None
        )
        settled = entry is None or entry["status"] in until
        if settled and not my_models._work.get(model_id):
            return entry
        time.sleep(0.02)
    raise AssertionError(f"{model_id} did not settle")


def _link_body(**changes) -> dict:
    return {
        "link": "https://huggingface.co/o/r",
        "stems": ["vocals"],
        "licence_confirmed": True,
        **changes,
    }


def _upload(
    c,
    *,
    model=None,
    config=CONFIG_BYTES,
    stems='["drums", "vocals"]',
    licence="true",
    model_name="mine.safetensors",
    config_name="config.yaml",
    name=None,
    headers=None,
):
    files = {}
    if model is not False:
        files["model"] = (
            model_name,
            _weights() if model is None else model,
            "application/octet-stream",
        )
    if config is not False:
        files["config"] = (config_name, config, "application/x-yaml")
    data = {"stems": stems, "licence": licence}
    if name is not None:
        data["name"] = name
    return c.post("/api/my-models/upload", files=files, data=data, headers=headers or {})


def _assert_clean(response, *secrets: str) -> None:
    text = response.text
    for secret in (str(um.root()), "Traceback", "Error", *secrets):
        assert secret not in text, secret


# ── the list ──


def test_the_list_starts_empty_with_its_limits(client):
    body = client.get("/api/my-models").json()
    assert body["models"] == [] and body["registry_reset"] is False and body["available"] is True
    assert body["limits"]["max_models"] == MAX_USER_MODELS
    assert body["limits"]["max_bytes"] == 2 * 1024**3


# ── by link ──


def test_a_link_downloads_checks_and_is_ready(client, hub, online_sources_on, check):
    weights = _weights()
    hub["repo"](
        {"model.safetensors": weights, "config.yaml": CONFIG_BYTES}, cardData={"license": "mit"}
    )
    r = client.post("/api/my-models/link", json=_link_body(stems=["drums", "vocals"], name="Mine"))
    assert r.status_code == 202
    first = r.json()
    assert first["status"] == "downloading" and first["name"] == "Mine"
    entry = _wait(client, first["id"])
    assert entry["status"] == "ready" and entry["usable"] is True
    assert entry["stems"] == ["vocals", "drums"]  # SELECTABLE_STEMS order
    assert entry["sha256"] == hashlib.sha256(weights).hexdigest()
    assert entry["source"] == {
        "kind": "hf",
        "label": f"o/r@{COMMIT[:12]}",
        "license_declared": "mit",
        # The link it was added from, for Settings' "Source: added by you".
        "link": "https://huggingface.co/o/r",
    }
    assert entry["params"] == RESULT["params"] and entry["cpu_rtf"] == RESULT["cpu_rtf"]
    folder = um.model_dir(first["id"])
    assert sorted(p.name for p in folder.iterdir()) == ["config.yaml", "model.safetensors"]
    # Every request was built from the parts, pinned to the commit.
    assert all(COMMIT in u for u in hub["asked"][1:])
    rec = um.record(first["id"])
    assert rec["outputs"] == {"vocals": 0, "drums": 1} and rec["arch"] == "mel_band_roformer"


def test_a_link_is_refused_while_online_sources_are_off(client, hub):
    r = client.post("/api/my-models/link", json=_link_body())
    assert r.status_code == 403 and r.json()["code"] == "online_sources_off"
    assert hub["asked"] == []


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (_link_body(stems=["flute"]), "stem_not_in_model"),
        (_link_body(stems=[]), "stem_not_in_model"),
        (_link_body(stems=["vocals", "vocals"]), "stem_not_in_model"),
        (_link_body(licence_confirmed=False), "licence"),
        (_link_body(licence_confirmed="true"), "licence"),
        ({"link": "https://huggingface.co/o/r", "stems": ["vocals"]}, "licence"),
        (_link_body(link="https://evil.example/o/r?token=SECRET"), "bad_link"),
        (_link_body(link="http://huggingface.co/o/r"), "bad_link"),
        (_link_body(extra=1), "bad_link"),
        ([1, 2], "bad_link"),
    ],
)
def test_a_bad_link_body_is_422_and_echoes_nothing(client, hub, online_sources_on, body, code):
    r = client.post("/api/my-models/link", json=body)
    assert r.status_code == 422 and r.json()["code"] == code
    _assert_clean(r, "SECRET", "evil", "flute")
    assert um.count() == 0 and hub["asked"] == []


def test_a_body_that_is_not_json_is_422(client, online_sources_on):
    r = client.post(
        "/api/my-models/link", content=b"{", headers={"Content-Type": "application/json"}
    )
    assert r.status_code == 422


@pytest.mark.parametrize(
    ("files", "routes", "code"),
    [
        (None, {"/api/models/o/r/revision/main?blobs=true": 404}, "not_found"),
        (None, {"/api/models/o/r/revision/main?blobs=true": 401}, "gated"),
        (
            {
                "a.safetensors": _weights(b"a"),
                "b.safetensors": _weights(b"b"),
                "c.yaml": CONFIG_BYTES,
            },
            {},
            "ambiguous",
        ),
        ({"model.safetensors": _weights()}, {}, "config_missing"),
        (
            {"model.safetensors": _weights(), "config.yaml": b"model: {dim: 16}\n"},
            {},
            "unsupported_arch",
        ),
        (
            {"model.safetensors": b"<html>Sign in</html>", "config.yaml": CONFIG_BYTES},
            {},
            "bad_format",
        ),
    ],
)
def test_a_link_that_fails_is_refused_with_its_code(
    client, hub, online_sources_on, files, routes, code
):
    if files is not None:
        hub["repo"](files)
    hub["routes"].update(routes)
    r = client.post("/api/my-models/link", json=_link_body())
    entry = _wait(client, r.json()["id"])
    assert entry["status"] == "refused" and entry["code"] == code
    # Nothing worth keeping stays on disk.
    assert list(um.model_dir(entry["id"]).iterdir()) == []


def test_a_declared_stem_the_config_lacks_is_refused_before_the_big_download(
    client, hub, online_sources_on
):
    hub["repo"]({"model.safetensors": _weights(), "config.yaml": CONFIG_BYTES})
    r = client.post("/api/my-models/link", json=_link_body(stems=["bass"]))
    entry = _wait(client, r.json()["id"])
    assert entry["code"] == "stem_not_in_model"
    assert not any(u.endswith("model.safetensors") for u in hub["asked"])


def test_the_same_file_twice_is_a_duplicate_before_it_downloads(client, hub, online_sources_on):
    hub["repo"]({"model.safetensors": _weights(), "config.yaml": CONFIG_BYTES})
    first = _wait(client, client.post("/api/my-models/link", json=_link_body()).json()["id"])
    assert first["status"] == "ready"
    hub["asked"].clear()
    second = _wait(client, client.post("/api/my-models/link", json=_link_body()).json()["id"])
    assert second["code"] == "duplicate"
    assert not any(u.endswith("model.safetensors") for u in hub["asked"])


def test_cancel_during_a_download_removes_the_entry(client, hub, online_sources_on):
    hub["repo"]({"model.safetensors": _weights(), "config.yaml": CONFIG_BYTES})
    hub["slow"].add(f"/o/r/resolve/{COMMIT}/model.safetensors")
    hub["slow"].add("/api/models/o/r/revision/main?blobs=true")
    model_id = client.post("/api/my-models/link", json=_link_body()).json()["id"]
    r = client.post(f"/api/my-models/{model_id}/cancel")
    assert r.status_code == 200 and r.json() == {"id": model_id, "model": None}
    assert um.record(model_id) is None
    assert not (um.root() / model_id).exists()


# ── by upload ──


def test_an_upload_is_stored_under_fixed_names_and_checked(client, check):
    r = _upload(client, model_name="../../evil name.safetensors")
    assert r.status_code == 202
    model_id = r.json()["id"]
    entry = _wait(client, model_id)
    assert entry["status"] == "ready" and entry["name"] == "evil name"
    assert entry["source"]["kind"] == "upload"
    # A model from files has no link to show.
    assert entry["source"]["link"] is None
    folder = um.model_dir(model_id)
    assert sorted(p.name for p in folder.iterdir()) == ["config.yaml", "model.safetensors"]
    assert (folder / "config.yaml").read_bytes() == CONFIG_BYTES
    # Nothing was written anywhere but the model's own folder.
    written = [p for p in um.root().rglob("*") if p.is_file()]
    assert all(p.parent == folder or p.name == "models.json" for p in written)
    assert check["calls"][0][1] == ["vocals", "drums"]


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"config": False}, "config_missing"),
        ({"model": False}, "empty"),
        ({"model": b""}, "empty"),
        ({"model_name": "m.onnx"}, "bad_format"),
        ({"config_name": "config.json"}, "bad_config"),
        ({"stems": "vocals"}, "stem_not_in_model"),
        ({"stems": '["flute"]'}, "stem_not_in_model"),
        ({"licence": "false"}, "licence"),
        ({"config": b"model: !!python/object/apply:os.system [echo]\n"}, "bad_config"),
        ({"stems": '["bass"]'}, "stem_not_in_model"),
        ({"model": b"<!doctype html>" + b" " * 40}, "bad_format"),
        ({"config": b"a: 1\n" * 60_000}, "too_large"),
    ],
)
def test_a_bad_upload_is_422_and_leaves_nothing(client, kwargs, code):
    r = _upload(client, **kwargs)
    assert r.status_code == 422, r.text
    assert r.json()["code"] == code
    _assert_clean(r, "os.system", "flute")
    assert um.count() == 0
    assert [p for p in um.root().rglob("*") if p.is_file() and p.name != "models.json"] == []


def test_an_upload_over_the_size_limit_is_refused_from_its_length(client):
    r = client.post(
        "/api/my-models/upload",
        content=b"x",
        headers={
            "Content-Length": str(3 * 1024**3),
            "Content-Type": "multipart/form-data; boundary=x",
        },
    )
    assert r.status_code == 422 and r.json()["code"] == "too_large"


def test_a_second_copy_of_a_file_is_a_duplicate(client):
    _wait(client, _upload(client).json()["id"])
    r = _upload(client)
    assert r.status_code == 409 and r.json()["code"] == "duplicate"
    assert um.count() == 1


def test_one_add_at_a_time(client, check):
    check["gate"] = threading.Event()
    first = _upload(client).json()["id"]
    r = _upload(client, model=_weights(b"z"))
    assert r.status_code == 409 and r.json()["code"] == "busy"
    r = client.post(f"/api/my-models/{first}/check")
    assert r.status_code == 409 and r.json()["code"] == "busy"
    check["gate"].set()
    assert _wait(client, first)["status"] == "ready"


def test_a_full_list_is_409_limit(client):
    for _ in range(MAX_USER_MODELS):
        model_id = um.new_entry(
            name="x",
            stems=["vocals"],
            source={"kind": "upload"},
            licence_confirmed_at=0,
            status="staging",
        )
        um.set_status(model_id, "refused", code="timeout")
    r = _upload(client)
    assert r.status_code == 409 and r.json()["code"] == "limit"


def test_cancel_during_a_check_keeps_the_files(client, check):
    check["gate"] = threading.Event()
    model_id = _upload(client).json()["id"]
    r = client.post(f"/api/my-models/{model_id}/cancel")
    assert r.status_code == 200
    assert r.json()["model"]["status"] == "refused" and r.json()["model"]["code"] == "interrupted"
    assert (um.model_dir(model_id) / "model.safetensors").is_file()


def test_a_failed_check_keeps_the_files_and_hides_the_log(client, check):
    check["code"] = "key_mismatch"
    model_id = _upload(client).json()["id"]
    entry = _wait(client, model_id)
    assert entry["status"] == "refused" and entry["code"] == "key_mismatch"
    assert (um.model_dir(model_id) / "model.safetensors").is_file()
    _assert_clean(client.get("/api/my-models"), "/path")


# ── check again ──


def test_check_again_passes_a_model_that_is_still_its_file(client, check):
    model_id = _upload(client).json()["id"]
    _wait(client, model_id)
    r = client.post(f"/api/my-models/{model_id}/check")
    assert r.status_code == 202 and r.json()["status"] == "checking"
    assert _wait(client, model_id)["status"] == "ready"
    assert len(check["calls"]) == 2


def test_check_again_finds_a_changed_file(client, check):
    model_id = _upload(client).json()["id"]
    _wait(client, model_id)
    path = um.model_dir(model_id) / "model.safetensors"
    path.write_bytes(_weights(b"q"))  # the same size, other bytes
    client.post(f"/api/my-models/{model_id}/check")
    entry = _wait(client, model_id)
    assert entry["status"] == "refused" and entry["code"] == "changed"
    assert len(check["calls"]) == 1


def test_check_again_fetches_a_failed_link_again(client, hub, online_sources_on):
    model_id = client.post("/api/my-models/link", json=_link_body()).json()["id"]
    assert _wait(client, model_id)["code"] == "not_found"
    hub["repo"]({"model.safetensors": _weights(), "config.yaml": CONFIG_BYTES})
    assert client.post(f"/api/my-models/{model_id}/check").status_code == 202
    assert _wait(client, model_id)["status"] == "ready"


# ── one model's routes: 404 before anything ──


@pytest.mark.parametrize(
    "model_id",
    ["user_000000000000", "user_ABCDEF012345", "user_0123456789abc", "user_..", "0123456789ab"],
)
@pytest.mark.parametrize(
    ("method", "suffix"), [("post", "/check"), ("post", "/cancel"), ("delete", "")]
)
def test_an_unknown_or_malformed_id_is_404(client, model_id, method, suffix):
    r = getattr(client, method)(f"/api/my-models/{model_id}{suffix}")
    assert r.status_code == 404


def test_a_traversal_id_never_reaches_the_disk(client, tmp_path):
    outside = tmp_path / "keep.txt"
    outside.write_text("x")
    for path in ("/api/my-models/..%2F..%2Fkeep.txt", "/api/my-models/%2e%2e"):
        assert client.delete(path).status_code in (404, 405)
    assert outside.exists()


# ── remove ──


def test_remove_takes_the_files_and_scrubs_the_setting(client):
    model_id = _upload(client).json()["id"]
    _wait(client, model_id)
    _settings.set_stem_model_choices({"vocals": model_id})
    assert _settings.get_stem_model_choices()["vocals"] == model_id
    r = client.delete(f"/api/my-models/{model_id}")
    assert r.status_code == 200 and r.json() == {"removed": model_id}
    assert not (um.root() / model_id).exists() and um.record(model_id) is None
    assert _settings.get_stem_model_choices()["vocals"] == "auto"


def test_remove_refuses_a_model_a_running_separation_uses(client):
    model_id = _upload(client).json()["id"]
    _wait(client, model_id)
    _jobs["a1a1a1a1a1a1"] = Job(id="a1a1a1a1a1a1", status="separating", title="Song")
    _jobs["a1a1a1a1a1a1"].stem_models = {"vocals": model_id}
    jobqueue._running_id = "a1a1a1a1a1a1"
    r = client.delete(f"/api/my-models/{model_id}")
    assert r.status_code == 409 and r.json()["code"] == "in_use"
    assert um.record(model_id) is not None


def test_remove_keeps_the_entry_when_files_will_not_go(client, monkeypatch):
    model_id = _upload(client).json()["id"]
    _wait(client, model_id)
    monkeypatch.setattr(um, "_remove_tree", lambda folder: False)
    r = client.delete(f"/api/my-models/{model_id}")
    assert r.status_code == 409 and r.json()["code"] == "busy"
    assert um.record(model_id)["code"] == "busy"


def test_remove_stops_a_check_first(client, check):
    check["gate"] = threading.Event()
    model_id = _upload(client).json()["id"]
    r = client.delete(f"/api/my-models/{model_id}")
    assert r.status_code == 200
    assert um.record(model_id) is None


# ── the gates every write route has ──


EVIL = "https://evil.example"


def _async_client(peer=("127.0.0.1", 51234)) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=main.app, client=peer)
    return httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000")


async def test_a_cross_site_upload_is_refused(check, monkeypatch):
    monkeypatch.setattr("app.main.get_allow_network", lambda: False)
    async with _async_client() as c:
        r = await c.post(
            "/api/my-models/upload",
            files={"model": ("m.safetensors", _weights(), "application/octet-stream")},
            headers={"Origin": EVIL},
        )
    assert r.status_code == 403
    assert um.count() == 0


async def test_a_multipart_upload_passes_the_content_type_guard(check, monkeypatch):
    """Multipart is allowed on the upload route only (csrf._RAW_BODY_ROUTES):
    the route itself answers, here that the config is missing."""
    monkeypatch.setattr("app.main.get_allow_network", lambda: False)
    files = {"model": ("m.safetensors", _weights(), "application/octet-stream")}
    async with _async_client() as c:
        r = await c.post(
            "/api/my-models/upload",
            files=files,
            data={"stems": '["vocals"]', "licence": "true"},
            headers={"Origin": "http://127.0.0.1:8000"},
        )
        assert r.status_code == 422 and r.json()["code"] == "config_missing"
        for path in ("/api/my-models/link", "/api/my-models/user_0123456789ab/check"):
            r = await c.post(path, files=files, headers={"Origin": "http://127.0.0.1:8000"})
            assert r.status_code == 415, path


def test_the_lan_gate_covers_these_routes(check):
    _settings.set_allow_network(False)
    with TestClient(app) as c:  # its peer is not loopback
        assert c.get("/api/my-models").status_code == 403
        assert _upload(c).status_code == 403
    assert um.count() == 0


def test_a_link_read_back_from_the_registry_is_checked_again():
    """The Source line's link comes from the registry file, which a person
    can edit: only an https huggingface.co address of plain characters is
    served, anything else is no link."""
    link = um._public_link
    assert (
        link({"kind": "hf", "link": "https://huggingface.co/o/r"}) == "https://huggingface.co/o/r"
    )
    assert link({"kind": "upload", "link": "https://huggingface.co/o/r"}) is None
    for bad in (
        "http://huggingface.co/o/r",
        "https://huggingface.co.evil.example/o/r",
        "javascript:alert(1)",
        'https://huggingface.co/o/r" onclick="x',
        "https://huggingface.co/o/r with space",
        "https://huggingface.co/" + "a" * 500,
        None,
        42,
    ):
        assert link({"kind": "hf", "link": bad}) is None, bad
