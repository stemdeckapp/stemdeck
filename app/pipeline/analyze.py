from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from app.core.config import JOBS_DIR, TIMEOUT_ANALYZE, ffmpeg_executable
from app.core.models import Job, _set

logger = logging.getLogger("stemdeck.analyze")

# Albrecht-Shanahan key profiles, derived from a corpus of popular music
# (Albrecht & Shanahan, 2013). Critically, the minor profile here weights
# b7 high (3.48) and M7 low (0.81) — the opposite of Temperley/Kostka-Payne,
# which were derived from Bach chorales and bias toward harmonic minor's
# leading tone. Pop/rock uses natural minor: the b7 is the diatonic
# seventh and rings out constantly (e.g. open D in "Come As You Are",
# which is in E minor and uses D as the b7). Values rescaled so that the
# tonic weight is ≈5 to match the prior code's magnitude.
_MAJOR_PROFILE = (
    5.47,
    0.14,
    2.55,
    0.14,
    3.15,
    2.16,
    0.37,
    4.92,
    0.21,
    1.84,
    0.18,
    1.86,
)
_MINOR_PROFILE = (
    5.06,
    0.14,
    2.42,
    2.42,
    0.35,
    1.96,
    0.35,
    4.16,
    2.53,
    0.28,
    2.67,
    0.62,
)
_PITCHES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")

# When the best-major and best-minor scores are this close, we prefer
# minor. Pop/rock has a strong minor-mode prior; the algorithm often
# walks toward the relative major because of an ostinato bass note
# (e.g. "Come As You Are" hammers the open D string in an E minor song),
# and minor is the better default when the call is genuinely ambiguous.
_MINOR_TIE_BREAK_FRAC = 0.05

# How much the song's edges count against the correlation when choosing
# between close candidates, a key and its relative above all. Tuned on
# tests/keyset.py: the reporter's
# i-VI-III-VII leads its relative major by up to 0.22 in correlation and
# trails it by about 0.8 in edge fit, and in every song detected correctly the
# edges agree with the correlation, so weighing them in never flips those.
_EDGE_WEIGHT = 0.4
# Keys this close to the best correlation, and their relatives, are all
# candidates when the edges are weighed in. A loop like i-VII-VI-VII puts the
# VII's own key ahead of the tonic's by a hair, and neither is the other's
# relative.
_FAMILY_WINDOW = 0.1
# Without edges, a relative pair this close in correlation counts as a tie.
_RELATIVE_MARGIN = 0.15
# Harmonic minor is reported when the raised seventh outweighs the flat one by
# this much and reaches this share of the loudest pitch class. On
# tests/keyset.py the ratio is at most 0.77 for natural minor and at least 1.54
# for harmonic minor, on the mix and on stems; the floor only keeps two
# near-silent pitch classes from deciding it.
_HARMONIC_RATIO = 1.3
_HARMONIC_FLOOR = 0.1
# A correlation gap this wide over the nearest unrelated key is full
# confidence; an edge margin this wide settles a relative pair outright.
_CONFIDENCE_FULL_GAP = 0.25
_EDGE_CONFIDENT_MARGIN = 0.3
# How much of the start and end of the music counts as its edges.
_EDGE_SECONDS = 4.0


def _correlate(profile: tuple[float, ...], chroma: list[float], shift: int) -> float:
    n = len(profile)
    rotated = [chroma[(i + shift) % n] for i in range(n)]
    mean_p = sum(profile) / n
    mean_c = sum(rotated) / n
    num = sum((profile[i] - mean_p) * (rotated[i] - mean_c) for i in range(n))
    denom_p = sum((profile[i] - mean_p) ** 2 for i in range(n)) ** 0.5
    denom_c = sum((rotated[i] - mean_c) ** 2 for i in range(n)) ** 0.5
    if denom_p == 0 or denom_c == 0:
        return 0.0
    return num / (denom_p * denom_c)


def _key_candidates(chroma_mean: list[float]) -> list[tuple[float, int, str]]:
    """Every key's Pearson correlation with its profile, best first, as
    (correlation, root, "maj" | "min")."""
    out = []
    for shift in range(12):
        out.append((_correlate(_MAJOR_PROFILE, chroma_mean, shift), shift, "maj"))
        out.append((_correlate(_MINOR_PROFILE, chroma_mean, shift), shift, "min"))
    out.sort(key=lambda c: c[0], reverse=True)
    return out


def _relative(root: int, mode: str) -> tuple[int, str]:
    return ((root + 9) % 12, "min") if mode == "maj" else ((root + 3) % 12, "maj")


