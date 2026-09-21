"""Tests for the same-register duet split.

Two things are worth pinning down. The collapse check, because it is the one
property of a split that can be judged without knowing the right answer and it
decides whether a second, slower model runs. And the separator's output
contract: stems that sum back to the recording, at the recording's own rate.

The separator is exercised through a stub model, so the mask maths and the
sample-rate transfer are covered without a 460 MB checkpoint in CI.
"""

from __future__ import annotations

import numpy as np
import pytest
import soundfile as sf

from app.pipeline.duet_medleyvox import MODEL_SR, register_split_collapsed, separate

SR = 44100


def _write(path, x, sr=SR):
    sf.write(str(path), np.asarray(x, dtype=np.float32), sr)
    return path


def _tone(f, n, sr=SR, amp=1.0):
    t = np.arange(n) / sr
    return amp * np.sin(2 * np.pi * f * t)


def test_collapse_into_one_stem_is_a_failure(tmp_path):
    """The shape the register model takes on two singers of the same range."""
    n = SR * 6
    a = _write(tmp_path / "a.wav", _tone(220, n))
    b = _write(tmp_path / "b.wav", _tone(220, n, amp=0.02))
    assert register_split_collapsed(a, b)


def test_alternating_split_passes(tmp_path):
    """Two stems that trade the lead, as a separated duet does."""
    n = SR * 8
    gate = np.zeros(n)
    for i in range(0, n, SR * 2):
        gate[i : i + SR] = 1.0
    a = _write(tmp_path / "a.wav", _tone(220, n) * gate)
    b = _write(tmp_path / "b.wav", _tone(330, n) * (1.0 - gate))
    assert not register_split_collapsed(a, b)


def test_silence_counts_as_failure(tmp_path):
    n = SR * 4
    a = _write(tmp_path / "a.wav", np.zeros(n))
    b = _write(tmp_path / "b.wav", np.zeros(n))
    assert register_split_collapsed(a, b)


class _StubModel:
    """Stands in for the checkpoint: splits by frequency, nothing learned.

    Lets the mask averaging and the 24 kHz-to-file-rate transfer be tested
    without downloading 460 MB, which is the part with real arithmetic in it.
    """

    def to(self, device):
        return self

    def eval(self):
        return self

    def __call__(self, x):
        import torch

        wav = x[0, 0].numpy().astype(np.float64)
        spec = np.fft.rfft(wav)
        freqs = np.fft.rfftfreq(len(wav), 1 / MODEL_SR)
        lo = np.fft.irfft(spec * (freqs < 400), n=len(wav))
        hi = wav - lo
        return torch.as_tensor(np.stack([lo, hi])[None, :, :], dtype=torch.float32)


def test_separate_preserves_the_recording_and_its_rate():
    """Whatever the model says, the two stems must add back to the input.

    Mixture consistency plus the mask normalisation is what makes solo and mute
    behave in the mixer, so it is worth asserting rather than assuming.
    """
    n = SR * 3
    mix = (_tone(180, n) + _tone(900, n, amp=0.7)).reshape(-1, 1)
    mix = mix / np.max(np.abs(mix))

    stems = separate([_StubModel()], mix, SR, "cpu")

    assert len(stems) == 2
    for s in stems:
        assert len(s) == n, "stems must come back at the input length"
        assert np.all(np.isfinite(s))

    mono = mix[:, 0]
    residual = mono - (stems[0] + stems[1])
    rel = np.sqrt(np.mean(residual**2)) / np.sqrt(np.mean(mono**2))
    assert rel < 1e-6, f"stems do not sum to the input (relative residual {rel:.2e})"


def test_separate_keeps_content_above_the_models_ceiling():
    """The model is 24 kHz; the output must not be band-limited to 12 kHz.

    Separating at 24 kHz and resampling back would silently drop the air and
    most of the sibilance from a vocal. The mask is transferred to the file's
    own grid instead, so energy above the model's ceiling has to survive.
    """
    n = SR * 3
    mix = (_tone(300, n) + _tone(16000, n, amp=0.3)).reshape(-1, 1)
    mix = mix / np.max(np.abs(mix))

    stems = separate([_StubModel()], mix, SR, "cpu")
    recombined = stems[0] + stems[1]

    spec = np.abs(np.fft.rfft(recombined))
    freqs = np.fft.rfftfreq(len(recombined), 1 / SR)
    above = float(np.sum(spec[freqs > 12000] ** 2))
    total = float(np.sum(spec**2)) + 1e-12
    assert above / total > 0.01, "content above 12 kHz was lost"


@pytest.mark.parametrize("channels", [1, 2])
def test_separate_keeps_the_input_channel_count(channels):
    """Stereo in, stereo out.

    The model is mono, so the obvious implementation hands back mono stems while
    every other stem in the job is stereo. That is not a cosmetic difference:
    a mono stem among stereo ones loses its pan position and width in the mixer.
    The mask is decided on the downmix and applied to each channel.
    """
    n = SR * 2
    left = _tone(220, n) + _tone(660, n, amp=0.5)
    right = _tone(220, n, amp=0.4) + _tone(660, n)
    wav = left.reshape(-1, 1) if channels == 1 else np.stack([left, right], axis=1)

    stems = separate([_StubModel()], wav, SR, "cpu")

    for stem in stems:
        assert len(stem) == n
        if channels == 1:
            assert stem.ndim == 1
        else:
            assert stem.shape == (n, 2)

    # Per channel, the two stems must still add back to that channel.
    for ch in range(channels):
        src = wav[:, ch]
        got = stems[0] + stems[1]
        got = got if channels == 1 else got[:, ch]
        rel = np.sqrt(np.mean((src - got) ** 2)) / np.sqrt(np.mean(src**2))
        assert rel < 1e-6, f"channel {ch} does not reconstruct (residual {rel:.2e})"

    if channels == 2:
        # A real stereo image must survive: the two channels of a stem must not
        # collapse to the same signal.
        for stem in stems:
            if np.sqrt(np.mean(stem[:, 0] ** 2)) > 1e-6:
                diff = np.mean(np.abs(stem[:, 0] - stem[:, 1]))
                assert diff > 1e-6, "stereo stem collapsed to dual mono"
