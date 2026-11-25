"""Which model separates which stem: the registry behind Settings, Separation.

Data and pure decisions only. Nothing here imports torch at module level or
loads a model: the server reads this for /api/settings, and GPU work belongs
to the worker processes. The worker and _separate() run what resolve() and
passes() describe.

Three built-in models. htdemucs_6s is the base: it supplies all six stems, and is what
every song was separated with before this existed. htdemucs_ft is Demucs'
fine-tuned four-stem bag, offered for drums and bass. kim_mel_roformer is Kim's
Mel-Band RoFormer vocal model, run through audio-separator's MelBandRoformer
class, and recommended for vocals on a cuda GPU with enough memory.

"other" is never chosen: it is the residual (the mix minus the five chosen
stems), so the stems always add up to the song. Lead and backing stay the
Karaoke 2 split of the finished vocals.

Beside them, the models a person added (Settings, My models;
app/core/user_models.py): a RoFormer file of their own, offered for the stems
it declares, by an id "user_<12 hex>" that travels in a plan like a built-in
id. Never recommended and never the default: "auto" never picks one. The
store of those models is read only through the functions below, and never by
the separation worker, which gets what it needs in its request line.

The numbers (real-time factors, VRAM) are measured on the maintainer's
machines; see docs/models.md. min_vram_mb for Kim is a floor taken with a
margin over the measured 2017 MB, not a measured failure point.
"""

from __future__ import annotations

import importlib.util
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.core.config import DEMUCS_MODEL, MODELS_DIR, USER_MODEL_ID_RE

_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")

Engine = Literal["demucs", "mel_roformer", "user_roformer"]
Requires = Literal["torch", "audio_separator"]
Reason = Literal["gpu", "cpu", "mps", "low-vram", "unavailable"]
PassInput = Literal["mix", "rest"]


@dataclass(frozen=True)
class ModelFile:
    """One checkpoint, pinned: what its download is verified against.

    The URL names fixed bytes (a revision, or a file name holding its hash),
    and the size and full SHA-256 are what the file must be, whoever served
    it. root "models" is a file StemDeck downloads itself, under
    config.MODELS_DIR; root "torch_hub" is one torch.hub keeps for Demucs, in
    <torch.hub.get_dir()>/checkpoints under exactly the name torch.hub gives
    it (app/core/model_download.py verifies those too). No "%" in the URL:
    the worker prints it on stderr, where separate.py reads a "%" as
    progress."""

    url: str
    sha256: str
    size: int
    relpath: str  # under root, forward slashes
    root: Literal["models", "torch_hub"] = "models"

    def __post_init__(self) -> None:
        if not self.url.startswith("https://") or "%" in self.url:
            raise ValueError("a model URL must be https and hold no '%'")
        if not _SHA256_RE.match(self.sha256):
            raise ValueError("sha256 must be 64 lowercase hex characters")
        if self.size <= 0:
            raise ValueError("size must be positive")
        parts = self.relpath.split("/")
        if (
            not self.relpath
            or self.relpath.startswith("/")
            or "\\" in self.relpath
            or ":" in self.relpath
            or any(p in ("", ".", "..") for p in parts)
        ):
            raise ValueError("relpath must be a plain relative path under MODELS_DIR")
        if self.root not in ("models", "torch_hub"):
            raise ValueError("root must be models or torch_hub")
        if self.root == "torch_hub" and ("/" in self.relpath or not self.relpath.endswith(".th")):
            raise ValueError("a torch.hub checkpoint is a plain .th file name")


@dataclass(frozen=True)
class StemModel:
    id: str
    name: str  # display name, not translated
    engine: Engine
    engine_name: str
    stems: frozenset[str]
    license: str
    source_url: str
    files: tuple[ModelFile, ...]  # every checkpoint the model loads, pinned
    download_bytes: int
    requires: Requires
    recommend_on: frozenset[str]  # devices this model is recommended on
    min_vram_mb: int | None
    cpu_rtf: float  # seconds of work per second of audio
    gpu_rtf: float | None
    vram_mb: int | None
    note_key: str | None  # i18n key of the one-line note under the choice