def _tonic_fit(root: int, mode: str, edges: list[tuple[list[float], list[float]]]) -> float:
    """How much the song's edges sound like this key's home chord.

    Each edge is (chroma, bass chroma) averaged over a few seconds at the start
    or the end of the music, each scaled to a maximum of 1. A song starts and
    ends on its tonic far more often than on any other chord, and that is the
    one thing a whole-song histogram cannot show: a minor loop such as
    i-VI-III-VII uses exactly its relative major's notes (#726).
    """
    triad = {root, (root + (4 if mode == "maj" else 3)) % 12, (root + 7) % 12}
    score = 0.0
    for harmony, bass in edges:
        inside = sum(harmony[i] for i in triad) / 3
        outside = sum(harmony[i] for i in range(12) if i not in triad) / 9
        score += inside - outside + bass[root]
    return score / len(edges)


def _detect_key(
    chroma_mean: list[float],
    edges: list[tuple[list[float], list[float]]] | None = None,
) -> tuple[str, str, int]:
    """Find the key: profile correlation narrows it to a few close candidates,
    and the song's edges decide between them, a key and its relative above all.

    It used to multiply each key's correlation by how loud its root was. In a
    minor song the relative major's root is the minor chord's third, and is
    also in two of the other three chords of a typical loop, so it is usually
    the louder one: every i-VI-III-VII progression came out as its relative
    major, at up to 100% confidence (#726). Correlation alone is scale
    invariant and does not have that pull; what it cannot do is tell relative
    keys apart, which is the edges' job.

    `edges` is optional: without it a near-tie between a key and its relative
    falls back on the pop/rock minor prior.

    Returns (label, scale_name, confidence_pct).
    - label:          e.g. "G# maj"
    - scale_name:     "Major", "Natural Minor" or "Harmonic Minor"
    - confidence_pct: 0-100
    """
    candidates = _key_candidates(chroma_mean)
    best = candidates[0]
    rel_root, rel_mode = _relative(best[1], best[2])
    rel = next(c for c in candidates if c[1] == rel_root and c[2] == rel_mode)

    chroma_str = ", ".join(f"{_PITCHES[i]}={chroma_mean[i]:.3f}" for i in range(12))
    top5_str = ", ".join(f"{_PITCHES[r]} {m}={p:+.3f}" for p, r, m in candidates[:5])
    logger.debug("chroma: %s", chroma_str)
    logger.debug("key candidates (top 5): %s", top5_str)

    winner = best
    # How clearly the winner was separated from its closest rival, 0-1.
    clarity = 1.0
    if edges:
        by_key = {(c[1], c[2]): c for c in candidates}
        pool = set()
        for c in candidates:
            if best[0] - c[0] <= _FAMILY_WINDOW:
                pool.add((c[1], c[2]))
                pool.add(_relative(c[1], c[2]))
        scored = sorted(
            (
                (by_key[k][0] + _EDGE_WEIGHT * _tonic_fit(k[0], k[1], edges), by_key[k])
                for k in pool
            ),
            key=lambda s: s[0],
            reverse=True,
        )
        logger.debug(
            "with edges: %s",
            ", ".join(f"{_PITCHES[c[1]]} {c[2]}={score:.3f}" for score, c in scored[:5]),
        )
        winner = scored[0][1]
        runner = scored[1][0] if len(scored) > 1 else scored[0][0] - _EDGE_CONFIDENT_MARGIN
        clarity = min(1.0, (scored[0][0] - runner) / _EDGE_CONFIDENT_MARGIN)
    elif best[0] - rel[0] < _RELATIVE_MARGIN:
        # Near-tie with nothing else to go on: prefer minor, the pop/rock
        # prior ("Come As You Are" hammers the open D of an E minor song).
        threshold = max(abs(best[0]), abs(rel[0])) * _MINOR_TIE_BREAK_FRAC
        if best[0] - rel[0] <= threshold and rel[2] == "min":
            winner = rel
        clarity = 0.5

    root, mode = winner[1], winner[2]
    label = f"{_PITCHES[root]} {mode}"

    scale_name = "Major"
    if mode == "min":
        # The raised seventh is the harmonic minor's signature (a major V);
        # the natural minor's is the flat seventh. Only called when the raised
        # one clearly dominates and is actually sounding.
        raised = chroma_mean[(root + 11) % 12]
        flat = chroma_mean[(root + 10) % 12]
        peak = max(chroma_mean) or 1.0
        harmonic = raised > _HARMONIC_RATIO * flat and raised > _HARMONIC_FLOOR * peak
        scale_name = "Harmonic Minor" if harmonic else "Natural Minor"

    # Confidence: how clearly the key family won, against the best candidate
    # that is neither the winner nor its relative (relative keys correlate
    # alike by construction, so they say nothing about the family), then
    # scaled by how clearly the pair itself was separated.
    # With edges, the margin over the runner-up already covers both, since
    # every close candidate was in the running.
    if edges:
        return label, scale_name, round(clarity * 100)
    others = [c for c in candidates if (c[1], c[2]) not in {(best[1], best[2]), (rel[1], rel[2])}]
    family_gap = best[0] - others[0][0]
    confidence = min(1.0, max(0.0, family_gap / _CONFIDENCE_FULL_GAP)) * clarity
    return label, scale_name, round(confidence * 100)


