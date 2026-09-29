"""Lyrics transcribed from the vocals stem when no lookup found any.

The last stage of a job, after separation and after the lyrics lookup
(lyrics_lookup.py) has been joined: it needs the vocals stem, and it only
runs when the lookup came back empty, so that a track with published lyrics
never pays for it. Whether it runs at all is the transcribe_lyrics setting
("auto" means a CUDA job only; mending found lyrics, below, runs on any
device unless it is "off").

Inference happens in a fresh worker process (transcribe_worker.py), with the
same cancellation, total timeout and output-stall watchdog as the section
worker. The process exiting is what releases its GPU memory, so the persistent
Demucs worker, which shares the card, keeps its own allocation untouched.

When the lookup did find lyrics but they look like a copy that lost its
accents (lyrics_repair.py), the same worker is asked instead to detect the
language and, only for one written with accents, transcribe: the heard words
give the letters back to LRCLIB's lines, whose timing is kept (mend_lyrics).

Lyrics are optional. Every failure here is logged and the job finishes
without them; only a cancel propagates.

The result is lyrics.json with source "whisper": Whisper's segments as lines,
long ones broken at pauses, written as enhanced LRC whose ``<mm:ss.xx>`` word
stamps are what the Lyrics tab's karaoke wipe follows (parseLrc and
wordTimings in static/js/lyricsLookup.js).
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.core.config import (
    LYRICS_REPAIR_LANGUAGES,
    LYRICS_REPAIR_MIN_LANGUAGE_PROB,
    TIMEOUT_TRANSCRIBE,
    TIMEOUT_TRANSCRIBE_STALL,
    TRANSCRIBE_GPU_MIN_FREE_MB,
    TRANSCRIBE_HALLUCINATION_SILENCE_SEC,
    TRANSCRIBE_LINE_BREAK_GAP_SEC,
    TRANSCRIBE_LINE_HARD_CHARS,
    TRANSCRIBE_LINE_SOFT_CHARS,
    TRANSCRIBE_MIN_VOCAL_PRESENCE,
    TRANSCRIBE_MODEL_CPU,
    TRANSCRIBE_MODEL_GPU,
    TRANSCRIBE_SMALL_GPU_MIN_FREE_MB,
    TRANSCRIBE_STANZA_GAP_SEC,
    TRANSCRIBE_WORD_GAP_SEC,
)
from app.core.models import Job, JobCancelled, _set
from app.core.registry import set_proc
from app.core.settings import lyrics_mending_enabled, transcribe_lyrics_enabled
from app.pipeline.lyrics_lookup import lyrics_path, read_lyrics, write_lyrics
from app.pipeline.lyrics_repair import looks_stripped, mend_from_transcript, words_of
from app.pipeline.lyrics_retime import save_transcript

logger = logging.getLogger("stemdeck.transcribe")

_PHASE_PREFIX = "@@PHASE@@"
_PCT_RE = re.compile(r"(\d{1,3})%\|")
# Whisper's own rules for a window it should have skipped: silence (a likely
# no-speech token and a low log probability together) and a loop (text that
# compresses too well). A segment that still carries either is not kept.
_NO_SPEECH_PROB = 0.6
_LOGPROB_FLOOR = -1.0
_COMPRESSION_RATIO_MAX = 2.4
# Punctuation a line may end on once it is long enough.
_CLAUSE_END = frozenset(",.;:!?")
# A pause long enough to end a line that is already long enough.
_SOFT_BREAK_GAP_SEC = 0.25
# Fewer words than this is not a transcript, just Whisper hearing something.
_MIN_WORDS = 3

_STAGE = "Transcribing lyrics..."


def whisper_models_dir() -> Path:
    """Where Whisper's weights are kept: <TORCH_HOME>/whisper, beside the
    Demucs checkpoints. TORCH_HOME is set for the desktop app (to the data
    directory's models/torch) and for Docker (/cache/torch); elsewhere this
    is torch's own default, worked out the way torch.hub does, so the server
    process does not have to import torch to know it."""
    torch_home = os.environ.get("TORCH_HOME", "").strip()
    if torch_home:
        base = Path(torch_home).expanduser()
    else:
        cache = os.environ.get("XDG_CACHE_HOME", "").strip()
        base = (Path(cache).expanduser() if cache else Path.home() / ".cache") / "torch"
    return base / "whisper"


def _spawn_worker_cmd(vocals: Path, device: str) -> list[str]:
    """The worker invocation. Module-level seam so tests can swap in a stub
    executable, the way separate.py exposes _spawn_worker_cmd."""
    return [
        sys.executable,
        "-m",
        "app.pipeline.transcribe_worker",
        "--audio",
        str(vocals),
        "--device",
        device,
        "--model",
        TRANSCRIBE_MODEL_GPU,
        "--fallback-model",
        TRANSCRIBE_MODEL_CPU,
        "--download-root",
        str(whisper_models_dir()),
        "--min-free-mb",
        str(TRANSCRIBE_GPU_MIN_FREE_MB),
        "--small-min-free-mb",
        str(TRANSCRIBE_SMALL_GPU_MIN_FREE_MB),
        "--silence-sec",
        str(TRANSCRIBE_HALLUCINATION_SILENCE_SEC),
    ]


_FOUND = "lyrics already found"


def skip_reason(job: Job, job_dir: Path) -> str | None:
    """Why this job gets no transcription, or None when it should get one."""
    if not transcribe_lyrics_enabled(job.compute_device):
        return f"off for device {job.compute_device or 'unknown'}"
    if job.has_lyrics or lyrics_path(job_dir).is_file():
        return _FOUND
    tags = job.audio_tags or {}
    if isinstance(tags.get("lyrics"), str) and tags["lyrics"].strip():
        return "the file carries its own lyrics"
    return _vocals_reason(job, job_dir)


def _vocals_reason(job: Job, job_dir: Path) -> str | None:
    """Why this job's vocals are not worth transcribing, or None."""
    if not (job_dir / "stems" / "vocals.wav").is_file():
        return "no vocals stem"
    presence = (job.stem_presence or {}).get("vocals")
    if isinstance(presence, int) and presence < TRANSCRIBE_MIN_VOCAL_PRESENCE:
        return f"vocals too quiet ({presence})"
    return None


def _device(job: Job) -> str:
    # macOS has no CUDA, and Whisper's alignment buffer is a sparse tensor
    # MPS cannot hold, so an MPS job transcribes on the CPU, CPU model.
    return "cuda" if job.compute_device == "cuda" else "cpu"


def _terminate(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        # wait, not communicate: the reader threads own the pipes.
        proc.wait(timeout=5)


def _stage_for(phase: str, pct: int | None) -> str:
    if phase == "download":
        return f"Downloading the lyrics model {pct}%" if pct is not None else _STAGE
    if phase == "transcribe" and pct is not None:
        return f"Transcribing lyrics {pct}%"
    return _STAGE


def _run_worker(
    job: Job,
    cmd: list[str],
    *,
    cancelled: Callable[[], bool] | None = None,
    report: Callable[[str, int | None], None] | None = None,
) -> dict[str, Any] | None:
    """Run the worker with cancellation, a total timeout and stall detection.
    Returns its JSON answer, or None when it failed, timed out or stalled.
    Raises JobCancelled when the job was cancelled meanwhile.

    ``cancelled`` says when to stop (by default, the job's own cancel), and
    ``report`` hears each phase and percentage (by default, the job's stage
    text shows them): a run on a finished job (lyrics_retime.py) keeps its own
    cancel and progress and leaves the job's stage alone."""
    if cancelled is None:

        def cancelled() -> bool:
            return job.cancel_requested

    env = os.environ.copy()
    # The worker hard-exits when this process disappears, so a Force Quit
    # cannot leave it holding the GPU (#519).
    env["STEMDECK_PARENT_PID"] = str(os.getpid())
    env["PYTHONIOENCODING"] = "utf-8:replace"
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=env,
    )
    if proc.stdout is None or proc.stderr is None:
        _terminate(proc)
        raise RuntimeError("transcription worker has no output pipes")

    stdout: deque[str] = deque(maxlen=4)
    stderr: deque[str] = deque(maxlen=40)
    lock = threading.Lock()
    last_output = [time.monotonic()]
    # [phase, percentage]; read by the loop below, which owns the job.
    progress: list[Any] = ["load", None]

    def read_stdout() -> None:
        for line in proc.stdout:
            with lock:
                stdout.append(line.rstrip("\n"))
                last_output[0] = time.monotonic()

    def read_stderr() -> None:
        # Universal newlines split tqdm's carriage-return redraws into lines.
        for line in proc.stderr:
            line = line.rstrip()
            with lock:
                last_output[0] = time.monotonic()
                if line.startswith(_PHASE_PREFIX):
                    progress[:] = [line[len(_PHASE_PREFIX) :], None]
                    continue
                m = _PCT_RE.search(line)
                if m:
                    progress[1] = max(0, min(100, int(m.group(1))))
                elif line:
                    stderr.append(line)

    readers = [
        threading.Thread(target=read_stdout, daemon=True),
        threading.Thread(target=read_stderr, daemon=True),
    ]
    for reader in readers:
        reader.start()

    started = time.monotonic()
    shown: Any = _STAGE if report is None else None
    failed = None
    set_proc(job.id, proc)
    try:
        while proc.poll() is None:
            if cancelled():
                _terminate(proc)
                raise JobCancelled()
            now = time.monotonic()
            with lock:
                silent_for = now - last_output[0]
                phase, pct = progress[0], progress[1]
            if report is not None:
                if (phase, pct) != shown:
                    shown = (phase, pct)
                    report(phase, pct)
            else:
                stage = _stage_for(phase, pct)
                if stage != shown:
                    shown = stage
                    _set(job, stage=stage)
            if now - started > TIMEOUT_TRANSCRIBE:
                failed = f"timed out after {TIMEOUT_TRANSCRIBE}s"
                _terminate(proc)
                break
            if silent_for > TIMEOUT_TRANSCRIBE_STALL:
                failed = f"stalled for {TIMEOUT_TRANSCRIBE_STALL}s"
                _terminate(proc)
                break
            time.sleep(0.1)
    finally:
        set_proc(job.id, None)
        for reader in readers:
            reader.join(timeout=2)

    if cancelled():
        raise JobCancelled()
    if failed is None and proc.returncode != 0:
        failed = f"exit {proc.returncode}"
    if failed is not None:
        logger.warning(
            "[%s] lyrics transcription %s: %s",
            job.id,
            failed,
            " | ".join(list(stderr)[-5:]) or "no diagnostic output",
        )
        return None
    lines = [line for line in stdout if line.startswith("{")]
    if len(lines) != 1:
        logger.warning("[%s] lyrics transcription returned unexpected output", job.id)
        return None
    try:
        answer = json.loads(lines[0])
    except json.JSONDecodeError:
        logger.warning("[%s] lyrics transcription returned invalid JSON", job.id)
        return None
    return answer if isinstance(answer, dict) else None


# ── the transcript as lyrics ──


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _kept(segment: Any) -> bool:
    if not isinstance(segment, dict):
        return False
    no_speech = _number(segment.get("no_speech_prob")) or 0.0
    logprob = _number(segment.get("avg_logprob")) or 0.0
    if no_speech > _NO_SPEECH_PROB and logprob < _LOGPROB_FLOOR:
        return False
    return (_number(segment.get("compression_ratio")) or 0.0) <= _COMPRESSION_RATIO_MAX


def _words(segment: dict[str, Any], floor: float) -> list[dict[str, Any]]:
    """The segment's words as {"text", "start", "end"}, in order, never
    earlier than ``floor`` and never ending before they start. Whisper's
    stamps can overlap by a few hundredths across words; the wipe needs them
    monotonic."""
    out: list[dict[str, Any]] = []
    for word in segment.get("words") or []:
        if not isinstance(word, dict):
            continue
        text = str(word.get("word", "")).strip()
        start = _number(word.get("start"))
        end = _number(word.get("end"))
        if not text or start is None or end is None:
            continue
        start = max(start, floor)
        end = max(end, start)
        out.append({"text": text, "start": start, "end": end})
        floor = start
    return out


def split_lines(words: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """One segment's words as lines. A line ends at a long pause, at a clause
    end or a short pause once it is long enough, and always before it would
    pass the hard length cap. A long pause after a single word does not end
    the line: a held "Yeah," before the next "yeah" is one line, not two."""
    lines: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    length = 0
    for word in words:
        if current:
            gap = word["start"] - current[-1]["end"]
            soft = length >= TRANSCRIBE_LINE_SOFT_CHARS and (
                current[-1]["text"][-1] in _CLAUSE_END or gap >= _SOFT_BREAK_GAP_SEC
            )
            if (
                (gap >= TRANSCRIBE_LINE_BREAK_GAP_SEC and len(current) > 1)
                or soft
                or length + 1 + len(word["text"]) > TRANSCRIBE_LINE_HARD_CHARS
            ):
                lines.append(current)
                current, length = [], 0
        length += len(word["text"]) + (1 if current else 0)
        current.append(word)
    if current:
        lines.append(current)
    return lines


def lrc_time(seconds: float) -> str:
    """mm:ss.xx, the stamp LRC uses. Rounded to the hundredth first, so 59.999
    is 01:00.00 and never 00:60.00."""
    centis = max(0, round(seconds * 100))
    minutes, rest = divmod(centis, 6000)
    return f"{minutes:02d}:{rest // 100:02d}.{rest % 100:02d}"


def lrc_line(words: list[dict[str, Any]]) -> str:
    """One line as enhanced LRC: "[00:12.00]<00:12.00>Hello <00:12.60>world<00:13.10>".

    Each word carries its start. A pause of TRANSCRIBE_WORD_GAP_SEC or more
    after a word is written as its end stamp, an empty piece the page skips,
    so the wipe waits through the pause instead of stretching the word; the
    last word always carries its end, so the wipe finishes when the singing
    does rather than at the next line."""
    parts = [f"[{lrc_time(words[0]['start'])}]"]
    for i, word in enumerate(words):
        parts.append(f"<{lrc_time(word['start'])}>{word['text']}")
        following = words[i + 1] if i + 1 < len(words) else None
        if following is None:
            parts.append(f"<{lrc_time(word['end'])}>")
        else:
            parts.append(" ")
            if following["start"] - word["end"] >= TRANSCRIBE_WORD_GAP_SEC:
                parts.append(f"<{lrc_time(word['end'])}>")
    return "".join(parts)


def _names(job: Job) -> tuple[str, str, str]:
    """(track, artist, album) for lyrics.json: the identified recording, else
    the file's tags, else the job's title."""
    identity = job.identity or {}
    tags = job.audio_tags or {}
    track = identity.get("title") or tags.get("title") or job.title or ""
    artist = identity.get("artist") or tags.get("artist") or ""
    album = identity.get("album") or tags.get("album") or ""
    return str(track), str(artist), str(album)


def build_lyrics(result: dict[str, Any], job: Job) -> dict[str, Any] | None:
    """lyrics.json from the worker's answer, or None when it heard nothing
    worth keeping."""
    segments = result.get("segments")
    if not isinstance(segments, list):
        return None
    lines: list[list[dict[str, Any]]] = []
    floor = 0.0
    for segment in segments:
        if not _kept(segment):
            continue
        words = _words(segment, floor)
        if not words:
            continue
        floor = words[-1]["start"]
        lines.extend(split_lines(words))
    if sum(len(line) for line in lines) < _MIN_WORDS:
        return None

    plain: list[str] = []
    for i, line in enumerate(lines):
        if i and line[0]["start"] - lines[i - 1][-1]["end"] >= TRANSCRIBE_STANZA_GAP_SEC:
            plain.append("")
        plain.append(" ".join(word["text"] for word in line))
    track, artist, album = _names(job)
    duration = _number(job.duration_sec) or lines[-1][-1]["end"]
    language = result.get("language")
    return {
        "v": 1,
        "source": "whisper",
        "track": track,
        "artist": artist,
        "album": album,
        "duration": float(duration),
        "synced": "\n".join(lrc_line(line) for line in lines),
        "plain": "\n".join(plain),
        "instrumental": False,
        "others": [],
        "lrclib_id": None,
        "language": language if isinstance(language, str) else None,
    }


def transcribe_lyrics(job: Job, job_dir: Path) -> bool:
    """Transcribe the job's vocals into lyrics.json when it has no lyrics and
    the setting allows. True when lyrics were written. Never raises except
    JobCancelled: lyrics are optional, and a failure here must not cost the
    user a separation that has already succeeded."""
    if job.cancel_requested:
        raise JobCancelled()
    started = time.monotonic()
    try:
        if lyrics_path(job_dir).is_file():
            # Found lyrics may have lost their accents: mended, not replaced,
            # and on any device unless the setting is "off" (see
            # lyrics_mending_enabled).
            return lyrics_mending_enabled() and mend_lyrics(job, job_dir)
        reason = skip_reason(job, job_dir)
        if reason is not None:
            logger.info("[%s] lyrics transcription skipped: %s", job.id, reason)
            return False
        _set(job, stage=_STAGE)
        vocals = job_dir / "stems" / "vocals.wav"
        result = _run_worker(job, _spawn_worker_cmd(vocals, _device(job)))
        if result is None:
            return False
        entry = build_lyrics(result, job)
        if entry is None:
            logger.info("[%s] lyrics transcription heard no words", job.id)
            return False
        # A lookup still in flight is dropped by its finish(), so nothing
        # else writes this file now; checked again all the same, since
        # published lyrics always beat a transcript.
        if lyrics_path(job_dir).is_file():
            return False
        if not write_lyrics(job, job_dir, entry):
            return False
    except JobCancelled:
        raise
    except Exception:
        logger.exception("[%s] lyrics transcription failed", job.id)
        return False
    logger.info(
        "[%s] lyrics transcribed in %.1fs: model=%s device=%s language=%s (%.2f) "
        "lines=%d peak_vram_mb=%s",
        job.id,
        time.monotonic() - started,
        result.get("model"),
        result.get("device"),
        result.get("language"),
        _number(result.get("language_probability")) or 0.0,
        entry["synced"].count("\n") + 1,
        result.get("peak_vram_mb"),
    )
    return True


# ── lyrics that lost their accents ──


def _language_gate() -> list[str]:
    """The worker arguments that make it stop once it has detected the
    language, unless that is one written with letters outside ASCII."""
    return [
        "--languages",
        ",".join(sorted(LYRICS_REPAIR_LANGUAGES)),
        "--min-language-probability",
        str(LYRICS_REPAIR_MIN_LANGUAGE_PROB),
    ]


def heard_words(result: dict[str, Any]) -> list[str]:
    """The words of the worker's answer, in order, from the segments
    build_lyrics would keep."""
    segments = result.get("segments")
    heard: list[str] = []
    for segment in segments if isinstance(segments, list) else []:
        if _kept(segment):
            for word in _words(segment, 0.0):
                heard.extend(words_of(word["text"]))
    return heard


def mend_lyrics(job: Job, job_dir: Path) -> bool:
    """Give the job's LRCLIB lyrics back the letters they lost, from what
    Whisper hears in its vocals. True when lyrics.json was rewritten, with
    "repaired": "whisper".

    Three gates, cheapest first, so that English and lyrics that have their
    accents never start the worker, and a song written without accents is
    never touched: the text (lyrics_repair.looks_stripped); the language
    Whisper detects in the vocals, before anything is transcribed
    (LYRICS_REPAIR_LANGUAGES); then the transcription, which must give
    letters back to enough words (lyrics_repair.mend_from_transcript). A word
    is only replaced by a heard word that strips down to it, and the timing
    stays LRCLIB's.

    Called by transcribe_lyrics once the setting allows a transcription, and
    shares its contract: never raises but JobCancelled."""
    entry = read_lyrics(job_dir)
    if entry is None or entry["source"] != "lrclib" or "repaired" in entry:
        return False
    if not looks_stripped(entry["synced"] or entry["plain"]):
        return False
    reason = _vocals_reason(job, job_dir)
    if reason is not None:
        logger.info("[%s] lyrics mending skipped: %s", job.id, reason)
        return False
    started = time.monotonic()
    _set(job, stage=_STAGE)
    vocals = job_dir / "stems" / "vocals.wav"
    result = _run_worker(job, _spawn_worker_cmd(vocals, _device(job)) + _language_gate())
    if result is None:
        return False
    if not result.get("skipped"):
        # The same words time the lyrics line by line (lyrics_retime.py)
        # without a second pass.
        save_transcript(job_dir, result)
    language = result.get("language")
    probability = _number(result.get("language_probability")) or 0.0
    if (
        result.get("skipped")
        or language not in LYRICS_REPAIR_LANGUAGES
        or probability < LYRICS_REPAIR_MIN_LANGUAGE_PROB
    ):
        logger.info(
            "[%s] lyrics left as they are: sung in %s (%.2f)", job.id, language, probability
        )
        return False
    mended = mend_from_transcript(entry, heard_words(result))
    if mended is None:
        logger.info("[%s] lyrics left as they are: the transcription does not bear it out", job.id)
        return False
    # Nothing else writes lyrics.json while the pipeline runs; checked all the
    # same, so a mend never lands on lyrics it was not made from.
    if read_lyrics(job_dir) != entry:
        return False
    if not write_lyrics(job, job_dir, mended):
        return False
    logger.info(
        "[%s] lyrics mended from the vocals in %.1fs: language=%s (%.2f) model=%s",
        job.id,
        time.monotonic() - started,
        language,
        probability,
        result.get("model"),
    )
    return True
