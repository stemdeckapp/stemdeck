"""Render the click track to audio so it can be included in exports.

Playback synthesises the click in the browser with Web Audio oscillators, which
never reach the server -- the export path is ffmpeg summing stem WAVs. This
module produces an equivalent WAV so the click can be mixed in as one more
ffmpeg input.

The voice is reproduced from `static/js/metronome.js` deliberately literally,
including its exponential gain ramps, because an export that sounds different
from what the user monitored is worse than no export option at all. The two
implementations are pinned together by `tests/test_click_render.py`, which
asserts the rendered peaks land on the same beats the scheduler would use.
"""

from __future__ import annotations

import hashlib
import logging
import wave
from pathlib import Path

logger = logging.getLogger("stemdeck.clickrender")

# Mirrors the constants at the top of static/js/metronome.js. Changing either
# side without the other makes exports diverge from playback.
CLICK_FREQ = 1000.0
ACCENT_FREQ = 1500.0
CLICK_DECAY = 0.035
CLICK_ATTACK = 0.001
CLICK_PEAK = 0.7
ACCENT_PEAK = 1.0
# exponentialRampToValueAtTime cannot start from zero, so the scheduler ramps
# from this floor; matching it keeps the attack shape identical.
RAMP_FLOOR = 0.0001

# Accent modes, matching the frontend's Accent selector.
ACCENT_AUTO = -1  # follow the detected bar marks
ACCENT_OFF = 0

_VALID_MULTIPLIERS = (0.5, 1.0, 2.0)


def rescale_beats(beats: list[float], multiplier: float) -> list[float]:
    """Apply the playback rate multiplier. Mirrors `_rescale` in metronome.js:
    doubling inserts midpoints, halving takes every other beat, and both derive
    from the original list so switching never compounds."""
    if multiplier == 2.0:
        out: list[float] = []
        for i in range(len(beats) - 1):
            out.append(beats[i])
            out.append((beats[i] + beats[i + 1]) / 2.0)
        if beats:
            out.append(beats[-1])
        return out
    if multiplier == 0.5:
        return beats[::2]
    return list(beats)


def source_index(i: int, multiplier: float) -> int | None:
    """Map an index in the rescaled grid back to the original beat it came from.

    Bar marks are recorded against the *detected* beats, so accents must be
    decided in that index space. At x2 the odd entries are inserted midpoints
    that correspond to no original beat and can never be downbeats; at /2 every
    entry is an original beat two apart.
    """
    if multiplier == 2.0:
        return i // 2 if i % 2 == 0 else None
    if multiplier == 0.5:
        return i * 2
    return i


def is_downbeat(index: int | None, bars: list[dict], accent_mode: int) -> bool:
    """Whether the beat at `index` (original grid) carries an accent."""
    if index is None or accent_mode == ACCENT_OFF:
        return False
    if accent_mode > 0:
        return index % accent_mode == 0
    # Auto: follow the last bar mark at or before this beat.
    mark = None
    for b in bars:
        beat = b.get("beat")
        if isinstance(beat, int) and beat <= index:
            mark = b
        else:
            break
    if mark is None:
        return False
    per_bar = mark.get("beats_per_bar")
    if not isinstance(per_bar, int) or per_bar < 1:
        return False
    return (index - mark["beat"]) % per_bar == 0


def count_in_beats_per_bar(bars: list[dict], accent_mode: int, start_index: int = 0) -> int:
    """How many clicks make one count-in bar.

    An explicit accent count wins; otherwise the detected meter in force at the
    start position; otherwise 4. Always >= 1, because a count-in needs a bar
    length even on a track with no bar marks and accents switched off -- unlike
    the running click, "no accent" must not mean "no bar" here.
    """
    if accent_mode > 0:
        return accent_mode
    # Auto / off: follow the meter in force at the start beat, if the detector
    # found one. bars index the detected grid, so the search is in index space.
    mark = None
    for b in bars:
        beat = b.get("beat")
        if isinstance(beat, int) and beat <= start_index:
            mark = b
        else:
            break
    if mark is not None:
        per_bar = mark.get("beats_per_bar")
        if isinstance(per_bar, int) and per_bar >= 1:
            return per_bar
    return 4


