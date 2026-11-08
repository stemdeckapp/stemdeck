"""scripts/encoder_matrix.py stays dependency free, so it carries its own copy
of the app's encoder arguments. These tests keep the copy identical to
app/pipeline/video_encoder.py (and the CPU arguments to take_render's), so the
matrix measures exactly what the app would run."""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

from app.core.config import VIDEO_GPU_ENCODERS
from app.pipeline import take_render
from app.pipeline import video_encoder as ve

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "encoder_matrix.py"


def _load():
    spec = importlib.util.spec_from_file_location("encoder_matrix", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


matrix = _load()

GEOMETRIES = [
    (1280, 720, 30),
    (1920, 1080, 30),
    (1080, 1920, 60),
    (3840, 2160, 30),
    (2160, 2160, 24),
]


@pytest.mark.parametrize("encoder", VIDEO_GPU_ENCODERS)
@pytest.mark.parametrize(("w", "h", "fps"), GEOMETRIES)
def test_matrix_runs_the_apps_gpu_arguments(encoder, w, h, fps):
    for variant in range(ve.variant_count(encoder)):
        assert matrix.encoder_args(encoder, w, h, fps, variant) == ve.encoder_args(
            encoder, w, h, fps, variant
        )
    assert matrix.variant_count(encoder) == ve.variant_count(encoder)
    assert matrix.output_filter(encoder) == ve.output_filter(encoder)


# The app renders 720 and 1080 only (no 4K since 0.21.0); the script still
# measures 4K with its own veryfast preset, as a diagnostic.
@pytest.mark.parametrize(("w", "h", "_fps"), [g for g in GEOMETRIES if min(g[0], g[1]) <= 1080])
def test_matrix_runs_the_apps_cpu_arguments(w, h, _fps):
    assert matrix.libx264_args(w, h) == take_render.libx264_args(w, h)


@pytest.mark.parametrize("forced", ["", "renderD129", "/dev/dri/renderD130", "card0"])
def test_matrix_tries_the_apps_vaapi_nodes_in_its_order(tmp_path, monkeypatch, forced):
    # Issue #995: the app tries every render node in number order; the matrix
    # must test the same nodes, or a hybrid-GPU report says nothing.
    from types import SimpleNamespace

    from app.core import config

    root = tmp_path / "dri"
    root.mkdir()
    for name in ("renderD1000", "renderD129", "renderD128", "card0"):
        (root / name).write_bytes(b"")
    monkeypatch.setattr(ve, "DRI_DIR", root)
    monkeypatch.setattr(matrix, "DRI_DIR", root)
    monkeypatch.setattr(ve, "_host_platform", lambda: "linux")
    monkeypatch.setattr(matrix, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setenv("STEMDECK_VAAPI_DEVICE", forced)
    monkeypatch.setattr(
        ve, "VAAPI_DEVICE_OVERRIDE", config._env_render_node("STEMDECK_VAAPI_DEVICE")
    )
    assert matrix.render_nodes() == ve.render_nodes()
    for node in ve.render_nodes():
        assert matrix.global_args("h264_vaapi", node) == ve.global_args("h264_vaapi", node)


def test_matrix_names_the_same_gpu_behind_each_node_as_the_app(tmp_path, monkeypatch):
    # Issue #1033: the app keys its probe cache on these ids; the matrix
    # prints them, so a hybrid-GPU report says which GPU each node is.
    from types import SimpleNamespace

    root = tmp_path / "sys"
    for name, vendor, device in (
        ("renderD128", "0x8086\n", "0x46A6\n"),
        ("renderD129", "0x10de\n", None),
        ("renderD130", "junk", "0x1234567"),
    ):
        base = root / name / "device"
        base.mkdir(parents=True)
        (base / "vendor").write_text(vendor, encoding="ascii")
        if device is not None:
            (base / "device").write_text(device, encoding="ascii")
    monkeypatch.setattr(ve, "SYS_DRM_DIR", root)
    monkeypatch.setattr(matrix, "SYS_DRM_DIR", root)
    monkeypatch.setattr(ve, "_host_platform", lambda: "linux")
    monkeypatch.setattr(matrix, "sys", SimpleNamespace(platform="linux"))
    for n, want in ((128, "8086:46a6"), (129, "10de:"), (130, ""), (131, "")):
        node = f"/dev/dri/renderD{n}"
        assert matrix.gpu_id(node) == ve.gpu_id(node) == want, node


def test_matrix_candidates_are_the_apps():
    assert matrix.CANDIDATES == ve.CANDIDATES
    assert matrix.GPU_MAX_BITS_PER_PIXEL == ve.VIDEO_GPU_MAX_BITS_PER_PIXEL


def test_matrix_imports_only_the_standard_library():
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    stdlib = {
        "__future__",
        "argparse",
        "json",
        "os",
        "platform",
        "re",
        "shutil",
        "subprocess",
        "sys",
        "tempfile",
        "time",
        "pathlib",
    }
    assert names <= stdlib, names - stdlib


def test_matrix_never_crashes_on_a_missing_ffmpeg_or_encoder(tmp_path, capsys):
    assert matrix.main(["--ffmpeg", str(tmp_path / "no-ffmpeg"), "--sizes", "720p"]) in (0, 2)
    rows = [
        {"encoder": "h264_nvenc", "size": "720p", "ok": False, "error": "Unknown encoder"},
        {"encoder": "libx264", "size": "720p", "ok": True, "speed": 2.0, "ssim": 0.9, "psnr": 40.0},
    ]
    text = matrix.table(rows)
    assert "FAIL" in text and "pass" in text
    assert matrix.table([]).startswith("encoder")
    assert matrix.error_line("[enc @ 0x1f] Unknown encoder 'h264_x'\nConversion failed!") == (
        "Unknown encoder 'h264_x'"
    )
