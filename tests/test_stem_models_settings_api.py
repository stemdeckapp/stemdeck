"""Settings, Separation over /api/settings: the model per stem.

GET carries the saved choice, what the next job here runs, the recommendation
and why, and the catalog the rows are drawn from. POST takes a partial choice,
checked whole before anything is written: a bad stem, a bad model or a model
this install cannot run answers 422 with a fixed sentence and changes nothing.
"""

from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient

from app.core import device_reason, stem_models
from app.core import settings as _settings
from app.core.stem_models import MODELS, SELECTABLE_STEMS
from app.main import app

KIM = "kim_mel_roformer"
SIX = "htdemucs_6s"
FT = "htdemucs_ft"
ALL_AUTO = dict.fromkeys(SELECTABLE_STEMS, "auto")
NEW_KEYS = {
    "stem_models",
    "stem_models_resolved",
    "stem_models_recommended",
    "stem_models_reason",
    "stem_model_catalog",
}


@pytest.fixture
def machine(monkeypatch):
    """Set the device the next job runs on and the NVIDIA GPU's memory.
    Every model is available unless the test says otherwise."""
    monkeypatch.setattr(stem_models, "model_available", lambda model_id: model_id in MODELS)

    def set_machine(device: str, memory_mb: int | None = 8192) -> None:
        monkeypatch.setattr("app.main.get_demucs_device", lambda: device)
        gpu = {"name": "NVIDIA GeForce RTX 3080", "driver": "616.64", "cuda": "13.4"}
        monkeypatch.setattr(device_reason, "nvidia_gpu", lambda: {**gpu, "memory_mb": memory_mb})

    set_machine("cuda")
    return set_machine


@pytest.fixture
def client(machine):
    with TestClient(app) as c:
        yield c


def _on_disk() -> bytes | None:
    path = _settings._SETTINGS_PATH
    return path.read_bytes() if path.exists() else None


def test_defaults_are_all_auto_and_the_new_keys_are_there(client):
    body = client.get("/api/settings").json()
    assert set(body) >= NEW_KEYS
    assert body["stem_models"] == ALL_AUTO
    assert body["stem_models_resolved"] == {**dict.fromkeys(SELECTABLE_STEMS, SIX), "vocals": KIM}
    assert body["stem_models_recommended"] == body["stem_models_resolved"]
    assert body["stem_models_reason"] == "gpu"
    catalog = body["stem_model_catalog"]
    assert [row["stem"] for row in catalog["stems"]] == [
        "vocals",
        "lead_backing",
        "drums",
        "bass",
        "guitar",
        "piano",
        "other",
    ]
    assert set(catalog["models"]) == set(MODELS)
    assert catalog["min_vram_mb"] == 4096
    # Reading Settings decides nothing for the user.
    assert _on_disk() is None or "stem_models" not in json.loads(_on_disk())


@pytest.mark.parametrize(
    ("device", "memory_mb", "vocals", "reason"),
    [
        ("cpu", None, SIX, "cpu"),
        ("mps", None, SIX, "mps"),
        ("cuda", 2048, SIX, "low-vram"),
        ("cuda", None, KIM, "gpu"),
    ],
)
def test_the_rows_recommend_for_the_device_the_next_job_runs_on(
    client, machine, device, memory_mb, vocals, reason
):
    machine(device, memory_mb)
    body = client.get("/api/settings").json()
    assert body["demucs_device_resolved"] == device
    assert body["stem_models_recommended"]["vocals"] == vocals
    assert body["stem_models_resolved"]["vocals"] == vocals
    assert body["stem_models_reason"] == reason