def _interval_near(grid: list[float], start: float, span: int) -> float | None:
    """Median beat interval of the (rescaled) grid around `start`.

    The count-in tempo is the song's tempo where playback begins, not its
    average: taking the median of one bar's worth of intervals from the first
    beat at or after `start` follows a track that speeds up or slows down.
    Returns None when the grid is too short to measure an interval.
    """
    if len(grid) < 2:
        return None
    i = 0
    while i < len(grid) and grid[i] < start:
        i += 1
    # Anchor on the beat at/after start, but never past the last interval.
    i = min(i, len(grid) - 2)
    diffs = [grid[k + 1] - grid[k] for k in range(i, min(i + max(1, span), len(grid) - 1))]
    diffs = [d for d in diffs if d > 0]
    if not diffs:
        return None
    diffs.sort()
    return diffs[len(diffs) // 2]


def count_in_beats(
    beats: list[float],
    bars: list[dict],
    count_bars: int = 1,
    multiplier: float = 1.0,
    accent_mode: int = ACCENT_AUTO,
    start: float = 0.0,
) -> tuple[float, list[tuple[float, bool]]]:
    """Compute the count-in that leads into playback at `start`.

    Returns `(lead_in, clicks)` where `lead_in` is the seconds of pre-roll to
    prepend and `clicks` is `[(offset, accent), ...]` with each offset in
    `[0, lead_in)`. One bar of the detected meter counts in by default:
    `PI po po po` on 4/4, the final click landing one beat before the audio so
    the song enters on the next downbeat.

    Pure and side-effect free so playback (metronome.js) and export
    (render_click_wav) can share one definition -- pinned by
    tests/test_click_render.py, exactly like rescale/source_index/is_downbeat.
    """
    if count_bars < 1:
        return 0.0, []
    grid = rescale_beats([float(b) for b in beats], multiplier)
    # Meter lookup uses the detected grid (bars index it); interval uses the
    # rescaled grid so the count matches the click rate the user hears.
    start_index = 0
    for k, t in enumerate(beats):
        if t <= start:
            start_index = k
        else:
            break
    bpb = count_in_beats_per_bar(bars, accent_mode, start_index)
    interval = _interval_near(grid, start, bpb)
    if interval is None:
        return 0.0, []
    n = count_bars * bpb
    lead_in = n * interval
    clicks = [(j * interval, (j % bpb) == 0) for j in range(n)]
    return lead_in, clicks


def _voice(peak: float, freq: float, sample_rate: int):
    """One click as a float array: a sine under the scheduler's two exponential
    gain ramps (RAMP_FLOOR -> peak over the attack, then back down over the rest
    of the decay). Phase starts at zero, exactly as a fresh OscillatorNode."""
    import numpy as np

    n = int(round(CLICK_DECAY * sample_rate))
    t = np.arange(n) / sample_rate
    attack = t <= CLICK_ATTACK
    env = np.empty(n)
    env[attack] = RAMP_FLOOR * (peak / RAMP_FLOOR) ** (t[attack] / CLICK_ATTACK)
    span = CLICK_DECAY - CLICK_ATTACK
    env[~attack] = peak * (RAMP_FLOOR / peak) ** ((t[~attack] - CLICK_ATTACK) / span)
    return np.sin(2.0 * np.pi * freq * t) * env


def cache_key(
    job_id: str,
    beats: list[float],
    bars: list[dict],
    duration: float,
    sample_rate: int,
    multiplier: float,
    accent_mode: int,
    count_in_bars: int = 0,
    include_click: bool = True,
    start: float | None = None,
    end: float | None = None,
) -> str:
    """Every input to the render is in the key. Beats are included by digest
    rather than by job id alone: an edited grid must not hit a cache entry
    rendered from the detected one.

    The count-in suffix is appended only when a count-in is present, so a plain
    click export keeps the exact key it always had (a stable cache across the
    change). With a count-in the render is region-specific -- the lead-in tempo
    comes from the beats at `start` and the song clicks are trimmed to the
    region -- so the region bounds and whether the song click is included both
    enter the key."""
    grid = hashlib.sha1(
        ("|".join(f"{b:.6f}" for b in beats)).encode("utf-8"), usedforsecurity=False
    ).hexdigest()
    bar_sig = ",".join(f"{b.get('beat')}:{b.get('beats_per_bar')}" for b in bars)
    raw = f"{job_id}|{grid}|{bar_sig}|{duration:.3f}|{sample_rate}|{multiplier}|{accent_mode}"
    if count_in_bars > 0:
        seg = f"{'' if start is None else f'{start:.3f}'}:{'' if end is None else f'{end:.3f}'}"
        raw += f"|ci{count_in_bars}|clk{int(include_click)}|{seg}"
    return hashlib.sha1(raw.encode("utf-8"), usedforsecurity=False).hexdigest()


def _song_click_events(
    beats: list[float], bars: list[dict], multiplier: float, accent_mode: int
) -> list[tuple[float, bool]]:
    """The (time, accent) pair for every beat of the running click track, in
    source time. Shared by the plain click render and the count-in render so the
    click sounds identical whether or not a count-in precedes it."""
    grid = rescale_beats([float(b) for b in beats], multiplier)
    return [
        (t, is_downbeat(source_index(i, multiplier), bars, accent_mode)) for i, t in enumerate(grid)
    ]


def _render_events(
    dest: Path, events: list[tuple[float, bool]], duration: float, sample_rate: int
) -> Path | None:
    """Stamp a list of (time, accent) clicks into a mono WAV of `duration`
    seconds. The single place clicks become audio, so playback parity only has
    to be maintained against the two voices, not against two render paths."""
    total = int(round(duration * sample_rate))
    if not events or total <= 0:
        return None

    import numpy as np

    buf = np.zeros(total, dtype=np.float64)
    # Only two distinct voices, so render each once and stamp it in.
    plain = _voice(CLICK_PEAK, CLICK_FREQ, sample_rate)
    accented = _voice(ACCENT_PEAK, ACCENT_FREQ, sample_rate)

    for t, accent in events:
        start = int(round(t * sample_rate))
        if start >= total or start < 0:
            continue
        voice = accented if accent else plain
        n = min(len(voice), total - start)
        # Clicks can overlap at very fast tempos; summing matches the graph,
        # where every click is its own node into the same gain.
        buf[start : start + n] += voice[:n]

    np.clip(buf, -1.0, 1.0, out=buf)
    pcm = (buf * 32767.0).astype("<i2")

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".wav.tmp")
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm.tobytes())
    tmp.replace(dest)
    return dest


