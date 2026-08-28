"""Automatic functional song-section analysis.

The semantic model runs after Demucs and consumes four stems. StemDeck uses
the six-stem model, so guitar, piano, and other are summed into a temporary
float WAV before an isolated worker performs inference. Every external result
is treated as untrusted data and normalized into the existing editable
Sections schema.
"""

from __future__ import annotations

import json
import logging
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from numbers import Real
from pathlib import Path

from app.core.config import (
    SECTION_MODEL,
    TIMEOUT_SECTIONS,
    TIMEOUT_SECTIONS_STALL,
    ffmpeg_executable,
)
from app.core.models import Job, JobCancelled
from app.core.registry import set_proc

logger = logging.getLogger("stemdeck.sections")

_KINDS = frozenset(("intro", "outro", "break", "bridge", "inst", "solo", "verse", "chorus", "part"))
_SENTINELS = frozenset(("start", "end"))
# What an interior sentinel becomes: the model named a real span with a bracket
# class, which says only that it could not name it musically.
_NEUTRAL_KIND = "part"
_NAMES = {
    "intro": "Intro",
    "outro": "Outro",
    "break": "Break",
    "bridge": "Bridge",
    "inst": "Instrumental",
    "solo": "Solo",
    "verse": "Verse",
    "chorus": "Chorus",
    "part": "Part",
}
_COLORS = {
    "intro": "#4a7fff",
    "verse": "#00c8a0",
    "chorus": "#9a4aff",
    "bridge": "#ff8a20",
    "break": "#2ab8e8",
    "inst": "#e8c840",
    "solo": "#ff4a90",
    "outro": "#00d4d4",
    "part": "#8391a5",
}
_MIN_SECTION_SECONDS = 0.5
_BOUNDARY_TOLERANCE_SECONDS = 0.25
_WORK_PREFIX = ".sections-work-"
_HEARTBEAT_PREFIX = "SECTION_HEARTBEAT"


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _raw_segments(raw: object) -> list[object] | None:
    if isinstance(raw, dict):
        raw = raw.get("segments")
    return raw if isinstance(raw, list) else None


def _merge_short(segments: list[dict[str, object]]) -> list[dict[str, object]]:
    result = list(segments)
    while True:
        short_index = next(
            (
                i
                for i, segment in enumerate(result)
                if float(segment["end"]) - float(segment["start"]) < _MIN_SECTION_SECONDS
            ),
            None,
        )
        if short_index is None:
            return result
        if len(result) <= 2:
            return []

        i = short_index
        if 0 < i < len(result) - 1 and result[i - 1]["kind"] == result[i + 1]["kind"]:
            result[i - 1]["end"] = result[i + 1]["end"]
            del result[i : i + 2]
        elif i > 0:
            result[i - 1]["end"] = result[i]["end"]
            del result[i]
        else:
            result[i + 1]["start"] = result[i]["start"]
            del result[i]


def normalize_sections(raw_segments: object, duration: float) -> list[dict]:
    """Convert untrusted model output into editable, gap-free section records."""
    duration_value = _number(duration)
    raw = _raw_segments(raw_segments)
    if duration_value is None or duration_value < 2 * _MIN_SECTION_SECONDS or not raw:
        return []

    parsed: list[dict[str, object]] = []
    for item in raw:
        if not isinstance(item, dict):
            return []
        label = item.get("label", item.get("kind"))
        if not isinstance(label, str):
            return []
        kind = label.strip().lower()
        if kind not in _KINDS | _SENTINELS:
            return []
        start = _number(item.get("start"))
        end = _number(item.get("end"))
        if start is None or end is None or end <= start:
            return []
        start = max(0.0, min(duration_value, start))
        end = max(0.0, min(duration_value, end))
        if end <= start:
            # Malformed input was already rejected above, so a span can only
            # collapse here by lying entirely outside the analyzed duration.
            # The model reads the stems while duration_sec is rounded, so its
            # timeline routinely overhangs by a fraction of a second and a real
            # track ended with a 10 ms span past it. That is one span with no
            # overlap to keep, not a reason to discard the whole song.
            continue
        parsed.append({"start": start, "end": end, "kind": kind})

    parsed.sort(key=lambda segment: (float(segment["start"]), float(segment["end"])))
    # The sentinels bracket the model's timeline and carry no musical meaning,
    # so they are stripped from either end however many there are and whichever
    # name they carry. The model does emit a degenerate one at the wrong end --
    # a real 484 s track produced a 10 ms "start" span *after* its final
    # section -- and treating that as scrambled output threw away every section
    # for the whole song. Only a sentinel sitting between two real sections
    # means the timeline itself cannot be trusted.
    lo, hi = 0, len(parsed)
    while lo < hi and parsed[lo]["kind"] in _SENTINELS:
        lo += 1
    while hi > lo and parsed[hi - 1]["kind"] in _SENTINELS:
        hi -= 1
    for segment in parsed[lo:hi]:
        if segment["kind"] in _SENTINELS:
            segment["kind"] = _NEUTRAL_KIND

    # Normalize tiny floating-point disagreements at adjacent boundaries, but
    # reject model output containing a real overlap or unlabeled internal gap.
    for left, right in zip(parsed, parsed[1:], strict=False):
        delta = float(right["start"]) - float(left["end"])
        if abs(delta) > _BOUNDARY_TOLERANCE_SECONDS:
            return []
        boundary = (float(left["end"]) + float(right["start"])) / 2
        left["end"] = boundary
        right["start"] = boundary

    meaningful = [segment for segment in parsed[lo:hi] if segment["kind"] in _KINDS]
    if len(meaningful) < 2:
        return []
    meaningful[0]["start"] = 0.0
    meaningful[-1]["end"] = duration_value
    # Boundaries and semantic labels are separate model tasks. Adjacent spans
    # with the same label still represent independently predicted structural
    # boundaries and must remain editable instead of being collapsed.
    meaningful = _merge_short(meaningful)
    if len(meaningful) < 2:
        return []

    sections: list[dict] = []
    for index, segment in enumerate(meaningful, start=1):
        kind = str(segment["kind"])
        start = round(float(segment["start"]), 3)
        end = round(float(segment["end"]), 3)
        if end - start < _MIN_SECTION_SECONDS:
            return []
        sections.append(
            {
                "id": f"auto-{index:03d}",
                "name": _NAMES[kind],
                "kind": kind,
                "start": start,
                "end": end,
                "color": _COLORS[kind],
            }
        )
    return sections


