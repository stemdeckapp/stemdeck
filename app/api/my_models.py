"""My models' routes (Settings, Separation): separation models the user adds.

  GET    /api/my-models               {"models": [...], "limits": {...},
                                       "registry_reset", "available"}
  POST   /api/my-models/link          add from a Hugging Face link: 202 the entry
  POST   /api/my-models/upload        add from two files (multipart "model",
                                       "config", "stems" JSON, "name", "licence"):
                                       202 the entry
  POST   /api/my-models/{id}/check    check again: 202 the entry
  POST   /api/my-models/{id}/cancel   stop a download or a check: 200
  DELETE /api/my-models/{id}          remove: 200 {"removed": id}

One add or check at a time: while any entry is downloading or checking,
another add or check answers 409 "busy". The work runs in a daemon thread;
the page polls GET. A link download is 0..80 of the entry's pct and the test
run 80..100; an upload's test run is 0..100.

The link route makes network calls, so it is refused while online sources
are off (403 "online_sources_off", app/core/online_sources.py); an upload is
not. Every refusal is {"detail": <fixed sentence>, "code": <code>}: never a
path, an exception, or the link that was sent. Paths come from
user_models.model_dir(), which checks the id and that the folder resolves
directly under the models folder.

Removing a model takes its folder first and its entry after; files that will
not go keep the entry ("busy", 409), so the list never claims a removal that
did not happen. Then every stem whose saved choice is that model goes back to
"auto" (settings.scrub_stem_model), with the models' lock released.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from starlette.datastructures import UploadFile

from app.core import config, settings, stem_models
from app.core import hf_fetch as hf
from app.core import user_model_check as checker
from app.core import user_models as um
from app.core.online_sources import require_online_sources
from app.core.user_model_format import (
    Refused,
    clean_config,
    display_text,
    parse_config,
    sniff_format,
)

logger = logging.getLogger("stemdeck.my_models")

router = APIRouter(tags=["settings"])

MAX_NAME_CHARS = 60
MODEL_EXTS = (".safetensors", ".ckpt", ".pth", ".pt")
CONFIG_EXTS = (".yaml", ".yml")
# A request body over this is refused before it is read.
MAX_UPLOAD_BODY = config.MAX_USER_MODEL_BYTES + config.MAX_USER_MODEL_CONFIG_BYTES + 64 * 1024
_CANCEL_SETTLE_SEC = 20.0
_COPY_CHUNK = 1024 * 1024
_LINK_SHARE = 80  # the share of the bar a link download takes

DETAILS: dict[str, str] = {
    "busy": "A model is being added or checked. Try again when it has finished.",
    "limit": "The list is full. Remove a model before adding another.",
    "duplicate": "This model file is already in the list.",
    "bad_link": "That is not a link to a model on huggingface.co.",
    "stems": "Choose one or more of: vocals, drums, bass, guitar, piano.",
    "licence": "Confirm that you may use this model.",
    "body": "The request is not in the expected shape.",
    "too_large": "The file is larger than the limit.",
    "empty": "A file is empty or missing.",
    "config_missing": "The model's config file is required.",
    "bad_format": "The model file is not a safetensors file or a PyTorch checkpoint.",
    "bad_config": "The config file is not a RoFormer config StemDeck can use.",
    "disk_full": "There is not enough free space for this model.",
    "in_use": "A song is being separated with this model. Try again when it has finished.",
    "changed": "This model's files are gone or changed. Remove it and add it again.",
    "refused": "This model cannot be used.",
}


def _refusal(code: str, status: int, detail_key: str | None = None) -> JSONResponse:
    detail = DETAILS.get(detail_key or code, DETAILS["refused"])
    return JSONResponse({"detail": detail, "code": code}, status_code=status)


# ── work in progress ──


@dataclass
class _Work:
    cancel: threading.Event = field(default_factory=threading.Event)
    procs: list[subprocess.Popen] = field(default_factory=list)
    done: threading.Event = field(default_factory=threading.Event)


_lock = threading.Lock()
_work: dict[str, _Work] = {}


def _busy_locked() -> bool:
    """Any work running, or any entry left mid-work. Callers hold _lock."""
    if _work:
        return True
    return any(e["status"] in um.WORKING for e in um.list_models())


def _admit_new(**entry: object) -> str:
    """Make a new entry and its work slot, atomically against another add.
    Raises Refused("busy") or Refused("limit")."""
    with _lock:
        if _busy_locked():
            raise Refused("busy")
        try:
            model_id = um.new_entry(**entry)  # type: ignore[arg-type]
        except um.LimitReached as exc:
            raise Refused("limit") from exc
        _work[model_id] = _Work()
        return model_id


def _admit_existing(model_id: str, status: str) -> None:
    """Start work on an entry that exists. Raises Refused("busy")."""
    with _lock:
        if _busy_locked():
            raise Refused("busy")
        um.set_status(model_id, status)
        um.set_progress(model_id, pct=0, step=None)
        _work[model_id] = _Work()


def _end_work(model_id: str) -> None:
    with _lock:
        work = _work.pop(model_id, None)
    if work is not None:
        work.done.set()


def _register(model_id: str, proc: subprocess.Popen | None) -> None:
    with _lock:
        work = _work.get(model_id)
        if work is None:
            return
        work.procs.clear()
        if proc is not None:
            work.procs.append(proc)


def _stop(model_id: str) -> _Work | None:
    """Ask the work on ``model_id`` to end; its subprocess is terminated now."""
    with _lock:
        work = _work.get(model_id)
        procs = list(work.procs) if work is not None else []
    if work is None:
        return None
    work.cancel.set()
    for proc in procs:
        if proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
    return work


def cancel_all() -> None:
    """Stop every download and check, at server shutdown."""
    with _lock:
        ids = list(_work)
    for model_id in ids:
        _stop(model_id)


def reset_state() -> None:
    """Forget the work slots, for tests."""
    with _lock:
        _work.clear()


def _spawn(target, *args: object) -> None:
    thread = threading.Thread(target=target, args=args, name="my-models", daemon=True)
    thread.start()


# ── shared steps ──


def _free_bytes(folder: Path) -> int:
    probe = folder
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return 0


def _check_disk(size: int) -> None:
    if _free_bytes(um.root()) < 2 * size + config.USER_MODEL_DISK_MARGIN_BYTES:
        raise Refused("disk_full")


def _finish_file(tmp: Path, final: Path) -> None:
    """Move a synced partial onto its final name, and make the move durable."""
    os.replace(tmp, final)
    settings.fsync_dir(final.parent)


def _check_stems(value: object) -> list[str]:
    """The declared stems, in SELECTABLE_STEMS order. Raises Refused."""
    allowed = stem_models.SELECTABLE_STEMS
    if not isinstance(value, list) or not 1 <= len(value) <= len(allowed):
        raise Refused("stem_not_in_model")
    if not all(isinstance(s, str) and s in allowed for s in value) or len(set(value)) != len(value):
        raise Refused("stem_not_in_model")
    return [s for s in allowed if s in value]


def _file_hash(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        while chunk := fh.read(_COPY_CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def _refuse(model_id: str, code: str, *, keep_files: bool, checked: bool = False) -> None:
    try:
        if not keep_files:
            um.clear_files(model_id)
        um.set_status(model_id, "refused", code=code, checked=checked)
    except um.UnknownModel:
        pass


def _run_check(model_id: str, work: _Work, base: int) -> None:
    """The test run, then "ready" or "refused" (the files kept either way)."""
    rec = um.record(model_id)
    if rec is None:
        return
    folder = um.model_dir(model_id)
    span = 100 - base
    um.set_progress(model_id, pct=base, step="read")
    run = checker.run_check(
        folder,
        list(rec["stems"]),
        on_step=lambda step: um.set_progress(model_id, step=step),
        on_pct=lambda pct: um.set_progress(model_id, pct=base + span * pct // 100),
        cancel=work.cancel,
        register=lambda proc: _register(model_id, proc),
    )
    if run.result is None:
        logger.warning(
            "my models: %s refused (%s); tail:\n%s",
            model_id,
            run.code,
            "\n".join(run.tail[-15:]) or "(no output)",
        )
        um.set_status(model_id, "refused", code=run.code or "load_failed", checked=True)
        return
    path = um.model_file_path(model_id)
    if path is None:
        um.set_status(model_id, "refused", code="changed", checked=True)
        return
    info = dict(rec["file"])
    info["mtime_ns"] = path.stat().st_mtime_ns
    um.set_status(
        model_id,
        "ready",
        params=run.result["params"],
        file=info,
        check={
            "cpu_rtf": run.result["cpu_rtf"],
            "torch": run.result["torch"],
            "separator": run.result["separator"],
        },
        checked=True,
    )
    logger.info("my models: %s is ready (%d parameters)", model_id, run.result["params"])


def _clean_from_disk(model_id: str, stems: list[str]):
    data = um.config_file_path(model_id).read_bytes()
    return clean_config(parse_config(data), stems)


# ── by link ──


def _link_work(model_id: str, link: hf.HfLink, stems: list[str], work: _Work) -> None:
    folder = um.model_dir(model_id)
    stored = False  # once the model file is in place, a failure keeps it
    try:
        um.set_progress(model_id, pct=0, step="info")
        info = hf.repo_info(link, work.cancel)
        weights, cfg = hf.choose_files(info, link)
        if cfg.size is not None and cfg.size > config.MAX_USER_MODEL_CONFIG_BYTES:
            raise Refused("too_large")
        if weights.size is not None and weights.size > config.MAX_USER_MODEL_BYTES:
            raise Refused("too_large")
        if weights.sha256 and um.find_by_sha(weights.sha256, exclude=model_id):
            raise Refused("duplicate")
        _check_disk(weights.size or config.MAX_USER_MODEL_BYTES)
        rec = um.record(model_id) or {}
        source = dict(rec.get("source") or {})
        source.update(
            commit=info.commit,
            label=f"{link.label}@{info.commit[:12]}",
            license_declared=info.license,
        )
        um.set_status(model_id, "downloading", source=source)

        um.set_progress(model_id, step="config")
        tmp, size, sha = hf.download(
            hf.file_url(link, info.commit, cfg.path),
            folder,
            um.CONFIG_FILE,
            max_bytes=config.MAX_USER_MODEL_CONFIG_BYTES,
            cancel=work.cancel,
            expected_size=cfg.size,
            expected_sha256=cfg.sha256,
        )
        try:
            cleaned = clean_config(parse_config(tmp.read_bytes()), stems)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        _finish_file(tmp, folder / um.CONFIG_FILE)
        um.set_status(
            model_id,
            "downloading",
            config={"name": um.CONFIG_FILE, "size": size, "sha256": sha},
            arch=cleaned.arch,
            kwargs=cleaned.kwargs,
            audio=cleaned.audio,
            outputs=cleaned.outputs,
            params=cleaned.params,
        )

        um.set_progress(model_id, pct=0, step="model")
        expected = weights.size

        def progress(received: int) -> None:
            if expected:
                um.set_progress(model_id, pct=_LINK_SHARE * received // expected)

        tmp, size, sha = hf.download(
            hf.file_url(link, info.commit, weights.path),
            folder,
            um.MODEL_STAGING_NAME,
            max_bytes=config.MAX_USER_MODEL_BYTES,
            cancel=work.cancel,
            expected_size=weights.size,
            expected_sha256=weights.sha256,
            on_bytes=progress,
        )
        try:
            kind = sniff_format(tmp)
            if um.find_by_sha(sha, exclude=model_id):
                raise Refused("duplicate")
            final = folder / um.MODEL_FILES[kind]
            _finish_file(tmp, final)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        um.set_status(
            model_id,
            "checking",
            format=kind,
            file={
                "name": final.name,
                "size": size,
                "sha256": sha,
                "mtime_ns": final.stat().st_mtime_ns,
            },
        )
        stored = True
        _run_check(model_id, work, _LINK_SHARE)
    except hf.Cancelled:
        um.drop_entry(model_id)
    except Refused as exc:
        _refuse(model_id, exc.code, keep_files=stored, checked=stored)
    except Exception:
        logger.exception("my models: adding %s from a link failed", model_id)
        _refuse(model_id, "load_failed", keep_files=stored, checked=stored)
    finally:
        _end_work(model_id)


@router.post("/my-models/link", status_code=202, dependencies=[Depends(require_online_sources)])
async def add_by_link(request: Request) -> JSONResponse:
    try:
        raw = await request.json()
    except Exception:  # noqa: BLE001 -- not JSON: the same fixed answer
        return _refusal("bad_link", 422, "body")
    if not isinstance(raw, dict) or set(raw) - {"link", "stems", "licence_confirmed", "name"}:
        return _refusal("bad_link", 422, "body")
    try:
        stems = _check_stems(raw.get("stems"))
    except Refused:
        return _refusal("stem_not_in_model", 422, "stems")
    if raw.get("licence_confirmed") is not True:
        return _refusal("licence", 422)
    try:
        link = hf.parse_link(raw.get("link"))
    except Refused:
        return _refusal("bad_link", 422)
    name = display_text(raw.get("name"), MAX_NAME_CHARS) or display_text(link.repo, MAX_NAME_CHARS)
    source = {
        "kind": "hf",
        "label": link.label,
        "link": link.canonical(),
        "commit": None,
        "license_declared": None,
    }
    try:
        model_id = await asyncio.to_thread(
            _admit_new,
            name=name or "Model",
            stems=stems,
            source=source,
            licence_confirmed_at=time.time(),
            status="downloading",
        )
    except Refused as exc:
        return _refusal(exc.code, 409)
    with _lock:
        work = _work[model_id]
    _spawn(_link_work, model_id, link, stems, work)
    return JSONResponse(um.get(model_id), status_code=202)


# ── by upload ──


def _copy_capped(src, folder: Path, name: str, cap: int) -> tuple[Path, int, str]:
    """Copy an uploaded part into "<name>.<32 hex>.partial", hashing it and
    refusing it the moment it passes ``cap`` (a chunked body has no length
    that could have been checked first). Flushed and synced."""
    tmp = folder / f"{name}.{uuid.uuid4().hex}.partial"
    digest = hashlib.sha256()
    size = 0
    try:
        src.seek(0)
        with open(tmp, "xb") as out:
            while chunk := src.read(_COPY_CHUNK):
                size += len(chunk)
                if size > cap:
                    raise Refused("too_large")
                out.write(chunk)
                digest.update(chunk)
            out.flush()
            os.fsync(out.fileno())
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise Refused("disk_full") from exc
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    if size == 0:
        tmp.unlink(missing_ok=True)
        raise Refused("empty")
    return tmp, size, digest.hexdigest()


def _part_size(part: UploadFile) -> int:
    part.file.seek(0, 2)
    size = part.file.tell()
    part.file.seek(0)
    return size


def _ext(name: str | None) -> str:
    name = (name or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name[name.rfind(".") :] if "." in name else ""


def _store_upload(model_id: str, model: UploadFile, config_bytes: bytes, cleaned) -> None:
    """The two files into the entry's folder under their fixed names, then
    the entry to "checking". Raises Refused (the caller drops the entry)."""
    folder = um.model_dir(model_id)
    tmp, size, sha = _copy_capped(
        model.file, folder, um.MODEL_STAGING_NAME, config.MAX_USER_MODEL_BYTES
    )
    try:
        kind = sniff_format(tmp)
        if um.find_by_sha(sha, exclude=model_id):
            raise Refused("duplicate")
        final = folder / um.MODEL_FILES[kind]
        _finish_file(tmp, final)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    cfg_tmp = folder / f"{um.CONFIG_FILE}.{uuid.uuid4().hex}.partial"
    try:
        with open(cfg_tmp, "xb") as out:
            out.write(config_bytes)
            out.flush()
            os.fsync(out.fileno())
        _finish_file(cfg_tmp, folder / um.CONFIG_FILE)
    except OSError as exc:
        cfg_tmp.unlink(missing_ok=True)
        raise Refused("disk_full") from exc
    um.set_status(
        model_id,
        "checking",
        format=kind,
        file={
            "name": final.name,
            "size": size,
            "sha256": sha,
            "mtime_ns": final.stat().st_mtime_ns,
        },
        config={
            "name": um.CONFIG_FILE,
            "size": len(config_bytes),
            "sha256": hashlib.sha256(config_bytes).hexdigest(),
        },
        arch=cleaned.arch,
        kwargs=cleaned.kwargs,
        audio=cleaned.audio,
        outputs=cleaned.outputs,
        params=cleaned.params,
    )


def _check_work(model_id: str, work: _Work, base: int) -> None:
    try:
        _run_check(model_id, work, base)
    except Exception:
        logger.exception("my models: checking %s failed", model_id)
        _refuse(model_id, "load_failed", keep_files=True, checked=True)
    finally:
        _end_work(model_id)


def _upload_status(code: str) -> int:
    return 409 if code in ("busy", "limit", "duplicate") else 422


@router.post("/my-models/upload", status_code=202)
async def add_by_upload(request: Request) -> JSONResponse:
    declared = request.headers.get("content-length")
    if declared and declared.strip().isdecimal():
        if int(declared) > MAX_UPLOAD_BODY:
            return _refusal("too_large", 422)
        if _free_bytes(um.root()) < 2 * int(declared) + config.USER_MODEL_DISK_MARGIN_BYTES:
            return _refusal("disk_full", 422)
    with _lock:
        busy = _busy_locked()
    if busy:
        return _refusal("busy", 409)
    if um.count() >= config.MAX_USER_MODELS:
        return _refusal("limit", 409)
    try:
        form = await request.form(max_files=2, max_fields=8)
    except Exception:  # noqa: BLE001 -- a body that is not multipart
        return _refusal("bad_format", 422, "body")
    try:
        return await _add_from_form(form)
    finally:
        await form.close()


async def _add_from_form(form) -> JSONResponse:
    model = form.get("model")
    cfg = form.get("config")
    if not isinstance(model, UploadFile):
        return _refusal("empty", 422)
    if not isinstance(cfg, UploadFile):
        return _refusal("config_missing", 422)
    try:
        stems = _check_stems(json.loads(str(form.get("stems") or "")))
    except (ValueError, Refused):
        return _refusal("stem_not_in_model", 422, "stems")
    if form.get("licence") != "true":
        return _refusal("licence", 422)
    if _ext(model.filename) not in MODEL_EXTS:
        return _refusal("bad_format", 422)
    if _ext(cfg.filename) not in CONFIG_EXTS:
        return _refusal("bad_config", 422)
    model_size = await asyncio.to_thread(_part_size, model)
    cfg_size = await asyncio.to_thread(_part_size, cfg)
    if model_size == 0 or cfg_size == 0:
        return _refusal("empty", 422)
    if model_size > config.MAX_USER_MODEL_BYTES or cfg_size > config.MAX_USER_MODEL_CONFIG_BYTES:
        return _refusal("too_large", 422)
    if _free_bytes(um.root()) < 2 * model_size + config.USER_MODEL_DISK_MARGIN_BYTES:
        return _refusal("disk_full", 422)
    config_bytes = await asyncio.to_thread(cfg.file.read, config.MAX_USER_MODEL_CONFIG_BYTES + 1)
    try:
        cleaned = await asyncio.to_thread(lambda: clean_config(parse_config(config_bytes), stems))
    except Refused as exc:
        return _refusal(exc.code, 422)
    filename = (model.filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    label = display_text(filename, 120)
    name = display_text(form.get("name"), MAX_NAME_CHARS) or display_text(
        filename.rsplit(".", 1)[0], MAX_NAME_CHARS
    )
    try:
        model_id = await asyncio.to_thread(
            _admit_new,
            name=name or "Model",
            stems=stems,
            source={"kind": "upload", "label": label, "commit": None, "license_declared": None},
            licence_confirmed_at=time.time(),
            status="staging",
        )
    except Refused as exc:
        return _refusal(exc.code, 409)
    try:
        await asyncio.to_thread(_store_upload, model_id, model, config_bytes, cleaned)
    except Refused as exc:
        await asyncio.to_thread(um.drop_entry, model_id)
        _end_work(model_id)
        return _refusal(exc.code, _upload_status(exc.code))
    except Exception:
        logger.exception("my models: storing an upload failed")
        await asyncio.to_thread(um.drop_entry, model_id)
        _end_work(model_id)
        return _refusal("load_failed", 422, "refused")
    with _lock:
        work = _work[model_id]
    _spawn(_check_work, model_id, work, 0)
    return JSONResponse(um.get(model_id), status_code=202)


# ── one model ──


def _known(model_id: str) -> dict | None:
    """The record of a well-formed id, or None (both answer 404)."""
    if not um.is_user_id(model_id):
        return None
    return um.record(model_id)


def _not_found() -> JSONResponse:
    return JSONResponse({"detail": "Not Found"}, status_code=404)


def _check_again_work(model_id: str, work: _Work) -> None:
    """Check again: the files against their record, the config through the
    current whitelist, then the test run."""
    try:
        rec = um.record(model_id)
        if rec is None:
            return
        path = um.model_file_path(model_id, rec)
        if path is None or not path.is_file():
            raise Refused("changed")
        size, sha = _file_hash(path)
        if size != rec["file"]["size"] or sha != rec["file"]["sha256"]:
            raise Refused("changed")
        cleaned = _clean_from_disk(model_id, list(rec["stems"]))
        um.set_status(
            model_id,
            "checking",
            arch=cleaned.arch,
            kwargs=cleaned.kwargs,
            audio=cleaned.audio,
            outputs=cleaned.outputs,
            params=cleaned.params,
        )
        _run_check(model_id, work, 0)
    except FileNotFoundError:
        _refuse(model_id, "changed", keep_files=True, checked=True)
    except Refused as exc:
        _refuse(model_id, exc.code, keep_files=True, checked=True)
    except Exception:
        logger.exception("my models: checking %s again failed", model_id)
        _refuse(model_id, "load_failed", keep_files=True, checked=True)
    finally:
        _end_work(model_id)


@router.post("/my-models/{model_id}/check", status_code=202)
async def check_again(model_id: str) -> JSONResponse:
    rec = await asyncio.to_thread(_known, model_id)
    if rec is None:
        return _not_found()
    source = rec.get("source") or {}
    if rec.get("file") is None:
        # Nothing was kept (a download that failed): a link is fetched again.
        if source.get("kind") != "hf":
            return _refusal("changed", 409)
        require_online_sources()
        try:
            link = hf.parse_link(source.get("link"))
        except Refused:
            return _refusal("changed", 409)
        try:
            await asyncio.to_thread(_admit_existing, model_id, "downloading")
        except Refused as exc:
            return _refusal(exc.code, 409)
        with _lock:
            work = _work[model_id]
        _spawn(_link_work, model_id, link, list(rec["stems"]), work)
        return JSONResponse(um.get(model_id), status_code=202)
    try:
        await asyncio.to_thread(_admit_existing, model_id, "checking")
    except Refused as exc:
        return _refusal(exc.code, 409)
    with _lock:
        work = _work[model_id]
    _spawn(_check_again_work, model_id, work)
    return JSONResponse(um.get(model_id), status_code=202)


async def _settle(work: _Work) -> bool:
    deadline = time.monotonic() + _CANCEL_SETTLE_SEC
    while time.monotonic() < deadline:
        if work.done.is_set():
            return True
        await asyncio.sleep(0.05)
    return work.done.is_set()


@router.post("/my-models/{model_id}/cancel")
async def cancel(model_id: str) -> JSONResponse:
    if await asyncio.to_thread(_known, model_id) is None:
        return _not_found()
    work = _stop(model_id)
    if work is not None:
        await _settle(work)
    return JSONResponse({"id": model_id, "model": await asyncio.to_thread(um.get, model_id)})


def _in_use(model_id: str) -> bool:
    """A separation running with this model in its plan, or the pipeline lock
    held (a stem fill, which may use any song's recorded plan) while some
    song's plan names it."""
    from app.core.registry import all_jobs
    from app.core.registry import get as get_job
    from app.pipeline import jobqueue
    from app.pipeline.runner import _pipeline_lock

    running, _waiting = jobqueue.snapshot()
    if running is not None:
        job = get_job(running)
        if job is not None and model_id in (job.stem_models or {}).values():
            return True
    if _pipeline_lock.locked():
        return any(model_id in (j.stem_models or {}).values() for j in all_jobs().values())
    return False


@router.delete("/my-models/{model_id}")
async def remove_model(model_id: str) -> JSONResponse:
    if await asyncio.to_thread(_known, model_id) is None:
        return _not_found()
    if await asyncio.to_thread(_in_use, model_id):
        return _refusal("in_use", 409)
    work = _stop(model_id)
    if work is not None and not await _settle(work):
        return _refusal("busy", 409)
    try:
        gone = await asyncio.to_thread(um.remove, model_id)
    except um.UnknownModel:
        return _not_found()
    if not gone:
        return _refusal("busy", 409)
    # After the models' lock is released (the lock order in user_models).
    await asyncio.to_thread(settings.scrub_stem_model, model_id)
    logger.info("my models: removed %s", model_id)
    return JSONResponse({"removed": model_id})


@router.get("/my-models")
def list_my_models() -> dict[str, object]:
    return {
        "models": um.list_models(),
        "limits": {
            "max_bytes": config.MAX_USER_MODEL_BYTES,
            "max_config_bytes": config.MAX_USER_MODEL_CONFIG_BYTES,
            "max_models": config.MAX_USER_MODELS,
            "max_params": config.MAX_USER_MODEL_PARAMS,
        },
        "registry_reset": um.registry_reset(),
        "available": um.requirements_met(),
    }
