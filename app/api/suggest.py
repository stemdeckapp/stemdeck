"""Suggest EQ and master: GET routes that measure a lane or the mix and
propose ordinary EQ settings (and, for the master, a loudness preset).

- ``GET /api/jobs/{id}/suggest/eq?lane=&target=&layout=&mode=&fx=``: one
  lane (a stem, "original", or a recording track), measured raw from its file.
- ``GET /api/jobs/{id}/suggest/master?stems=&gains=&pitches=&fx=&target=&mode=``:
  the mix as the export builds it (lanes, gains, keys, lane EQ), without the
  master EQ, measured over the whole song.

Both answer with numbers and i18n keys (app/pipeline/eq_suggest.py). The
measurement (the gated spectrum, app/pipeline/eq_analysis.py, and ebur128's
loudness and true peak) runs once per lane or mix in one ffmpeg pass and is
cached on disk; asking for another target or mode reuses it. A request whose
client has gone away is stopped, like the loudness route.

Validation follows the new-endpoint and security skills: the job id first,
lane names from the allow-lists (stems, "original", take and track ids), the
path resolved under JOBS_DIR by the same guards as the exports, every other
parameter from a fixed set or through the strict fx= parser, all before any
I/O; error details are generic.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import uuid
from collections import deque
from pathlib import Path

import numpy as np
from fastapi import APIRouter, HTTPException, Query, Request

from app.api.stems import (
    _DISCONNECT_POLL_SEC,
    _MIXDOWN_NAMES,
    LANES_PARAM_MAX_CHARS,
    LANES_QUERY_DESCRIPTION,
    _expand_render_lanes,
    _is_recorded_lane,
    _lane_chain,
    _parse_lane_gains,
    _parse_pitches,
    _rubberband_available,
    _take_lane_path,
    _take_versions,
    _validate_stem_path,
    build_render_fx,
    parse_fx_query,
    parse_lanes_param,
)
from app.core.config import CACHE_DIR, JOB_ID_RE, TIMEOUT_FFMPEG, ffmpeg_executable
from app.pipeline.eq_analysis import (
    ANALYSIS_RATE,
    Spectrum,
    SpectrumAccumulator,
    spectrum_from_json,
    spectrum_to_json,
)
from app.pipeline.eq_fit import MAX_BANDS_LANE, MAX_BANDS_MASTER, recommend_master
from app.pipeline.eq_suggest import make_suggestion
from app.pipeline.eq_targets import default_target, get_target, targets_for_uses, uses_for_lane
from app.pipeline.fx_filters import FX_PARAM_MAX_CHARS, FxRequest
from app.pipeline.mastering import Loudness, meter_filter, parse_meter, plan_graph

logger = logging.getLogger("stemdeck.suggest")

router = APIRouter(tags=["suggest"])

_SUGGEST_CACHE_DIR = CACHE_DIR / "suggest"
_CACHE_MAX_FILES = 200
_CACHE_VERSION = "1"
# Two analyses at once at most; one per lane or mix at a time.
_analysis_slots = asyncio.Semaphore(2)
_analysis_locks: dict[str, asyncio.Lock] = {}

# Panel kinds (static/js/eq/eqPanelKinds.js) and the fitter layout each uses.
LANE_LAYOUTS = {"graph": "free", "drum": "free", "bass": "bass", "guitar": "guitar", "keys": "keys"}
MODES = ("replace", "merge")
_MERGE_LAYOUTS = ("graph", "drum")
_READ_BYTES = 1 << 20
_FRAME_BYTES = 8  # two float32 channels


class AnalysisError(RuntimeError):
    """ffmpeg failed while analysing. The message is for the log only."""


async def _run_analysis(inputs: list[str], graph: str) -> tuple[Spectrum, Loudness]:
    """One ffmpeg pass: ``graph`` (ending in [pre]) resampled to the analysis
    rate, split into ebur128 (stderr) and 32-bit float stereo (stdout), the
    latter streamed into the spectrum accumulator."""
    rate = ANALYSIS_RATE
    cmd = [
        ffmpeg_executable(),
        "-nostdin",
        "-hide_banner",
        "-nostats",
        "-loglevel",
        "info",
        *inputs,
        "-filter_complex",
        f"{graph};[pre]aresample={rate},asplit=2[m][o];[m]{meter_filter(rate)}[mo]",
        "-map",
        "[mo]",
        "-f",
        "null",
        "-",
        "-map",
        "[o]",
        "-ac",
        "2",
        "-c:a",
        "pcm_f32le",
        "-f",
        "f32le",
        "pipe:1",
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    tail: deque[str] = deque(maxlen=64)

    async def drain() -> None:
        assert proc.stderr is not None
        while True:
            line = await proc.stderr.readline()
            if not line:
                return
            tail.append(line.decode("utf-8", "replace").rstrip())

    acc = SpectrumAccumulator()

    async def read() -> None:
        assert proc.stdout is not None
        carry = b""
        while True:
            chunk = await proc.stdout.read(_READ_BYTES)
            if not chunk:
                break
            data = carry + chunk
            usable = len(data) - len(data) % _FRAME_BYTES
            carry = data[usable:]
            if usable:
                samples = np.frombuffer(data[:usable], dtype="<f4")
                await asyncio.to_thread(acc.feed, samples)
        await asyncio.to_thread(acc.finish)

    drainer = asyncio.create_task(drain())
    try:
        await asyncio.wait_for(read(), timeout=TIMEOUT_FFMPEG)
        await asyncio.wait_for(proc.wait(), timeout=30)
        await drainer
    except (TimeoutError, asyncio.TimeoutError):
        raise AnalysisError("analysis timed out") from None
    finally:
        # A cancelled request (the client went away) unwinds through here.
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        if not drainer.done():
            drainer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await drainer
    if proc.returncode != 0:
        raise AnalysisError(f"ffmpeg exit {proc.returncode}: {' | '.join(list(tail)[-8:])}")
    loud = parse_meter("\n".join(tail))
    return await asyncio.to_thread(acc.result), loud


def _read_cached(path: Path) -> tuple[Spectrum, Loudness] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return spectrum_from_json(data["spectrum"]), Loudness(**data["loudness"])
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError):
        logger.debug("unreadable suggest cache %s", path, exc_info=True)
        return None


def _write_cached(path: Path, spec: Spectrum, loud: Loudness) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    body = {
        "spectrum": spectrum_to_json(spec),
        "loudness": {"i": loud.i, "tp": loud.tp, "lra": loud.lra},
    }
    tmp.write_text(json.dumps(body, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)
    try:
        entries = sorted(
            (p for p in path.parent.iterdir() if p.is_file() and not p.name.startswith(".")),
            key=lambda p: p.stat().st_mtime,
        )
        for old in entries[: max(0, len(entries) - _CACHE_MAX_FILES)]:
            if old != path:
                old.unlink(missing_ok=True)
    except OSError:
        logger.debug("suggest cache prune failed", exc_info=True)


async def analysis(job_id: str, raw_key: str, inputs: list[str], graph: str):
    """The cached measurement for ``raw_key``, measured now when absent."""
    key = hashlib.sha1(raw_key.encode("utf-8"), usedforsecurity=False).hexdigest()
    path = _SUGGEST_CACHE_DIR / f"{key}.json"
    cached = await asyncio.to_thread(_read_cached, path)
    if cached is not None:
        return cached
    lock = _analysis_locks.setdefault(key, asyncio.Lock())
    try:
        async with lock:
            cached = await asyncio.to_thread(_read_cached, path)
            if cached is not None:
                return cached
            async with _analysis_slots:
                try:
                    spec, loud = await _run_analysis(inputs, graph)
                except AnalysisError as exc:
                    logger.warning("suggest analysis failed [job=%s]: %s", job_id, exc)
                    raise HTTPException(status_code=500, detail="analysis failed") from None
            await asyncio.to_thread(_write_cached, path, spec, loud)
            return spec, loud
    finally:
        if not lock.locked():
            _analysis_locks.pop(key, None)


async def _until_done(request: Request, coro):
    """Run ``coro``, stopping it (and its ffmpeg) when the client goes away."""
    task = asyncio.create_task(coro)
    while not task.done():
        await asyncio.wait({task}, timeout=_DISCONNECT_POLL_SEC)
        if not task.done() and await request.is_disconnected():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            raise HTTPException(status_code=499, detail="client went away")
    return task.result()


def _choices(uses: tuple[str, ...]) -> list[dict]:
    return [{"id": t.id, "label_key": t.label_key} for t in targets_for_uses(uses)]


def _file_fragment(path: Path) -> str:
    st = path.stat()
    return f"{path}@{st.st_size}:{st.st_mtime_ns}"


@router.get("/jobs/{job_id}/suggest/eq")
async def suggest_lane_eq(
    request: Request,
    job_id: str,
    lane: str = Query(..., max_length=32, description="Mixer lane: a stem, original, or a track"),
    target: str = Query(default="", max_length=40, description="Target id; default per lane"),
    layout: str = Query(default="graph", max_length=16, description="The lane's EQ panel kind"),
    mode: str = Query(default="replace", max_length=16, description="replace or merge"),
    fx: str = Query(default="", max_length=FX_PARAM_MAX_CHARS, description="The lane's EQ now"),
) -> dict:
    """Measure one lane (its file, before any EQ) and suggest EQ bands that
    move it towards a target curve, in the lane's panel's own terms."""
    if not JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    if layout not in LANE_LAYOUTS:
        raise HTTPException(status_code=422, detail="unknown layout")
    if mode not in MODES or (mode == "merge" and layout not in _MERGE_LAYOUTS):
        raise HTTPException(status_code=422, detail="unknown mode")
    recorded = _is_recorded_lane(lane)
    if not recorded and lane not in _MIXDOWN_NAMES:
        raise HTTPException(status_code=422, detail="unknown lane")
    uses = uses_for_lane(lane, recorded)
    if not uses:
        raise HTTPException(status_code=422, detail="no target for this lane")
    chosen = get_target(target) if target else default_target(lane, recorded)
    if chosen is None or chosen.use not in uses:
        raise HTTPException(status_code=422, detail="unknown target")
    fx_req = parse_fx_query(fx)
    path = _take_lane_path(job_id, lane) if recorded else _validate_stem_path(job_id, lane)
    raw_key = "|".join([_CACHE_VERSION, "lane", job_id, lane, _file_fragment(path)])
    spec, _loud = await _until_done(
        request, analysis(job_id, raw_key, ["-i", str(path)], "[0:a]anull[pre]")
    )
    current = (fx_req.lanes.get(lane) or [None])[0]
    answer = await asyncio.to_thread(
        make_suggestion,
        spec,
        chosen,
        LANE_LAYOUTS[layout],
        mode,
        current,
        MAX_BANDS_LANE,
        _choices(uses),
    )
    answer["lane"] = lane
    return answer


@router.get("/jobs/{job_id}/suggest/master")
async def suggest_master(
    request: Request,
    job_id: str,
    stems: str = Query(..., description="Comma-separated lane names to sum"),
    gains: str = Query(..., description="Comma-separated linear gains, parallel to stems"),
    pitches: str = Query(default="", max_length=128, description="As for mixdown"),
    fx: str = Query(default="", max_length=FX_PARAM_MAX_CHARS, description="As for mixdown"),
    target: str = Query(default="", max_length=40, description="Target id; default balanced"),
    mode: str = Query(default="replace", max_length=16, description="replace or merge"),
    lanes: str | None = Query(
        default=None, max_length=LANES_PARAM_MAX_CHARS, description=LANES_QUERY_DESCRIPTION
    ),
) -> dict:
    """Measure the mix (as the export sums it, before the master EQ) and
    suggest master EQ bands and a loudness preset."""
    if not JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    if mode not in MODES:
        raise HTTPException(status_code=422, detail="unknown mode")
    chosen = get_target(target) if target else get_target("mix.balanced")
    if chosen is None or chosen.use != "mix":
        raise HTTPException(status_code=422, detail="unknown target")
    names, parsed_gains = _parse_lane_gains(stems, gains)
    parsed_pitches = _parse_pitches(pitches, names)
    fx_req = parse_fx_query(fx)
    if any(parsed_pitches) and not await asyncio.to_thread(_rubberband_available):
        raise HTTPException(
            status_code=422,
            detail="this ffmpeg build cannot transpose (no rubberband filter)",
        )
    render_lanes, _unpitched = _expand_render_lanes(
        job_id, names, parsed_gains, parsed_pitches, lanes=parse_lanes_param(lanes)
    )
    paths = [lane.path for lane in render_lanes]
    # The lanes' EQ is part of the mix; the master EQ is what is suggested.
    lanes_only = FxRequest(lanes=fx_req.lanes)
    render_fx = await asyncio.to_thread(
        build_render_fx, [lane.name for lane in render_lanes], paths, lanes_only
    )
    chains = [
        _lane_chain(render_fx.lanes[i], lane.pitch, lane.gain)
        for i, lane in enumerate(render_lanes)
    ]
    graph = plan_graph(chains, "")
    inputs: list[str] = []
    for p in paths:
        inputs += ["-i", str(p)]
    raw_key = "|".join(
        [
            _CACHE_VERSION,
            "mix",
            job_id,
            ";".join(str(p) for p in paths),
            graph,
            ",".join(_take_versions(job_id, names)),
        ]
    )
    spec, loud = await _until_done(request, analysis(job_id, raw_key, inputs, graph))
    current = (fx_req.master or [None])[0]
    answer = await asyncio.to_thread(
        make_suggestion,
        spec,
        chosen,
        "master",
        mode,
        current,
        MAX_BANDS_MASTER,
        _choices(("mix",)),
    )
    answer["master"] = recommend_master(loud.i, loud.tp, chosen.target_lufs)
    answer["analysis"].update({"i": loud.i, "tp": loud.tp, "lra": loud.lra})
    return answer
