"""The separation model registry (app/core/stem_models.py).

Data the whole feature builds on: what each model is, which stem may use
which, what "auto" picks on which machine, and the passes a plan runs. The
registry checks guard against a silent wrong download (a "%" in a URL is read
as progress by separate.py; a malformed SHA-256 verifies nothing).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.core import stem_models
from app.core.stem_models import (
    CHOICES,
    FIXED,
    MODELS,
    PREFERENCE,
    SELECTABLE_STEMS,
    ModelFile,
    passes,
    recommendation_reason,
    recommended,
    resolve,
)

KIM = "kim_mel_roformer"
SIX = "htdemucs_6s"
FT = "htdemucs_ft"


@pytest.fixture
def kim_available(monkeypatch):
    """Every model available, as on a Windows or Linux install with
    audio-separator; the test then decides on the machine itself."""
    monkeypatch.setattr(stem_models, "model_available", lambda model_id: model_id in MODELS)


@pytest.fixture
def kim_unavailable(monkeypatch):
    monkeypatch.setattr(stem_models, "model_available", lambda model_id: model_id in (SIX, FT))


# ── the registry itself ──


def test_every_model_file_is_pinned_and_safe():
    files = [f for m in MODELS.values() for f in m.files]
    assert files, "Kim's checkpoint is downloaded by StemDeck itself"
    for f in files:
        assert re.fullmatch(r"[a-f0-9]{64}", f.sha256), f.url
        assert "%" not in f.url, "separate.py reads a % on stderr as progress"
        assert f.url.startswith("https://")
        assert f.size > 0
        assert ".." not in f.relpath.split("/")


def test_no_url_anywhere_holds_a_percent_sign():
    for m in MODELS.values():
        assert "%" not in m.source_url
        for f in m.files:
            assert "%" not in f.url


def test_kim_is_pinned_to_a_revision_with_the_agreed_checkpoint():
    [f] = MODELS[KIM].files
    assert f.sha256 == "87201f4d31afb5bc79993230fc49446918425574db48c01c405e44f365c7559e"
    assert f.size == 913_106_900
    assert f.relpath == "roformer/MelBandRoformer.ckpt"
    # A commit hash, never a branch: the bytes behind "main" can change.
    m = re.fullmatch(
        r"https://huggingface\.co/KimberleyJSN/melbandroformer/resolve/([0-9a-f]{40})/"
        r"MelBandRoformer\.ckpt",
        f.url,
    )
    assert m is not None and m.group(1) == stem_models.KIM_REVISION
    assert MODELS[KIM].download_bytes == f.size


@pytest.mark.parametrize(
    "kwargs",
    [
        {"url": "http://example.org/a.ckpt"},
        {"url": "https://example.org/a%20b.ckpt"},
        {"sha256": "ABC"},
        {"sha256": "g" * 64},
        {"size": 0},
        {"relpath": "../escape.ckpt"},
        {"relpath": "/abs.ckpt"},
        {"relpath": "a\\b.ckpt"},
        {"relpath": "C:/x.ckpt"},
        {"relpath": ""},
    ],
)
def test_a_model_file_that_is_not_safe_cannot_be_declared(kwargs):
    base = {"url": "https://example.org/a.ckpt", "sha256": "a" * 64, "size": 1, "relpath": "a/b"}
    with pytest.raises(ValueError):
        ModelFile(**{**base, **kwargs})


def test_every_choice_is_a_model_that_supplies_its_stem():
    assert tuple(CHOICES) == SELECTABLE_STEMS
    for stem, ids in CHOICES.items():
        assert ids, stem
        for model_id in ids:
            assert model_id in MODELS, model_id
            assert stem in MODELS[model_id].stems, (stem, model_id)


def test_every_preference_is_a_choice_and_ends_with_a_model_every_device_gets():
    assert tuple(PREFERENCE) == SELECTABLE_STEMS
    for stem, ids in PREFERENCE.items():
        assert set(ids) <= set(CHOICES[stem]), stem
        assert MODELS[ids[-1]].recommend_on >= {"cuda", "mps", "cpu"}, stem


def test_the_fixed_stems_are_not_selectable():
    assert set(FIXED) == {"lead_backing", "other"}
    assert not set(FIXED) & set(SELECTABLE_STEMS)


def test_the_registry_does_not_import_torch(tmp_path):
    # In a fresh interpreter, pointed at a throwaway data folder: in this one
    # another test has imported torch long ago.
    import os
    import subprocess
    import sys

    root = Path(__file__).resolve().parent.parent
    env = {
        **os.environ,
        "STEMDECK_DATA_DIR": str(tmp_path),
        "STEMDECK_JOBS_DIR": str(tmp_path / "jobs"),
        "STEMDECK_CACHE_DIR": str(tmp_path / "cache"),
    }
    code = (
        "import sys; import app.core.stem_models as m; "
        "m.model_available('kim_mel_roformer'); m.resolve({}, 'cuda', None); "
        "print(sorted({'torch', 'demucs', 'audio_separator'} & set(sys.modules)))"
    )
    out = subprocess.run(  # noqa: S603 -- this interpreter, fixed code
        [sys.executable, "-c", code],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    assert out.stdout.strip() == "[]"


# ── availability ──


def test_kim_is_available_when_audio_separators_roformer_file_exists(monkeypatch, tmp_path):
    pkg = tmp_path / "audio_separator"
    roformer = pkg / "separator" / "uvr_lib_v5" / "roformer"
    roformer.mkdir(parents=True)
    dirs = {
        "torch": [tmp_path / "torch"],
        "demucs": [tmp_path / "demucs"],
        "audio_separator": [pkg],
    }
    monkeypatch.setattr(stem_models, "_package_dirs", lambda name: dirs.get(name, []))
    assert stem_models.model_available(KIM) is False, "the module moved: not available"
    (roformer / "mel_band_roformer.py").write_text("", encoding="utf-8")
    assert stem_models.model_available(KIM) is True
    assert stem_models.model_available(SIX) is True


def test_kim_is_unavailable_without_audio_separator(monkeypatch, tmp_path):
    dirs = {"torch": [tmp_path], "demucs": [tmp_path]}
    monkeypatch.setattr(stem_models, "_package_dirs", lambda name: dirs.get(name, []))
    assert stem_models.model_available(KIM) is False
    assert stem_models.model_available(SIX) is True
    assert stem_models.model_available("no_such_model") is False


def test_a_checkpoint_of_the_wrong_size_is_not_downloaded(monkeypatch, tmp_path):
    monkeypatch.setattr(stem_models, "MODELS_DIR", tmp_path)
    [f] = MODELS[KIM].files
    path = stem_models.model_path(f)
    assert path == tmp_path / "roformer" / "MelBandRoformer.ckpt"
    assert stem_models.model_downloaded(KIM) is False
    path.parent.mkdir(parents=True)
    path.write_bytes(b"cut off")
    assert stem_models.model_downloaded(KIM) is False


# ── the recommendation ──


@pytest.mark.usefixtures("kim_available")
@pytest.mark.parametrize(
    ("device", "vram_mb", "vocals", "reason"),
    [
        ("cuda", 8192, KIM, "gpu"),
        ("cuda", 4096, KIM, "gpu"),
        ("cuda", None, KIM, "gpu"),  # unknown memory (ROCm, no nvidia-smi) is enough
        ("cuda", 2048, SIX, "low-vram"),
        ("cuda", 4095, SIX, "low-vram"),
        ("cpu", None, SIX, "cpu"),
        ("cpu", 8192, SIX, "cpu"),
        ("mps", None, SIX, "mps"),
    ],
)
def test_the_recommendation_matrix(device, vram_mb, vocals, reason):
    assert recommended("vocals", device, vram_mb) == vocals
    assert recommendation_reason(device, vram_mb) == reason
    for stem in ("drums", "bass", "guitar", "piano"):
        assert recommended(stem, device, vram_mb) == SIX, stem


@pytest.mark.usefixtures("kim_unavailable")
def test_kim_unavailable_recommends_6s_and_says_why():
    assert recommended("vocals", "cuda", 8192) == SIX
    assert recommendation_reason("cuda", 8192) == "unavailable"
    assert recommendation_reason("cpu", None) == "cpu"


# ── resolve ──


@pytest.mark.usefixtures("kim_available")
def test_auto_and_missing_resolve_to_the_recommendation():
    plan = resolve({"vocals": "auto"}, "cuda", 8192)
    assert plan == {"vocals": KIM, "drums": SIX, "bass": SIX, "guitar": SIX, "piano": SIX}
    assert resolve(None, "cpu", None) == dict.fromkeys(SELECTABLE_STEMS, SIX)
    assert resolve({}, "cpu", None) == dict.fromkeys(SELECTABLE_STEMS, SIX)


@pytest.mark.usefixtures("kim_available")
def test_an_explicit_choice_wins_over_the_recommendation():
    plan = resolve({"vocals": SIX, "bass": FT}, "cuda", 8192)
    assert plan == {"vocals": SIX, "drums": SIX, "bass": FT, "guitar": SIX, "piano": SIX}
    # Chosen on a CPU, Kim runs there: the slow warning is the UI's job.
    assert resolve({"vocals": KIM}, "cpu", None)["vocals"] == KIM


@pytest.mark.usefixtures("kim_available")
@pytest.mark.parametrize(
    "choices",
    [
        {"vocals": "no_such_model", "drums": "no_such_model"},
        {"drums": KIM, "guitar": FT},  # models that do not supply those stems
        {"vocals": 5, "bass": None, "piano": ["htdemucs_6s"]},
        "htdemucs_6s",
    ],
)
def test_unknown_and_non_supplying_choices_resolve_to_the_recommendation(choices):
    assert resolve(choices, "cpu", None) == dict.fromkeys(SELECTABLE_STEMS, SIX)


@pytest.mark.usefixtures("kim_unavailable")
def test_an_unavailable_choice_resolves_to_the_recommendation():
    assert resolve({"vocals": KIM}, "cuda", 8192)["vocals"] == SIX


def test_a_deployment_model_outside_the_registry_runs_every_stem(monkeypatch):
    monkeypatch.setattr(stem_models, "BASE", "mdx_extra_q")
    plan = resolve({"vocals": KIM}, "cuda", 8192)
    assert plan == dict.fromkeys(SELECTABLE_STEMS, "mdx_extra_q")
    assert passes(plan) == [("mdx_extra_q", "mix", SELECTABLE_STEMS)]
    catalog = stem_models.catalog(lead_backing_available=True)
    assert catalog["deployment"] == "mdx_extra_q"
    rows = {row["stem"]: row for row in catalog["stems"]}
    assert all(rows[s] == {"stem": s, "fixed": "mdx_extra_q"} for s in SELECTABLE_STEMS)


# ── passes ──


def test_passes_kim_with_6s():
    plan = {"vocals": KIM, "drums": SIX, "bass": SIX, "guitar": SIX, "piano": SIX}
    assert passes(plan) == [
        (KIM, "mix", ("vocals",)),
        (SIX, "rest", ("drums", "bass", "guitar", "piano")),
    ]


def test_passes_kim_with_ft_bass_and_6s_drums():
    plan = {"vocals": KIM, "drums": SIX, "bass": FT, "guitar": SIX, "piano": SIX}
    assert passes(plan) == [
        (KIM, "mix", ("vocals",)),
        (SIX, "rest", ("drums", "guitar", "piano")),
        (FT, "rest", ("bass",)),
    ]


def test_passes_all_6s_is_one_pass_on_the_mix():
    assert passes(dict.fromkeys(SELECTABLE_STEMS, SIX)) == [(SIX, "mix", SELECTABLE_STEMS)]


def test_passes_6s_vocals_with_ft_drums_run_both_on_the_mix():
    plan = {"vocals": SIX, "drums": FT, "bass": SIX, "guitar": SIX, "piano": SIX}
    assert passes(plan) == [
        (SIX, "mix", ("vocals", "bass", "guitar", "piano")),
        (FT, "mix", ("drums",)),
    ]


def test_every_stem_comes_from_exactly_one_pass():
    for vocals in CHOICES["vocals"]:
        for drums in CHOICES["drums"]:
            for bass in CHOICES["bass"]:
                plan = {"vocals": vocals, "drums": drums, "bass": bass, "guitar": SIX, "piano": SIX}
                taken = [stem for _m, _i, stems in passes(plan) for stem in stems]
                assert sorted(taken) == sorted(SELECTABLE_STEMS), plan
                for model_id, _input, stems in passes(plan):
                    assert all(plan[s] == model_id for s in stems)


# ── catalog ──


@pytest.mark.usefixtures("kim_available")
def test_the_catalog_lists_every_row_in_order_and_every_model(monkeypatch, tmp_path):
    monkeypatch.setattr(stem_models, "MODELS_DIR", tmp_path)
    catalog = stem_models.catalog(lead_backing_available=False)
    assert [row["stem"] for row in catalog["stems"]] == [
        "vocals",
        "lead_backing",
        "drums",
        "bass",
        "guitar",
        "piano",
        "other",
    ]
    rows = {row["stem"]: row for row in catalog["stems"]}
    assert rows["vocals"]["choices"] == [SIX, KIM]
    assert rows["guitar"]["choices"] == [SIX]
    assert rows["lead_backing"] == {
        "stem": "lead_backing",
        "fixed": "uvr_karaoke_2",
        "available": False,
    }
    assert rows["other"] == {"stem": "other", "fixed": "residual"}
    assert set(catalog["models"]) == set(MODELS)
    kim = catalog["models"][KIM]
    assert set(kim) == {
        "name",
        "user",
        "engine",
        "stems",
        "license",
        "source_url",
        "download_bytes",
        "cpu_rtf",
        "gpu_rtf",
        "available",
        "downloaded",
        "note_key",
    }
    assert kim["available"] is True and kim["downloaded"] is False
    assert kim["user"] is False
    assert (kim["engine"], kim["stems"]) == ("mel_roformer", ["vocals"])
    six = catalog["models"][SIX]
    assert six["engine"] == "demucs"
    assert six["stems"] == ["bass", "drums", "guitar", "other", "piano", "vocals"]
    assert catalog["models"]["htdemucs_ft"]["stems"] == ["bass", "drums", "other", "vocals"]
    assert kim["note_key"] == "settings.stemModels.note.kim_mel_roformer"
    assert catalog["min_vram_mb"] == 4096
    assert catalog["deployment"] is None


def test_the_hub_checkpoint_names_match_demucs_own_list():
    """The names Settings looks for are the ones torch.hub writes. Read from
    the demucs package, so a demucs that renamed them fails here instead of
    showing "not downloaded" forever."""
    demucs = pytest.importorskip("demucs")
    remote = Path(demucs.__file__).parent / "remote"
    listed = set((remote / "files.txt").read_text(encoding="utf-8").split())
    for model_id, names in stem_models._HUB_CHECKPOINTS.items():
        yaml = (remote / f"{model_id}.yaml").read_text(encoding="utf-8")
        signatures = re.findall(r"'([0-9a-f]{8})'", yaml)
        assert sorted(n.split("-")[0] for n in names) == sorted(signatures), model_id
        assert {n.split(".")[0] for n in names} <= {
            line.rsplit("/", 1)[-1].split(".")[0] for line in listed
        }, model_id


# ── models the person added (My models) ──

USER_A = "user_0123456789ab"
USER_B = "user_ba9876543210"


def fake_store(monkeypatch, models: dict[str, dict]) -> None:
    """The user-model store (app/core/user_models.py) answering from
    ``models``: id -> {"name", "stems", "arch", "usable", "cpu_rtf"}, in the
    order they were added. Only its read functions are replaced."""
    from app.core import user_models

    def usable(model_id):
        return model_id in models and models[model_id].get("usable", True)

    def public(model_id):
        if model_id not in models:
            return None
        m = models[model_id]
        return {
            "id": model_id,
            "name": m.get("name", model_id),
            "status": "ready" if m.get("usable", True) else "refused",
            "stems": list(m["stems"]),
            "arch": m.get("arch", "mel_band_roformer"),
            "size": 1234,
            "source": {"kind": "upload", "label": "x", "license_declared": m.get("license")},
            "cpu_rtf": m.get("cpu_rtf", 2.0),
            "usable": usable(model_id),
            "missing": False,
        }

    monkeypatch.setattr(user_models, "usable", usable)
    monkeypatch.setattr(user_models, "get", public)
    monkeypatch.setattr(
        user_models,
        "record",
        lambda i: {"arch": models[i].get("arch", "mel_band_roformer")} if i in models else None,
    )
    monkeypatch.setattr(user_models, "list_models", lambda: [public(i) for i in models])
    monkeypatch.setattr(
        user_models,
        "choices_for",
        lambda stem: tuple(i for i in models if usable(i) and stem in models[i]["stems"]),
    )
    monkeypatch.setattr(
        user_models, "name_of", lambda i: models[i]["name"] if i in models else None
    )


@pytest.fixture
def classes_there(monkeypatch):
    """torch and both of audio-separator's RoFormer class files are there."""
    monkeypatch.setattr(stem_models, "roformer_arch_available", lambda arch: True)


