"""The last pipeline stage: keep a finished song's stems as WAV or MP3 (#783).

Separation and every analysis stage run on WAV exactly as they always did.
Only once the song has nothing left to read from its stems does this stage
look at the Stem storage format setting (read now, so a change applies to the
next song without a restart):

- WAV: nothing is touched. The job records "wav".
- MP3: every stem file in stems/ (the six stems, original, mix) is encoded by
  ffmpeg's libmp3lame at a constant bitrate into a temporary file, and each
  one is verified: ffmpeg and libsndfile, the two decoders the server reads
  stems with, must both decode it to exactly the WAV's frame count, and the
  start of the decoded audio must line up with the WAV's. Only when every stem
  passed are the MP3s moved into place (rename, so never half-written) and the
  WAVs deleted.

Any failure (ffmpeg without libmp3lame, a full disk, a stall, an output that
does not decode or does not line up) removes the temporary files, keeps every
WAV and records the song as "wav". The song finishes either way: this stage
never fails a job. A cancel raises JobCancelled, after the same cleanup.

Sample accuracy: libmp3lame adds an encoder delay and padding. ffmpeg writes
both into the LAME/Xing header of the first frame (-write_xing 1, its
default, made explicit below), and ffmpeg, libsndfile (mpg123) and Chromium's
decodeAudioData all trim by it, so a decoded MP3 starts on the same sample as
the WAV and has the same length. tests/test_stem_storage.py measures it.

`original.wav` here is the complement backing track (make_original_track),
a stem like the others, so it is stored the same way. The song's own source is
not in stems/ and is never touched: an upload's kept source.<ext> stays as it
was uploaded.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path

from app.core.config import (
    EXTRA_STEM_NAMES,
    STEM_NAMES,
    TIMEOUT_FFMPEG,
    TIMEOUT_STEM_TRANSCODE_STALL,
    ffmpeg_executable,
)
from app.core.models import Job, JobCancelled, _set
from app.core.registry import set_proc
from app.core.settings import get_stem_format, get_stem_mp3_kbps
from app.core.stem_files import stem_url

logger = logging.getLogger("stemdeck.pipeline")

# Every stem file a finished song can hold.
STORED_NAMES: tuple[str, ...] = (*STEM_NAMES, *EXTRA_STEM_NAMES, "original", "mix")
# The alignment check compares the loudest block of this many seconds of each
# stem, found in one sequential pass over both files (no seeking, constant
# memory). After the exact length check it catches an output that decodes to
# the right length but not to the right place.
_ALIGN_BLOCK_SEC = 10
# A block quieter than this RMS is near silence: a stem that never plays has
# nothing to line up, so only its length is checked.
_ALIGN_MIN_RMS = 0.01
# The lags searched either side of 0, in samples: past the largest offset a
# decoder that ignored the LAME header would add (1105 = 576 encoder delay +
# 529 decoder delay at 44.1 kHz).
_ALIGN_MAX_LAG = 4096
# A correct encode correlates at about 0.99 at lag 0 even at 128 kbps.
# Refused: a block whose lag-0 correlation is under the floor (not this audio),
# or clearly below the best one within the searched lags (shifted). The ratio
# leaves room for steady tones, whose correlation repeats every period.
_ALIGN_MIN_CORR = 0.5
_ALIGN_MIN_RATIO = 0.98
# The encoder's output is shown to nobody. Kept for the log line on failure.
_TAIL_LINES = 15


class StemStorageError(RuntimeError):
    """The MP3s could not be made or did not verify. The WAVs stay."""


def _encode_cmd(src: Path, dest: Path, kbps: int) -> list[str]:
    """One stem to CBR MP3. Module-level seam: tests swap in a stub encoder.

    `-progress pipe:1` prints a status block about once a second, which is
    what the stall watchdog listens to."""
    return [
        ffmpeg_executable(),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostats",
        "-progress",
        "pipe:1",
        "-i",
        str(src),
        "-map",
        "0:a:0",
        "-c:a",
        "libmp3lame",
        "-b:a",
        f"{kbps}k",
        # The LAME header carries the encoder delay and padding, which is
        # what makes the decoded MP3 sample-aligned with the WAV.
        "-write_xing",
        "1",
        "-f",
        "mp3",
        "-y",
        str(dest),
    ]


def _encode(job: Job, src: Path, dest: Path, kbps: int) -> None:
    """Run the encoder for one stem, registered for cancel, under a stall
    watchdog. Raises JobCancelled or StemStorageError."""
    try:
        proc = subprocess.Popen(  # noqa: S603 -- list args, no shell, trusted ffmpeg
            _encode_cmd(src, dest, kbps),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        raise StemStorageError("the MP3 encoder could not be started") from exc
    set_proc(job.id, proc)
    last_output = [time.monotonic()]
    stalled = threading.Event()
    done = threading.Event()
    err_tail: list[str] = []

    def _watchdog() -> None:
        while not done.wait(timeout=0.2):
            if job.cancel_requested:
                proc.terminate()
                return
            if time.monotonic() - last_output[0] > TIMEOUT_STEM_TRANSCODE_STALL:
                logger.warning(
                    "[%s] stem encode silent for %ss; stopping it",
                    job.id,
                    TIMEOUT_STEM_TRANSCODE_STALL,
                )
                stalled.set()
                proc.terminate()
                return

    def _drain_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            err_tail.append(line.rstrip())
            del err_tail[:-_TAIL_LINES]

    watch = threading.Thread(target=_watchdog, daemon=True)
    drain = threading.Thread(target=_drain_stderr, daemon=True)
    watch.start()
    drain.start()
    try:
        assert proc.stdout is not None
        for _line in proc.stdout:
            last_output[0] = time.monotonic()
        try:
            proc.wait(timeout=TIMEOUT_FFMPEG)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise StemStorageError("the MP3 encoder did not exit") from None
    finally:
        done.set()
        set_proc(job.id, None)
        watch.join(timeout=2)
        drain.join(timeout=2)
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
    if job.cancel_requested:
        raise JobCancelled()
    if stalled.is_set():
        raise StemStorageError("the MP3 encoder stalled")
    if proc.returncode != 0:
        detail = " | ".join(err_tail[-3:]) or f"exit {proc.returncode}"
        raise StemStorageError(f"the MP3 encoder failed: {detail}")
    if not dest.is_file() or dest.stat().st_size == 0:
        raise StemStorageError("the MP3 encoder wrote nothing")


def _ffmpeg_frames(job: Job, path: Path, channels: int) -> int:
    """Frames ffmpeg decodes `path` to: the whole file, counted as it streams."""
    cmd = [
        ffmpeg_executable(),
        "-nostdin",
        "-loglevel",
        "error",
        "-i",
        str(path),
        "-map",
        "0:a:0",
        "-ac",
        str(channels),
        "-c:a",
        "pcm_s16le",
        "-f",
        "s16le",
        "-",
    ]
    proc = subprocess.Popen(  # noqa: S603 -- list args, no shell, trusted ffmpeg
        cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )
    set_proc(job.id, proc)
    total = 0
    try:
        assert proc.stdout is not None
        while chunk := proc.stdout.read(1 << 20):
            total += len(chunk)
        proc.wait(timeout=TIMEOUT_FFMPEG)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise StemStorageError("the MP3 check did not finish") from None
    finally:
        set_proc(job.id, None)
    if job.cancel_requested:
        raise JobCancelled()
    if proc.returncode != 0:
        raise StemStorageError("the MP3 does not decode")
    return total // (2 * channels)


def verify_mp3(job: Job, wav: Path, mp3: Path) -> None:
    """Raise StemStorageError unless `mp3` decodes to `wav`'s exact length in
    both ffmpeg and libsndfile, at its rate and channel count, and its
    loudest block lines up with the WAV's (cross-correlation peak at lag 0)."""
    import numpy as np
    import soundfile as sf

    try:
        ref = sf.info(str(wav))
        got = sf.info(str(mp3))
    except Exception as exc:  # libsndfile raises its own types
        raise StemStorageError("the MP3 cannot be read by libsndfile") from exc
    if (got.samplerate, got.channels) != (ref.samplerate, ref.channels):
        raise StemStorageError("the MP3 has another rate or channel count")
    if got.frames != ref.frames:
        raise StemStorageError(f"libsndfile decodes {got.frames} frames, the WAV has {ref.frames}")
    ff = _ffmpeg_frames(job, mp3, ref.channels)
    if ff != ref.frames:
        raise StemStorageError(f"ffmpeg decodes {ff} frames, the WAV has {ref.frames}")
    try:
        a, b = _loudest_block(wav, mp3, ref.samplerate * _ALIGN_BLOCK_SEC)
    except Exception as exc:
        raise StemStorageError("the MP3 cannot be read back") from exc
    if a is None or float(np.sqrt(np.mean(a * a))) < _ALIGN_MIN_RMS:
        return
    zero, best, lag = _correlation(a, b, _ALIGN_MAX_LAG)
    if zero < _ALIGN_MIN_CORR or zero < _ALIGN_MIN_RATIO * best:
        raise StemStorageError(
            f"the MP3 does not line up with the WAV (lag {lag}, {zero:.3f} at 0 against {best:.3f})"
        )


