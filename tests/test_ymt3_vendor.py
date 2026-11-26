"""The vendored YourMT3+ code in app/_vendor/ymt3 (its README says how).

Files of the Hugging Face Space mimbres/YourMT3 at a pinned revision, checked
here file by file against manifest.json and against their git blob ids in
that revision, two of them with a patch that, taken out again, gives the
Space's own bytes back; stand-ins for transformers and Lightning beside them.
An edit to vendored code, a line-ending rewrite by git or a partial refresh
fails the suite. And the engine stays out of the server process: only the
worker imports it.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import textwrap
from pathlib import Path

from app.pipeline import ymt3
from app.pipeline.midi_file import VENDOR_DIR as BASIC_PITCH_VENDOR
from tests.test_midi_vendor import _isolated_env, _patch_sections, _sha256, _unpatch

YMT3 = ymt3.VENDOR_DIR
SPACE_REVISION = "5e66c1ea173a8186e0d20432b841d3180cc015b5"
TRANSFORMERS_WHEEL = "21e3f47aa7256dbbfb5215937a3168a984c94432ce3a16b7908265807d62aee8"
# The only files the patch may touch, and so the only ones that differ.
PATCHED = {"src/model/ymt3.py", "src/utils/utils.py"}


def _manifest() -> dict:
    return json.loads((YMT3 / "manifest.json").read_text(encoding="utf-8"))


def _git_blob(data: bytes) -> str:
    """git's id of a file's bytes, as the Space's tree lists it."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()  # noqa: S324


def test_the_manifest_names_the_exact_sources():
    manifest = _manifest()
    assert manifest["source"]["revision"] == SPACE_REVISION
    assert manifest["source"]["licence"] == "Apache-2.0"
    assert manifest["github"]["licence"] == "GPL-3.0"
    shim = manifest["shim"]["transformers"]
    assert (shim["version"], shim["wheel_sha256"]) == ("4.45.1", TRANSFORMERS_WHEEL)
    assert shim["url"].endswith("/" + shim["wheel"])
    listed = sorted(
        (rel, entry["sha256"])
        for group in (
            manifest["files"],
            manifest["added"],
            manifest["shim"]["files"],
            manifest["licences"],
        )
        for rel, entry in group.items()
    )
    ident = hashlib.sha256("".join(f"{rel}:{sha}\n" for rel, sha in listed).encode())
    assert manifest["id"] == ident.hexdigest()[:16] == ymt3.vendor_id()
    assert ymt3.engine_version() == SPACE_REVISION[:7]


def test_every_file_matches_the_manifest():
    """Each file is the one the manifest names; each unpatched one is, byte
    for byte, the Space's file at the pinned revision (its git blob id); and
    nothing is here that the manifest does not list."""
    manifest = _manifest()
    patch = YMT3 / manifest["patch"]
    assert _sha256(patch) == manifest["patch_sha256"]
    listed = {YMT3 / "manifest.json", YMT3 / "README.md", patch}
    for rel, entry in manifest["files"].items():
        path = YMT3 / rel
        listed.add(path)
        data = path.read_bytes()
        assert hashlib.sha256(data).hexdigest() == entry["sha256"], f"{rel} was changed"
        assert entry["upstream"] == "amt/" + rel
        if rel in PATCHED:
            assert entry["sha256"] != entry["upstream_sha256"], rel
        else:
            assert entry["sha256"] == entry["upstream_sha256"], f"{rel} differs from the Space"
            assert _git_blob(data) == entry["git_blob"], rel
    assert set(manifest["patched"]) == PATCHED
    for group in (manifest["added"], manifest["shim"]["files"], manifest["licences"]):
        for rel, entry in group.items():
            path = YMT3 / rel
            listed.add(path)
            assert _sha256(path) == entry["sha256"], f"{rel} was changed"
    for rel in manifest["added"]:
        # Only the empty package markers README.md explains.
        assert rel.endswith("__init__.py") and (YMT3 / rel).read_bytes() == b"", rel
    present = {p for p in YMT3.rglob("*") if p.is_file() and "__pycache__" not in p.parts}
    assert present - listed == set(), "files that are not in the manifest"