def _terminate(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _run_registered_process(job: Job, cmd: list[str]) -> tuple[int, list[str], list[str]]:
    """Run a child with cancellation, total timeout, and output-stall detection."""
    env = os.environ.copy()
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
        raise RuntimeError("section-analysis process has no output pipes")

    stdout: deque[str] = deque(maxlen=20)
    stderr: deque[str] = deque(maxlen=80)
    last_output = [time.monotonic()]
    output_lock = threading.Lock()

    def read_lines(stream, sink: deque[str]) -> None:
        for line in stream:
            with output_lock:
                sink.append(line.rstrip())
                last_output[0] = time.monotonic()

    readers = [
        threading.Thread(target=read_lines, args=(proc.stdout, stdout), daemon=True),
        threading.Thread(target=read_lines, args=(proc.stderr, stderr), daemon=True),
    ]
    for reader in readers:
        reader.start()

    started = time.monotonic()
    set_proc(job.id, proc)
    try:
        while proc.poll() is None:
            if job.cancel_requested:
                _terminate(proc)
                raise JobCancelled()
            now = time.monotonic()
            if now - started > TIMEOUT_SECTIONS:
                logger.warning("section analysis timed out for job %s", job.id)
                _terminate(proc)
                break
            with output_lock:
                silent_for = now - last_output[0]
            if silent_for > TIMEOUT_SECTIONS_STALL:
                logger.warning(
                    "section analysis stalled for %ss for job %s",
                    TIMEOUT_SECTIONS_STALL,
                    job.id,
                )
                _terminate(proc)
                break
            time.sleep(0.1)
    finally:
        set_proc(job.id, None)
        for reader in readers:
            reader.join(timeout=2)

    if job.cancel_requested:
        raise JobCancelled()
    return proc.returncode or 0, list(stdout), list(stderr)


def _mix_other_stems(job: Job, stems_dir: Path, work_dir: Path) -> Path | None:
    inputs = [stems_dir / f"{name}.wav" for name in ("other", "guitar", "piano")]
    if not all(path.is_file() for path in inputs):
        return None
    output = work_dir / "other.wav"
    cmd = [ffmpeg_executable(), "-y", "-nostdin", "-loglevel", "error"]
    for path in inputs:
        cmd += ["-i", str(path)]
    cmd += [
        "-filter_complex",
        "[0:a][1:a][2:a]amix=inputs=3:normalize=0:duration=longest",
        "-c:a",
        "pcm_f32le",
        str(output),
    ]
    returncode, _stdout, stderr = _run_registered_process(job, cmd)
    if returncode != 0 or not output.is_file() or output.stat().st_size == 0:
        detail = " | ".join(stderr[-3:]) or "no diagnostic output"
        logger.warning("could not prepare section stems for job %s: %s", job.id, detail)
        output.unlink(missing_ok=True)
        return None
    return output


def _run_worker(job: Job, work_dir: Path) -> object | None:
    cmd = [
        sys.executable,
        "-m",
        "app.pipeline.section_worker",
        "--stems-dir",
        str(work_dir),
        "--identifier",
        job.id,
        "--model",
        SECTION_MODEL,
    ]
    beat_grid = work_dir / "beats.json"
    if beat_grid.is_file():
        cmd += ["--beat-grid", str(beat_grid)]
    returncode, stdout, stderr = _run_registered_process(job, cmd)
    diagnostics = [line for line in stderr if not line.startswith(_HEARTBEAT_PREFIX)]
    if returncode != 0:
        logger.warning(
            "section model failed for job %s: %s",
            job.id,
            " | ".join(diagnostics[-5:]) or f"exit {returncode}",
        )
        return None
    if len(stdout) != 1:
        logger.warning("section model returned unexpected output for job %s", job.id)
        return None
    try:
        return json.loads(stdout[0])
    except json.JSONDecodeError:
        logger.warning("section model returned invalid JSON for job %s", job.id)
        return None


def _link_or_copy(source: Path, target: Path) -> None:
    try:
        os.link(source, target)
        return
    except OSError:
        pass
    try:
        target.symlink_to(source.resolve())
        return
    except OSError:
        pass
    shutil.copy2(source, target)


def _safe_rmtree(path: Path, parent: Path) -> None:
    resolved = path.resolve()
    if resolved.parent == parent.resolve() and resolved.name.startswith(_WORK_PREFIX):
        shutil.rmtree(resolved, ignore_errors=True)
    else:  # pragma: no cover - construction is internal; guard prevents future widening
        logger.error("refusing to remove invalid section workspace %s", resolved)


def sweep_orphaned_workspaces(jobs_dir: Path) -> int:
    """Remove section workspaces a previous process died before cleaning up.

    detect_sections stages inside the job's own stems folder and removes the
    directory in a finally, which covers every ordinary ending including
    cancellation. It does not cover the process dying: a force quit, a lost
    machine, an OOM kill, or the desktop shell tearing the backend down while
    the stage runs. The stage is a CPU inference pass measured in minutes and
    is the last thing a job does, so it is running exactly when an impatient
    user quits.

    What is left behind is not trivial. ``other.wav`` inside it is a real file,
    the other/guitar/piano mix written as pcm_f32le: about 1.27 GB for a
    60-minute track, plus the extracted spectrograms. The name starts with a
    dot, so a user wondering why their library outgrew their songs cannot
    easily find it (#483).

    Call this at startup only. Nothing is analyzing yet at that point, so every
    workspace found is certainly dead; running it later could delete one out
    from under a live job. Errors are swallowed for the same reason the stage
    itself is non-fatal: tidying up must never be what breaks a library.
    """
    removed = 0
    try:
        job_dirs = list(jobs_dir.iterdir())
    except OSError:
        return 0
    for job_dir in job_dirs:
        stems_dir = job_dir / "stems"
        try:
            candidates = list(stems_dir.iterdir()) if stems_dir.is_dir() else []
        except OSError:
            continue
        for entry in candidates:
            if not entry.is_dir() or not entry.name.startswith(_WORK_PREFIX):
                continue
            # Same guard as the in-band cleanup: prefix and parent must both
            # match before anything inside a user's library is deleted.
            _safe_rmtree(entry, stems_dir)
            if not entry.exists():
                removed += 1
    if removed:
        logger.info("removed %d orphaned section workspace(s)", removed)
    return removed


def detect_sections(job: Job, stems_dir: Path, duration: float) -> list[dict] | None:
    """Return automatic section suggestions, or None when analysis is unavailable."""
    if job.cancel_requested:
        raise JobCancelled()
    required = [stems_dir / f"{name}.wav" for name in ("bass", "drums", "vocals")]
    if not all(path.is_file() for path in required):
        logger.info("section analysis skipped for job %s: required stems are missing", job.id)
        return None

    work_dir = Path(tempfile.mkdtemp(prefix=_WORK_PREFIX, dir=stems_dir)).resolve()
    try:
        for name in ("bass", "drums", "vocals"):
            _link_or_copy(stems_dir / f"{name}.wav", work_dir / f"{name}.wav")
        beat_grid = stems_dir / "beats.json"
        if beat_grid.is_file():
            _link_or_copy(beat_grid, work_dir / "beats.json")
        other_path = _mix_other_stems(job, stems_dir, work_dir)
        if other_path is None:
            return None
        raw = _run_worker(job, work_dir)
        if raw is None:
            return None
        normalized = normalize_sections(raw, duration)
        if not normalized:
            logger.info("section model produced no valid structure for job %s", job.id)
            return None
        return normalized
    finally:
        _safe_rmtree(work_dir, stems_dir)