def _loudest_block(wav: Path, mp3: Path, size: int):
    """Channel 0 of the loudest `size`-frame block of `wav`, and the same
    block of `mp3`, read side by side from the start. (None, None) for an
    empty file."""
    import numpy as np
    import soundfile as sf

    best_rms, best = -1.0, (None, None)
    pairs = zip(
        sf.blocks(str(wav), blocksize=size, dtype="float32", always_2d=True),
        sf.blocks(str(mp3), blocksize=size, dtype="float32", always_2d=True),
        strict=True,
    )
    for a, b in pairs:
        if len(a) != len(b):
            raise StemStorageError("the MP3 reads back at another length")
        a0 = a[:, 0].astype(np.float64)
        rms = float(np.sqrt(np.mean(a0 * a0))) if a0.size else 0.0
        if rms > best_rms:
            best_rms, best = rms, (a0, b[:, 0].astype(np.float64))
    return best


def _correlation(a, b, max_lag: int) -> tuple[float, float, int]:
    """Normalised cross-correlation of b against a: its value at lag 0, its
    best value within +-max_lag, and that lag."""
    import numpy as np

    n = len(a)
    nfft = 1 << (2 * n).bit_length()
    c = np.fft.irfft(np.fft.rfft(b, nfft) * np.conj(np.fft.rfft(a, nfft)), nfft)
    denom = float(np.sqrt(np.sum(a * a) * np.sum(b * b)))
    if denom <= 0:
        return 0.0, 0.0, 0
    k = min(max_lag, n - 1)
    window = np.concatenate([c[-k:], c[: k + 1]]) / denom if k else c[:1] / denom
    i = int(np.argmax(window))
    return float(c[0] / denom), float(window[i]), i - k


