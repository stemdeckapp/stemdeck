"""A truncated checkpoint must not be trusted forever (#502).

torch.hub and audio-separator both check only that a cached model file exists,
never that it loads, so a connection that drops mid-download leaves a file that
poisons every later run. The symptom is silent: beat detection quietly falls
back to librosa and nothing tells the user why.
"""

from __future__ import annotations

import pytest

import app.core.model_cache as model_cache
from app.core.model_cache import load_or_heal


@pytest.fixture(autouse=True)
def _reset_healed():
    """The once-per-process guard is module state and would leak between tests."""
    model_cache._healed.clear()
    yield
    model_cache._healed.clear()


def test_a_good_load_is_left_alone(tmp_path):
    """The happy path must not go near the cache, or touch the disk at all."""
    artifact = tmp_path / "model.ckpt"
    artifact.write_bytes(b"complete")
    calls = []

    def stale():
        raise AssertionError("the cache must not be consulted on a successful load")

    result = load_or_heal(lambda: calls.append(1) or "model", stale)

    assert result == "model"
    assert len(calls) == 1
    assert artifact.exists()


def test_a_truncated_checkpoint_is_discarded_and_refetched(tmp_path):
    """The bug. First load fails on the bad file, second succeeds without it."""
    artifact = tmp_path / "beat_this-final0.ckpt"
    artifact.write_bytes(b"\x50\x4b\x03\x04truncated")
    attempts = []

    def load():
        attempts.append(artifact.exists())
        if artifact.exists():
            raise RuntimeError("PytorchStreamReader failed reading zip archive")
        return "healed"

    assert load_or_heal(load, lambda: [artifact]) == "healed"
    assert attempts == [True, False], "it must retry exactly once, after removing it"
    assert not artifact.exists()


def test_a_load_that_fails_with_nothing_cached_is_not_retried(tmp_path):
    """Offline with an empty cache is not a corruption, and must not pay twice.

    Retrying here would just spend the same network timeout again to reach the
    same conclusion, on the machine least able to afford it.
    """
    attempts = []

    def load():
        attempts.append(1)
        raise RuntimeError("connection refused")

    with pytest.raises(RuntimeError, match="connection refused"):
        load_or_heal(load, lambda: [tmp_path / "never-downloaded.ckpt"])

    assert len(attempts) == 1


def test_a_missing_package_is_never_blamed_on_the_cache(tmp_path):
    """An ImportError means the wheel is absent. Deleting models cannot help."""
    artifact = tmp_path / "model.ckpt"
    artifact.write_bytes(b"fine")

    with pytest.raises(ImportError):
        load_or_heal(lambda: (_ for _ in ()).throw(ImportError("no beat_this")), lambda: [artifact])

    assert artifact.exists(), "a good cache must survive an unrelated failure"


def test_the_second_failure_is_reported_not_swallowed(tmp_path):
    """Healing is one retry, not a loop. A genuinely dead download still raises."""
    artifact = tmp_path / "model.ckpt"
    artifact.write_bytes(b"bad")
    attempts = []

    def load():
        attempts.append(1)
        raise RuntimeError("still broken")

    with pytest.raises(RuntimeError, match="still broken"):
        load_or_heal(load, lambda: [artifact])

    assert len(attempts) == 2


def test_a_cache_directory_is_removed_whole(tmp_path):
    """audio-separator caches a directory of files, not a single checkpoint."""
    cache = tmp_path / "audio-separator"
    cache.mkdir()
    (cache / "UVR_MDXNET_KARA_2.onnx").write_bytes(b"truncated")
    (cache / "model_data.json").write_text("{}")
    attempts = []

    def load():
        attempts.append(cache.exists())
        if cache.exists():
            raise RuntimeError("Unsupported Model File: parameters for MD5 hash ...")
        return "healed"

    assert load_or_heal(load, lambda: [cache]) == "healed"
    assert not cache.exists()


def test_an_unresolvable_cache_path_reports_the_original_failure(tmp_path):
    """If we cannot even work out where the cache is, the load error is the news."""

    def stale():
        raise RuntimeError("torch is not importable")

    with pytest.raises(RuntimeError, match="the real problem"):
        load_or_heal(lambda: (_ for _ in ()).throw(RuntimeError("the real problem")), stale)


def test_a_permanently_broken_load_is_only_healed_once(tmp_path):
    """beat_this reports every failure as the same ValueError, so a load that a
    fresh copy cannot fix is indistinguishable from a truncated one. Without a
    guard that would re-fetch ~100 MB on every attempt for the life of the
    process."""
    artifact = tmp_path / "model.ckpt"
    artifact.write_bytes(b"bad")
    attempts = []

    def load():
        attempts.append(1)
        raise RuntimeError("Could not load the checkpoint given the provided name")

    for _ in range(3):
        artifact.write_bytes(b"bad")  # as if a re-download landed damaged again
        with pytest.raises(RuntimeError):
            load_or_heal(load, lambda: [artifact])

    # 2 for the first call (try, heal, retry), then 1 each for the two after it.
    assert len(attempts) == 4
    assert artifact.exists(), "later attempts must not keep deleting it"
