"""The vendored note engine in app/_vendor/transcribe.

Five PyPI wheels unpacked as they ship (README.md there says how), checked
here file by file against each wheel's own RECORD, so an edit to vendored
code, a line-ending rewrite by git or a partial refresh fails the suite. And
the engine stays out of the server process: only the worker imports it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

from app.pipeline import midi_file as mf

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
                  for n in ("basic_pitch", "pretty_midi", "mir_eval", "mido")))
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
