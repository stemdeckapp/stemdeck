"""The vendored note engine in app/_vendor/transcribe.

Five PyPI wheels unpacked as they ship (README.md there says how), checked
here file by file against each wheel's own RECORD, so an edit to vendored
code, a line-ending rewrite by git or a partial refresh fails the suite. And
the engine stays out of the server process: only the worker imports it.

The second piano engine (app/_vendor/piano_hr) is two sdists with a patch:
every file is checked against manifest.json, and the patch taken out again
gives the sdists' own bytes back.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

from app.pipeline import midi_file as mf
from app.pipeline import piano_hr

VENDOR = mf.VENDOR_DIR
EXPECTED = {
    # name: (version, wheel sha256 as PyPI publishes it, licence)
    "basic-pitch": (
        "0.4.0",
        "738adb503aae7fdfc7d1e1511aa0ce35052315f260a19531ef4c356708425db0",
        "Apache-2.0",
    ),
    "pretty-midi": (
        "0.2.11.post0",
        "a3e2321736245a9c92259716d85a4762f16b7bde2f591fb0dba95bdb6e793d8a",
        "MIT",
    ),
    "mir-eval": (
        "0.8.2",
        "114cda33d8e17408c170598e0b36ed0d71ff4a2fee8eaf9e165b58ecf1c87170",
        "MIT",
    ),
    "mido": (
        "1.3.3",
        "01033c9b10b049e4436fca2762194ca839b09a4334091dd3c34e7f4ae674fd8a",
        "MIT",
    ),
    "importlib-resources": (
        "7.1.0",
        "1bd7b48b4088eddb2cd16382150bb515af0bd2c70128194392725f82ad2c96a1",
        "Apache-2.0",
    ),
}


def _manifest() -> dict:
    return json.loads((VENDOR / "manifest.json").read_text(encoding="utf-8"))


def _record_hash(data: bytes) -> str:
    digest = hashlib.sha256(data).digest()
    return "sha256=" + base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def test_the_manifest_names_the_exact_wheels():
    manifest = _manifest()
    packages = {p["name"]: p for p in manifest["packages"]}
    assert set(packages) == set(EXPECTED)
    for name, (version, sha, licence) in EXPECTED.items():
        p = packages[name]
        assert (p["version"], p["sha256"], p["licence"]) == (version, sha, licence)
        assert p["url"].startswith("https://files.pythonhosted.org/packages/")
        assert p["url"].endswith("/" + p["wheel"])
    ident = hashlib.sha256("".join(p["sha256"] for p in manifest["packages"]).encode())
    assert manifest["id"] == ident.hexdigest()[:16] == mf.vendor_id()


def test_every_vendored_file_matches_its_wheel_record():
    manifest = _manifest()
    listed: set[Path] = {VENDOR / "manifest.json", VENDOR / "README.md"}
    for package in manifest["packages"]:
        dist = VENDOR / package["dist_info"]
        removed = set(package["removed"])
        for line in (dist / "RECORD").read_text(encoding="utf-8").splitlines():
            name, digest, _size = line.rsplit(",", 2)
            path = VENDOR / name
            if name in removed:
                assert not path.exists(), f"{name} was left out on purpose"
                continue
            listed.add(path)
            assert path.is_file(), f"{name} is missing"
            if digest:
                assert _record_hash(path.read_bytes()) == digest, f"{name} was changed"
    present = {p for p in VENDOR.rglob("*") if p.is_file() and "__pycache__" not in p.parts}
    assert present - listed == set(), "files that are not in any wheel"


def test_left_out_on_purpose():
    # The soundfont (licence unstated, 6 MB) and every model but the ONNX one.
    assert not list(VENDOR.rglob("*.sf2"))
    models = VENDOR / "basic_pitch" / "saved_models" / "icassp_2022"
    assert sorted(p.name for p in models.iterdir()) == ["nmp.onnx"]
    assert not (VENDOR / "importlib_resources" / "tests").exists()


def test_licences_travel_with_the_code():
    for rel in (
        "basic_pitch-0.4.0.dist-info/LICENSE",
        "basic_pitch-0.4.0.dist-info/NOTICE",
        "pretty_midi-0.2.11.post0.dist-info/licenses/LICENSE.txt",
        "mir_eval-0.8.2.dist-info/LICENSE.txt",
        "mido-1.3.3.dist-info/LICENSE",
        "importlib_resources-7.1.0.dist-info/licenses/LICENSE",
    ):
        assert (VENDOR / rel).is_file(), rel
    readme = (VENDOR / "README.md").read_text(encoding="utf-8")
    for name, (version, sha, _licence) in EXPECTED.items():
        assert version in readme and sha[:16] in readme, name


def _isolated_env(tmp_path: Path) -> dict[str, str]:
    """The subprocess's library, data and cache folders: a temp dir, never
    the one this machine's app uses."""
    env = dict(os.environ)
    for key, sub in (
        ("STEMDECK_JOBS_DIR", "jobs"),
        ("STEMDECK_DATA_DIR", "data"),
        ("STEMDECK_CACHE_DIR", "cache"),
    ):
        env[key] = str(tmp_path / sub)
    return env


