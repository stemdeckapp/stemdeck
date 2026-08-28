import os
from unittest.mock import patch

from app.pipeline import warmup


def test_section_warmup_loads_cpu_model():
    with patch("allin1_infer.models.load_pretrained_model") as load:
        warmup._warm_sections()

    load.assert_called_once_with(model_name=warmup.SECTION_MODEL, device="cpu")


def test_warmup_continues_after_individual_failure(monkeypatch, capsys):
    calls = []

    def fail():
        calls.append("fail")
        raise RuntimeError("offline")

    def succeed():
        calls.append("succeed")

    monkeypatch.setattr(warmup, "_STEPS", (("sections", fail), ("demucs", succeed)))

    assert warmup.main() == 0
    assert calls == ["fail", "succeed"]
    assert capsys.readouterr().out.splitlines() == [
        "WARMUP_FAILED sections offline",
        "WARMUP_OK demucs",
    ]


def test_section_warmup_disables_hugging_face_symlinks(monkeypatch):
    """Unelevated Windows cannot create the cache symlinks the hub prefers.

    Stubbed rather than patched through the real package so the guarantee is
    checked even where the optional inference dependency is not installed.
    """
    import sys
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