def test_a_user_id_is_run_as_a_roformer_never_as_demucs():
    assert stem_models.engine_of(USER_A) == "user_roformer"
    assert stem_models.engine_of(KIM) == "mel_roformer"
    assert stem_models.engine_of(SIX) == "demucs"
    # A deployment's base model outside the registry is still Demucs.
    assert stem_models.engine_of("mdx_extra_q") == "demucs"
    # Shape only: neither upper case nor 13 hex is one.
    assert stem_models.engine_of("user_0123456789AB") == "demucs"
    assert stem_models.engine_of("user_0123456789abc") == "demucs"


def test_passes_put_a_user_model_on_the_mix_before_demucs():
    """F on the old code, which ran an unknown id as Demucs on the rest."""
    plan = {"vocals": SIX, "drums": USER_A, "bass": USER_A, "guitar": SIX, "piano": SIX}
    assert passes(plan) == [
        (USER_A, "mix", ("drums", "bass")),
        (SIX, "rest", ("vocals", "guitar", "piano")),
    ]


def test_passes_run_kim_and_two_user_models_each_on_the_mix():
    plan = {"vocals": KIM, "drums": USER_A, "bass": USER_B, "guitar": SIX, "piano": FT}
    assert passes(plan) == [
        (KIM, "mix", ("vocals",)),
        (USER_A, "mix", ("drums",)),
        (USER_B, "mix", ("bass",)),
        (SIX, "rest", ("guitar",)),
        (FT, "rest", ("piano",)),
    ]


