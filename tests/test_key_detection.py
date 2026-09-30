"""Telling a minor key from its relative major (#726).

A minor loop such as i-VI-III-VII uses exactly the notes of its relative
major, so a whole-song pitch histogram cannot separate them. The detector used
to break the tie by how loud each candidate's root was, and in a minor song
the relative major's root is usually louder: every i-VI-III-VII came out as
its relative major, at up to 100% confidence. "Plug in Baby", in B minor,
came out as D major.

These score the detector on tests/keyset.py, synthetic songs whose keys are
known. On that set the old detector got 27 of 33 keys and 21 of 33 keys with
their scale; it never reported harmonic minor.
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest

from app.core.models import Job
from app.pipeline import analyze as az
from tests import keyset
from tests.ffmpeg_probe import skip_without_ffmpeg


def _stems_key(song: keyset.Song) -> tuple[str, str, int]:
    result = az.detect_key_from_audio(
        song.harmony.astype(np.float32), song.bass.astype(np.float32), keyset.SR
    )
    assert result is not None
    return result


def _mix_key(song: keyset.Song) -> tuple[str, str, int]:
    import librosa

    harmonic, _ = librosa.effects.hpss(song.mix.astype(np.float32))
    result = az.detect_key_from_audio(harmonic, None, keyset.SR)
    assert result is not None
    return result


def test_the_reported_loop_is_minor_from_the_mix():
    # B minor, i-VI-III-VII: the old detector said D major.
    song = keyset.render("i-VI-III-VII", 11)
    label, scale, _ = _mix_key(song)
    assert (label, scale) == ("B min", "Natural Minor")


@pytest.mark.parametrize(
    "name, tonic",
    [("i-VI-III-VII", 11), ("i-VII-VI-VII", 7), ("I-V-vi-IV", 4), ("i-iv-v-i", 4)],
)
def test_keys_from_stems(name, tonic):
    song = keyset.render(name, tonic)
    label, scale, _ = _stems_key(song)
    assert (label, scale) == (song.label, song.scale)


@pytest.mark.parametrize("name", ["i-iv-V-i", "i-VI-iv-V"])
def test_a_major_five_reads_as_harmonic_minor(name):
    song = keyset.render(name, 4)
    label, scale, _ = _stems_key(song)
    assert (label, scale) == ("E min", "Harmonic Minor")


def test_the_whole_labelled_set_from_stems():
    # 30 of 33 when this was written. The three misses are vi-IV-I-V, which
    # starts and ends on its minor chord and reads as minor: ambiguous enough
    # that the set's "major" label is arguable.
    songs = keyset.songs()
    right = sum(_stems_key(s)[:2] == (s.label, s.scale) for s in songs)
    assert right >= 30, f"{right} of {len(songs)}"


def test_confidence_does_not_depend_on_level():
    # It did: the old score multiplied correlation by a raw chroma value, so
    # the same vector read 100% raw and 21% normalised.
    vec = [0.9, 0.05, 0.4, 0.05, 0.7, 0.4, 0.05, 0.8, 0.05, 0.5, 0.05, 0.4]
    edges = [([1.0, 0, 0, 0, 0.8, 0, 0, 0.9, 0, 0, 0, 0], [1.0] + [0.0] * 11)]
    for with_edges in (None, edges):
        loud = az._detect_key([v * 5 for v in vec], with_edges)
        quiet = az._detect_key([v / 5 for v in vec], with_edges)
        assert loud == quiet


def test_edges_decide_between_a_key_and_its_relative():
    # The same histogram, opened on a C major chord or on an A minor one.
    diatonic = [1.0, 0.05, 0.8, 0.05, 0.9, 0.8, 0.05, 0.9, 0.05, 0.9, 0.05, 0.7]
    c_chord = [1.0, 0, 0, 0, 0.9, 0, 0, 0.9, 0, 0, 0, 0]
    a_chord = [0.9, 0, 0, 0, 0.9, 0, 0, 0, 0, 1.0, 0, 0]
    c_bass = [1.0] + [0.0] * 11
    a_bass = [0.0] * 9 + [1.0, 0.0, 0.0]
    assert az._detect_key(diatonic, [(c_chord, c_bass)])[0] == "C maj"
    assert az._detect_key(diatonic, [(a_chord, a_bass)])[0] == "A min"


# ── refine_key_from_stems ────────────────────────────────────────────


def _write_wav(path: Path, samples: np.ndarray, sr: int) -> None:
    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def _job(key="D maj", scale="Major") -> Job:
    job = Job(id="a1b2c3d4e5f6")
    job.key, job.scale, job.key_confidence = key, scale, 90
    return job


def _stems_dir(job: Job) -> Path:
    # The decoder only reads inside the jobs directory, which the test
    # fixtures point at a temporary one.
    d = az.JOBS_DIR / job.id / "stems"
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_stems_replace_the_mix_estimate():
    skip_without_ffmpeg()
    song = keyset.render("i-VI-III-VII", 11)
    job = _job()
    stems = _stems_dir(job)
    _write_wav(stems / "other.wav", song.harmony, keyset.SR)
    _write_wav(stems / "bass.wav", song.bass, keyset.SR)
    az.refine_key_from_stems(job, stems)
    assert (job.key, job.scale) == ("B min", "Natural Minor")


def test_no_stems_keeps_the_mix_estimate():
    job = _job()
    az.refine_key_from_stems(job, _stems_dir(job))
    assert (job.key, job.scale, job.key_confidence) == ("D maj", "Major", 90)


def test_a_failure_keeps_the_mix_estimate(monkeypatch):
    skip_without_ffmpeg()
    song = keyset.render("i-VI-III-VII", 11)
    job = _job()
    stems = _stems_dir(job)
    _write_wav(stems / "other.wav", song.harmony, keyset.SR)

    def boom(*_a, **_k):
        raise RuntimeError("chroma exploded")

    monkeypatch.setattr(az, "detect_key_from_audio", boom)
    az.refine_key_from_stems(job, stems)
    assert (job.key, job.scale, job.key_confidence) == ("D maj", "Major", 90)
