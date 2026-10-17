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