def test_the_server_process_cannot_import_the_engine(tmp_path):
    """app/__init__ puts app/_vendor on sys.path; the engine is one folder
    further in, so `import basic_pitch` fails in the server. Checked in a
    fresh interpreter, since this one may have run the writer tests."""
    code = textwrap.dedent(
        """
        import importlib.util
        import app.api.router  # noqa: F401
        print(all(importlib.util.find_spec(n) is None
                  for n in ("basic_pitch", "pretty_midi", "mir_eval", "mido",
                            "piano_transcription_inference", "torchlibrosa")))
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


def test_the_engine_imports_from_the_vendored_copy_alone():
    """With only the vendored folder added to an isolated interpreter's path,
    every package resolves there, TensorFlow and friends are never tried, and
    the explicit model path loads as ONNX."""
    code = textwrap.dedent(
        f"""
        import sys
        for name in ("tensorflow", "coremltools", "tflite_runtime"):
            sys.modules[name] = None
        sys.path.insert(0, {str(VENDOR)!r})
        import logging
        logging.disable(logging.WARNING)
        import basic_pitch, pretty_midi, mir_eval, mido, importlib_resources
        from basic_pitch import inference
        for m in (basic_pitch, pretty_midi, mir_eval, mido, importlib_resources):
            assert m.__file__.startswith({str(VENDOR)!r}), m.__file__
        assert not basic_pitch.TF_PRESENT and basic_pitch.ONNX_PRESENT
        model = inference.Model({str(mf.MODEL_PATH)!r})
        assert model.model_type == inference.Model.MODEL_TYPES.ONNX
        print("ok")
        """
    )
    out = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=180
    )
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip().splitlines()[-1] == "ok"


# -- the second piano engine, app/_vendor/piano_hr (its README says how)

PIANO_HR = piano_hr.VENDOR_DIR
EXPECTED_PIANO_HR = {
    # name: (version, sdist sha256 as PyPI publishes it, licence)
    "piano-transcription-inference": (
        "0.0.6",
        "b6dd00f9b4bcacb6140725f03b4139a5e0f4acd35a69e492a5d1f734ecfbd231",
        "MIT",
    ),
    "torchlibrosa": (
        "0.1.0",
        "62a8beedf9c9b4141a06234df3f10229f7ba86e67678ccee02489ec4ef044028",
        "MIT",
    ),
}
# The only files the patch may touch, and so the only ones that differ.
PATCHED = {
    "piano_transcription_inference/__init__.py",
    "piano_transcription_inference/models.py",
    "piano_transcription_inference/utilities.py",
}


def _hr_manifest() -> dict:
    return json.loads((PIANO_HR / "manifest.json").read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _patch_sections(text: str) -> dict[str, list[str]]:
    """{path: its diff lines} of a unified diff, one entry per file."""
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in text.splitlines(keepends=True):
        if line.startswith("--- a/"):
            current = None
        elif line.startswith("+++ b/"):
            current = sections.setdefault(line[6:].rstrip("\r\n"), [])
        elif current is not None:
            current.append(line)
    return sections


def _unpatch(patched: str, diff: list[str]) -> str:
    """``patched`` with the diff taken out again: each hunk's new lines (which
    must be there, exactly) replaced by its old ones. A "No newline at end of
    file" marker takes the newline off the line before it."""
    lines = patched.splitlines(keepends=True)
    out: list[str] = []
    pos = 0
    i = 0
    while i < len(diff):
        m = _HUNK_RE.match(diff[i])
        assert m, diff[i]
        new_start, new_len = int(m[3]), int(m[4] if m[4] is not None else 1)
        start = new_start - 1 if new_len else new_start
        out += lines[pos:start]
        pos = start
        old: list[str] = []
        new: list[str] = []
        last = None
        i += 1
        while i < len(diff) and not diff[i].startswith("@@"):
            line = diff[i]
            kind = line[0]
            if kind == "\\":
                if last in (" ", "-"):
                    old[-1] = old[-1].rstrip("\n")
                if last in (" ", "+"):
                    new[-1] = new[-1].rstrip("\n")
            else:
                if kind in (" ", "-"):
                    old.append(line[1:])
                if kind in (" ", "+"):
                    new.append(line[1:])
                last = kind
            i += 1
        assert lines[pos : pos + len(new)] == new, "the patch does not match the file"
        out += old
        pos += len(new)
    return "".join(out + lines[pos:])


