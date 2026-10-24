"""The registry must not lose the library to one bad record or one bad write.

registry.json is the only place favourites, the Trash, the queue and the
delete tombstones live. One malformed record used to abort the whole load, and
the persist that follows orphan recovery then wrote the survivors over the
file. A corrupt or empty file was overwritten the same way with no copy kept,
and the write itself was not fsynced, so a power loss could produce the empty
file in the first place.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from app.core import registry as _registry
from app.core.models import Job
from app.core.registry import registry_path

GOOD_A = "a1a1a1a1a1a1"
GOOD_B = "b2b2b2b2b2b2"
BAD = "c3c3c3c3c3c3"


@pytest.fixture(autouse=True)
def _isolate_registry():
    _registry._jobs.clear()
    _registry._deleted.clear()
    yield
    _registry._jobs.clear()
    _registry._deleted.clear()


def _record(job_id: str, **kw) -> dict:
    job = Job(id=job_id, status="done", title=f"Song {job_id[:2]}", **kw)
    return job.to_record()


def _write(jobs_dir: Path, payload) -> Path:
    path = registry_path(jobs_dir)
    text = payload if isinstance(payload, str) else json.dumps(payload)
    path.write_text(text, encoding="utf-8")
    return path


def _sidecars(jobs_dir: Path, kind: str) -> list[Path]:
    return sorted(jobs_dir.glob(f"registry.json.{kind}-*"))


# ── one bad record among good ones ──


@pytest.mark.parametrize(
    "bad",
    [
        "not a record",
        42,
        None,
        ["a", "list"],
        {"title": "no id at all", "status": "done"},
    ],
)
def test_one_bad_record_does_not_cost_the_others(tmp_path: Path, bad):
    _write(
        tmp_path,
        {
            "version": 1,
            "jobs": [_record(GOOD_A, favorite=True), bad, _record(GOOD_B)],
            "deleted": [BAD],
        },
    )

    _registry.restore(tmp_path)

    jobs = _registry.all_jobs()
    assert set(jobs) == {GOOD_A, GOOD_B}
    assert jobs[GOOD_A].favorite is True, "a favourite next to a bad record survives"
    assert BAD in _registry.deleted_ids(), "the tombstones survive too"
    kept = _sidecars(tmp_path, "bad")
    assert len(kept) == 1, "the bad record is kept, not silently dropped"
    assert json.loads(kept[0].read_text(encoding="utf-8"))["records"] == [bad]


def test_the_persist_after_a_bad_record_keeps_the_good_ones(tmp_path: Path):
    _write(tmp_path, {"jobs": [_record(GOOD_A, favorite=True), "junk"]})
    _registry.restore(tmp_path)
    _registry.persist(tmp_path)

    on_disk = json.loads(registry_path(tmp_path).read_text(encoding="utf-8"))
    assert [r["id"] for r in on_disk["jobs"]] == [GOOD_A]
    assert on_disk["jobs"][0]["favorite"] is True


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("created_at", "yesterday"),
        ("favorite", "yes"),
        ("trashed_at", [1, 2]),
        ("takes", "none"),
        ("queue_position", True),
        ("status", ["done"]),
    ],
)
def test_wrong_types_per_field_are_repaired_not_fatal(tmp_path: Path, field, value):
    record = _record(GOOD_A)
    record[field] = value
    _write(tmp_path, {"jobs": [record, _record(GOOD_B, favorite=True)]})

    _registry.restore(tmp_path)
    _registry.persist(tmp_path)  # sorting by created_at used to raise forever

    jobs = _registry.all_jobs()
    assert jobs[GOOD_B].favorite is True
    if field != "status":  # a reset status is "queued", which restore resumes
        assert GOOD_A in jobs
        default = getattr(Job(id=GOOD_A), field)
        if field != "created_at":
            assert getattr(jobs[GOOD_A], field) == default
    assert _sidecars(tmp_path, "bad"), "the original record is kept beside the registry"
    on_disk = json.loads(registry_path(tmp_path).read_text(encoding="utf-8"))
    assert GOOD_B in {r["id"] for r in on_disk["jobs"]}


def test_a_good_registry_writes_no_sidecar(tmp_path: Path):
    _write(tmp_path, {"jobs": [_record(GOOD_A), _record(GOOD_B)]})
    _registry.restore(tmp_path)
    assert set(_registry.all_jobs()) == {GOOD_A, GOOD_B}
    assert _sidecars(tmp_path, "bad") == []
    assert _sidecars(tmp_path, "corrupt") == []


# ── a file that cannot be read as a whole ──


@pytest.mark.parametrize(
    "text",
    [
        "",  # zero-length, what a lost write leaves
        '{"version": 1, "jobs": [{"id": "a1a1a1a1a1a1", "status": "do',  # truncated
        "{not json",
        "[1, 2]",
        "null",
    ],
)
def test_an_unreadable_registry_is_moved_aside_before_any_persist(tmp_path: Path, text):
    _write(tmp_path, text)
    # A job folder on disk, so restore() re-adopts something and persists.
    stems = tmp_path / GOOD_A / "stems"
    stems.mkdir(parents=True)
    (stems / "vocals.wav").write_bytes(b"RIFF")
    (tmp_path / GOOD_A / "metadata.json").write_text('{"title": "Kept"}', encoding="utf-8")

    _registry.restore(tmp_path)

    corrupt = _sidecars(tmp_path, "corrupt")
    assert len(corrupt) == 1, "the original bytes must be kept for recovery"
    assert corrupt[0].read_text(encoding="utf-8") == text
    assert GOOD_A in _registry.all_jobs(), "the jobs on disk are re-adopted"
    fresh = json.loads(registry_path(tmp_path).read_text(encoding="utf-8"))
    assert [r["id"] for r in fresh["jobs"]] == [GOOD_A]


def test_invalid_utf8_is_moved_aside(tmp_path: Path):
    registry_path(tmp_path).write_bytes(b'{"jobs": ["\xff\xfe"]}')
    _registry.restore(tmp_path)
    assert len(_sidecars(tmp_path, "corrupt")) == 1


# ── the write ──


def test_a_crash_at_the_rename_leaves_the_old_file_and_no_temp(tmp_path: Path, monkeypatch):
    _write(tmp_path, {"version": 1, "jobs": [_record(GOOD_A, favorite=True)], "deleted": []})
    before = registry_path(tmp_path).read_text(encoding="utf-8")
    _registry.restore(tmp_path)
    _registry.register(Job(id=GOOD_B, status="done", title="New"))

    def crash(src, dst):
        raise OSError("simulated crash at the rename")

    monkeypatch.setattr(_registry.os, "replace", crash)
    _registry.persist(tmp_path)  # logs, does not raise

    assert registry_path(tmp_path).read_text(encoding="utf-8") == before
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]


def test_the_write_is_fsynced_before_the_rename(tmp_path: Path, monkeypatch):
    order: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(
        _registry.os, "fsync", lambda fd: (order.append("fsync"), real_fsync(fd))[1]
    )
    monkeypatch.setattr(
        _registry.os,
        "replace",
        lambda a, b: (order.append("replace"), real_replace(a, b))[1],
    )
    _registry.register(Job(id=GOOD_A, status="done", title="Song"))
    _registry.persist(tmp_path)

    assert order.index("fsync") < order.index("replace")
    assert GOOD_A in registry_path(tmp_path).read_text(encoding="utf-8")


def test_concurrent_saves_always_leave_a_whole_file(tmp_path: Path):
    for i in range(20):
        _registry.register(Job(id=f"{i:012x}", status="done", title=f"S{i}"))
    errors: list[BaseException] = []

    def save():
        try:
            for _ in range(15):
                _registry.persist(tmp_path)
        except BaseException as exc:  # pragma: no cover - the assertion reports it
            errors.append(exc)

    threads = [threading.Thread(target=save) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    data = json.loads(registry_path(tmp_path).read_text(encoding="utf-8"))
    assert len(data["jobs"]) == 20
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
