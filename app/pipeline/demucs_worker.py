"""Persistent separation worker (#309).

Run as its own process: `python -m app.pipeline.demucs_worker <device>`.
Loads the base Demucs model once, then serves jobs one at a time over
stdin/stderr -- eliminating the torch import + model load + CUDA kernel warmup
cost that dominated repeated per-job subprocess spawns on GPU (measured 35-42%
of the separate stage on an RTX 3080; see #288/#309). The parent
(separate.py) keeps this process alive across consecutive successful jobs on
the same device and only tears it down on cancel or a genuine failure.

A job runs a separation plan (app/core/stem_models.py): the model each
selectable stem is separated with. One pass per distinct model, in
stem_models.passes() order: each RoFormer pass (Kim's vocal model, a model the
person added) on the mix first, then each Demucs model on the rest (the mix
minus the stems those passes kept), or on the mix when no RoFormer pass ran.
Each stem is taken from the model the plan assigned it, and only those: a
model the person added that makes more stems than the plan gave it keeps the
rest to itself, and they are neither written nor taken from the rest. "other"
is the mix minus the five kept stems, and everything stays float32 in memory
until one shared scale is applied at the write (stem_plan.py).

Models outside the plan are dropped before it runs. Every model but the base
goes back to the CPU after its pass, with the GPU's cache emptied, so two
models never hold the card's memory at once; the base model stays resident.

Protocol:
  - Once the base model is loaded, before reading any job, the worker writes
      "@@READY@@"
    to stderr. Before it, on first use, torch.hub downloads the model (#125):
    from its 'Downloading: "<url>" to <path>' line to that file's 100% or
    "@@READY@@", any "NN%" line is download progress, not separation, and the
    parent holds that phase to a much shorter stall limit. A download that
    fails after its tries writes "@@ERROR@@<json message>" instead, the
    message starting with "model download failed:" and naming the file's
    address and the folder to put it in by hand, and the worker exits(1).
  - Parent writes one JSON line to stdin per job:
      {"source": "<path>", "job_dir": "<path>", "shifts": 1,
       "plan": {"vocals": "<model id>", "drums": ..., "bass": ...,
                "guitar": ..., "piano": ...}}
    An absent "plan" is every stem from the base model, as before plans
    existed. "shifts" applies to the Demucs passes only. A plan that names a
    model the person added comes with
      "user_models": {"<id>": {"path", "arch", "kwargs", "audio", "outputs",
                               "num_stems", "sha256", "size", "mtime_ns",
                               "stems", "format", "id"}}
    for exactly those ids (user_models.worker_spec): the worker never reads
    the store of those models, nor Settings.
  - Before each pass the worker writes "@@PHASE@@<model id>", then
    "@@SWEEPS@@<n>", the number of 0-100% sweeps the pass's "NN%" lines will
    make: Demucs runs one per model of its bag and per shift (htdemucs_ft is
    a bag of four), the vocal model one. A drop in the percentage starts the
    next sweep. A model the pass needs that is not downloaded yet is
    fetched there, announced with the same 'Downloading:' line and progress
    as above, and a download that fails fails the job with the same worded
    "model download failed:" message.
  - When the stems had to be scaled down to fit, one "@@SCALE@@<s>" line.
  - A model the person added that cannot be loaded or run (its file changed,
    it does not fit its class, it ran out of memory, it gave samples that are
    not numbers) fails the job with one line of the cause, then
      "@@ERROR@@<json "user model failed: <id>: <code>">"
    as the last line, the code one of mel_roformer.USER_FAILURE_CODES, and
    the worker exits(1) like any failure. The parent reads that last line and
    runs the job again with HT Demucs 6 stems in that model's place.
  - On completion the worker writes one more stderr line and, only on
    failure, exits:
      "@@DONE@@"                          -- job ok, worker keeps serving
      "@@ERROR@@<json-encoded message>"   -- job failed, worker exits(1)
    A job failure always exits the worker rather than trying to keep
    serving: after an exception mid-inference (OOM-adjacent or not), GPU
    memory / CUDA context state for future jobs isn't something we can
    vouch for, so the parent respawns fresh rather than risk reusing a
    worker in an unknown state. This matches today's behavior, where any
    failure already meant "process is dead, next attempt spawns fresh" --
    the reuse win only applies to the happy path.
  - EOF on stdin (parent closed the pipe) ends the worker's loop cleanly.
  - STEMDECK_PARENT_PID, if set, arms a watchdog that exits the worker when
    that process disappears. See _watch_parent for why the pipe alone is not
    enough.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from app.core import stem_models
from app.core.config import DEMUCS_MODEL, MODEL_DOWNLOAD_ATTEMPTS, TIMEOUT_MODEL_DOWNLOAD_READ
from app.core.model_download import ModelDownloadError, bounded_downloads, drop_unverified
from app.core.process import arm_parent_watchdog
from app.core.stem_models import USER_ENGINE
from app.pipeline import stem_plan


def _say(line: str) -> None:
    sys.stderr.write(line + "\n")
    sys.stderr.flush()


# The message the parent reads as "this model the person added could not
# run" (separate.py's _USER_FAILED_RE matches it, anchored, on the last line).
USER_FAILED_PREFIX = "user model failed: "


class UserModelFailed(Exception):
    """A model the person added could not be loaded or run in this job."""

    def __init__(self, model_id: str, code: str) -> None:
        super().__init__(f"{USER_FAILED_PREFIX}{model_id}: {code}")
        self.model_id = model_id
        self.code = code


def _percent(pct: int) -> None:
    _say(f"{pct}%")


def load_demucs(name: str) -> Any:
    """Demucs model ``name`` on the CPU in eval mode, downloaded through
    torch.hub when it is not cached, each download bounded (#125) and, for a
    pinned checkpoint, verified by its full SHA-256. A cached copy that is not
    the pinned file is removed first, so torch.hub never loads it."""
    from demucs.pretrained import get_model

    with bounded_downloads(TIMEOUT_MODEL_DOWNLOAD_READ, MODEL_DOWNLOAD_ATTEMPTS):
        drop_unverified(name)
        model = get_model(name)
    model.eval()
    model.cpu()
    return model


def _load_mel_roformer(model_id: str) -> Any:
    from app.pipeline import mel_roformer

    return mel_roformer.load(
        read_timeout=TIMEOUT_MODEL_DOWNLOAD_READ, attempts=MODEL_DOWNLOAD_ATTEMPTS
    )


def run_demucs(model: Any, wav: Any, device: str, shifts: int) -> Any:
    """Demucs over ``wav`` (channels x samples): one source per model.sources."""
    from demucs.apply import apply_model

    # Identical to demucs.separate.main()'s per-track body (same functions,
    # same default split/overlap/segment) -- we're not reimplementing the
    # audio pipeline, just calling it repeatedly on an already-loaded model
    # instead of once per fresh process.
    ref = wav.mean(0)
    spread = float(ref.std())
    if not spread > 0:
        # Silence (or a constant offset): nothing to separate, and dividing
        # by zero would make every sample NaN. Every source is silent; the
        # residual "other" keeps whatever offset there was.
        _say("@@SWEEPS@@1")
        _percent(100)
        return [wav * 0 for _ in model.sources]
    _say(f"@@SWEEPS@@{len(getattr(model, 'models', None) or [model]) * max(1, shifts)}")
    wav = wav - ref.mean()
    wav = wav / ref.std()
    sources = apply_model(
        model,
        wav[None],
        device=device,
        shifts=shifts,
        split=True,
        overlap=0.25,
        progress=True,
        num_workers=0,
        segment=None,
    )[0]
    sources = sources * ref.std()
    return sources + ref.mean()


def _release(model: Any, device: str) -> None:
    """Move a model that is not the base back to the CPU and give its GPU
    memory back, so the next pass's model has the card to itself."""
    model.cpu()
    if device == "cuda":
        import torch

        torch.cuda.empty_cache()
    elif device == "mps":
        import torch

        if torch.backends.mps.is_available():
            torch.mps.empty_cache()


def _load_user_model(model_id: str, spec: Any, file: Any) -> Any:
    from app.pipeline import mel_roformer

    return mel_roformer.load_user(spec, file)


class Models:
    """The models this worker holds, by id. The base model is loaded at
    start and never dropped; the others load on the first job that needs them
    and stay until a job runs without them. A model the person added is held
    with the SHA-256 it was loaded from, and loaded again if a request names
    other bytes for it."""

    def __init__(self, base_id: str, base: Any) -> None:
        self.base_id = base_id
        self._loaded: dict[str, Any] = {base_id: base}
        self._user_sha: dict[str, str] = {}

    @property
    def base(self) -> Any:
        return self._loaded[self.base_id]

    def loaded(self) -> set[str]:
        return set(self._loaded)

    def keep_only(self, model_ids: set[str]) -> None:
        for model_id in list(self._loaded):
            if model_id != self.base_id and model_id not in model_ids:
                del self._loaded[model_id]
                self._user_sha.pop(model_id, None)

    def get(self, model_id: str) -> Any:
        if model_id not in self._loaded:
            engine = _engine(model_id)
            if engine == USER_ENGINE:
                raise ValueError("a model the person added is loaded with get_user")
            if engine == "mel_roformer":
                self._loaded[model_id] = _load_mel_roformer(model_id)
            else:
                self._loaded[model_id] = load_demucs(model_id)
        return self._loaded[model_id]

    def get_user(self, model_id: str, spec: Any, file: Any) -> Any:
        if model_id not in self._loaded or self._user_sha.get(model_id) != file.sha256:
            self._loaded.pop(model_id, None)
            self._loaded[model_id] = _load_user_model(model_id, spec, file)
            self._user_sha[model_id] = file.sha256
        return self._loaded[model_id]


# What runs a model id: stem_models' rule, so a model the person added is never
# run as Demucs.
_engine = stem_models.engine_of


def _mel_roformer_pass(model: Any, wav: Any, device: str, samplerate: int) -> Any:
    from app.pipeline import mel_roformer

    if samplerate != mel_roformer.SAMPLE_RATE or wav.shape[0] != mel_roformer.CHANNELS:
        raise ValueError("the vocal model needs 44.1 kHz stereo audio")
    _say("@@SWEEPS@@1")
    model.to(device)
    try:
        return mel_roformer.separate_vocals(model, wav, device, _percent)
    finally:
        _release(model, device)


def _cause(e: BaseException) -> str:
    """The first line of an exception, for the log line before a user model's
    failure line."""
    text = str(e).splitlines()[0] if str(e) else ""
    return f"{type(e).__name__}: {text}" if text else type(e).__name__


def _user_pass(
    models: Models,
    model_id: str,
    user: dict[str, tuple[Any, Any]],
    wav: Any,
    device: str,
    samplerate: int,
    stems: tuple[str, ...],
) -> dict[str, Any]:
    """The stems ``stems`` of ``wav`` from a model the person added, and only
    those. Any failure, from loading its file to a sample that is not a
    number, is UserModelFailed with one code; the cause is said on the line
    before."""
    from app.pipeline import mel_roformer

    model = None
    try:
        if model_id not in user:
            raise mel_roformer.UserModelError("unavailable", "the request does not describe it")
        spec, file = user[model_id]
        if samplerate != mel_roformer.SAMPLE_RATE or wav.shape[0] != mel_roformer.CHANNELS:
            raise mel_roformer.UserModelError("unsupported_audio", "it needs 44.1 kHz stereo audio")
        model = models.get_user(model_id, spec, file)
        _say("@@SWEEPS@@1")
        model.to(device)
        try:
            separated = mel_roformer.separate(model, wav, device, spec, _percent, stems)
        except Exception as e:  # noqa: BLE001 -- out of memory and the like: one code
            raise mel_roformer.UserModelError("forward_failed", _cause(e)) from e
        for audio in separated.values():
            if not bool(audio.isfinite().all()):
                raise mel_roformer.UserModelError(
                    "non_finite", "it gave samples that are not numbers"
                )
        return separated
    except mel_roformer.UserModelError as e:
        _say(f"user model {model_id} could not run ({e.code}): {_cause(e)}")
        raise UserModelFailed(model_id, e.code) from e
    except Exception as e:  # noqa: BLE001 -- anything else while loading it
        _say(f"user model {model_id} could not run (load_failed): {_cause(e)}")
        raise UserModelFailed(model_id, "load_failed") from e
    finally:
        if model is not None:
            _release(model, device)


def separate_plan(
    models: Models,
    device: str,
    wav: Any,
    plan: dict[str, str],
    shifts: int,
    user: dict[str, tuple[Any, Any]] | None = None,
) -> dict:
    """The stems ``plan`` makes of ``wav``, before the write: every kept stem,
    then "other" as the residual. ``user`` holds, per model the person added
    that the plan names, its (mel_roformer.RoformerSpec, UserFile)."""
    steps = stem_models.passes(plan)
    models.keep_only({model_id for model_id, _, _ in steps})
    samplerate = models.base.samplerate
    outputs: dict[str, dict[str, Any]] = {}
    rest = None
    for model_id, source, stems in steps:
        _say(f"@@PHASE@@{model_id}")
        engine = _engine(model_id)
        if engine == USER_ENGINE:
            kept = _user_pass(models, model_id, user or {}, wav, device, samplerate, stems)
            outputs[model_id] = kept
            # Only the stems the plan gave this model leave the rest: one it
            # made for a stem another model was given stays in what that
            # model hears.
            for audio in kept.values():
                rest = wav - audio if rest is None else rest - audio
            continue
        model = models.get(model_id)
        if engine == "mel_roformer":
            vocals = _mel_roformer_pass(model, wav, device, samplerate)
            outputs[model_id] = {"vocals": vocals}
            rest = wav - vocals if rest is None else rest - vocals
            continue
        audio = rest if source == "rest" and rest is not None else wav
        try:
            separated = run_demucs(model, audio, device, shifts)
        finally:
            if model_id != models.base_id:
                _release(model, device)
        # Only the stems this pass is for, each its own copy, so the rest of
        # the pass's output (ft's vocals and other, 6s's vocals when the
        # vocal model ran) is freed now rather than at the end of the job.
        outputs[model_id] = {
            name: _own(audio)
            for name, audio in zip(model.sources, separated, strict=True)
            if name in stems
        }
        del separated
    return stem_plan.assemble(wav, plan, outputs)


def _own(audio: Any) -> Any:
    """``audio`` as an array of its own, not a view that keeps the whole
    pass's output alive."""
    return audio.clone() if hasattr(audio, "clone") else audio.copy()


def _run_one_job(models: Models, device: str, req: dict) -> None:
    from demucs.audio import save_audio
    from demucs.separate import load_track

    source = Path(req["source"])
    job_dir = Path(req["job_dir"])
    shifts = int(req.get("shifts", 1))
    user = _user_models(req.get("user_models"))
    plan = stem_plan.check_plan(
        req.get("plan"),
        base=models.base_id,
        user_models={model_id: stems for model_id, (_, _, stems) in user.items()},
    )

    base = models.base
    wav = load_track(source, base.audio_channels, base.samplerate)
    stems = separate_plan(
        models, device, wav, plan, shifts, {m: (spec, f) for m, (spec, f, _) in user.items()}
    )

    # One scale for every stem, so none clips and they still add up to the
    # song once written; it keeps every sample under the clamp's ceiling, so
    # "clamp" changes nothing.
    scale = stem_plan.shared_scale(stems)
    if scale > 1.0:
        _say(f"@@SCALE@@{scale:.4f}")
    out_dir = job_dir / DEMUCS_MODEL / source.stem
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, audio in stems.items():
        save_audio(
            audio / scale,
            str(out_dir / f"{name}.wav"),
            samplerate=base.samplerate,
            clip="clamp",
            bits_per_sample=16,
            as_float=False,
        )


def _user_models(raw: object) -> dict[str, tuple[Any, Any, tuple[str, ...]]]:
    """The request's "user_models", each as (spec, file, declared stems). An
    entry that does not describe a model fails the job as that model's
    failure, so the parent stands HT Demucs 6 stems in."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("user_models must be an object of model id to description")
    from app.pipeline import mel_roformer

    out: dict[str, tuple[Any, Any, tuple[str, ...]]] = {}
    for model_id, entry in raw.items():
        if not stem_models.is_user_model(model_id):
            raise ValueError("user_models names a model that is not one a person added")
        try:
            out[model_id] = mel_roformer.user_spec(model_id, entry)
        except mel_roformer.UserModelError as e:
            _say(f"user model {model_id} could not run ({e.code}): {_cause(e)}")
            raise UserModelFailed(model_id, e.code) from e
    return out


def main() -> None:
    device = sys.argv[1] if len(sys.argv) > 1 else "cpu"
    arm_parent_watchdog()

    try:
        base = load_demucs(DEMUCS_MODEL)
    except ModelDownloadError as e:
        sys.stderr.write(f"@@ERROR@@{json.dumps(str(e))}\n")
        sys.stderr.flush()
        sys.exit(1)
    models = Models(DEMUCS_MODEL, base)
    sys.stderr.write("@@READY@@\n")
    sys.stderr.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            _run_one_job(models, device, req)
        except Exception as e:
            sys.stderr.write(f"@@ERROR@@{json.dumps(str(e))}\n")
            sys.stderr.flush()
            sys.exit(1)
        sys.stderr.write("@@DONE@@\n")
        sys.stderr.flush()


if __name__ == "__main__":
    main()
