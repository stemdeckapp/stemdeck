"""Tests for the pipeline failure classifier and SeparationError (#294, #277)."""

from __future__ import annotations

import pytest

from app.pipeline.errors import SeparationError, classify_failure


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("RuntimeError: CUDA out of memory. Tried to allocate 2.00 GiB", "out-of-memory"),
        ("RuntimeError: MPS backend out of memory (MPS allocated: 5.2 GB)", "out-of-memory"),
        ("OSError: cannot allocate memory", "out-of-memory"),
        ("RuntimeError: no kernel image is available for execution", "unsupported-device"),
        ("AssertionError: Torch not compiled with CUDA enabled", "unsupported-device"),
        ("OSError: [Errno 28] No space left on device", "disk-full"),
        ("Invalid data found when processing input", "bad-input"),
        ("RuntimeError: no stems produced by demucs", "bad-input"),
        ("something entirely novel went wrong", "unknown"),
        ("", "unknown"),
    ],
)
def test_classify_failure(text: str, expected: str):
    assert classify_failure(text) == expected


def test_classify_is_case_insensitive():
    assert classify_failure("CUDA OUT OF MEMORY") == "out-of-memory"


def test_separation_error_carries_evidence():
    err = SeparationError("demucs failed: boom", tail=["line1", "boom"], device="mps")
    assert isinstance(err, RuntimeError)
    assert err.tail == ["line1", "boom"]
    assert err.device == "mps"


def test_separation_error_defaults():
    err = SeparationError("plain")
    assert err.tail == []
    assert err.device is None
