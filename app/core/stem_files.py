"""Where a stem's audio file is, whatever format the song keeps it in (#783).

A library holds songs stored as WAV (every song before #783, and any song made
with the WAV setting) and songs stored as MP3. Every reader that opens a stem
of a finished song goes through `stem_path`, so none of them needs to know
which kind of song it has.

The rule is the file on disk, WAV first:

- A WAV song has `<name>.wav`. It may also have `<name>.mp3`, but that is the
  download cache GET /stems/{name}.mp3 writes beside the WAV (a VBR transcode),
  never the stem itself, so the WAV always wins.
- An MP3 song has only `<name>.mp3`: its WAVs are deleted once the MP3s are
  verified (app/pipeline/stem_storage.py).
- While that step runs, and if it fails, the WAVs are still there and win.

So the answer is right in every state a song's folder can be in, without
reading the job record. `job.stem_format` records what the song was stored as,
for the page and for diagnostics.

MP3 stems decode to exactly the WAV's samples in length and position: the LAME
header ffmpeg writes carries the encoder delay and padding, and ffmpeg,
libsndfile and Chromium's decodeAudioData all honour it (measured, see
tests/test_stem_storage.py). The storage step verifies the length with both
server-side decoders before it deletes anything.
"""

from __future__ import annotations

import contextlib
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

from app.core.config import TIMEOUT_FFMPEG, ffmpeg_executable

# In the order they are looked for. WAV first: see the module docstring.
STEM_EXTS: tuple[str, ...] = (".wav", ".mp3")

_MEDIA_TYPES = {".wav": "audio/wav", ".mp3": "audio/mpeg"}


def stem_path(stems_dir: Path, name: str) -> Path:
    """The file holding stem `name`: `<name>.wav` when it exists, else
    `<name>.mp3` when that exists, else the `.wav` path (which does not
    exist, so a caller's own is_file() check fails as it always did).

    `name` must already be whitelisted by the caller."""
    wav = stems_dir / f"{name}.wav"
    if wav.is_file():
        return wav
    mp3 = stems_dir / f"{name}.mp3"
    if mp3.is_file():
        return mp3
    return wav


def stem_exists(stems_dir: Path, name: str) -> bool:
    return stem_path(stems_dir, name).is_file()


def stem_ext(path: Path) -> str:
    """ "wav" or "mp3" for a stem file."""
    return path.suffix.lower().removeprefix(".")


def stem_media_type(path: Path) -> str:
    return _MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")


def stem_url(job_id: str, stems_dir: Path, name: str) -> str:
    """The URL the page loads stem `name` from, ending in the file's own
    extension so the browser and a download both get the right type."""
    return f"/api/jobs/{job_id}/stems/{name}.{stem_ext(stem_path(stems_dir, name))}"


def stem_names_on_disk(stems_dir: Path) -> list[str]:
    """Every name with a stem file in `stems_dir`, sorted, each once."""
    names: set[str] = set()
    for ext in STEM_EXTS:
        names.update(
            p.stem for p in stems_dir.glob(f"*{ext}") if p.is_file() and not p.name.startswith(".")
        )
    return sorted(names)


def is_stem_audio(path: Path) -> bool:
    return path.suffix.lower() in STEM_EXTS


@contextlib.contextmanager
def decoded_wav(path: Path) -> Iterator[Path]:
    """`path` itself when it is a WAV, else a float32 WAV decoded from it by
    ffmpeg in a temporary folder that is removed afterwards.

    For a reader that hands the stem to a third-party library which may not
    decode MP3 the way ffmpeg does (the vocal split's audio-separator). Raises
    RuntimeError when the decode fails."""
    if path.suffix.lower() == ".wav":
        yield path
        return
    folder = Path(tempfile.mkdtemp(prefix="stemdeck-decode-"))
    try:
        out = folder / f"{path.stem}.wav"
        cmd = [
            ffmpeg_executable(),
            "-nostdin",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-c:a",
            "pcm_f32le",
            "-f",
            "wav",
            "-y",
            str(out),
        ]
        proc = subprocess.run(  # noqa: S603 -- list args, no shell, trusted ffmpeg
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=TIMEOUT_FFMPEG,
        )
        if proc.returncode != 0 or not out.is_file():
            raise RuntimeError(f"could not decode {path.name}")
        yield out
    finally:
        shutil.rmtree(folder, ignore_errors=True)