@pytest.mark.usefixtures("classes_there")
def test_choices_for_adds_usable_user_models_that_declare_the_stem(monkeypatch):
    fake_store(
        monkeypatch,
        {
            USER_A: {"stems": ["drums", "bass"]},
            USER_B: {"stems": ["drums"], "usable": False},
        },
    )
    before = {k: tuple(v) for k, v in CHOICES.items()}
    assert stem_models.choices_for("drums") == (SIX, FT, USER_A)
    assert stem_models.choices_for("bass") == (SIX, FT, USER_A)
    assert stem_models.choices_for("vocals") == (SIX, KIM)
    assert stem_models.choices_for("other") == ()
    assert before == CHOICES


@pytest.mark.usefixtures("classes_there")
def test_a_pinned_deployment_offers_no_user_model(monkeypatch):
    fake_store(monkeypatch, {USER_A: {"stems": ["drums"]}})
    monkeypatch.setattr(stem_models, "BASE", "mdx_extra_q")
    assert stem_models.choices_for("drums") == CHOICES["drums"]
    assert resolve({"drums": USER_A}, "cpu", None)["drums"] == "mdx_extra_q"
    catalog = stem_models.catalog(lead_backing_available=False)
    assert USER_A not in catalog["models"]


@pytest.mark.usefixtures("classes_there", "kim_available")
def test_resolve_takes_a_usable_user_model_for_a_stem_it_declares(monkeypatch):
    monkeypatch.setattr(
        stem_models,
        "model_available",
        lambda model_id: model_id in MODELS or stem_models.user_model_usable(model_id),
    )
    fake_store(
        monkeypatch,
        {USER_A: {"stems": ["drums"]}, USER_B: {"stems": ["bass"], "usable": False}},
    )
    plan = resolve({"drums": USER_A, "bass": USER_B, "piano": USER_A}, "cpu", None)
    assert plan["drums"] == USER_A
    # Not usable, or not a stem it declares: the recommendation.
    assert plan["bass"] == SIX and plan["piano"] == SIX


