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
# Group starts sit between the downbeat and a plain beat in both pitch and
# level, so a grouped bar reads as "strong, medium, weak" rather than as three
# identical accents (#595). Geometric middle of the two existing voices: the ear
# hears pitch ratios, so 1225 is the midpoint of 1000 and 1500, not 1250.
GROUP_FREQ = 1225.0
ACCENT_FREQ = 1500.0
CLICK_DECAY = 0.035
CLICK_ATTACK = 0.001
CLICK_PEAK = 0.7
GROUP_PEAK = 0.85
ACCENT_PEAK = 1.0

# Click strength at one beat. The renderer and static/js/metronome.js must agree
# on these three, or a monitored click and an exported one differ.
LEVEL_WEAK = 0
LEVEL_GROUP = 1
LEVEL_DOWNBEAT = 2
# exponentialRampToValueAtTime cannot start from zero, so the scheduler ramps
# from this floor; matching it keeps the attack shape identical.
RAMP_FLOOR = 0.0001

# Accent modes, matching the frontend's Accent selector.
ACCENT_AUTO = -1  # follow the detected bar marks
ACCENT_OFF = 0

_VALID_MULTIPLIERS = (0.5, 1.0, 2.0)


def default_grouping(beats_per_bar: int) -> list[int]:
    """How a bar of `beats_per_bar` divides, when the user has not said.

    Odd and compound meters carry internal stresses that a flat bar does not
    express: 7 is played 3+2+2, 6 is two dotted-quarter groups, 5 is 3+2. The
    click has to mark those or it gives a player nothing to lock onto (#595).

    2, 3 and 4 deliberately return a single group, leaving them exactly as they
    sounded before. 4/4 does carry a real secondary stress on beat 3, but
    turning that on by default would change the most common meter in the app for
    every existing user, which is a decision for them and not a default.

    Mirrored by defaultGrouping() in static/js/metronome.js -- keep both in step.
    """
    if beats_per_bar < 1:
        return []
    if beats_per_bar in (5, 7):
        # 3 first: the long group leads in both, which is the commoner reading
        # (Take Five is 3+2, and 7/8 is more often 3+2+2 than 2+2+3).
        return [3] + [2] * ((beats_per_bar - 3) // 2)
    if beats_per_bar >= 6 and beats_per_bar % 3 == 0:
        # Compound: 6, 9 and 12 are felt in dotted-quarter groups of three.
        return [3] * (beats_per_bar // 3)
    return [beats_per_bar]


def normalise_grouping(groups: list[int] | None, beats_per_bar: int) -> list[int]:
    """A grouping that is safe to index a bar with, or the default.

    Anything that is not a list of positive ints summing to the bar length is
    rejected wholesale rather than repaired: a half-understood grouping would
    put accents on beats the user never asked for, and silently playing the
    default is the honest failure.
    """
    if beats_per_bar < 1:
        return []
    if not groups:
        return default_grouping(beats_per_bar)
    if not all(isinstance(g, int) and g >= 1 for g in groups):
        return default_grouping(beats_per_bar)
    if sum(groups) != beats_per_bar:
        return default_grouping(beats_per_bar)
    return list(groups)


def group_offsets(groups: list[int]) -> set[int]:
    """Beat offsets inside a bar that start a group, excluding the downbeat
    (which is already the stronger LEVEL_DOWNBEAT)."""
    offsets: set[int] = set()
    at = 0
    for g in groups[:-1]:
        at += g
        offsets.add(at)
    return offsets


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


def _bar_position(index: int, bars: list[dict], accent_mode: int) -> tuple[int, int] | None:
    """(offset into the bar, bar length) for a beat, or None when no bar applies.

    Split out of is_downbeat so grouping can ask *where* in the bar a beat falls,
    not merely whether it is the first one (#595).
    """
    if accent_mode == ACCENT_OFF:
        return None
    if accent_mode > 0:
        return index % accent_mode, accent_mode
    # Auto: follow the last bar mark at or before this beat.
    mark = None
    for b in bars:
        beat = b.get("beat")
        if isinstance(beat, int) and beat <= index:
            mark = b
        else:
            break
    if mark is None:
        return None
    per_bar = mark.get("beats_per_bar")
    if not isinstance(per_bar, int) or per_bar < 1:
        return None
    return (index - mark["beat"]) % per_bar, per_bar


def is_downbeat(index: int | None, bars: list[dict], accent_mode: int) -> bool:
    """Whether the beat at `index` (original grid) carries an accent."""
    if index is None:
        return False
    pos = _bar_position(index, bars, accent_mode)
    return pos is not None and pos[0] == 0


def beat_level(
    index: int | None, bars: list[dict], accent_mode: int, groups: list[int] | None = None
) -> int:
    """How strongly the beat at `index` is clicked: downbeat, group start, weak.

    `groups` applies only to an explicit meter. Under Auto the bar length can
    change from bar to bar, so a single user-supplied grouping cannot be assumed
    to fit every bar; each bar falls back to the default for its own length,
    which is what makes a detected 6/8 passage group in threes without the user
    configuring anything.
    """
    if index is None:
        return LEVEL_WEAK
    pos = _bar_position(index, bars, accent_mode)
    if pos is None:
        return LEVEL_WEAK
    offset, per_bar = pos
    if offset == 0:
        return LEVEL_DOWNBEAT
    if accent_mode > 0:
        bar_groups = normalise_grouping(groups, per_bar)
    else:
        bar_groups = default_grouping(per_bar)
    return LEVEL_GROUP if offset in group_offsets(bar_groups) else LEVEL_WEAK


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
    groups: list[int] | None = None,
) -> tuple[float, list[tuple[float, int]]]:
    """Compute the count-in that leads into playback at `start`.

    Returns `(lead_in, clicks)` where `lead_in` is the seconds of pre-roll to
    prepend and `clicks` is `[(offset, level), ...]` with each offset in
    `[0, lead_in)`. One bar of the detected meter counts in by default:
    `PI po po po` on 4/4, the final click landing one beat before the audio so
    the song enters on the next downbeat.

    The count-in is grouped exactly as the running click is (#595), so counting
    a player into 7/8 gives them the 3+2+2 pulse they are about to play rather
    than seven flat clicks.

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
    # The count-in is its own run of bars, so its grouping comes from bpb
    # directly rather than from the bar marks: there is no detected bar to
    # consult in front of the audio.
    bar_groups = normalise_grouping(groups, bpb) if accent_mode > 0 else default_grouping(bpb)
    starts = group_offsets(bar_groups)
    clicks = []
    for j in range(n):
        offset = j % bpb
        if offset == 0:
            level = LEVEL_DOWNBEAT
        elif offset in starts:
            level = LEVEL_GROUP
        else:
            level = LEVEL_WEAK
        clicks.append((j * interval, level))
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
    groups: list[int] | None = None,
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
    # Appended only when a grouping is actually in force, so every export that
    # predates grouping keeps the exact key it had and the cache survives the
    # change. Without this a re-grouped render would serve the old flat file.
    if groups:
        raw += f"|g{'+'.join(str(int(g)) for g in groups)}"
    if count_in_bars > 0:
        seg = f"{'' if start is None else f'{start:.3f}'}:{'' if end is None else f'{end:.3f}'}"
        raw += f"|ci{count_in_bars}|clk{int(include_click)}|{seg}"
    return hashlib.sha1(raw.encode("utf-8"), usedforsecurity=False).hexdigest()


def _song_click_events(
    beats: list[float],
    bars: list[dict],
    multiplier: float,
    accent_mode: int,
    groups: list[int] | None = None,
) -> list[tuple[float, int]]:
    """The (time, level) pair for every beat of the running click track, in
    source time. Shared by the plain click render and the count-in render so the
    click sounds identical whether or not a count-in precedes it."""
    grid = rescale_beats([float(b) for b in beats], multiplier)
    return [
        (t, beat_level(source_index(i, multiplier), bars, accent_mode, groups))
        for i, t in enumerate(grid)
    ]


def _render_events(
    dest: Path, events: list[tuple[float, int]], duration: float, sample_rate: int
) -> Path | None:
    """Stamp a list of (time, level) clicks into a mono WAV of `duration`
    seconds. The single place clicks become audio, so playback parity only has
    to be maintained against the three voices, not against two render paths."""
    total = int(round(duration * sample_rate))
    if not events or total <= 0:
        return None

    import numpy as np

    # float32, not float64: the buffer is one sample per frame for the whole
    # render, so a long export was allocating twice what it needed and then
    # again in the int16 conversion below. The output is 16-bit PCM, so the
    # extra mantissa was never audible (#512).
    buf = np.zeros(total, dtype=np.float32)
    # Only three distinct voices, so render each once and stamp it in.
    voices = {
        LEVEL_WEAK: _voice(CLICK_PEAK, CLICK_FREQ, sample_rate),
        LEVEL_GROUP: _voice(GROUP_PEAK, GROUP_FREQ, sample_rate),
        LEVEL_DOWNBEAT: _voice(ACCENT_PEAK, ACCENT_FREQ, sample_rate),
    }

    for t, level in events:
        start = int(round(t * sample_rate))
        if start >= total or start < 0:
            continue
        voice = voices.get(int(level), voices[LEVEL_WEAK])
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
    groups: list[int] | None = None,
) -> Path | None:
    """Write a mono WAV of the click track spanning the whole track.

    Full length regardless of where the beats start, so the export's region trim
    (`-ss` before every ffmpeg input) lines the click up with the stems without
    any special-casing. Returns the path, or None when there is nothing to
    render.
    """
    if multiplier not in _VALID_MULTIPLIERS:
        multiplier = 1.0
    events = _song_click_events(beats, bars, multiplier, accent_mode, groups)
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
    groups: list[int] | None = None,
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
        beats, bars, count_in_bars, multiplier, accent_mode, start=seg_start, groups=groups
    )
    events: list[tuple[float, int]] = list(count_clicks)
    if include_click:
        for t, level in _song_click_events(beats, bars, multiplier, accent_mode, groups):
            if seg_start <= t < seg_end:
                events.append((t - seg_start + lead_in, level))

    out = _render_events(dest, events, lead_in + seg_len, sample_rate)
    if out is None:
        return None
    logger.info("count-in render: %.3f s lead-in, %d clicks -> %s", lead_in, len(events), dest.name)
    return out, lead_in