def test_a_partial_choice_round_trips_and_merges(client):
    r = client.post("/api/settings", json={"stem_models": {"bass": FT}})
    assert r.status_code == 200
    assert r.json()["stem_models"] == {**ALL_AUTO, "bass": FT}
    assert r.json()["stem_models_resolved"]["bass"] == FT

    r = client.post("/api/settings", json={"stem_models": {"vocals": SIX}})
    assert r.status_code == 200
    assert r.json()["stem_models"] == {**ALL_AUTO, "bass": FT, "vocals": SIX}
    assert r.json()["stem_models_resolved"]["vocals"] == SIX, "the explicit choice wins"

    saved = json.loads(_on_disk())["stem_models"]
    assert saved == {**ALL_AUTO, "bass": FT, "vocals": SIX}
    # A fresh process reads the file back.
    _settings._state = None
    assert client.get("/api/settings").json()["stem_models"] == saved

    r = client.post("/api/settings", json={"stem_models": {"bass": "auto", "vocals": "auto"}})
    assert r.json()["stem_models"] == ALL_AUTO


def test_an_empty_choice_writes_nothing(client):
    r = client.post("/api/settings", json={"stem_models": {}})
    assert r.status_code == 200
    assert r.json()["stem_models"] == ALL_AUTO
    assert _on_disk() is None


_ONE_OF_VOCALS = f"stem_models.vocals must be one of: auto, {SIX}, {KIM}"
_ONE_OF_DRUMS = f"stem_models.drums must be one of: auto, {SIX}, {FT}"
_NOT_AN_OBJECT = "stem_models must be an object of stem to model"
_UNKNOWN_STEM = "stem_models: unknown stem; choose from vocals, drums, bass, guitar, piano"


@pytest.mark.parametrize(
    ("value", "detail"),
    [
        ("htdemucs_6s", _NOT_AN_OBJECT),
        (["vocals"], _NOT_AN_OBJECT),
        (None, _NOT_AN_OBJECT),
        (5, _NOT_AN_OBJECT),
        ({"other": SIX}, _UNKNOWN_STEM),
        ({"lead_backing": SIX}, _UNKNOWN_STEM),
        ({"<script>x</script>": SIX}, _UNKNOWN_STEM),
        ({"bass": FT, "kazoo": SIX}, _UNKNOWN_STEM),
        ({"vocals": "<script>x</script>"}, _ONE_OF_VOCALS),
        ({"vocals": FT}, _ONE_OF_VOCALS),
        ({"vocals": 5}, _ONE_OF_VOCALS),
        ({"vocals": None}, _ONE_OF_VOCALS),
        ({"drums": KIM}, _ONE_OF_DRUMS),
        # A good stem before a bad one: the good one is not kept either.
        ({"bass": FT, "drums": "best_drums"}, _ONE_OF_DRUMS),
    ],
)
def test_a_bad_value_answers_422_and_changes_nothing(client, value, detail):
    assert client.post("/api/settings", json={"stem_models": {"piano": SIX}}).status_code == 200
    before = _on_disk()

    # Sent with a good setting that comes earlier in the handler: it is not
    # written either.
    r = client.post("/api/settings", json={"stem_models": value, "allow_network": True})

    assert r.status_code == 422
    assert r.json()["detail"] == detail
    assert "<script>" not in r.text
    assert _on_disk() == before
    _settings._state = None
    assert _settings.get_stem_model_choices() == {**ALL_AUTO, "piano": SIX}


def test_a_bad_value_writes_no_other_setting(client):
    assert _settings.get_separation_quality() == "standard"
    r = client.post(
        "/api/settings", json={"separation_quality": "best", "stem_models": {"vocals": "x"}}
    )
    assert r.status_code == 422
    assert _settings.get_separation_quality() == "standard"
    assert _on_disk() is None


def test_a_good_choice_with_another_bad_setting_is_not_kept(client):
    r = client.post("/api/settings", json={"stem_models": {"bass": FT}, "video_encoder": 5})
    assert r.status_code == 422
    assert _settings.get_stem_model_choices() == ALL_AUTO


