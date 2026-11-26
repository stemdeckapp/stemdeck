"""GET /api/models: Settings, Models' list of the built-in models.

What each model is for, whether its files are on disk and how much they hold,
read from the folders the downloading code uses. Read-only, and never a path in
the answer. Every folder here is a tmp_path: torch.hub's checkpoints folder is
replaced through stem_models.hub_checkpoints_dir (which model_path and the
beat model's path both go through), MODELS_DIR through the inventory module's
own binding, and Whisper's folder through TORCH_HOME.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.core import model_cache, model_inventory, stem_models
from app.main import app

SIX = stem_models.MODELS["htdemucs_6s"]
FT = stem_models.MODELS["htdemucs_ft"]
KIM = stem_models.MODELS["kim_mel_roformer"]


@pytest.fixture
def folders(tmp_path, monkeypatch):
    """Empty, not yet created folders for every model; returns them."""
    hub = tmp_path / "torch" / "hub" / "checkpoints"
    models = tmp_path / "models"
    torch_home = tmp_path / "torch"
    monkeypatch.setattr(stem_models, "hub_checkpoints_dir", lambda: hub)
    monkeypatch.setattr(stem_models, "MODELS_DIR", models)
    monkeypatch.setattr(model_inventory, "MODELS_DIR", models)
    monkeypatch.setattr(
        model_cache, "beat_this_artifacts", lambda name: [hub / f"beat_this-{name}.ckpt"]
    )
    monkeypatch.setenv("TORCH_HOME", str(torch_home))
    monkeypatch.setattr(stem_models, "model_available", lambda model_id: True)
    return {"hub": hub, "models": models, "whisper": torch_home / "whisper", "root": tmp_path}


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _by_id(body: dict) -> dict[str, dict]:
    return {m["id"]: m for m in body["models"]}


def _write(path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.truncate(size)


def test_lists_every_built_in_model_by_purpose(client, folders):
    r = client.get("/api/models")
    assert r.status_code == 200
    body = r.json()
    assert body["remove"] is False
    models = _by_id(body)
    for model in stem_models.MODELS.values():
        entry = models[model.id]
        assert entry["group"] == "separation"
        assert entry["use"] == "stems"
        assert entry["name"] == model.name
        assert set(entry["stems"]) == set(model.stems)
        assert entry["license"] == model.license
        assert entry["download_bytes"] == model.download_bytes
    assert models["uvr_karaoke_2"]["use"] == "lead_backing"
    assert models["uvr_karaoke_2"]["stems"] == ["lead_backing"]
    assert models["uvr_karaoke_2"]["license"] == "MIT"
    bp = models["basic_pitch"]
    assert (bp["group"], bp["use"], bp["state"], bp["license"]) == (
        "transcription",
        "midi",
        "built_in",
        "Apache-2.0",
    )
    # Every stem it can transcribe, in Settings' order: not the drums.
    assert bp["stems"] == ["vocals", "bass", "guitar", "piano", "other"]
    assert models["beat_this"]["group"] == "analysis"
    assert models["beat_this"]["use"] == "beats"
    lyrics = [m for m in body["models"] if m["use"].startswith("lyrics")]
    assert lyrics and all(m["group"] == "analysis" for m in lyrics)
    assert {m["group"] for m in body["models"]} == {"separation", "transcription", "analysis"}
    assert all(m["state"] in model_inventory.STATES for m in body["models"])


def test_missing_folders_read_as_not_downloaded(client, folders):
    """Nothing downloaded yet, and not one of the folders exists: every
    downloadable model says so, with no size, and the route still answers."""
    assert not folders["hub"].exists() and not folders["models"].exists()
    models = _by_id(client.get("/api/models").json())
    for model_id, entry in models.items():
        if model_id == "basic_pitch":
            continue
        assert entry["state"] == "not_downloaded", model_id
        assert entry["size_bytes"] is None, model_id


def test_sizes_on_disk_and_partial_downloads(client, folders):
    hub = folders["hub"]
    for f in SIX.files:
        _write(hub / f.relpath, f.size)
    # Two of the fine-tuned bag's four checkpoints, one at the wrong size.
    _write(hub / FT.files[0].relpath, FT.files[0].size)
    _write(hub / FT.files[1].relpath, 1000)
    _write(folders["models"] / "audio-separator" / "UVR_MDXNET_KARA_2.onnx", 5000)
    _write(hub / "beat_this-final0.ckpt", 7000)
    _write(folders["whisper"] / "small.pt", 9000)
    _write(folders["whisper"] / "large-v3-turbo.pt", 11000)

    models = _by_id(client.get("/api/models").json())
    assert models["htdemucs_6s"]["state"] == "downloaded"
    assert models["htdemucs_6s"]["size_bytes"] == SIX.download_bytes
    assert models["htdemucs_ft"]["state"] == "partial"
    assert models["htdemucs_ft"]["size_bytes"] == FT.files[0].size + 1000
    assert models["kim_mel_roformer"]["state"] == "not_downloaded"
    assert models["uvr_karaoke_2"]["state"] == "downloaded"
    assert models["uvr_karaoke_2"]["size_bytes"] == 5000
    assert models["beat_this"]["state"] == "downloaded"
    assert models["beat_this"]["size_bytes"] == 7000
    assert models["whisper_small"]["size_bytes"] == 9000
    assert models["whisper_turbo"]["state"] == "downloaded"
    assert models["whisper_turbo"]["size_bytes"] == 11000


def test_a_pinned_file_of_the_wrong_size_is_not_downloaded(client, folders):
    """Kim's one checkpoint at the wrong size: torch would never load it, so
    it is partial, never downloaded."""
    _write(folders["models"] / "roformer" / "MelBandRoformer.ckpt", 123)
    kim = _by_id(client.get("/api/models").json())["kim_mel_roformer"]
    assert kim["state"] == "partial"
    assert kim["size_bytes"] == 123


def test_a_folder_that_cannot_be_worked_out_is_unknown(client, folders, monkeypatch):
    """torch does not load (#730): torch.hub's folder is unknown, and so are
    the models in it. The ones elsewhere still answer."""

    def broken():
        raise OSError("torch failed to load")

    monkeypatch.setattr(stem_models, "hub_checkpoints_dir", broken)
    monkeypatch.setattr(model_cache, "beat_this_artifacts", lambda name: broken())
    r = client.get("/api/models")
    assert r.status_code == 200
    models = _by_id(r.json())
    assert models["htdemucs_6s"]["state"] == "unknown"
    assert models["htdemucs_6s"]["size_bytes"] is None
    assert models["beat_this"]["state"] == "unknown"
    assert models["kim_mel_roformer"]["state"] == "not_downloaded"


def test_no_path_in_the_answer(client, folders):
    """Sizes and states only: not the folders, not a file name, not the user's
    home, whatever is on disk."""
    for f in SIX.files:
        _write(folders["hub"] / f.relpath, f.size)
    _write(folders["whisper"] / "small.pt", 10)
    text = client.get("/api/models").text
    body = json.loads(text)
    for needle in (str(folders["root"]), folders["root"].as_posix(), "checkpoints", ".th", ".pt"):
        assert needle not in text, needle
    allowed = {
        "id",
        "group",
        "use",
        "name",
        "stems",
        "license",
        "source",
        "state",
        "size_bytes",
        "download_bytes",
        "available",
    }
    for entry in body["models"]:
        assert set(entry) == allowed


def test_a_karaoke_model_swapped_in_is_named_by_its_file_with_no_licence(
    client, folders, monkeypatch
):
    monkeypatch.setattr(model_inventory, "VOCAL_SPLIT_MODEL", "other_karaoke.ckpt")
    entry = _by_id(client.get("/api/models").json())["karaoke"]
    assert entry["name"] == "other_karaoke.ckpt"
    assert entry["license"] is None
    assert entry["download_bytes"] is None
    # The code does not know who made it: "Source: not verified", no link.
    assert entry["source"] is None


def test_whisper_file_names():
    assert model_inventory.whisper_file("turbo") == "large-v3-turbo.pt"
    assert model_inventory.whisper_file("small") == "small.pt"
    assert model_inventory.whisper_file("medium") == "medium.pt"


def test_the_route_is_read_only(client, folders):
    assert client.post("/api/models").status_code == 405
    assert client.delete("/api/models").status_code == 405


def test_the_second_piano_engine_is_listed_with_its_download(client, folders):
    """ByteDance Piano Transcription: its weights' licence, what a full
    download is, and its state from the checkpoint the run downloads
    (piano_hr.checkpoint_path, a tmp_path here: conftest's _isolate_piano_hr)."""
    from app.pipeline import piano_hr

    entry = _by_id(client.get("/api/models").json())["piano_hr"]
    assert (entry["group"], entry["use"], entry["stems"]) == (
        "transcription",
        "midi_piano",
        ["piano"],
    )
    assert (entry["license"], entry["download_bytes"]) == ("CC BY 4.0", 171_966_578)
    assert (entry["state"], entry["size_bytes"]) == ("not_downloaded", None)
    _write(piano_hr.checkpoint_path(), 1000)
    entry = _by_id(client.get("/api/models").json())["piano_hr"]
    assert (entry["state"], entry["size_bytes"]) == ("partial", 1000)
    _write(piano_hr.checkpoint_path(), 171_966_578)
    entry = _by_id(client.get("/api/models").json())["piano_hr"]
    assert (entry["state"], entry["size_bytes"]) == ("downloaded", 171_966_578)


def test_yourmt3_is_listed_with_its_download_and_what_it_keeps(client, folders):
    """YourMT3+: its weights' licence, the download its first run makes (the
    published checkpoint), and its state from the weights-only file it keeps
    (ymt3.weights_path, under a temporary MODELS_DIR: conftest's
    _isolate_ymt3). A checkpoint waiting for its conversion is "partial"."""
    from app.pipeline import ymt3

    entry = _by_id(client.get("/api/models").json())["ymt3"]
    assert (entry["group"], entry["use"], entry["stems"]) == (
        "transcription",
        "midi_multi",
        ["drums", "bass", "guitar", "piano", "other"],
    )
    assert (entry["license"], entry["download_bytes"]) == ("Apache-2.0", 561_544_628)
    assert (entry["state"], entry["size_bytes"]) == ("not_downloaded", None)
    _write(ymt3.checkpoint_path(), 1000)
    assert _by_id(client.get("/api/models").json())["ymt3"]["state"] == "partial"
    ymt3.checkpoint_path().unlink()
    _write(ymt3.weights_path(), 188_412_704)
    entry = _by_id(client.get("/api/models").json())["ymt3"]
    assert (entry["state"], entry["size_bytes"]) == ("downloaded", 188_412_704)


# The original author's own page for each model, read on that page on
# 2026-11-26 (docs/models.md, "Where each model comes from"). Never a mirror
# or a re-upload: a change here is a change of fact, checked again first.
SOURCES = {
    "htdemucs_6s": (
        "Meta AI (Rouard, Massa, Défossez)",
        "https://github.com/facebookresearch/demucs",
    ),
    "htdemucs_ft": (
        "Meta AI (Rouard, Massa, Défossez)",
        "https://github.com/facebookresearch/demucs",
    ),
    "kim_mel_roformer": ("Kimberley Jensen", "https://huggingface.co/KimberleyJSN/melbandroformer"),
    "uvr_karaoke_2": (
        "Ultimate Vocal Remover (Anjok07, aufr33)",
        "https://github.com/Anjok07/ultimatevocalremovergui",
    ),
    "basic_pitch": ("Spotify", "https://github.com/spotify/basic-pitch"),
    "piano_hr": ("Qiuqiang Kong (ByteDance)", "https://zenodo.org/records/4034264"),
    "ymt3": ("Sungkyun Chang", "https://huggingface.co/mimbres/YourMT3"),
    "whisper_turbo": ("OpenAI", "https://github.com/openai/whisper"),
    "whisper_small": ("OpenAI", "https://github.com/openai/whisper"),
    "beat_this": ("Foscarin, Schlüter and Widmer (JKU Linz)", "https://github.com/CPJKU/beat_this"),
}

LICENCES = {
    "htdemucs_6s": "MIT",
    "htdemucs_ft": "MIT",
    # The model card at the pinned revision declares MIT; the repository
    # has no LICENSE file (docs/models.md).
    "kim_mel_roformer": "MIT",
    # UVR's README: "honor the MIT license by providing credit to UVR".
    "uvr_karaoke_2": "MIT",
    "basic_pitch": "Apache-2.0",
    "piano_hr": "CC BY 4.0",
    "ymt3": "Apache-2.0",
    "whisper_turbo": "MIT",
    "whisper_small": "MIT",
    # Its README: the code and the published weights are MIT. It said no
    # licence here before 2026-11-26.
    "beat_this": "MIT",
}


def test_every_built_in_model_names_its_author_and_page(client, folders):
    models = _by_id(client.get("/api/models").json())
    assert set(models) == set(SOURCES)
    for model_id, (author, url) in SOURCES.items():
        assert models[model_id]["source"] == {"author": author, "url": url}, model_id
        assert models[model_id]["license"] == LICENCES[model_id], model_id


def test_a_source_is_always_an_https_page_of_its_author():
    for key, (author, url) in model_inventory._SOURCES.items():
        assert author and author == author.strip(), key
        assert url.startswith("https://") and " " not in url, key
        # Never a mirror: the trainer-less BS-RoFormer SW is never listed,
        # and no re-upload host stands in for an author's page.
        for host in ("mirror", "seanghay", "Acapellas", "uvr5-models", "TRvlvr"):
            assert host not in url, (key, url)


def test_a_beat_checkpoint_swapped_in_has_no_licence_and_no_source(client, folders, monkeypatch):
    monkeypatch.setattr(model_inventory, "BEAT_MODEL_CHECKPOINT", "my_own")
    entry = _by_id(client.get("/api/models").json())["beat_this"]
    assert entry["license"] is None
    assert entry["source"] is None