# Kim's checkpoint, pinned to the Hugging Face commit that holds it. Revision,
# size and SHA-256 (the LFS oid) read from
# https://huggingface.co/api/models/KimberleyJSN/melbandroformer/tree/<revision>
# on 2026-11-23, and the X-Linked-Size / X-Linked-ETag of the resolve URL below,
# without downloading the file. The same SHA-256 and size as the contributor's
# fork and the maintainer's own copy.
KIM_REVISION = "ac9b0614ab3cd7f77219e18ba494dfd93956c348"
KIM_CHECKPOINT = ModelFile(
    url=(
        "https://huggingface.co/KimberleyJSN/melbandroformer/resolve/"
        f"{KIM_REVISION}/MelBandRoformer.ckpt"
    ),
    sha256="87201f4d31afb5bc79993230fc49446918425574db48c01c405e44f365c7559e",
    size=913_106_900,
    relpath="roformer/MelBandRoformer.ckpt",
)

# Demucs' own checkpoints, as torch.hub names and keeps them. Each name's part
# after the dash is the start of its SHA-256 (torch.hub's only check); the
# full SHA-256 and size were measured on 2026-11-23 from the files Meta serves
# at these addresses (htdemucs_6s's on 2026-11-12, model_download.py
# MIRRORED_FILES), so a file that only matches the prefix is never loaded.
_DEMUCS_HOST = "https://dl.fbaipublicfiles.com/demucs/hybrid_transformer/"


def _hub_file(name: str, sha256: str, size: int) -> ModelFile:
    return ModelFile(
        url=_DEMUCS_HOST + name, sha256=sha256, size=size, relpath=name, root="torch_hub"
    )


HTDEMUCS_6S_FILES = (
    _hub_file(
        "5c90dfd2-34c22ccb.th",
        "34c22ccb381c6f9fdbf324f04e1e2fe21aaaf293f5ded163a162697ff9a02ddd",
        54_996_327,
    ),
)
HTDEMUCS_FT_FILES = (
    _hub_file(
        "f7e0c4bc-ba3fe64a.th",
        "ba3fe64ae8ef66ac9a4857222ce48efbdc5eb3ad375cb79dd13debee5aaa4066",
        84_141_271,
    ),
    _hub_file(
        "d12395a8-e57c48e6.th",
        "e57c48e6b0e38af4f7118d7bd08c49f0a0c0edf7d09143bdd902ea0d237303e6",
        84_141_271,
    ),
    _hub_file(
        "92cfc3b6-ef3bcb9c.th",
        "ef3bcb9c8b40d14ae5d51b6db2587339cc12c6b77c0be151ce6d69002e087bf2",
        84_141_271,
    ),
    _hub_file(
        "04573f0d-f3cf25b2.th",
        "f3cf25b222c4eed7cd49dd8b2c9597d50c18bd154090f7b919cfa5f93cf22c49",
        84_141_271,
    ),
)

MODELS: dict[str, StemModel] = {
    m.id: m
    for m in (
        StemModel(
            id="htdemucs_6s",
            name="HT Demucs 6 stems",
            engine="demucs",
            engine_name="Demucs v4",
            stems=frozenset({"vocals", "drums", "bass", "guitar", "piano", "other"}),
            license="MIT",
            source_url="https://github.com/facebookresearch/demucs",
            files=HTDEMUCS_6S_FILES,
            download_bytes=sum(f.size for f in HTDEMUCS_6S_FILES),
            requires="torch",
            recommend_on=frozenset({"cuda", "mps", "cpu"}),
            min_vram_mb=None,
            cpu_rtf=0.50,
            gpu_rtf=0.068,
            vram_mb=619,
            note_key="settings.stemModels.note.htdemucs_6s",
        ),
        StemModel(
            id="htdemucs_ft",
            name="HT Demucs fine-tuned",
            engine="demucs",
            engine_name="Demucs v4",
            stems=frozenset({"drums", "bass", "other", "vocals"}),
            license="MIT",
            source_url="https://github.com/facebookresearch/demucs",
            files=HTDEMUCS_FT_FILES,
            download_bytes=sum(f.size for f in HTDEMUCS_FT_FILES),
            requires="torch",
            recommend_on=frozenset(),
            min_vram_mb=None,
            cpu_rtf=1.84,
            gpu_rtf=0.145,
            vram_mb=555,
            note_key="settings.stemModels.note.htdemucs_ft",
        ),
        StemModel(
            id="kim_mel_roformer",
            name="Mel-Band RoFormer (Kim)",
            engine="mel_roformer",
            engine_name="Mel-Band RoFormer",
            stems=frozenset({"vocals"}),
            # The license the model card declares.
            license="MIT",
            source_url="https://huggingface.co/KimberleyJSN/melbandroformer",
            files=(KIM_CHECKPOINT,),
            download_bytes=KIM_CHECKPOINT.size,
            requires="audio_separator",
            recommend_on=frozenset({"cuda"}),
            min_vram_mb=4096,
            cpu_rtf=3.40,
            # Measured 2.1 times the 6s time on an RTX 3080 (15.1 s against 7.2 s for a
            # 232.8 s song), scaled by the 6s figure below. One card: the speed test
            # corrects it on each machine.
            gpu_rtf=0.143,
            vram_mb=2017,
            note_key="settings.stemModels.note.kim_mel_roformer",
        ),
    )
}

