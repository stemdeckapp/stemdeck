"""The store of the user's own models (app/core/user_models.py): the registry
file, its atomic writes, what a startup settles, and what counts as usable.
Every test runs in a temporary root (tests/conftest.py)."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path

import pytest

from app.core import settings as _settings
from app.core import user_model_format as fmt
from app.core import user_models as um
from app.core.config import MAX_USER_MODELS

DOC = {
    "audio": {"chunk_size": 44_100},
    "model": {
        "dim": 16,
        "depth": 1,
        "stereo": True,
        "num_bands": 8,
        "stft_n_fft": 256,
        "stft_win_length": 256,
        "stft_hop_length": 64,
        "heads": 1,
        "dim_head": 8,
        "time_transformer_depth": 1,
        "freq_transformer_depth": 1,
        "num_stems": 2,
    },
    "training": {"instruments": ["vocals", "drums"]},
}


@pytest.fixture(autouse=True)
def _runnable(monkeypatch):
    """This install can run a user model, whatever packages the machine has."""
    monkeypatch.setattr(um, "requirements_met", lambda: True)


def _entry(stems=("vocals",), status="checking", name="Mine") -> str:
    return um.new_entry(
        name=name,
        stems=list(stems),
        source={"kind": "upload", "label": "m.ckpt", "commit": None, "license_declared": None},
        licence_confirmed_at=time.time(),
        status=status,
    )


def _ready(stems=("vocals",), data: bytes = b"weights", name="Mine") -> str:
    model_id = _entry(stems, name=name)
    path = um.model_dir(model_id) / "model.ckpt"
    path.write_bytes(data)
    cleaned = fmt.clean_config(DOC, list(stems))
    um.set_status(
        model_id,
        "ready",
        format="torch",
        file={
            "name": "model.ckpt",
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "mtime_ns": path.stat().st_mtime_ns,
        },
        config={"name": "config.yaml", "size": 1, "sha256": "0" * 64},
        arch=cleaned.arch,
        kwargs=cleaned.kwargs,
        audio=cleaned.audio,
        outputs=cleaned.outputs,
        params=cleaned.params,
        check={"cpu_rtf": 0.5, "torch": "2.6.0", "separator": "0.44.5"},
        checked=True,
    )
    return model_id


def _reload() -> None:
    um.reset_state()


def test_the_root_is_a_temporary_folder(tmp_path):
    assert um.root().is_relative_to(tmp_path)
    assert um.registry_path().parent == um.root()


def test_the_id_shape():
    assert um.is_user_id("user_0123456789ab")
    for bad in ("user_0123456789AB", "user_0123456789a", "user_0123456789abc", "../x", 5, None):
        assert not um.is_user_id(bad)
    with pytest.raises(ValueError):
        um.model_dir("../../etc")


def test_a_new_entry_is_saved_before_its_folder_exists(monkeypatch):
    seen: list[bool] = []
    real_mkdir = Path.mkdir

    def spy(self, *args, **kwargs):
        if um.is_user_id(self.name):
            saved = json.loads(um.registry_path().read_text())
            seen.append(self.name in saved["models"])
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", spy)
    model_id = _entry()
    assert seen == [True]
    assert um.model_dir(model_id).is_dir()
    _reload()
    assert um.record(model_id)["status"] == "checking"


def test_the_list_holds_at_most_the_limit():
    for _ in range(MAX_USER_MODELS):
        _entry()
    with pytest.raises(um.LimitReached):
        _entry()
    assert um.count() == MAX_USER_MODELS


def test_writes_are_synced_then_renamed(monkeypatch):
    calls: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(um.os, "fsync", lambda fd: (calls.append("fsync"), real_fsync(fd)))
    monkeypatch.setattr(
        um.os, "replace", lambda a, b: (calls.append("replace"), real_replace(a, b))
    )
    _entry()
    assert calls[:2] == ["fsync", "replace"]


def test_a_write_cut_before_the_rename_leaves_the_old_registry(monkeypatch):
    first = _entry(name="First")
    before = um.registry_path().read_bytes()

    def crash(a, b):
        raise OSError("power cut")

    real_replace = os.replace
    monkeypatch.setattr(um.os, "replace", crash)
    um.set_status(first, "refused", code="timeout")
    assert um.registry_path().read_bytes() == before
    assert [p.name for p in um.root().iterdir() if p.suffix == ".tmp"] == []
    # Never monkeypatch.undo() here: it would also undo the root's isolation.
    monkeypatch.setattr(um.os, "replace", real_replace)
    _reload()
    assert um.record(first)["status"] == "checking"


def test_a_corrupt_registry_is_moved_aside_and_model_folders_are_kept(caplog):
    model_id = _ready()
    um.registry_path().write_text("{not json")
    _reload()
    assert um.list_models() == []
    assert um.registry_reset() is True
    assert list(um.root().glob("models.json.corrupt-*"))
    um.reconcile()
    # The folder still holds the user's model file, so it stays, and says so.
    assert (um.root() / model_id / "model.ckpt").is_file()
    assert "holds a model file" in caplog.text


def test_an_unreadable_entry_is_dropped_and_the_rest_kept():
    keep = _entry()
    data = json.loads(um.registry_path().read_text())
    data["models"]["user_ffffffffffff"] = {"status": "nonsense"}
    data["models"]["../x"] = {"status": "ready"}
    um.registry_path().write_text(json.dumps(data))
    _reload()
    assert [m["id"] for m in um.list_models()] == [keep]


def test_reconcile_settles_each_status(tmp_path):
    downloading = _entry(status="downloading")
    staging = _entry(status="staging")
    checking = _entry(status="checking")
    ready = _ready()
    (um.model_dir(downloading) / "model.0123456789abcdef0123456789abcdef.partial").write_bytes(b"x")
    _reload()
    um.reconcile()
    assert um.record(downloading) is None and not (um.root() / downloading).exists()
    assert um.record(staging) is None and not (um.root() / staging).exists()
    assert um.record(checking)["status"] == "refused"
    assert um.record(checking)["code"] == "interrupted"
    assert um.record(ready)["status"] == "ready"


def test_reconcile_removes_old_empty_orphans_and_keeps_the_rest():
    base = um.root()
    base.mkdir(parents=True, exist_ok=True)
    old_empty = base / "user_aaaaaaaaaaaa"
    young_empty = base / "user_bbbbbbbbbbbb"
    old_model = base / "user_cccccccccccc"
    other = base / "not-a-model"
    for folder in (old_empty, young_empty, old_model, other):
        folder.mkdir()
    (old_model / "model.safetensors").write_bytes(b"x")
    past = time.time() - 2 * um.ORPHAN_MIN_AGE_SEC
    for folder in (old_empty, old_model, other):
        os.utime(folder, (past, past))
    um.reconcile()
    assert not old_empty.exists()
    assert young_empty.exists() and old_model.exists() and other.exists()


def test_reconcile_sweeps_old_partials_only():
    model_id = _ready()
    folder = um.model_dir(model_id)
    old = folder / "model.0123456789abcdef0123456789abcdef.partial"
    young = folder / "config.yaml.fedcba9876543210fedcba9876543210.partial"
    old.write_bytes(b"x")
    young.write_bytes(b"x")
    past = time.time() - 3600
    os.utime(old, (past, past))
    um.reconcile()
    assert not old.exists() and young.exists()


def test_reconcile_never_raises(monkeypatch):
    monkeypatch.setattr(um, "_reconcile", lambda: 1 / 0)
    um.reconcile()


def test_find_by_sha():
    model_id = _ready(data=b"abc")
    sha = hashlib.sha256(b"abc").hexdigest()
    assert um.find_by_sha(sha) == model_id
    assert um.find_by_sha(sha, exclude=model_id) is None
    assert um.find_by_sha("0" * 64) is None


def test_usable_needs_ready_the_recorded_file_and_the_packages(monkeypatch):
    model_id = _ready()
    assert um.usable(model_id)
    path = um.model_dir(model_id) / "model.ckpt"
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000_000))
    assert not um.usable(model_id)
    assert um.get(model_id)["code"] == "changed" and um.get(model_id)["status"] == "ready"
    path.write_bytes(b"other size")
    assert not um.usable(model_id)
    assert um.get(model_id)["status"] == "missing" and um.get(model_id)["missing"] is True
    path.unlink()
    assert um.get(model_id)["status"] == "missing"
    other = _ready(data=b"second")
    monkeypatch.setattr(um, "requirements_met", lambda: False)
    assert not um.usable(other)


def test_a_hand_edited_record_is_not_usable():
    model_id = _ready()
    data = json.loads(um.registry_path().read_text())
    data["models"][model_id]["kwargs"]["dim"] = 100_000
    um.registry_path().write_text(json.dumps(data))
    _reload()
    assert um.record(model_id)["status"] == "ready"
    assert not um.usable(model_id)
    with pytest.raises(um.UnknownModel):
        um.worker_spec(model_id)


def test_choices_for_filters_by_stem_and_status_in_added_order():
    first = _ready(stems=("vocals", "drums"), data=b"1")
    second = _ready(stems=("drums",), data=b"2")
    _entry(stems=("drums",))  # still checking
    assert um.choices_for("drums") == (first, second)
    assert um.choices_for("vocals") == (first,)
    assert um.choices_for("bass") == ()


def test_worker_spec_carries_what_the_worker_needs():
    model_id = _ready(stems=("drums", "vocals"), data=b"weights")
    spec = um.worker_spec(model_id)
    assert set(spec) == {
        "id",
        "path",
        "arch",
        "kwargs",
        "audio",
        "outputs",
        "num_stems",
        "sha256",
        "size",
        "mtime_ns",
        "stems",
        "format",
    }
    assert spec["outputs"] == {"vocals": 0, "drums": 1}
    assert spec["num_stems"] == 2 and spec["arch"] == "mel_band_roformer"
    assert Path(spec["path"]) == um.model_dir(model_id) / "model.ckpt"
    with pytest.raises(um.UnknownModel):
        um.worker_spec(_entry())


def test_the_public_entry_has_no_path_or_link():
    model_id = _ready()
    entry = um.get(model_id)
    assert set(entry) == {
        "id",
        "name",
        "status",
        "pct",
        "step",
        "code",
        "size",
        "sha256",
        "stems",
        "arch",
        "params",
        "source",
        "added_at",
        "checked_at",
        "cpu_rtf",
        "usable",
        "missing",
    }
    assert set(entry["source"]) == {"kind", "label", "license_declared"}
    assert str(um.root()) not in json.dumps(entry)
    assert entry["usable"] is True and entry["cpu_rtf"] == 0.5
    assert um.name_of(model_id) == "Mine"


def test_progress_is_memory_only():
    model_id = _entry(status="downloading")
    um.set_progress(model_id, pct=40, step="model")
    assert um.get(model_id)["pct"] == 40 and um.get(model_id)["step"] == "model"
    _reload()
    assert um.get(model_id)["pct"] == 0


def test_set_status_refuses_fixed_fields():
    model_id = _entry()
    with pytest.raises(ValueError):
        um.set_status(model_id, "ready", name="renamed")
    with pytest.raises(ValueError):
        um.set_status(model_id, "nonsense")
    with pytest.raises(um.UnknownModel):
        um.set_status("user_000000000000", "ready")


def test_remove_takes_the_folder_then_the_entry():
    model_id = _ready()
    assert um.remove(model_id) is True
    assert um.record(model_id) is None
    assert not (um.root() / model_id).exists()
    _reload()
    assert um.record(model_id) is None
    with pytest.raises(um.UnknownModel):
        um.remove(model_id)


def test_remove_keeps_the_entry_when_files_remain(monkeypatch):
    model_id = _ready()

    def stuck(path, *args, **kwargs):
        raise PermissionError("in use")

    monkeypatch.setattr(um.shutil, "rmtree", stuck)
    monkeypatch.setattr(um.time, "sleep", lambda s: None)
    assert um.remove(model_id) is False
    assert um.record(model_id)["code"] == "busy"
    assert (um.root() / model_id / "model.ckpt").is_file()


def test_clear_files_empties_the_folder_and_keeps_the_entry():
    model_id = _ready()
    assert um.clear_files(model_id)
    assert list(um.model_dir(model_id).iterdir()) == []
    rec = um.record(model_id)
    assert rec["file"] is None and rec["config"] is None


def test_removing_never_deadlocks_against_settings(monkeypatch):
    """settings._LOCK is taken before the store's lock (choices are checked
    under it); a removal scrubs the setting only after releasing the store's
    lock. Two threads doing both, 200 times each, finish."""
    monkeypatch.setattr(_settings.stem_models, "deployment_pinned", lambda: False)
    errors: list[BaseException] = []

    def remover() -> None:
        try:
            for i in range(200):
                model_id = _ready(data=f"w{i}".encode())
                _settings.set_stem_model_choices({"vocals": model_id})
                um.remove(model_id)
                _settings.scrub_stem_model(model_id)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def reader() -> None:
        try:
            for _ in range(200):
                _settings.get_stem_model_choices()
                with _settings._LOCK:
                    um.choices_for("vocals")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=remover), threading.Thread(target=reader)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert not any(t.is_alive() for t in threads), "deadlock"
    assert errors == []
    assert _settings.get_stem_model_choices()["vocals"] == "auto"
