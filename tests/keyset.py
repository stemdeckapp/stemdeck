"""A labelled set of synthetic songs for measuring key detection (#726).

Each song is a chord loop over a bass line, rendered to audio with numpy, so
the right answer is known and nothing is downloaded. The harmony and the bass
come back separately as well as mixed, which is what lets the full-mix path
and the stems path be scored on the same material.

The progressions are the ones that make relative keys hard: a minor loop such
as i-VI-III-VII uses exactly the notes of its relative major. Like real songs,
each loop starts on its tonic chord and the song ends on it.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass

import numpy as np

SR = 22050
PITCHES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")

# Chords as (semitones above the tonic, quality). "M" major, "m" minor.
PROGRESSIONS: dict[str, tuple[str, list[tuple[int, str]]]] = {
    # Major keys
    "I-V-vi-IV": ("maj", [(0, "M"), (7, "M"), (9, "m"), (5, "M")]),
    "I-IV-V-I": ("maj", [(0, "M"), (5, "M"), (7, "M"), (0, "M")]),
    "I-vi-IV-V": ("maj", [(0, "M"), (9, "m"), (5, "M"), (7, "M")]),
    "I-bVII-IV-I": ("maj", [(0, "M"), (10, "M"), (5, "M"), (0, "M")]),
    "vi-IV-I-V": ("maj", [(9, "m"), (5, "M"), (0, "M"), (7, "M")]),
    # Natural minor keys
    "i-VI-III-VII": ("min", [(0, "m"), (8, "M"), (3, "M"), (10, "M")]),
    "i-VII-VI-VII": ("min", [(0, "m"), (10, "M"), (8, "M"), (10, "M")]),
    "i-iv-v-i": ("min", [(0, "m"), (5, "m"), (7, "m"), (0, "m")]),
    "i-III-VII-iv": ("min", [(0, "m"), (3, "M"), (10, "M"), (5, "m")]),
    # Harmonic minor: a major V carries the raised seventh
    "i-iv-V-i": ("hmin", [(0, "m"), (5, "m"), (7, "M"), (0, "m")]),
    "i-VI-iv-V": ("hmin", [(0, "m"), (8, "M"), (5, "m"), (7, "M")]),
}

# Every progression in three keys, spread round the circle.
TONICS = (11, 4, 7)  # B, E, G


@dataclass
class Song:
    name: str
    tonic: int
    mode: str  # "maj", "min" or "hmin"
    harmony: np.ndarray
    bass: np.ndarray

    @property
    def mix(self) -> np.ndarray:
        return self.harmony + self.bass

    @property
    def label(self) -> str:
        return f"{PITCHES[self.tonic]} {'maj' if self.mode == 'maj' else 'min'}"

    @property
    def scale(self) -> str:
        return {"maj": "Major", "min": "Natural Minor", "hmin": "Harmonic Minor"}[self.mode]


def _tone(freq: float, n: int, harmonics: int, rng: np.random.Generator) -> np.ndarray:
    t = np.arange(n) / SR
    phase = rng.uniform(0, 2 * np.pi)
    out = np.zeros(n)
    for h in range(1, harmonics + 1):
        out += np.sin(2 * np.pi * freq * h * t + phase * h) / h
    # A short attack and release, so chord changes are not clicks.
    env = np.minimum(1.0, np.minimum(t / 0.02, (n / SR - t) / 0.05))
    return out * np.clip(env, 0, 1)


def _midi_freq(midi: int) -> float:
    return 440.0 * 2 ** ((midi - 69) / 12)


def render(name: str, tonic: int, bars_per_chord: int = 1, loops: int = 4, bpm: int = 110) -> Song:
    mode, chords = PROGRESSIONS[name]
    rng = np.random.default_rng(zlib.crc32(f"{name}/{tonic}".encode()))
    bar = int(SR * 4 * 60 / bpm) * bars_per_chord
    harmony: list[np.ndarray] = []
    bass: list[np.ndarray] = []
    sequence = chords * loops + [chords[0]]  # end on the tonic chord
    for offset, quality in sequence:
        root = tonic + offset
        third = 4 if quality == "M" else 3
        chord = np.zeros(bar)
        for interval in (0, third, 7):
            chord += _tone(_midi_freq(60 + (root + interval) % 12), bar, 6, rng)
        harmony.append(0.12 * chord)
        # Root on the beat, fifth on the offbeats: a plain rock bass line.
        beat = bar // (4 * bars_per_chord)
        line = np.zeros(bar)
        for b in range(4 * bars_per_chord):
            note = root if b % 2 == 0 else root + 7
            line[b * beat : (b + 1) * beat] = _tone(_midi_freq(36 + note % 12), beat, 4, rng)
        bass.append(0.25 * line)
    return Song(name, tonic % 12, mode, np.concatenate(harmony), np.concatenate(bass))


def songs() -> list[Song]:
    return [render(name, tonic) for name in PROGRESSIONS for tonic in TONICS]