@pytest.mark.usefixtures("classes_there", "kim_available")
def test_a_user_model_is_never_recommended(monkeypatch):
    fake_store(monkeypatch, {USER_A: {"stems": list(SELECTABLE_STEMS), "cpu_rtf": 0.01}})
    for device in ("cpu", "cuda", "mps"):
        for stem in SELECTABLE_STEMS:
            assert not stem_models.is_user_model(recommended(stem, device, 24_000))
        assert USER_A not in resolve(None, device, 24_000).values()


def test_a_bs_roformer_needs_its_own_class_file(monkeypatch, tmp_path):
    """The store checks MelBandRoformer's file only; a BS-RoFormer model is
    unavailable when audio-separator has no bs_roformer.py."""
    pkg = tmp_path / "audio_separator"
    roformer = pkg / "separator" / "uvr_lib_v5" / "roformer"
    roformer.mkdir(parents=True)
    (roformer / "mel_band_roformer.py").write_text("")
    dirs = {"torch": [tmp_path / "torch"], "audio_separator": [pkg]}
    monkeypatch.setattr(stem_models, "_package_dirs", lambda name: dirs.get(name, []))
    fake_store(
        monkeypatch,
        {
            USER_A: {"stems": ["drums"], "arch": "bs_roformer"},
            USER_B: {"stems": ["bass"], "arch": "mel_band_roformer"},
        },
    )
    assert stem_models.user_model_usable(USER_B) is True
    assert stem_models.user_model_usable(USER_A) is False
    (roformer / "bs_roformer.py").write_text("")
    assert stem_models.user_model_usable(USER_A) is True
    assert stem_models.model_available(USER_A) is True
    # An id the store does not know, or not of the shape, is not usable.
    assert stem_models.model_available("user_ffffffffffff") is False
    assert stem_models.model_available("no_such_model") is False


