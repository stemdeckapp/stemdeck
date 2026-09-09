"""Beat and downbeat detection back-ends for the click track.

Two implementations behind one call:

**beat_this** (default) -- a transformer beat/downbeat tracker from the same lab
as madmom, MIT-licensed for both code and published weights. It is here because
librosa's tracker has a systematic, unrecoverable failure on fast music: its
lognormal tempo prior is centred on 120 BPM, so a 180 BPM punk song resolves to
90 and the click plays exactly half-time. Measured on Green Day "Welcome To
Paradise" (librosa 90.0 against a true ~180; beat_this 176.5) and reproduced on
synthetic punk at 176 (librosa 117.5; beat_this 176.5). This is not tunable
away -- widening the prior to catch 180 breaks slow material instead -- and no
quality metric detects it after the fact, because a half-time grid puts a real
drum hit under every single beat and scores 94% confidence.

It also returns *downbeats*, which librosa cannot do at all. That is what lets
the click accent real bar lines instead of counting a fixed four from the top of
the track.

**librosa** -- the fallback. Needs no model download and no network, so it keeps
the feature working on a fresh offline install and on any box where the model
cannot load. Selected automatically in that case; forceable with
STEMDECK_BEAT_DETECTOR=librosa.

Deliberately *not* madmom: its pretrained models are CC BY-NC-SA 4.0, which
would quietly make an Apache-2.0 project non-commercial for everyone who
installs it. Its DBN post-processor is BSD and model-free, and beat_this can use
it, but doing so needs a git install with an undeclared Cython build step on
three platforms. The interior-gap fill in beatgrid.py recovers what the DBN was
buying us (continuity through drum-free sections) with no dependency at all.
"""

from __future__ import annotations

import logging
import threading

from app.core.config import BEAT_DETECTOR, BEAT_MODEL_CHECKPOINT, BEATGRID_HOP

logger = logging.getLogger("stemdeck.beatdetect")

# Loading the checkpoint costs ~1 s and 81 MB of RAM, so the model is built once
# and reused. Guarded because pipeline stages can run from a worker thread.
_model = None
_model_failed = False
_lock = threading.Lock()


def _get_model():
    """Build (once) and return the beat_this predictor, or None if unavailable.

    A failure is cached: if the import is missing or the weights cannot be
    fetched, retrying per job would stall every job on the same network
    timeout.
    """
    global _model, _model_failed
    if _model is not None or _model_failed:
        return _model
    with _lock:
        if _model is not None or _model_failed:
            return _model
        try:
            from beat_this.inference import Audio2Beats

            from app.core.model_cache import beat_this_artifacts, load_or_heal

            # A checkpoint that downloaded incompletely is still a file on
            # disk, and torch.hub will keep handing it back forever. Without
            # this, one dropped connection costs the user the neural beat grid
            # permanently and silently, because the librosa fallback below
            # looks exactly like a machine that never had the model (#502).
            _model = load_or_heal(
                lambda: Audio2Beats(checkpoint_path=BEAT_MODEL_CHECKPOINT, device="cpu", dbn=False),
                lambda: beat_this_artifacts(BEAT_MODEL_CHECKPOINT),
            )
            logger.info("beat model loaded (checkpoint=%s)", BEAT_MODEL_CHECKPOINT)
        except ImportError:
            logger.info("beat_this not installed -- using librosa beat tracking")
            _model_failed = True
        except Exception:
            # Most likely the weights could not be downloaded (offline first
            # run). Non-fatal: librosa still produces a grid.
            logger.warning("beat model unavailable -- falling back to librosa", exc_info=True)
            _model_failed = True
    return _model


def reset_model_cache() -> None:
    """Drop the cached model and failure flag. For tests."""
    global _model, _model_failed
    with _lock:
        _model = None
        _model_failed = False


def _detect_model(y: object, sr: int) -> tuple[list[float], list[float]] | None:
    model = _get_model()
    if model is None:
        return None
    try:
        beats, downbeats = model(y, sr)
    except Exception:
        logger.warning("beat model inference failed -- falling back", exc_info=True)
        return None
    beats = [float(t) for t in beats]
    downbeats = [float(t) for t in downbeats]
    if len(beats) < 2:
        logger.warning("beat model returned %d beats -- falling back", len(beats))
        return None
    return beats, downbeats


def _detect_librosa(y: object, sr: int, onset_env: object) -> tuple[list[float], list[float]]:
    """Classic tracker. Returns (beats, []) -- it has no downbeat notion."""
    import librosa

    # trim=False keeps beats in the quiet head/tail of the track. Trimming is
    # right for a tempo estimate and wrong for a grid: a click that stops during
    # a soft intro reads as a bug.
    _tempo, beat_times = librosa.beat.beat_track(
        onset_envelope=onset_env,
        sr=sr,
        hop_length=BEATGRID_HOP,
        units="time",
        trim=False,
    )
    return [float(t) for t in beat_times], []


def detect(y: object, sr: int, onset_env: object) -> tuple[list[float], list[float], str]:
    """Detect beats and downbeats.

    Returns (beat_times, downbeat_times, detector_name). `downbeat_times` is
    empty for the librosa back-end. Raises RuntimeError only when the model was
    explicitly required and is unavailable.
    """
    if BEAT_DETECTOR != "librosa":
        got = _detect_model(y, sr)
        if got is not None:
            return got[0], got[1], "beat_this"
        if BEAT_DETECTOR == "model":
            raise RuntimeError("beat model required (STEMDECK_BEAT_DETECTOR=model) but unavailable")
    beats, downbeats = _detect_librosa(y, sr, onset_env)
    return beats, downbeats, "librosa"