def test_kim_where_it_is_not_available_answers_422(client, monkeypatch):
    monkeypatch.setattr(stem_models, "model_available", lambda model_id: model_id in (SIX, FT))
    r = client.post("/api/settings", json={"stem_models": {"vocals": KIM}})
    assert r.status_code == 422
    assert r.json()["detail"] == f"stem_models.vocals: {KIM} is not available on this install"
    assert _settings.get_stem_model_choices() == ALL_AUTO
    body = client.get("/api/settings").json()
    assert body["stem_models_reason"] == "unavailable"
    assert body["stem_models_resolved"]["vocals"] == SIX
    assert body["stem_model_catalog"]["models"][KIM]["available"] is False


def test_a_saved_choice_that_became_unavailable_is_kept_and_resolves_to_6s(client, monkeypatch):
    # Saved on this install, then audio-separator went away (a reinstall).
    assert client.post("/api/settings", json={"stem_models": {"vocals": KIM}}).status_code == 200
    monkeypatch.setattr(stem_models, "model_available", lambda model_id: model_id in (SIX, FT))
    body = client.get("/api/settings").json()
    assert body["stem_models"]["vocals"] == KIM
    assert body["stem_models_resolved"]["vocals"] == SIX


def test_a_damaged_saved_value_reads_as_auto(client):
    _settings._SETTINGS_PATH.write_text(
        json.dumps({"stem_models": {"vocals": "gone_model", "drums": 7, "bass": FT}}),
        encoding="utf-8",
    )
    _settings._state = None
    assert client.get("/api/settings").json()["stem_models"] == {**ALL_AUTO, "bass": FT}
    _settings._SETTINGS_PATH.write_text(json.dumps({"stem_models": "kim"}), encoding="utf-8")
    _settings._state = None
    assert client.get("/api/settings").json()["stem_models"] == ALL_AUTO


def test_settings_still_answer_when_the_models_cannot_be_worked_out(client, monkeypatch, caplog):
    def broken(*_a, **_kw):
        raise RuntimeError("probe failed")

    monkeypatch.setattr(stem_models, "recommended", broken)
    with caplog.at_level(logging.WARNING, logger="stemdeck"):
        r = client.get("/api/settings")
    assert r.status_code == 200
    body = r.json()
    assert body["stem_models_recommended"] == dict.fromkeys(SELECTABLE_STEMS, SIX)
    assert body["stem_models_resolved"] == dict.fromkeys(SELECTABLE_STEMS, SIX)
    assert body["stem_models"] == ALL_AUTO
    assert body["stem_model_catalog"]["models"][KIM]["available"] is False
    assert any("separation models" in rec.getMessage() for rec in caplog.records)


def test_resolve_stem_models_uses_the_saved_choice_and_the_gpu_memory(machine):
    machine("cuda", 2048)
    assert _settings.resolve_stem_models("cuda")["vocals"] == SIX
    machine("cuda", 8192)
    assert _settings.resolve_stem_models("cuda")["vocals"] == KIM
    assert _settings.resolve_stem_models("cpu")["vocals"] == SIX
    _settings.set_stem_model_choices({"vocals": KIM, "drums": FT})
    assert _settings.resolve_stem_models("cpu") == {
        "vocals": KIM,
        "drums": FT,
        "bass": SIX,
        "guitar": SIX,
        "piano": SIX,
    }


# ── models the person added (My models) ──

USER_A = "user_0123456789ab"
USER_B = "user_ba9876543210"


@pytest.fixture
def store(monkeypatch, machine):
    """Two models the person added: USER_A for drums and bass, usable;
    USER_B for vocals, refused. Every model the store says is usable is
    available here."""
    from tests.test_stem_models import fake_store

    models = {
        USER_A: {"name": "My drums", "stems": ["drums", "bass"]},
        USER_B: {"name": "Refused", "stems": ["vocals"], "usable": False},
    }
    fake_store(monkeypatch, models)
    monkeypatch.setattr(stem_models, "roformer_arch_available", lambda arch: True)
    monkeypatch.setattr(
        stem_models,
        "model_available",
        lambda model_id: model_id in MODELS or stem_models.user_model_usable(model_id),
    )
    return models