def test_piano_hr_manifest_names_the_exact_sdists():
    manifest = _hr_manifest()
    packages = {p["name"]: p for p in manifest["packages"]}
    assert set(packages) == set(EXPECTED_PIANO_HR)
    for name, (version, sha, licence) in EXPECTED_PIANO_HR.items():
        p = packages[name]
        assert (p["version"], p["sha256"], p["licence"]) == (version, sha, licence)
        assert p["url"].startswith("https://files.pythonhosted.org/packages/")
        assert p["url"].endswith("/" + p["sdist"])
    ident = hashlib.sha256("".join(p["sha256"] for p in manifest["packages"]).encode())
    assert manifest["id"] == ident.hexdigest()[:16] == piano_hr.vendor_id()
    assert piano_hr.engine_version() == "0.0.6"


def test_every_piano_hr_file_matches_the_manifest():
    manifest = _hr_manifest()
    patch = PIANO_HR / manifest["patch"]
    assert _sha256(patch) == manifest["patch_sha256"]
    listed = {PIANO_HR / "manifest.json", PIANO_HR / "README.md", patch}
    for package in manifest["packages"]:
        for rel, sums in package["files"].items():
            path = PIANO_HR / rel
            listed.add(path)
            assert path.is_file(), f"{rel} is missing"
            assert _sha256(path) == sums["sha256"], f"{rel} was changed"
            if rel in PATCHED:
                assert sums["sha256"] != sums["sdist_sha256"], rel
            else:
                assert sums["sha256"] == sums["sdist_sha256"], f"{rel} differs from the sdist"
        assert set(package["patched"]) <= PATCHED
        for rel in package.get("removed", {}):
            # Left out on purpose (README.md): the class that downloads with
            # wget, eval()s its model name and loads without weights_only.
            assert not (PIANO_HR / rel).exists(), f"{rel} was left out on purpose"
        licence = package.get("licence_file")
        if licence:
            path = PIANO_HR / licence["path"]
            listed.add(path)
            assert _sha256(path) == licence["sha256"]
    present = {p for p in PIANO_HR.rglob("*") if p.is_file() and "__pycache__" not in p.parts}
    assert present - listed == set(), "files that are not in the manifest"


def test_the_patch_is_all_that_changed():
    """Taking the patch out of each patched file gives the sdist's own bytes
    back, so the files differ from the sdists by exactly the patch."""
    manifest = _hr_manifest()
    sections = _patch_sections((PIANO_HR / manifest["patch"]).read_text(encoding="utf-8"))
    assert set(sections) == PATCHED
    files = {rel: s for p in manifest["packages"] for rel, s in p["files"].items()}
    for rel, diff in sections.items():
        patched = (PIANO_HR / rel).read_bytes().decode("utf-8")
        original = _unpatch(patched, diff).encode("utf-8")
        assert hashlib.sha256(original).hexdigest() == files[rel]["sdist_sha256"], rel
    package = PIANO_HR / "piano_transcription_inference"
    assert not (package / "inference.py").exists()
    manifest_removed = {r for p in manifest["packages"] for r in p.get("removed", {})}
    assert manifest_removed == {"piano_transcription_inference/inference.py"}
    models = (package / "models.py").read_text(encoding="utf-8")
    utilities = (package / "utilities.py").read_text(encoding="utf-8")
    assert "matplotlib" not in models
    assert "audioread" not in utilities and "def load_audio" not in utilities


def test_piano_hr_licences_travel_with_the_code():
    for rel in (
        "piano_transcription_inference-0.0.6.sdist/LICENSE",
        "piano_transcription_inference-0.0.6.sdist/PKG-INFO",
        "torchlibrosa-0.1.0.sdist/LICENSE.md",
        "torchlibrosa-0.1.0.sdist/PKG-INFO",
    ):
        assert (PIANO_HR / rel).is_file(), rel
    licence = PIANO_HR / "piano_transcription_inference-0.0.6.sdist" / "LICENSE"
    assert "MIT License" in licence.read_text(encoding="utf-8")
    readme = (PIANO_HR / "README.md").read_text(encoding="utf-8")
    for name, (version, sha, _licence) in EXPECTED_PIANO_HR.items():
        assert version in readme and sha in readme, name


def test_the_piano_hr_engine_imports_from_the_vendored_copy_alone():
    """In an isolated interpreter with matplotlib blocked, as the worker
    blocks it: both packages resolve to the vendored copies and the model
    builds (fresh weights; the checkpoint is not needed for that)."""
    code = textwrap.dedent(
        f"""
        import sys
        sys.modules["matplotlib"] = None
        sys.path.insert(0, {str(VENDOR)!r})
        sys.path.insert(0, {str(PIANO_HR)!r})
        import piano_transcription_inference, torchlibrosa
        from piano_transcription_inference.models import Note_pedal
        from piano_transcription_inference.utilities import RegressionPostProcessor
        for m in (piano_transcription_inference, torchlibrosa):
            assert m.__file__.startswith({str(PIANO_HR)!r}), m.__file__
        model = Note_pedal(frames_per_second=100, classes_num=88)
        assert sum(p.numel() for p in model.parameters()) > 10_000_000
        print("ok")
        """
    )
    out = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=300
    )
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip().splitlines()[-1] == "ok"