# torch.hub's file names for the Demucs models (demucs/remote/files.txt).
_HUB_CHECKPOINTS: dict[str, tuple[str, ...]] = {
    m.id: tuple(f.relpath for f in m.files if f.root == "torch_hub")
    for m in MODELS.values()
    if any(f.root == "torch_hub" for f in m.files)
}


def hub_files() -> dict[str, ModelFile]:
    """Every pinned torch.hub checkpoint, by the address torch.hub fetches."""
    return {f.url: f for m in MODELS.values() for f in m.files if f.root == "torch_hub"}


# The stems a person chooses a model for. "other" is the residual and
# lead/backing the Karaoke 2 split, so neither is chosen.
SELECTABLE_STEMS: tuple[str, ...] = ("vocals", "drums", "bass", "guitar", "piano")

# What each stem may be separated with, in the order Settings lists them.
CHOICES: dict[str, tuple[str, ...]] = {
    "vocals": ("htdemucs_6s", "kim_mel_roformer"),
    "drums": ("htdemucs_6s", "htdemucs_ft"),
    "bass": ("htdemucs_6s", "htdemucs_ft"),
    "guitar": ("htdemucs_6s",),
    "piano": ("htdemucs_6s",),
}

# What "auto" picks, best first. htdemucs_ft is never recommended: 0.6 dB
# better drums and bass for 3.7 times the CPU time is the user's trade to make.
PREFERENCE: dict[str, tuple[str, ...]] = {
    "vocals": ("kim_mel_roformer", "htdemucs_6s"),
    "drums": ("htdemucs_6s",),
    "bass": ("htdemucs_6s",),
    "guitar": ("htdemucs_6s",),
    "piano": ("htdemucs_6s",),
}

# The stems nobody chooses a model for, and what makes them.
FIXED: dict[str, str] = {"lead_backing": "uvr_karaoke_2", "other": "residual"}

# The Demucs model a deployment pinned with STEMDECK_DEMUCS_MODEL. When it is
# not one of the models above, every stem is separated with it, as before this
# registry existed, and Settings shows the stems as set by the deployment.
BASE = DEMUCS_MODEL

# What a stem falls back to when no preference fits (an unexpected device).
_FALLBACK = "htdemucs_6s"

# The file audio-separator keeps the MelBandRoformer class in. An internal
# module: if a release moves it, Kim reads as unavailable rather than failing
# a job part way.
_ROFORMER_FILE = ("separator", "uvr_lib_v5", "roformer", "mel_band_roformer.py")
# The one beside it holding BSRoformer, which a model the person added may use.
_BS_ROFORMER_FILE = ("separator", "uvr_lib_v5", "roformer", "bs_roformer.py")
_ARCH_FILES: dict[str, tuple[str, ...]] = {
    "mel_band_roformer": _ROFORMER_FILE,
    "bs_roformer": _BS_ROFORMER_FILE,
}

# What runs a model the person added: a RoFormer pass on the mix, always.
USER_ENGINE: Engine = "user_roformer"
# The GPU real-time factor of a model the person added, which only its CPU
# dry run measured: that CPU figure times Kim's GPU-to-CPU ratio (both are
# RoFormers), served as an estimate.
_USER_GPU_PER_CPU = 0.143 / 3.40


def deployment_pinned() -> bool:
    """Whether the deployment's base model is outside the registry."""
    return BASE not in MODELS


def _package_dirs(name: str) -> list[Path]:
    """The folders of top-level package ``name``, found without importing it."""
    try:
        spec = importlib.util.find_spec(name)
    except (ImportError, ValueError):
        return []
    if spec is None or not spec.submodule_search_locations:
        return []
    return [Path(p) for p in spec.submodule_search_locations]


