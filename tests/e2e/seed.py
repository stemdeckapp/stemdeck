"""Build a jobs directory containing two finished tracks, for the browser tests.

The point is that the tests talk to the real backend: real Range requests for
stems, the real registry, the real endpoints. Only the pipeline is skipped,
because running demucs to test a menu would be absurd.

The stems are genuine PCM16 WAVs rather than placeholder bytes. The chunked
audio engine parses WAV containers itself, so a file that is not really a WAV
loads with no duration and the studio comes up without playback -- which is the
#358 failure mode, and it would make every test here fail for the wrong reason.

Each artifact has to live where the real pipeline puts it -- both peaks.json and
beats.json under stems/ -- or the endpoint that serves it 404s and the feature
it drives is quietly untestable. Both were in the job root once: the studio fell
back to decoding every stem for its waveforms, and the click track never
appeared at all.

There are two jobs, and they deliberately share one `source_url`. That is the
whole shape of #542: the catalog deduplicates imports by source URL, so a second
job for a URL the library already knows is what makes the dedup branch run at
all. Processing the same link twice is ordinary user behaviour, and until #542
it silently evicted a trashed track. A fixture with a single job cannot reach
that code path.

The sibling is shorter than the fixture track. Nothing needs six seconds of it,
and the tone generator is a per-sample Python loop, so the extra seconds are
paid on every single run of the suite.

Both jobs are real and complete -- stems, peaks and beats -- so a later test can
open either one. A half-built second job would look usable in the registry and
fail only once someone clicked it.

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
DURATION_SEC = 6

# A second extraction of the same source. Same `source_url` as the fixture
# track, which is the point: see the module docstring.
SIBLING_JOB_ID = "e2e0cafebabe"
SIBLING_TITLE = "E2E Fixture Track (again)"
SIBLING_DURATION_SEC = 2

SOURCE_URL = "local:e2e-fixture.wav"
STEMS = ["vocals", "drums", "bass", "other"]
SAMPLE_RATE = 44100
CHANNELS = 2


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


def _build_job(jobs_dir: Path, job_id: str, title: str, seconds: int) -> dict:
    """Write one finished job's files and return its registry record."""
    stems_dir = jobs_dir / job_id / "stems"
    stems_dir.mkdir(parents=True, exist_ok=True)

    for index, name in enumerate(STEMS):
        (stems_dir / f"{name}.wav").write_bytes(_wav_bytes(220.0 * (index + 1), seconds))

    (stems_dir / "peaks.json").write_text(
        json.dumps({name: _peaks() for name in STEMS}), encoding="utf-8"
    )
    # stems/beats.json, not <job>/beats.json: that is where the pipeline writes
    # it and the only place GET /api/jobs/{id}/beats looks (_beats_paths). Put
    # it in the job root and the endpoint 404s, the studio reports "No beat grid
    # for this track", and every click-track control stays disabled -- so the
    # click track, the count-in and the grid editor were untestable in a browser
    # while looking, from the fixture, as though they were covered.
    #
    # Shape matches what beatgrid.py emits, `bars` included: with no bar marks
    # the accent mode falls back to "Auto (none found)" and the detected-meter
    # path never runs.
    beats = [round(i * 0.5, 3) for i in range(seconds * 2)]
    (stems_dir / "beats.json").write_text(
        json.dumps(
            {
                "version": 1,
                "source": "drums",
                "detector": "e2e-fixture",
                # One 4/4 region from the first beat: 120 BPM, a downbeat every
                # 2 s. Enough for "Auto (detected)" and a one-bar count-in.
                "bars": [{"beat": 0, "beats_per_bar": 4}],
                "bpm": 120.0,
                "duration": float(seconds),
                "confidence": 95,
                # The grid editor snaps dragged beats onto these.
                "onsets": beats,
                "beats": beats,
            }
        ),
        encoding="utf-8",
    )

    # Field names are the dataclass's, not the API's: from_record filters on
    # Job's own fields, so "stage" or "duration" would be silently dropped and
    # the track would load without a duration.
    return {
        "id": job_id,
        "status": "done",
        "progress": 1.0,
        "stage_message": "Done",
        "title": title,
        "duration_sec": float(seconds),
        "source_url": SOURCE_URL,
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
        "stems": [{"name": name, "url": f"/api/jobs/{job_id}/stems/{name}.wav"} for name in STEMS],
    }


def seed(jobs_dir: Path) -> list[str]:
    records = [
        _build_job(jobs_dir, JOB_ID, TITLE, DURATION_SEC),
        _build_job(jobs_dir, SIBLING_JOB_ID, SIBLING_TITLE, SIBLING_DURATION_SEC),
    ]
    (jobs_dir / "registry.json").write_text(
        json.dumps({"version": 1, "jobs": records}, indent=2) + "\n", encoding="utf-8"
    )
    return [record["id"] for record in records]


if __name__ == "__main__":
    target = Path(sys.argv[1]).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    for job_id in seed(target):
        print(job_id)
