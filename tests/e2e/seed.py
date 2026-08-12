"""Build a jobs directory containing one finished track, for the browser tests.

The point is that the tests talk to the real backend: real Range requests for
stems, the real registry, the real endpoints. Only the pipeline is skipped,
because running demucs to test a menu would be absurd.

The stems are genuine PCM16 WAVs rather than placeholder bytes. The chunked
audio engine parses WAV containers itself, so a file that is not really a WAV
loads with no duration and the studio comes up without playback -- which is the
#358 failure mode, and it would make every test here fail for the wrong reason.

Usage:  python tests/e2e/seed.py <jobs-dir>
"""

from __future__ import annotations

import json
import math
import struct
import sys
import time
from pathlib import Path

JOB_ID = "e2e0deadbeef"
TITLE = "E2E Fixture Track"
STEMS = ["vocals", "drums", "bass", "other"]
SAMPLE_RATE = 44100
CHANNELS = 2
DURATION_SEC = 6


def _wav_bytes(freq: float, seconds: int = DURATION_SEC) -> bytes:
    """A short stereo PCM16 tone. Audible content matters: silence would make a
    broken mix indistinguishable from a working one if these tests ever grow
    real audio assertions."""
    frames = SAMPLE_RATE * seconds
    body = bytearray()
    for i in range(frames):
        value = int(12000 * math.sin(2 * math.pi * freq * i / SAMPLE_RATE))
        body += struct.pack("<hh", value, value)

    block_align = CHANNELS * 2
    fmt_chunk = struct.pack(
        "<HHIIHH", 1, CHANNELS, SAMPLE_RATE, SAMPLE_RATE * block_align, block_align, 16
    )
    chunks = b"fmt " + struct.pack("<I", len(fmt_chunk)) + fmt_chunk
    chunks += b"data" + struct.pack("<I", len(body)) + bytes(body)
    return b"RIFF" + struct.pack("<I", 4 + len(chunks)) + b"WAVE" + chunks


def _peaks(points: int = 400) -> list[list[float]]:
    """Matches what the backend writes: min/max pairs per bucket, so the studio
    renders overview waveforms from peaks instead of falling back to decoding
    every stem in the browser."""
    out = []
    for i in range(points):
        amp = abs(math.sin(i / 18.0)) * 0.8
        out.append([round(-amp, 4), round(amp, 4)])
    return out


def seed(jobs_dir: Path) -> str:
    job_dir = jobs_dir / JOB_ID
    stems_dir = job_dir / "stems"
    stems_dir.mkdir(parents=True, exist_ok=True)

    for index, name in enumerate(STEMS):
        (stems_dir / f"{name}.wav").write_bytes(_wav_bytes(220.0 * (index + 1)))

    (job_dir / "peaks.json").write_text(
        json.dumps({name: _peaks() for name in STEMS}), encoding="utf-8"
    )
    (job_dir / "beats.json").write_text(
        json.dumps(
            {
                "bpm": 120.0,
                "beats": [round(i * 0.5, 3) for i in range(DURATION_SEC * 2)],
                "downbeats": [round(i * 2.0, 3) for i in range(DURATION_SEC // 2)],
            }
        ),
        encoding="utf-8",
    )

    # Field names are the dataclass's, not the API's: from_record filters on
    # Job's own fields, so "stage" or "duration" would be silently dropped and
    # the track would load without a duration.
    record = {
        "id": JOB_ID,
        "status": "done",
        "progress": 1.0,
        "stage_message": "Done",
        "title": TITLE,
        "duration_sec": float(DURATION_SEC),
        "source_url": "local:e2e-fixture.wav",
        # Now, not a fixed date. The hourly sweep deletes job directories older
        # than JOB_TTL_SECONDS, and it runs at startup: a fixture with a
        # hardcoded timestamp is reaped before the first test opens the page,
        # leaving a registry entry pointing at nothing.
        "created_at": time.time(),
        "bpm": 120,
        "key": "C maj",
        "scale": "Major",
        # Per-stem RMS as 0-100 ints, which is what drives the presence cards.
        "stem_presence": {name: 80 for name in STEMS},
        # Same shape the pipeline writes (runner.py): entries, not bare names.
        # A list of strings deserialises without error and then leaves the
        # studio with nothing to play.
        "stems": [{"name": name, "url": f"/api/jobs/{JOB_ID}/stems/{name}.wav"} for name in STEMS],
    }
    (jobs_dir / "registry.json").write_text(
        json.dumps({"version": 1, "jobs": [record]}, indent=2) + "\n", encoding="utf-8"
    )
    return JOB_ID


if __name__ == "__main__":
    target = Path(sys.argv[1]).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    print(seed(target))