@pytest.mark.usefixtures("classes_there", "kim_available")
def test_the_catalog_lists_usable_user_models_after_the_built_in_choices(monkeypatch, tmp_path):
    monkeypatch.setattr(stem_models, "MODELS_DIR", tmp_path)
    fake_store(
        monkeypatch,
        {
            USER_A: {"name": "My drums", "stems": ["bass", "drums"], "license": "CC BY 4.0"},
            USER_B: {"name": "Broken", "stems": ["vocals"], "usable": False},
        },
    )
    catalog = stem_models.catalog(
        lead_backing_available=False,
        available=lambda m: m in MODELS or stem_models.user_model_usable(m),
    )
    rows = {row["stem"]: row for row in catalog["stems"]}
    assert rows["drums"]["choices"] == [SIX, FT, USER_A]
    assert rows["bass"]["choices"] == [SIX, FT, USER_A]
    assert rows["vocals"]["choices"] == [SIX, KIM]
    assert rows["guitar"]["choices"] == [SIX]
    assert USER_B not in catalog["models"]
    entry = catalog["models"][USER_A]
    assert entry["user"] is True and entry["estimated"] is True
    assert entry["available"] is True and entry["downloaded"] is True
    assert entry["name"] == "My drums" and entry["engine"] == "user_roformer"
    assert entry["stems"] == ["drums", "bass"]
    assert entry["license"] == "CC BY 4.0" and entry["source_url"] is None
    assert entry["cpu_rtf"] == 2.0
    assert entry["gpu_rtf"] == pytest.approx(2.0 * 0.143 / 3.40)
    assert entry["note_key"] is None and entry["download_bytes"] == 0
    # The fallback payload leaves them out.
    plain = stem_models.catalog(lead_backing_available=False, include_user=False)
    assert set(plain["models"]) == set(MODELS)
    assert {r["stem"]: r for r in plain["stems"]}["drums"]["choices"] == [SIX, FT]


@pytest.mark.usefixtures("classes_there")
def test_a_user_models_rtf_is_its_measure_on_the_cpu_and_an_estimate_elsewhere(monkeypatch):
    fake_store(monkeypatch, {USER_A: {"stems": ["drums"], "cpu_rtf": 4.0}})
    assert stem_models.user_model_rtf(USER_A, "cpu") == 4.0
    assert stem_models.user_model_rtf(USER_A, "cuda") == pytest.approx(4.0 * 0.143 / 3.40)
    assert stem_models.user_model_rtf("user_ffffffffffff", "cpu") is None