def _measure_loudness(y: object, sr: int) -> tuple[float | None, float | None]:
    """Compute integrated loudness (LUFS, BS.1770) and sample peak (dBFS)
    of the loaded mono signal. Returns (lufs, peak_db); either may be
    None on failure or silence. We use sample peak rather than oversampled
    true peak -- the difference is typically <1 dB and not worth the 4x
    resample cost for a display field."""
    import numpy as np

    if y is None or getattr(y, "size", 0) == 0:
        return None, None

    peak_lin = float(np.abs(y).max())
    peak_db = 20.0 * float(np.log10(peak_lin)) if peak_lin > 1e-9 else None

    lufs: float | None = None
    try:
        import pyloudnorm as pyln

        meter = pyln.Meter(sr)  # BS.1770-4 with default 400ms blocks
        lufs_raw = float(meter.integrated_loudness(y))
        # pyloudnorm returns -inf for silence; surface as None instead so
        # the frontend can hide the field rather than render "-inf LUFS".
        if np.isfinite(lufs_raw):
            lufs = lufs_raw
    except (ImportError, ValueError) as e:
        # ValueError fires if the clip is shorter than the gating window.
        logger.warning("LUFS measurement failed: %s", e)
    return lufs, peak_db


def _load_audio_ffmpeg(
    source: Path,
    sr: int = 22050,
    duration: float | None = 180.0,
    timeout: int = TIMEOUT_ANALYZE,
) -> tuple[object, int] | None:
    """Decode `source` to a mono float32 numpy array at `sr` via ffmpeg.
    Bypasses librosa's deprecated audioread fallback (which fires a
    FutureWarning on .webm/.m4a/.opus inputs because soundfile can't
    read those directly). `duration=None` decodes the whole file (used by
    the beat-grid stage, which must cover the full track). Returns
    (samples, sr) or None on failure."""
    import numpy as np

    # Defence in depth: even though `source` is constructed by the server
    # (never user-typed), confirm it's a real file inside JOBS_DIR before
    # handing it to a subprocess. Belt-and-suspenders against a future
    # caller change that would let a path slip in from elsewhere.
    resolved = source.resolve()
    jobs_resolved = JOBS_DIR.resolve()
    if not resolved.is_file():
        logger.warning("analyze source is not a file: %s", source)
        return None
    if not resolved.is_relative_to(jobs_resolved):
        logger.warning(
            "analyze source escapes JOBS_DIR (%s not under %s)",
            resolved,
            jobs_resolved,
        )
        return None

    cmd = [
        ffmpeg_executable(),
        "-nostdin",
        "-loglevel",
        "error",
        "-i",
        str(resolved),
        "-ac",
        "1",  # mono
        "-ar",
        str(sr),  # resample
        "-f",
        "f32le",  # raw 32-bit float little-endian
    ]
    if duration is not None:
        cmd += ["-t", str(duration)]  # cap input duration
    cmd.append("-")  # write to stdout
    try:
        proc = subprocess.run(cmd, capture_output=True, check=True, timeout=timeout)
    # OSError, not just FileNotFoundError. A missing binary is only one way
    # spawning fails: a portable install whose ffmpeg lost its executable bit
    # raises PermissionError, which is an OSError but not a FileNotFoundError,
    # and would have crashed the analysis instead of degrading. Everything this
    # function produces is a display field, so None is always the right answer
    # on failure.
    except (subprocess.SubprocessError, OSError) as e:
        logger.warning("ffmpeg decode failed for %s: %s", source, e)
        return None
    y = np.frombuffer(proc.stdout, dtype=np.float32)
    if y.size == 0:
        return None
    return y, sr


