"""Moving LRCLIB's timing onto a track from its vocals stem (lyrics_align.py).

Synthetic envelopes, 40 ms a level: silence, and singing where a phrase is
placed. A real track shifted by a known amount was checked by hand; the
report of the change that added this says what it recovered.
"""

from __future__ import annotations

import json
import os
import random
from pathlib import Path

import numpy as np
import pytest

from app.core.models import Job
from app.pipeline.lyrics_align import (
    align_lyrics,
    estimate_offset,
    line_starts,
    onset_strength,
    shift_lrc,
)
from app.pipeline.lyrics_lookup import read_lyrics, write_lyrics

HOP = 0.04

# Twenty lines at uneven gaps, as a song has them.
LINES = [12.0, 15.5, 19.2, 23.0, 30.4, 33.1, 37.9, 41.2, 55.0, 58.3,
         62.7, 66.0, 70.5, 88.2, 91.0, 95.6, 99.9, 103.4, 120.0, 124.8]  # fmt: skip


def envelope(onsets, seconds=200.0, length=1.5, level=-15, silence=-90):
    db = np.full(round(seconds / HOP), silence)
    for start in onsets:
        a, b = round(start / HOP), round((start + length) / HOP)
        db[max(0, a) : max(0, b)] = level
    return db.tolist()


def lrc(starts) -> str:
    return "\n".join(f"[{int(t // 60):02d}:{t % 60:05.2f}]Line {i}" for i, t in enumerate(starts))


def estimate(starts, onsets, **kwargs):
    return estimate_offset(starts, onset_strength(envelope(onsets, **kwargs)), HOP)


# ── the estimate ──


def test_a_known_shift_is_recovered():
    assert abs(estimate(LINES, [t + 7 for t in LINES]) - 7) <= HOP
    assert abs(estimate(LINES, [t - 9.5 for t in LINES]) + 9.5) <= HOP


def test_it_survives_a_sloppy_singer_missing_lines_and_backing_vocals():
    rng = random.Random(4)
    onsets = [t + 7 + rng.uniform(-0.15, 0.15) for t in LINES]
    del onsets[3], onsets[9], onsets[15]
    onsets += [140.0, 150.0, 160.0, 5.0]
    assert abs(estimate(LINES, sorted(onsets)) - 7) <= 0.15


def test_singing_that_matches_no_shift_is_not_confident():
    rng = random.Random(7)
    onsets = sorted(rng.uniform(0, 190) for _ in range(40))
    assert estimate(LINES, onsets) is None


def test_a_song_with_no_distinguishing_rhythm_is_not_confident():
    """Every line four seconds apart, and so every phrase: a shift of four
    seconds lines them up as well as the right one."""
    lines = [10.0 + 4 * i for i in range(20)]
    assert estimate(lines, [t + 2 for t in lines], length=1.0) is None


def test_no_vocals_is_no_estimate():
    assert not onset_strength(envelope([])).any()
    assert estimate(LINES, []) is None


def test_too_few_lines_is_no_estimate():
    assert estimate(LINES[:3], [t + 7 for t in LINES[:3]]) is None


def test_strength_is_where_singing_starts_not_where_it_holds():
    strength = onset_strength(envelope([10.0, 30.0], seconds=60.0, length=5))
    rising = np.flatnonzero(strength) * HOP
    assert all(abs(t - 10) < 0.2 or abs(t - 30) < 0.2 for t in rising), rising


# ── the LRC ──


def test_line_starts_skip_gaps_and_apply_the_offset_tag():
    text = "[ar:X]\n[offset:+500]\n[00:10.00]One\n[00:12.00]\n[00:14.00][01:00.00]Two"
    assert line_starts(text) == [9.5, 13.5, 59.5]


def test_shifting_moves_line_and_word_stamps_and_folds_the_offset_in():
    text = "[offset:+1000]\n[00:10.00]<00:10.00>One <00:10.50>two\n[00:00.50]Early"
    assert shift_lrc(text, 7) == "[00:16.00]<00:16.00>One <00:16.50>two\n[00:06.50]Early"
    assert shift_lrc("[00:00.50]Early", -2) == "[00:00.00]Early", "never before zero"
    assert shift_lrc("[01:59.99]x", 0.02) == "[02:00.01]x"


# ── on a job ──