def _requirement_met(requires: str) -> bool:
    if requires == "torch":
        return bool(_package_dirs("torch")) and bool(_package_dirs("demucs"))
    if requires == "audio_separator":
        return roformer_arch_available("mel_band_roformer")
    return False


def roformer_arch_available(arch: str) -> bool:
    """Whether this install has torch and audio-separator's class file for
    ``arch`` ("mel_band_roformer" or "bs_roformer"). Never imports them."""
    parts = _ARCH_FILES.get(arch)
    if parts is None:
        return False
    try:
        if not _package_dirs("torch"):
            return False
        return any(d.joinpath(*parts).is_file() for d in _package_dirs("audio_separator"))
    except OSError:
        return False


def _user_models():
    """app.core.user_models, imported when first needed. The server is the
    only reader of that store; the worker never calls this."""
    from app.core import user_models

    return user_models


def is_user_model(model_id: object) -> bool:
    """Whether ``model_id`` has the shape of a model the person added. Shape
    only: whether it is there and can run is user_model_usable()."""
    return isinstance(model_id, str) and USER_MODEL_ID_RE.match(model_id) is not None


def engine_of(model_id: str) -> Engine:
    """What runs ``model_id``: the registry's engine for a built-in model,
    "user_roformer" for a model the person added, and "demucs" for any other
    id (a deployment's base model, STEMDECK_DEMUCS_MODEL)."""
    model = MODELS.get(model_id)
    if model is not None:
        return model.engine
    if is_user_model(model_id):
        return USER_ENGINE
    return "demucs"


def user_model_entry(model_id: str) -> dict | None:
    """The store's public record of a model the person added, or None when
    there is none by that id."""
    if not is_user_model(model_id):
        return None
    return _user_models().get(model_id)


def user_model_usable(model_id: str) -> bool:
    """Whether a model the person added can run now: checked, its file as
    recorded, its packages installed (the store decides), and the class file
    of its own architecture there too (the store checks MelBandRoformer's, the
    one Kim needs; a BS-RoFormer needs bs_roformer.py beside it)."""
    if not is_user_model(model_id):
        return False
    store = _user_models()
    if not store.usable(model_id):
        return False
    rec = store.record(model_id)
    return isinstance(rec, Mapping) and roformer_arch_available(str(rec.get("arch")))


def user_model_name(model_id: str) -> str | None:
    """The name of a model the person added, or None."""
    if not is_user_model(model_id):
        return None
    return _user_models().name_of(model_id)


def user_worker_spec(model_id: str) -> dict:
    """What the separation worker needs to run a model the person added, for
    its request line: the worker never reads the store itself."""
    return _user_models().worker_spec(model_id)


def user_model_rtf(model_id: str, device: str) -> float | None:
    """Seconds of work per second of audio for a model the person added on
    ``device``: what its CPU dry run measured, scaled by _USER_GPU_PER_CPU
    off the CPU. None when the store holds no measure."""
    entry = user_model_entry(model_id)
    rtf = _entry_cpu_rtf(entry) if isinstance(entry, Mapping) else None
    if rtf is None:
        return None
    return rtf if device == "cpu" else rtf * _USER_GPU_PER_CPU