def analyze(job: Job, source: Path) -> tuple[int | None, str | None]:
    """Best-effort BPM and key detection. On failure, returns (None, None)
    and leaves job fields untouched -- the chips stay as placeholders."""
    logger.info("analyze: entering for job %s, source=%s", job.id, source)
    _set(job, status="analyzing", progress=0.0, stage="Analyzing audio...")
    try:
        import librosa
    except ImportError:
        logger.warning("librosa not installed -- skipping BPM/key analysis")
        return None, None

    try:
        # Analyse the first 180 s. Decode via ffmpeg directly into numpy
        # to avoid librosa's deprecated audioread fallback for
        # .webm/.m4a/.opus inputs.
        loaded = _load_audio_ffmpeg(source, sr=22050, duration=180.0)
        if loaded is None:
            return None, None
        y, sr = loaded

        # Harmonic / percussive separation. Beat tracking sees a cleaner
        # onset envelope on the percussive component; chroma sees a
        # cleaner pitch profile on the harmonic component (no cymbal
        # smear, no kick fundamentals leaking in).
        y_harmonic, y_percussive = librosa.effects.hpss(y)

        tempo_arr, beat_frames = librosa.beat.beat_track(y=y_percussive, sr=sr)
        try:
            tempo = float(tempo_arr[0])  # type: ignore[index]
        except (TypeError, IndexError):
            tempo = float(tempo_arr)
        bpm = int(round(tempo)) if tempo > 0 else None

        # chroma_cqt is constant-Q based — better pitch resolution than
        # chroma_stft, especially in the bass register where the open
        # strings of a guitar live.
        # The decode stops at 180 s, so the song's last chord is only among
        # the evidence when the whole song fitted.
        whole_song = y.size < 179.5 * sr
        detected = detect_key_from_audio(y_harmonic, None, sr, whole_song=whole_song)
        key, scale, key_confidence = detected if detected else (None, None, None)

        # LUFS / peak. Computed on the same 22 kHz mono buffer; this
        # loses a few dB of accuracy vs full-sample-rate stereo, but
        # it's good enough for a UI display and adds ~50 ms to analyze.
        lufs, peak_db = _measure_loudness(y, sr)

        dynamic_range: float | None = None
        if lufs is not None and peak_db is not None:
            dynamic_range = round(peak_db - lufs, 1)

        # Beat interval coefficient of variation → stability 0-100.
        # CV = std/mean of inter-beat intervals; CV=0 is perfectly metronomic.
        tempo_stability: int | None = None
        import numpy as np

        beat_times = librosa.frames_to_time(beat_frames, sr=sr)
        if len(beat_times) > 2:
            intervals = np.diff(beat_times)
            mean_iv = float(intervals.mean())
            if mean_iv > 0:
                cv = float(intervals.std() / mean_iv)
                tempo_stability = max(0, min(100, round((1 - min(cv, 1)) * 100)))

        _set(
            job,
            bpm=bpm,
            key=key,
            scale=scale,
            key_confidence=key_confidence,
            lufs=lufs,
            peak_db=peak_db,
            dynamic_range=dynamic_range,
            tempo_stability=tempo_stability,
            progress=1.0,
            stage="Analysis complete",
        )
        return bpm, key
    except Exception:
        # Full traceback goes to the log; the UI stage line stays generic --
        # raw exception reprs (paths, library internals) must not reach it.
        logger.exception("analyze failed for job %s", job.id)
        _set(job, stage="Analysis skipped")
        return None, None