def _stamp(path: Path) -> list[int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return [st.st_size, st.st_mtime_ns]


def _keep_derived_files(job_dir: Path, vocals_wav_stamp: list[int] | None) -> None:
    """Files kept beside the stems that are checked against the vocals stem's
    size and time: the same audio is now in another file, so they stay valid
    and are not recomputed. Best effort."""
    stems_dir = job_dir / "stems"
    vocals_mp3 = stems_dir / "vocals.mp3"
    if vocals_wav_stamp is None or not vocals_mp3.is_file():
        return
    try:
        from app.pipeline.lyrics_retime import restamp_transcript

        restamp_transcript(job_dir, vocals_wav_stamp)
    except Exception:
        logger.info("[%s] could not keep the transcript's stamp", job_dir.name, exc_info=True)
    envelope = stems_dir / "vocal_envelope.json"
    try:
        # Kept while it is not older than the stem (lyrics_align._envelope,
        # GET /vocal-envelope): it was made from the WAV, and still holds.
        if envelope.is_file() and envelope.stat().st_mtime_ns >= vocals_wav_stamp[1]:
            os.utime(envelope)
    except OSError:
        logger.info("[%s] could not keep the vocal envelope", job_dir.name, exc_info=True)


def _record(job: Job, fmt: str, kbps: int | None) -> None:
    _set(job, stem_format=fmt, stem_mp3_kbps=kbps)


def _refresh_urls(job: Job, stems_dir: Path) -> None:
    """Point the job's stem URLs (and the mix link, when it is a stem file) at
    the files now on disk."""
    stems = []
    for entry in job.stems:
        name = entry.get("name", "")
        stems.append({**entry, "url": stem_url(job.id, stems_dir, name)} if name else entry)
    prefix = f"/api/jobs/{job.id}/stems/"
    mix_url = job.mix_url
    if mix_url and mix_url.startswith(prefix):
        name = Path(mix_url[len(prefix) :]).stem
        if name in STORED_NAMES:
            mix_url = stem_url(job.id, stems_dir, name)
    _set(job, stems=stems, mix_url=mix_url)


def store_stems(job: Job, job_dir: Path) -> None:
    """Keep the song's stems in the format the setting asks for now. Never
    raises but for a cancel; see the module docstring."""
    if job.cancel_requested:
        raise JobCancelled()
    fmt = get_stem_format()
    if fmt != "mp3":
        _record(job, "wav", None)
        return
    kbps = get_stem_mp3_kbps()
    stems_dir = job_dir / "stems"
    wavs = [p for p in (stems_dir / f"{n}.wav" for n in STORED_NAMES) if p.is_file()]
    if not wavs:
        _record(job, "wav", None)
        return

    _set(job, stage="Compressing stems...")
    started = time.monotonic()
    made: list[tuple[Path, Path]] = []
    try:
        for wav in wavs:
            if job.cancel_requested:
                raise JobCancelled()
            # Dot-prefixed and .tmp-marked: no reader takes it for a stem.
            tmp = stems_dir / f".{wav.stem}.{uuid.uuid4().hex}.tmp.mp3"
            made.append((wav, tmp))
            _encode(job, wav, tmp, kbps)
            verify_mp3(job, wav, tmp)
    except JobCancelled:
        _discard(made)
        raise
    except Exception as exc:
        # StemStorageError, or anything unexpected (OSError on a full disk,
        # libsndfile): the WAVs are all still here, so the song is a WAV song.
        _discard(made)
        logger.warning("[%s] stems kept as WAV: MP3 storage failed: %s", job.id, exc)
        _record(job, "wav", None)
        return

    vocals_stamp = _stamp(stems_dir / "vocals.wav")
    moved: list[Path] = []
    try:
        for wav, tmp in made:
            final = wav.with_suffix(".mp3")
            os.replace(tmp, final)
            moved.append(final)
    except OSError as exc:
        # Every WAV is still here and wins over an MP3 of the same name
        # (stem_files.stem_path); the moved ones are removed so nothing
        # half-done is left behind.
        _discard(made)
        for final in moved:
            final.unlink(missing_ok=True)
        logger.warning("[%s] stems kept as WAV: could not move the MP3s: %s", job.id, exc)
        _record(job, "wav", None)
        return

    before = sum(_size(w) for w in wavs)
    after = sum(_size(w.with_suffix(".mp3")) for w in wavs)
    for wav in wavs:
        try:
            wav.unlink()
        except OSError:
            # Harmless: a WAV left behind is still a valid stem and wins over
            # its MP3. Logged, so the disk it costs is not a mystery.
            logger.warning("[%s] could not delete %s after storing it as MP3", job.id, wav.name)
    _keep_derived_files(job_dir, vocals_stamp)
    _refresh_urls(job, stems_dir)
    _record(job, "mp3", kbps)
    logger.info(
        "[%s] stems stored as MP3 %dk: %d files, %.1f MB -> %.1f MB in %.1fs",
        job.id,
        kbps,
        len(wavs),
        before / 1e6,
        after / 1e6,
        time.monotonic() - started,
    )


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _discard(made: list[tuple[Path, Path]]) -> None:
    for _wav, tmp in made:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            logger.debug("could not remove %s", tmp, exc_info=True)


def store_split_stems(job: Job, stems_dir: Path, names: list[str]) -> None:
    """The on-demand lead/backing split writes WAVs into a song that may be
    stored as MP3. They are stored the way the song is, with the same
    verification, and stay WAV on any failure. Never raises."""
    if job.stem_format != "mp3" or not job.stem_mp3_kbps:
        return
    made: list[tuple[Path, Path]] = []
    try:
        for name in names:
            wav = stems_dir / f"{name}.wav"
            if not wav.is_file():
                continue
            tmp = stems_dir / f".{name}.{uuid.uuid4().hex}.tmp.mp3"
            made.append((wav, tmp))
            _encode(job, wav, tmp, int(job.stem_mp3_kbps))
            verify_mp3(job, wav, tmp)
        for wav, tmp in made:
            os.replace(tmp, wav.with_suffix(".mp3"))
    except Exception as exc:
        _discard(made)
        for wav, _tmp in made:
            if wav.is_file():
                # Never both for long: the WAV wins anyway, the MP3 is clutter.
                wav.with_suffix(".mp3").unlink(missing_ok=True)
        logger.warning("[%s] split stems kept as WAV: %s", job.id, exc)
        return
    for wav, _tmp in made:
        try:
            wav.unlink()
        except OSError:
            logger.warning("[%s] could not delete %s after storing it as MP3", job.id, wav.name)