def _positive(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if 0 < value < float("inf") else None


def choices_for(stem: str) -> tuple[str, ...]:
    """What ``stem`` may be separated with: the built-in choices, then the
    usable models the person added that declare it, in the order they were
    added. Empty for a stem that is not selectable. CHOICES itself never
    changes. A deployment that pins its own base model offers no added one."""
    built_in = CHOICES.get(stem)
    if built_in is None:
        return ()
    if deployment_pinned():
        return built_in
    added = tuple(m for m in _user_models().choices_for(stem) if m not in built_in)
    return built_in + added


def model_available(model_id: str) -> bool:
    """Whether this install can run ``model_id``: its packages are installed.

    Never imports them. Kim needs audio-separator's MelBandRoformer file, which
    Intel macOS does not have (audio-separator is not installed there). A
    model the person added is available while the store says it is usable."""
    model = MODELS.get(model_id)
    if model is None:
        return user_model_usable(model_id)
    try:
        return _requirement_met(model.requires)
    except OSError:
        return False


def hub_checkpoints_dir() -> Path:
    """Where torch.hub keeps checkpoints: <torch.hub.get_dir()>/checkpoints.
    Imports torch; the server has it imported already (the device probe)."""
    import torch

    return Path(torch.hub.get_dir()) / "checkpoints"


def model_path(model_file: ModelFile) -> Path:
    """Where ``model_file`` lives on disk."""
    if model_file.root == "torch_hub":
        return hub_checkpoints_dir() / model_file.relpath
    return MODELS_DIR.joinpath(*model_file.relpath.split("/"))


def model_downloaded(model_id: str) -> bool:
    """Whether ``model_id``'s weights are on disk already, so the next job
    downloads nothing for it. A file of the wrong size counts as missing."""
    model = MODELS.get(model_id)
    if model is None or not model.files:
        return False
    try:
        return all(
            model_path(f).is_file() and model_path(f).stat().st_size == f.size for f in model.files
        )
    except Exception:  # noqa: BLE001 -- Settings must answer; "not downloaded" is safe
        return False


def _vram_ok(model: StemModel, vram_mb: int | None) -> bool:
    # Unknown memory (ROCm, no nvidia-smi) is taken as enough: a card that is
    # too small fails loudly and the job retries on the CPU.
    return model.min_vram_mb is None or vram_mb is None or vram_mb >= model.min_vram_mb


def recommended(stem: str, device: str, vram_mb: int | None) -> str:
    """The model "auto" picks for ``stem`` on ``device``: the first preference
    that is available, recommended on that device, and fits its memory."""
    if deployment_pinned():
        return BASE
    for model_id in PREFERENCE.get(stem, ()):
        model = MODELS[model_id]
        if device in model.recommend_on and _vram_ok(model, vram_mb) and model_available(model_id):
            return model_id
    return _FALLBACK


def recommendation_reason(device: str, vram_mb: int | None) -> Reason:
    """Why the vocals recommendation is what it is, as one code."""
    if device == "mps":
        return "mps"
    if device != "cuda":
        return "cpu"
    kim = MODELS["kim_mel_roformer"]
    if not model_available(kim.id):
        return "unavailable"
    if not _vram_ok(kim, vram_mb):
        return "low-vram"
    return "gpu"


def resolve(
    choices: Mapping[str, object] | None, device: str, vram_mb: int | None
) -> dict[str, str]:
    """The model each selectable stem is separated with on ``device``.

    "auto", an unknown id, a model that does not supply that stem and one that
    is not available here all resolve to the recommendation, so a job always
    runs something this install has. A model the person added counts only
    while it is usable and declares that stem (choices_for). Always the five
    SELECTABLE_STEMS keys."""
    if deployment_pinned():
        return {stem: BASE for stem in SELECTABLE_STEMS}
    choices = choices if isinstance(choices, Mapping) else {}
    plan: dict[str, str] = {}
    for stem in SELECTABLE_STEMS:
        choice = choices.get(stem)
        if isinstance(choice, str) and choice in choices_for(stem) and model_available(choice):
            plan[stem] = choice
        else:
            plan[stem] = recommended(stem, device, vram_mb)
    return plan


def passes(plan: Mapping[str, str]) -> list[tuple[str, PassInput, tuple[str, ...]]]:
    """The separation passes ``plan`` needs, in the order they run.

    Each is (model id, what it separates, the stems taken from it). The
    RoFormer passes (Kim, and every model the person added) each run on the
    mix first; the Demucs models then run on the rest (the mix minus the
    stems those passes kept), or on the mix when no RoFormer pass ran. One
    pass per distinct model, stems in SELECTABLE_STEMS order. engine_of()
    decides: an id that is neither built in nor added by the person (a
    deployment's base model) is run as Demucs."""
    order: list[str] = []
    stems_of: dict[str, list[str]] = {}
    for stem in SELECTABLE_STEMS:
        model_id = plan.get(stem)
        if not isinstance(model_id, str):
            continue
        if model_id not in stems_of:
            order.append(model_id)
            stems_of[model_id] = []
        stems_of[model_id].append(stem)

    first = [m for m in order if engine_of(m) != "demucs"]
    demucs = [m for m in order if engine_of(m) == "demucs"]
    rest: PassInput = "rest" if first else "mix"
    return [(m, "mix", tuple(stems_of[m])) for m in first] + [
        (m, rest, tuple(stems_of[m])) for m in demucs
    ]


def catalog(
    *,
    lead_backing_available: bool,
    available: Callable[[str], bool] | None = None,
    downloaded: Callable[[str], bool] | None = None,
    include_user: bool = True,
) -> dict:
    """What Settings, Separation draws: every stem row and every model.

    "deployment" is the pinned base model when STEMDECK_DEMUCS_MODEL names one
    outside the registry, else None; every selectable stem is then fixed to it
    and no model the person added is offered. ``available`` and ``downloaded``
    default to model_available and model_downloaded, looked up when called.
    ``include_user`` False leaves the models the person added out: the
    payload Settings falls back to when working out the models failed.

    A model the person added is listed while it is usable, after the built-in
    choices of each stem it declares; one being checked or refused is in the
    My models list only. Its entry says "user": true, its declared stems, the
    licence its source declared (advisory, never checked), the CPU real-time
    factor its dry run measured, and a GPU one estimated from it
    ("estimated": true).
    """
    available = available or model_available
    downloaded = downloaded or model_downloaded
    pinned = deployment_pinned()
    added: list[dict] = []
    if include_user and not pinned:
        added = [
            e
            for e in _user_models().list_models()
            if e.get("usable") is True and is_user_model(e.get("id"))
        ]
    stems: list[dict] = []
    for stem in ("vocals", "lead_backing", "drums", "bass", "guitar", "piano", "other"):
        if stem == "lead_backing":
            stems.append(
                {"stem": stem, "fixed": FIXED[stem], "available": bool(lead_backing_available)}
            )
        elif stem == "other":
            stems.append({"stem": stem, "fixed": FIXED[stem]})
        elif pinned:
            stems.append({"stem": stem, "fixed": BASE})
        else:
            choices = list(CHOICES[stem])
            choices += [e["id"] for e in added if stem in _declared(e)]
            stems.append({"stem": stem, "choices": choices})
    models = {
        m.id: {
            "name": m.name,
            "user": False,
            # What runs it, and every stem it can supply (sorted).
            "engine": m.engine,
            "stems": sorted(m.stems),
            "license": m.license,
            "source_url": m.source_url,
            "download_bytes": m.download_bytes,
            "cpu_rtf": m.cpu_rtf,
            "gpu_rtf": m.gpu_rtf,
            "available": bool(available(m.id)),
            "downloaded": bool(downloaded(m.id)),
            "note_key": m.note_key,
        }
        for m in MODELS.values()
    }
    for entry in added:
        models[entry["id"]] = _user_catalog_entry(entry, bool(available(entry["id"])))
    return {
        "stems": stems,
        "models": models,
        "min_vram_mb": MODELS["kim_mel_roformer"].min_vram_mb,
        "deployment": BASE if pinned else None,
    }


def _declared(entry: Mapping) -> list[str]:
    """The selectable stems a store entry declares, in SELECTABLE_STEMS order."""
    stems = entry.get("stems")
    stems = stems if isinstance(stems, (list, tuple)) else ()
    return [s for s in SELECTABLE_STEMS if s in stems]


def _entry_cpu_rtf(entry: Mapping) -> float | None:
    """The CPU real-time factor a store entry's dry run measured: "cpu_rtf"
    in a public entry, "check"."cpu_rtf" in a stored one."""
    rtf = entry.get("cpu_rtf")
    if rtf is None and isinstance(entry.get("check"), Mapping):
        rtf = entry["check"].get("cpu_rtf")
    return _positive(rtf)


def _user_catalog_entry(entry: Mapping, is_available: bool) -> dict:
    """A usable model the person added, as catalog() lists it."""
    cpu_rtf = _entry_cpu_rtf(entry)
    source = entry.get("source")
    licence = source.get("license_declared") if isinstance(source, Mapping) else None
    size = entry.get("size")
    return {
        "name": str(entry.get("name") or entry["id"]),
        "user": True,
        "engine": USER_ENGINE,
        "stems": _declared(entry),
        "license": licence if isinstance(licence, str) and licence else None,
        "source_url": None,
        "download_bytes": 0,
        "size": size if isinstance(size, int) and not isinstance(size, bool) else None,
        "cpu_rtf": cpu_rtf,
        "gpu_rtf": cpu_rtf * _USER_GPU_PER_CPU if cpu_rtf is not None else None,
        "estimated": True,
        "available": is_available,
        "downloaded": True,
        "note_key": None,
    }