def _key_evidence(
    harmony: object,
    bass: object | None,
    sr: int,
    whole_song: bool,
) -> tuple[list[float], list[tuple[list[float], list[float]]]]:
    """The whole-song chroma, and the chroma and bass chroma at the edges of
    the music, for _detect_key.

    `bass` is the separated bass stem when there is one. Without it, the low
    three octaves of `harmony` stand in for it. The end of the song is only an
    edge when `whole_song`: a decode capped part way through ends mid-song.
    """
    import librosa
    import numpy as np

    # 93 ms frames. Key needs no finer: measured on a 9.5 min track's stems,
    # 512 took 8.6 s for both chromas and 2048 took 4.1 s, with the same
    # score on tests/keyset.py.
    hop = 2048
    chroma = librosa.feature.chroma_cqt(y=harmony, sr=sr, hop_length=hop)
    low = bass if bass is not None else harmony
    bass_chroma = librosa.feature.chroma_cqt(
        y=low, sr=sr, hop_length=hop, fmin=librosa.note_to_hz("C1"), n_octaves=3
    )
    level = librosa.feature.rms(y=harmony if bass is None else harmony + bass, hop_length=hop)[0]
    frames = min(chroma.shape[1], bass_chroma.shape[1], level.shape[0])
    chroma, bass_chroma, level = chroma[:, :frames], bass_chroma[:, :frames], level[:frames]

    # Music, not the silence or count-in before it: frames within 20 dB of
    # the loudest.
    active = np.flatnonzero(level > 0.1 * (level.max() or 1.0))
    span = max(1, int(_EDGE_SECONDS * sr / hop))

    def region(a: int, b: int) -> tuple[list[float], list[float]]:
        h = chroma[:, a:b].mean(axis=1)
        lo = bass_chroma[:, a:b].mean(axis=1)
        return (h / (h.max() or 1.0)).tolist(), (lo / (lo.max() or 1.0)).tolist()

    edges = []
    if active.size:
        start = int(active[0])
        edges.append(region(start, start + span))
        if whole_song:
            end = int(active[-1]) + 1
            edges.append(region(max(0, end - span), end))
    return chroma.mean(axis=1).tolist(), edges


def detect_key_from_audio(
    harmony: object, bass: object | None, sr: int, whole_song: bool = True
) -> tuple[str, str, int] | None:
    """Key, scale and confidence from mono audio, or None for silence."""
    chroma_mean, edges = _key_evidence(harmony, bass, sr, whole_song)
    if not any(chroma_mean):
        return None
    return _detect_key(chroma_mean, edges)


# Stems that carry harmony. Vocals count for half: a melody leans on the tonic
# at phrase ends, but it also dwells on passing notes.
_HARMONY_STEMS = {"guitar": 1.0, "piano": 1.0, "other": 1.0, "vocals": 0.5}
# The stems are decoded whole up to this long, so the end of the song counts.
_STEM_KEY_MAX_SECONDS = 600.0


def refine_key_from_stems(job: Job, stems_dir: Path) -> None:
    """Detect the key again from the separated stems, and keep it if it works.

    The first estimate comes from the full mix before separation, so the key
    shows while separation runs. The stems are better evidence: the bass line
    on its own says which note is home, drums no longer smear the chroma, and
    the whole song, including its last chord, is available rather than the
    first 180 s. Any failure keeps the first estimate.
    """
    import numpy as np

    sr = 22050
    try:
        harmony = None
        heard = []
        longest = 0
        for name, weight in _HARMONY_STEMS.items():
            path = stems_dir / f"{name}.wav"
            if not path.is_file():
                continue
            loaded = _load_audio_ffmpeg(path, sr=sr, duration=_STEM_KEY_MAX_SECONDS)
            if loaded is None:
                continue
            y = loaded[0] * weight
            longest = max(longest, y.size)
            harmony = y if harmony is None else _sum_padded(harmony, y)
            heard.append(name)
        if harmony is None:
            logger.info("key from stems: no harmony stems for job %s, keeping %s", job.id, job.key)
            return
        bass = None
        bass_path = stems_dir / "bass.wav"
        if bass_path.is_file():
            loaded = _load_audio_ffmpeg(bass_path, sr=sr, duration=_STEM_KEY_MAX_SECONDS)
            if loaded is not None:
                bass = loaded[0]
                harmony, bass = _pad_pair(harmony, bass)
        if bass is None:
            logger.info(
                "key from stems: no bass stem for job %s, using the harmony's low end", job.id
            )
        whole = longest < (_STEM_KEY_MAX_SECONDS - 1) * sr
        result = detect_key_from_audio(harmony.astype(np.float32), bass, sr, whole_song=whole)
        if result is None:
            return
        key, scale, confidence = result
        if key != job.key or scale != job.scale:
            logger.info(
                "key from stems (%s%s): %s %s %s%% (mix said %s %s)",
                "+".join(heard),
                "+bass" if bass is not None else "",
                key,
                scale,
                confidence,
                job.key,
                job.scale,
            )
        _set(job, key=key, scale=scale, key_confidence=confidence)
    except Exception:
        logger.exception("key from stems failed for job %s, keeping %s", job.id, job.key)


def _sum_padded(a: object, b: object) -> object:
    a, b = _pad_pair(a, b)
    return a + b


def _pad_pair(a: object, b: object) -> tuple[object, object]:
    import numpy as np

    n = max(a.size, b.size)  # type: ignore[attr-defined]
    return np.pad(a, (0, n - a.size)), np.pad(b, (0, n - b.size))  # type: ignore[attr-defined]