def test_the_patch_is_all_that_changed():
    """Taking the patch out of each patched file gives the Space's own bytes
    back (its git blob id), so the files differ by exactly the patch, and
    what the patch took out is gone."""
    manifest = _manifest()
    sections = _patch_sections((YMT3 / manifest["patch"]).read_text(encoding="utf-8"))
    assert set(sections) == PATCHED
    for rel, diff in sections.items():
        patched = (YMT3 / rel).read_bytes().decode("utf-8")
        original = _unpatch(patched, diff).encode("utf-8")
        assert hashlib.sha256(original).hexdigest() == manifest["files"][rel]["upstream_sha256"]
        assert _git_blob(original) == manifest["files"][rel]["git_blob"], rel
    model = (YMT3 / "src" / "model" / "ymt3.py").read_text(encoding="utf-8")
    for gone in (
        "wandb",
        "def training_step",
        "def configure_optimizers",
        "ConformerYMT3",
        "AMTMetrics",
    ):
        assert gone not in model, gone
    utils = (YMT3 / "src" / "utils" / "utils.py").read_text(encoding="utf-8")
    for gone in ("requests", "tarfile", "download_and_extract", "tqdm"):
        assert gone not in utils, gone


def test_the_vendored_code_has_no_downloader_and_runs_nothing():
    """No network, shell, eval, pickle or checkpoint loading anywhere in the
    vendored folders (the patch took out the one branch that would have
    fetched a T5 config from the Hugging Face hub)."""
    pattern = re.compile(
        r"\b(requests|urllib|socket|subprocess|os\.system|(?<![.\w])eval\(|exec\(|pickle|wget|"
        r"torch\.load|hf_hub|from_pretrained\()"
    )
    found = []
    for path in sorted(YMT3.rglob("*.py")):
        for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if pattern.search(code):
                found.append(f"{path.relative_to(YMT3).as_posix()}:{no}: {code.strip()}")
    assert found == []


def test_licences_travel_with_the_code():
    for name in (
        "LICENSE.yourmt3",
        "LICENSE.transformers",
        "LICENSE.rotary-embedding-torch",
        "LICENSE.x-transformers",
    ):
        assert (YMT3 / name).is_file(), name
    yourmt3 = (YMT3 / "LICENSE.yourmt3").read_text(encoding="utf-8")
    assert "Apache License" in yourmt3 and "GPL-3.0" in yourmt3
    for name in ("LICENSE.rotary-embedding-torch", "LICENSE.x-transformers"):
        assert "MIT License" in (YMT3 / name).read_text(encoding="utf-8")
    readme = (YMT3 / "README.md").read_text(encoding="utf-8")
    assert SPACE_REVISION in readme and "4.45.1" in readme
    for shim in (YMT3 / "shim").rglob("*.py"):
        # Each stand-in says what it is and where its copied code came from.
        assert "StemDeck stand-in" in shim.read_text(encoding="utf-8"), shim


def test_the_server_process_cannot_import_the_engine(tmp_path):
    code = textwrap.dedent(
        """
        import importlib.util, sys
        import app.api.router  # noqa: F401
        import app.pipeline.ymt3  # noqa: F401
        print(all(importlib.util.find_spec(n) is None
                  for n in ("transformers", "pytorch_lightning"))
              and "model.ymt3" not in sys.modules)
        """
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
        env=_isolated_env(tmp_path),
    )
    assert out.stdout.strip().splitlines()[-1] == "True", out.stderr[-2000:]


def test_the_model_builds_from_the_vendored_copy_alone():
    """In an isolated interpreter with the worker's own path and blocks: the
    model builds from the vendored code and the stand-ins only (no real
    transformers or Lightning), every module of those names comes from the
    vendored folders, and it holds the 655 tensors and 47,081,642 values of
    the pinned weights file (scripts/ymt3_weights.py), so they load strictly."""
    root = Path(__file__).resolve().parent.parent
    code = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(root)!r})
        from app.pipeline import midi_worker, ymt3
        folders = [ymt3.VENDOR_DIR / "shim", ymt3.VENDOR_DIR / "src"]
        midi_worker._install_engine(ymt3.Path({str(BASIC_PITCH_VENDOR)!r}), folders, "ymt3")
        midi_worker._import_engine("ymt3", folders)
        state = ymt3.build_model().state_dict()
        print(len(state), sum(t.numel() for t in state.values()))
        import transformers, pytorch_lightning
        bad = [m for m in sys.modules if m.split(".")[0] in ("lightning", "wandb", "torchmetrics")
               and sys.modules[m] is not None]
        print(bad == [] and "shim" in transformers.__file__ and "shim" in pytorch_lightning.__file__)
        """
    )
    out = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=600
    )
    assert out.returncode == 0, out.stderr[-3000:]
    lines = out.stdout.strip().splitlines()
    assert lines[-2:] == ["655 47081642", "True"]