def render_click_wav(
    dest: Path,
    beats: list[float],
    bars: list[dict],
    duration: float,
    sample_rate: int = 44100,
    multiplier: float = 1.0,
    accent_mode: int = ACCENT_AUTO,
) -> Path | None:
    """Write a mono WAV of the click track spanning the whole track.

    Full length regardless of where the beats start, so the export's region trim
    (`-ss` before every ffmpeg input) lines the click up with the stems without
    any special-casing. Returns the path, or None when there is nothing to
    render.
    """
    if multiplier not in _VALID_MULTIPLIERS:
        multiplier = 1.0
    events = _song_click_events(beats, bars, multiplier, accent_mode)
    out = _render_events(dest, events, duration, sample_rate)
    if out is not None:
        logger.info("click render: %d beats, %.1f s -> %s", len(events), duration, dest.name)
    return out


def render_count_in_wav(
    dest: Path,
    beats: list[float],
    bars: list[dict],
    duration: float,
    sample_rate: int = 44100,
    multiplier: float = 1.0,
    accent_mode: int = ACCENT_AUTO,
    count_in_bars: int = 1,
    include_click: bool = True,
    start: float = 0.0,
    end: float | None = None,
) -> tuple[Path, float] | None:
    """Render the click WAV for a count-in export, in *output* coordinates.

    Unlike render_click_wav (source-time, trimmed by ffmpeg's `-ss`), this bakes
    the lead-in into the file: the count-in clicks occupy `[0, lead_in)` and,
    when `include_click`, the region's song clicks follow shifted by `lead_in`.
    The stems are delayed by the same lead_in in the ffmpeg graph, so the file
    and the stems share one origin. Returns `(path, lead_in)`, or None when
    there is nothing to render (grid too short and no song click requested).
    """
    if multiplier not in _VALID_MULTIPLIERS:
        multiplier = 1.0
    seg_start = start or 0.0
    seg_end = duration if end is None else end
    seg_len = max(0.0, seg_end - seg_start)

    lead_in, count_clicks = count_in_beats(
        beats, bars, count_in_bars, multiplier, accent_mode, start=seg_start
    )
    events: list[tuple[float, bool]] = list(count_clicks)
    if include_click:
        for t, accent in _song_click_events(beats, bars, multiplier, accent_mode):
            if seg_start <= t < seg_end:
                events.append((t - seg_start + lead_in, accent))

    out = _render_events(dest, events, lead_in + seg_len, sample_rate)
    if out is None:
        return None
    logger.info("count-in render: %.3f s lead-in, %d clicks -> %s", lead_in, len(events), dest.name)
    return out, lead_in
