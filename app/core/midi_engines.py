"""The MIDI engines a stem can be transcribed with: what each is and takes.

Plain facts, for every process: the settings (which engine a stem may be
set to), the status route, the model list and the run itself
(app/pipeline/midi.py, which keeps how each engine is run beside this). No
engine code is imported here.

Each engine is one MidiEngine, registered under its id. The three built in
are registered below from config.MIDI_ENGINE_STEMS. An engine whose weights
the person brings is meant to be registered the same way, with source
"user", and an adapter in app/pipeline/midi.py that says how to run it: the
per-stem settings, the status and the UI take whatever is registered for a
stem, so nothing else has to change for it.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from app.core.config import (
    MIDI_DEFAULT_ENGINES,
    MIDI_ENGINE_STEMS,
    MIDI_STEMS,
    PIANO_HR_CHECKPOINT_SIZE,
    YMT3_CHECKPOINT_SIZE,
    YMT3_WEIGHTS_SIZE,
)


@dataclass(frozen=True)
class MidiEngine:
    """One engine.

    stems: what it transcribes, in config.MIDI_STEMS order.
    gpu: whether it runs on CUDA when the separation device resolves to it
      (else always on the CPU).
    download_bytes: what its first run downloads, None when it is built in.
    disk_bytes: what it keeps on disk once ready, None when built in.
    uses_detail: whether settings.transcribe_detail changes its notes.
    source: "built_in", or "user" for weights the person brings."""

    id: str
    label: str
    licence: str
    model_licence: str
    stems: tuple[str, ...]
    gpu: bool
    download_bytes: int | None
    disk_bytes: int | None
    uses_detail: bool = True
    source: str = "built_in"


_lock = threading.Lock()
_engines: dict[str, MidiEngine] = {}


def register(engine: MidiEngine) -> None:
    """Add ``engine``, or replace the one with its id. Its stems must be
    stems MIDI is made from."""
    if not engine.id or not engine.id.replace("_", "").isalnum() or engine.id != engine.id.lower():
        raise ValueError("an engine id is lowercase letters, digits and underscores")
    if not engine.stems or any(s not in MIDI_STEMS for s in engine.stems):
        raise ValueError("an engine transcribes one or more of config.MIDI_STEMS")
    with _lock:
        _engines[engine.id] = engine


def get(engine_id: object) -> MidiEngine | None:
    if not isinstance(engine_id, str):
        return None
    with _lock:
        return _engines.get(engine_id)


def all_engines() -> tuple[MidiEngine, ...]:
    """Every engine, in the order they were registered."""
    with _lock:
        return tuple(_engines.values())


def ids() -> tuple[str, ...]:
    return tuple(e.id for e in all_engines())


def for_stem(stem: str) -> tuple[str, ...]:
    """The engines that can transcribe ``stem``, in registration order."""
    return tuple(e.id for e in all_engines() if stem in e.stems)


def default_for(stem: str) -> str:
    """The engine ``stem`` uses until the person picks one."""
    return MIDI_DEFAULT_ENGINES[stem]


register(
    MidiEngine(
        id="basic_pitch",
        label="Basic Pitch",
        licence="Apache-2.0",
        model_licence="Apache-2.0",
        stems=MIDI_ENGINE_STEMS["basic_pitch"],
        gpu=False,
        download_bytes=None,
        disk_bytes=None,
    )
)
register(
    MidiEngine(
        id="piano_hr",
        label="ByteDance Piano Transcription",
        licence="MIT",
        model_licence="CC BY 4.0",
        stems=MIDI_ENGINE_STEMS["piano_hr"],
        gpu=True,
        download_bytes=PIANO_HR_CHECKPOINT_SIZE,
        disk_bytes=PIANO_HR_CHECKPOINT_SIZE,
    )
)
register(
    MidiEngine(
        id="ymt3",
        label="YourMT3+",
        licence="Apache-2.0",
        model_licence="Apache-2.0",
        stems=MIDI_ENGINE_STEMS["ymt3"],
        gpu=True,
        # The checkpoint as published; what stays is the weights made from it.
        download_bytes=YMT3_CHECKPOINT_SIZE,
        disk_bytes=YMT3_WEIGHTS_SIZE,
        # It has no thresholds: every note it decodes is one it is sure of.
        uses_detail=False,
    )
)

# Every stem has a default, and it can run that stem.
if any(default_for(s) not in for_stem(s) for s in MIDI_STEMS):
    raise RuntimeError("config.MIDI_DEFAULT_ENGINES names an engine that cannot run its stem")