def _job_with_lyrics(tmp_path: Path, timing: str, onsets) -> Job:
    job = Job(id="abcdefabc400", duration_sec=200.0)
    stems = tmp_path / "stems"
    stems.mkdir()
    (stems / "vocals.wav").write_bytes(b"RIFF")
    kept = stems / "vocal_envelope.json"
    kept.write_text(json.dumps({"hop": HOP, "db": envelope(onsets)}), encoding="utf-8")
    later = (stems / "vocals.wav").stat().st_mtime_ns + 10**9
    os.utime(kept, ns=(later, later))
    entry = {
        "v": 1,
        "source": "lrclib",
        "track": "T",
        "artist": "A",
        "album": "",
        "duration": 193.0,
        "synced": lrc(LINES),
        "plain": "",
        "instrumental": False,
        "timing": timing,
        "others": [],
        "lrclib_id": 5,
    }
    assert write_lyrics(job, tmp_path, entry)
    return job


def test_unverified_lyrics_are_moved_onto_the_track(tmp_path: Path):
    job = _job_with_lyrics(tmp_path, "unverified", [t + 7 for t in LINES])
    assert align_lyrics(job, tmp_path) == "shifted"
    kept = read_lyrics(tmp_path)
    assert kept["timing"] == "shifted"
    assert np.allclose(line_starts(kept["synced"]), [t + 7 for t in LINES], atol=0.05)


def test_a_fit_that_is_not_clear_leaves_them_as_they_were(tmp_path: Path):
    job = _job_with_lyrics(tmp_path, "unverified", [])
    before = read_lyrics(tmp_path)
    assert align_lyrics(job, tmp_path) == "unverified"
    assert read_lyrics(tmp_path) == before


def test_a_version_the_tracks_length_timed_to_another_cut_is_moved_too(tmp_path: Path):
    """The same length proves nothing: Green Day's "Basket Case" video sings
    16 s after LRCLIB's copy of the same length starts its lines."""
    job = _job_with_lyrics(tmp_path, "exact", [t + 7 for t in LINES])
    assert align_lyrics(job, tmp_path) == "shifted"
    assert line_starts(read_lyrics(tmp_path)["synced"])[0] == pytest.approx(LINES[0] + 7, abs=0.1)


def test_exact_timing_within_a_second_is_left_alone(tmp_path: Path):
    job = _job_with_lyrics(tmp_path, "exact", [t + 0.5 for t in LINES])
    before = read_lyrics(tmp_path)
    assert align_lyrics(job, tmp_path) == "exact"
    assert read_lyrics(tmp_path) == before


def test_a_transcription_is_the_tracks_own_timing_and_never_moved(tmp_path: Path):
    job = _job_with_lyrics(tmp_path, "exact", [t + 7 for t in LINES])
    entry = read_lyrics(tmp_path)
    write_lyrics(job, tmp_path, {**entry, "source": "whisper"})
    before = read_lyrics(tmp_path)
    assert align_lyrics(job, tmp_path) == "exact"
    assert read_lyrics(tmp_path) == before


def test_no_vocals_stem_leaves_them_unverified(tmp_path: Path):
    job = _job_with_lyrics(tmp_path, "unverified", [t + 7 for t in LINES])
    (tmp_path / "stems" / "vocals.wav").unlink()
    assert align_lyrics(job, tmp_path) == "unverified"


def test_a_pipeline_keeping_a_version_of_another_length_moves_it_onto_the_track(
    tmp_path: Path, monkeypatch
):
    """LyricsLookup.finish runs once separation is done, so the vocals stem
    is there: the version is kept, then moved by what the stem shows."""
    import app.pipeline.lyrics_lookup as ll
    from app.pipeline.lyrics_lookup import LyricsLookup

    job = _job_with_lyrics(tmp_path, "exact", [t + 7 for t in LINES])
    (tmp_path / "lyrics.json").unlink()
    job.audio_tags = {"artist": "A", "title": "T"}
    version = {
        "id": 5,
        "trackName": "T",
        "artistName": "A",
        "albumName": "",
        "duration": 193.0,
        "instrumental": False,
        "syncedLyrics": lrc(LINES),
        "plainLyrics": "words",
    }
    monkeypatch.setattr(
        ll, "_fetch_json", lambda endpoint, params: None if endpoint == "get" else [version]
    )
    LyricsLookup.start(job, tmp_path).finish(job, tmp_path, 5)
    kept = read_lyrics(tmp_path)
    assert (kept["lrclib_id"], kept["timing"]) == (5, "shifted")
    assert np.allclose(line_starts(kept["synced"]), [t + 7 for t in LINES], atol=0.05)
