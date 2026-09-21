"""Separate two singers of the same vocal range.

The register model in `duet_split_worker.py` splits a duet by vocal weight. That
works when the two singers sit in different ranges and does not when they do
not: measured on "What Is This Feeling" (Wicked, two sopranos) it led with one
stem in 76% of frames and the other in 7%, which is one voice with gaps rather
than a duet. This is the path for that case.

It runs the MedleyVox Conv-TasNet, which is trained on multi-singer mixtures
rather than on singing-versus-speech, and is the only open model measured to
separate simultaneous same-register singing at all.

Measured against ground-truth duets built from real material, with an ideal-mask
ceiling of +13.3 dB on the same audio:

  the other singer ends up 14.5 dB down (no split is 0 dB, ideal mask is 24.5)
  +7.3 dB SI-SDRi, roughly 55% of what any masking method could reach
  around 7x realtime on CPU, faster on GPU

What it does *not* do is choruses. The model has two outputs, so once an
ensemble joins, both stems contain everyone. That cannot be detected from the
audio, before or after -- see `register_split_collapsed` for the five measures
tried and what each one missed -- so it is a documented limit rather than a
guard, and STEMDECK_DUET_SAME_REGISTER lets someone who has listened choose.

Weights are Carson Evans' training run, published CC-BY-4.0, fetched on first
use the way Demucs already is. The model code is `_convtasnet.py`, vendored from
asteroid (MIT); the upstream MedleyVox inference repo carries no licence at all
and is deliberately not used.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np

logger = logging.getLogger("stemdeck.pipeline")

# The model's own rate. Everything below decides *which singer* at this rate and
# then applies the decision at the file's rate, so the output keeps its top end.
MODEL_SR = 24000

_REPO = "https://huggingface.co/Cyru5/MedleyVox/resolve/main"
# Two checkpoints of the same run at different epochs. They fail differently on
# the same audio, so averaging their masks is worth +0.1 to +0.2 dB for one
# extra forward pass. Both are from the multi-singing training; the
# singing_librispeech ones in the same repo score less than half as well here
# because they were trained to pull singing away from *speech*.
CHECKPOINTS = ("multi_singing_librispeech", "multi_singing_librispeech_138")

_NPER, _NOV = 4096, 3072


def artifacts(models_dir: Path) -> list[Path]:
    """Every file this model owns, for `load_or_heal` to clear and re-fetch."""
    root = models_dir / "medleyvox"
    return [root / name / fn for name in CHECKPOINTS for fn in ("vocals.json", "vocals.pth")]


def _fetch(models_dir: Path) -> list[tuple[Path, Path]]:
    """Download both checkpoints if absent. Returns (config, weights) pairs."""
    import requests

    out = []
    for name in CHECKPOINTS:
        d = models_dir / "medleyvox" / name
        d.mkdir(parents=True, exist_ok=True)
        for fn in ("vocals.json", "vocals.pth"):
            dest = d / fn
            if dest.is_file() and dest.stat().st_size > 1024:
                continue
            url = f"{_REPO}/{name}/{fn}"
            logger.info("duet: fetching %s/%s", name, fn)
            # .part first: a download cut short must not leave a file that looks
            # present and fails to load for good, which is the shape of #502.
            part = dest.with_suffix(dest.suffix + ".part")
            with requests.get(url, stream=True, timeout=120) as r:
                r.raise_for_status()
                with open(part, "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
            part.replace(dest)
        out.append((d / "vocals.json", d / "vocals.pth"))
    return out


def load_models(models_dir: Path, device: str = "cpu"):
    import torch

    from app.pipeline._convtasnet import build_from_config

    models = []
    for cfg_path, pth_path in _fetch(models_dir):
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))["args"]
        model = build_from_config(cfg)
        # weights_only, because a checkpoint is a pickle and an unrestricted
        # load executes whatever it contains. This one is fetched over the
        # network at first use, so the file is not something the app has seen
        # before it opens it. The contents are plain tensors either way.
        raw = torch.load(pth_path, map_location="cpu", weights_only=True)
        want = model.state_dict()
        # Training kept an exponential moving average beside the live weights.
        # The EMA copy is the one that was evaluated, and it is the better one.
        got = {
            k.replace("ema_model.module.", ""): v
            for k, v in raw.items()
            if k.startswith("ema_model.module.") and k.replace("ema_model.module.", "") in want
        }
        if len(got) != len(want):
            raise ValueError(
                f"duet checkpoint {cfg_path.parent.name} covers {len(got)} of "
                f"{len(want)} tensors; the file is probably truncated"
            )
        model.load_state_dict(got)
        model.eval()
        models.append(model.to(device))
    return models


def _separate_once(model, mix, device):
    import pyloudnorm as pyln
    import torch

    meter = pyln.Meter(MODEL_SR)
    gain = -24.0 - meter.integrated_loudness(mix.astype(np.float32))
    x = torch.as_tensor(
        (mix * 10 ** (gain / 20)).reshape(1, 1, -1), dtype=torch.float32, device=device
    )
    with torch.no_grad():
        y = model(x)
    return [
        y[0, i, : len(mix)].cpu().numpy().astype(np.float64) * 10 ** (-gain / 20) for i in (0, 1)
    ]


def _masks(est, sr, nper, nov):
    from scipy.signal import stft

    S = [stft(e, sr, nperseg=nper, noverlap=nov)[2] for e in est]
    power = np.abs(S[0]) ** 2 + np.abs(S[1]) ** 2 + 1e-12
    return [np.abs(S[k]) ** 2 / power for k in (0, 1)]


def separate(models, wav, sr, device="cpu"):
    """Two stems at the input's own sample rate, summing back to the input.

    Each model sees the audio forwards and reversed, which is a genuinely
    different view for a convolutional network and worth about +0.2 dB. Every
    view becomes a soft mask, the masks are aligned and averaged, and the
    average is applied to the *original-rate* spectrogram.

    That last step matters. The model is 24 kHz, so separating and resampling
    back would throw away everything above 12 kHz -- on a vocal, the air and
    most of the sibilance. The mask is resampled onto the real grid instead, in
    Hz and seconds rather than by bin index: bin 40 is 234 Hz at 24 kHz and
    431 Hz at 44.1 kHz, and frames are 85 ms against 46 ms, so index-wise
    copying shears the mask across both axes and costs about 7.8 dB.
    """
    from scipy.signal import istft, resample_poly, stft

    mono = wav.mean(1) if wav.ndim > 1 else wav
    lo = resample_poly(mono.astype(np.float64), MODEL_SR, sr)

    ref, acc, n = None, None, 0
    for model in models:
        rev = _separate_once(model, lo[::-1].copy(), device)
        for view in (_separate_once(model, lo, device), [r[::-1].copy() for r in rev]):
            m = _masks(view, MODEL_SR, _NPER, _NOV)
            if ref is None:
                ref, acc = m, [a.copy() for a in m]
            else:
                # Which output is which singer is arbitrary per view. Average
                # them unaligned and both stems become both singers.
                keep = sum(np.mean(np.abs(m[i] - ref[i])) for i in (0, 1))
                swap = sum(np.mean(np.abs(m[1 - i] - ref[i])) for i in (0, 1))
                if swap < keep:
                    m = m[::-1]
                acc = [a + b for a, b in zip(acc, m, strict=True)]
            n += 1
    avg = [a / n for a in acc]

    f_lo, t_lo, _ = stft(lo, MODEL_SR, nperseg=_NPER, noverlap=_NOV)
    f_hi, t_hi, M = stft(mono, sr, nperseg=_NPER, noverlap=_NOV)
    grids = []
    for k in (0, 1):
        a = np.empty((len(f_lo), len(t_hi)))
        for i in range(len(f_lo)):
            a[i] = np.interp(t_hi, t_lo, avg[k][i])
        b = np.empty((len(f_hi), len(t_hi)))
        for j in range(len(t_hi)):
            # np.interp clamps past the end of f_lo, which is exactly the wanted
            # "hold the highest band the model saw" behaviour above 12 kHz.
            b[:, j] = np.interp(f_hi, f_lo, a[:, j])
        grids.append(b)

    total = grids[0] + grids[1] + 1e-12
    # One mask per singer, decided on the downmix, applied to every channel.
    # Deciding per channel would let the two sides disagree about who is
    # singing and smear the image; applying to only the downmix would hand back
    # mono stems while every other stem in the job is stereo, which breaks pan
    # and width in the mixer.
    channels = wav.shape[1] if wav.ndim > 1 else 1
    stems = []
    for k in (0, 1):
        mask = grids[k] / total
        out = np.empty((len(mono), channels), dtype=np.float64)
        for ch in range(channels):
            src = wav[:, ch].astype(np.float64) if wav.ndim > 1 else mono
            _, _, C = stft(src, sr, nperseg=_NPER, noverlap=_NOV)
            cols = min(mask.shape[1], C.shape[1])
            y = np.real(istft(C[:, :cols] * mask[:, :cols], sr, nperseg=_NPER, noverlap=_NOV)[1])
            y = y[: len(mono)]
            if len(y) < len(mono):
                y = np.pad(y, (0, len(mono) - len(y)))
            out[:, ch] = y
        stems.append(out[:, 0] if channels == 1 else out)
    return stems


def register_split_collapsed(path_a: Path, path_b: Path, min_share: float = 0.12) -> bool:
    """Did the register model put essentially everything in one stem?

    This is the only property of a split that can be judged without knowing the
    right answer, and it is deliberately the *only* test made here. Four richer
    ones were tried and all of them passed a split that had not happened:

      energy share       -- misses two copies of one performance at 40/60
      share spread       -- a stem holding almost everything, with gaps, swings
                            between extremes and scores 0.287, higher than some
                            real splits
      who leads          -- measures whether a song has singers taking turns,
                            not whether separation worked; scored 0.000 on
                            ground-truth stems of a simultaneous duet
      stem correlation   -- the bad split scored 0.09, better than the model's
        and decisiveness    own correct output at 0.17-0.29, because it is
                            confidently wrong rather than confused

    The lesson is that a wrong separation looks exactly like a right one unless
    you already know the answer. So the cascade turns only on the unambiguous
    case -- one stem is essentially silent -- and anything subtler is the user's
    call via STEMDECK_DUET_SAME_REGISTER rather than a guess made on their
    behalf.
    """
    import soundfile as sf

    def energy(p: Path) -> float:
        x, _ = sf.read(str(p), dtype="float32", always_2d=True)
        return float(np.sum(x.astype(np.float64) ** 2))

    ea, eb = energy(path_a), energy(path_b)
    total = ea + eb
    if total <= 0:
        return True
    return min(ea, eb) / total < min_share