def test_a_usable_user_model_is_a_choice_that_round_trips_and_runs(client, store):
    r = client.post("/api/settings", json={"stem_models": {"drums": USER_A}})
    assert r.status_code == 200, r.text
    body = client.get("/api/settings").json()
    assert body["stem_models"] == {**ALL_AUTO, "drums": USER_A}
    assert body["stem_models_resolved"]["drums"] == USER_A
    # Never the recommendation.
    assert body["stem_models_recommended"]["drums"] == SIX
    catalog = body["stem_model_catalog"]
    rows = {row["stem"]: row for row in catalog["stems"]}
    assert rows["drums"]["choices"] == [SIX, FT, USER_A]
    assert rows["vocals"]["choices"] == [SIX, KIM]
    assert catalog["models"][USER_A]["user"] is True
    assert USER_B not in catalog["models"]


@pytest.mark.parametrize(
    "value",
    [
        {"vocals": USER_B},  # refused by its check
        {"piano": USER_A},  # a stem it does not declare
        {"drums": "user_ffffffffffff"},  # no such model
    ],
)
def test_a_user_model_that_is_not_a_choice_answers_422_without_echoing(client, store, value):
    before = _on_disk()
    r = client.post("/api/settings", json={"stem_models": value})
    assert r.status_code == 422
    stem = next(iter(value))
    detail = r.json()["detail"]
    assert detail.startswith(f"stem_models.{stem} must be one of: auto, ")
    assert value[stem] not in detail
    assert _on_disk() == before


def test_the_422_lists_the_user_models_that_are_choices(client, store):
    r = client.post("/api/settings", json={"stem_models": {"drums": "best_drums"}})
    assert r.status_code == 422
    assert r.json()["detail"] == f"stem_models.drums must be one of: auto, {SIX}, {FT}, {USER_A}"


def test_a_saved_user_model_that_stops_being_usable_reads_as_auto(client, store):
    assert client.post("/api/settings", json={"stem_models": {"bass": USER_A}}).status_code == 200
    store[USER_A]["usable"] = False
    body = client.get("/api/settings").json()
    assert body["stem_models"]["bass"] == "auto"
    assert body["stem_models_resolved"]["bass"] == SIX
    # Usable again (a drive plugged back in): the saved choice is back.
    store[USER_A]["usable"] = True
    assert client.get("/api/settings").json()["stem_models"]["bass"] == USER_A


def test_removing_a_model_scrubs_it_from_every_stem(client, store):
    r = client.post("/api/settings", json={"stem_models": {"drums": USER_A, "bass": USER_A}})
    assert r.status_code == 200
    assert client.post("/api/settings", json={"stem_models": {"vocals": KIM}}).status_code == 200

    assert sorted(_settings.scrub_stem_model(USER_A)) == ["bass", "drums"]

    saved = json.loads(_settings._SETTINGS_PATH.read_text(encoding="utf-8"))["stem_models"]
    assert saved["drums"] == "auto" and saved["bass"] == "auto" and saved["vocals"] == KIM
    assert _settings.scrub_stem_model(USER_A) == []
    assert _settings.get_stem_model_choices() == {**ALL_AUTO, "vocals": KIM}


def test_a_speed_test_plan_never_overrides_a_user_model(machine, store, monkeypatch):
    _settings.set_stem_model_choices({"drums": USER_A})
    measured = dict.fromkeys(SELECTABLE_STEMS, FT)
    monkeypatch.setattr(_settings, "measured_stem_models", lambda device: dict(measured))
    plan = _settings.resolve_stem_models("cpu")
    assert plan["drums"] == USER_A
    assert plan["bass"] == FT
