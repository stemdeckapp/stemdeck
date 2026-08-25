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
        # Source-fetch failures (#434). Strings taken verbatim from the yt-dlp
        # output in the issue rather than paraphrased.
        ("ERROR: [youtube] abc: Sign in to confirm you're not a bot.", "source-blocked"),
        ("HTTP Error 429: Too Many Requests", "source-blocked"),
        ("ERROR: [youtube] abc: Requested format is not available", "source-unavailable"),
        ("WARNING: Only images are available for download.", "source-unavailable"),
        ("n challenge solving failed: Some formats may be missing.", "source-unavailable"),
        ("ERROR: [youtube] abc: Video unavailable", "source-unavailable"),
        (
            "ERROR: [youtube] abc: Private video. Sign in if you've been granted access",
            "source-unavailable",
        ),
    ],
)
def test_classify_failure(text: str, expected: str):
    assert classify_failure(text) == expected


def test_resource_causes_win_over_source_causes():
    """A disk-full that happens mid-download is a disk-full, not a fetch
    failure: the resource-level patterns are ordered first for exactly this."""
    text = "ERROR: unable to write; OSError: [Errno 28] No space left on device"
    assert classify_failure(text) == "disk-full"


def test_bot_check_matches_regardless_of_apostrophe():
    """yt-dlp has changed the quoting of this message between releases, so the
    pattern deliberately stops before the apostrophe."""
    for variant in ("you're not a bot", "you’re not a bot", "you are not a bot"):
        assert classify_failure(f"Sign in to confirm {variant}.") == "source-blocked"


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
