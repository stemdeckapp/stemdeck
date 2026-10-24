"""registry.restore() on the inputs the other registry suites do not build: a
jobs list that is not a list, a record whose id is not a job id, a record that
fails while being resumed, a file that fails as a whole after it parsed, and
orphan folders with a mix, unreadable metadata or a read-only folder.

restore() runs at import time, so every one of these must leave the backend
booting, and none may lose what the user had without keeping a copy.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from app.core import registry as _registry
from app.core.models import Job
from app.core.registry import registry_path

GOOD = "a1b2c3d4e5f6"
OTHER = "a1b2c3d4e5f7"


@pytest.fixture(autouse=True)
def _empty_registry():
    _registry._jobs.clear()
    yield
    _registry._jobs.clear()


def _write(jobs_dir: Path, data) -> Path:
    path = registry_path(jobs_dir)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _stems(jobs_dir: Path, job_id: str, *names: str) -> Path:
    stems = jobs_dir / job_id / "stems"
    stems.mkdir(parents=True, exist_ok=True)
    for name in names or ("vocals",):
        (stems / f"{name}.wav").write_bytes(b"RIFF")
    return jobs_dir / job_id


def _kept(jobs_dir: Path, kind: str) -> list[Path]:
    return sorted(jobs_dir.glob(f"registry.json.{kind}-*"))


def test_a_jobs_value_that_is_not_a_list_is_kept_aside(tmp_path):
    _write(tmp_path, {"version": 1, "jobs": {"id": GOOD}})
    _stems(tmp_path, OTHER)

    _registry.restore(tmp_path)

    [bad] = _kept(tmp_path, "bad")
    kept = json.loads(bad.read_text(encoding="utf-8"))
    assert kept["why"] == "the jobs list was not a list"
    assert kept["records"] == [{"id": GOOD}]
    # Orphan recovery still ran.
    assert OTHER in _registry.all_jobs()


def test_a_record_whose_id_is_not_a_job_id_is_not_loaded(tmp_path):
    good = Job(id=GOOD, status="done", title="Song").to_record()
    bad = Job(id="../escape", status="done", title="Nope").to_record()
    _write(tmp_path, {"version": 1, "jobs": [bad, good]})

    _registry.restore(tmp_path)

    assert set(_registry.all_jobs()) == {GOOD}


def test_a_record_that_fails_while_resuming_is_kept_and_the_rest_load(
    tmp_path, monkeypatch, caplog
):
    resumable = Job(id=OTHER, status="separating", title="Half").to_record()
    good = Job(id=GOOD, status="done", title="Song").to_record()
    _write(tmp_path, {"version": 1, "jobs": [resumable, good]})

    def broken(job, job_dir):
        raise PermissionError(13, "locked by another program")

    monkeypatch.setattr(_registry, "_resume_or_recover", broken)

    with caplog.at_level(logging.WARNING, logger="stemdeck"):
        _registry.restore(tmp_path)

    assert set(_registry.all_jobs()) == {GOOD}
    [bad] = _kept(tmp_path, "bad")
    assert [r["id"] for r in json.loads(bad.read_text(encoding="utf-8"))["records"]] == [OTHER]
    assert f"could not restore registry record {OTHER}" in caplog.text


def test_a_file_that_fails_as_a_whole_is_quarantined_and_orphans_still_load(tmp_path, monkeypatch):
    path = _write(tmp_path, {"version": 1, "jobs": []})
    original = path.read_bytes()
    _stems(tmp_path, OTHER)

    def broken(_data):
        raise RuntimeError("a migration that cannot handle this file")

    monkeypatch.setattr(_registry, "_migrate", broken)

    _registry.restore(tmp_path)

    [corrupt] = _kept(tmp_path, "corrupt")
    assert corrupt.read_bytes() == original, "the bytes are kept, not rewritten"
    assert OTHER in _registry.all_jobs()


def test_failing_to_keep_bad_records_never_stops_the_load(tmp_path, monkeypatch, caplog):
    good = Job(id=GOOD, status="done", title="Song").to_record()
    _write(tmp_path, {"version": 1, "jobs": [good, "not a record"]})
    real = _registry._write_atomic

    def no_room(path, payload):
        if ".bad-" in path.name:
            raise OSError(28, "No space left on device")
        return real(path, payload)

    monkeypatch.setattr(_registry, "_write_atomic", no_room)

    with caplog.at_level(logging.WARNING, logger="stemdeck"):
        _registry.restore(tmp_path)

    assert GOOD in _registry.all_jobs()
    assert "could not keep bad registry records aside" in caplog.text


def test_an_orphan_with_a_mix_points_at_it(tmp_path):
    _stems(tmp_path, OTHER, "vocals", "drums", "mix")

    _registry.restore(tmp_path)

    job = _registry.get(OTHER)
    assert job.mix_url == f"/api/jobs/{OTHER}/stems/mix.wav"
    assert job.selected_stems == ["vocals", "drums"]


def test_an_orphan_without_a_mix_has_none(tmp_path):
    _stems(tmp_path, OTHER, "vocals")

    _registry.restore(tmp_path)

    assert _registry.get(OTHER).mix_url is None


def test_an_orphan_with_unreadable_metadata_is_still_recovered(tmp_path):
    job_dir = _stems(tmp_path, OTHER)
    (job_dir / "metadata.json").write_text("{torn", encoding="utf-8")

    _registry.restore(tmp_path)

    job = _registry.get(OTHER)
    assert job is not None and job.status == "done"
    assert job.title is None
    assert (job_dir / "metadata.json").read_text(encoding="utf-8") == "{torn", "left as found"


def test_an_orphan_in_a_read_only_folder_is_recovered_with_a_placeholder(
    tmp_path, monkeypatch, caplog
):
    job_dir = _stems(tmp_path, OTHER)
    real = Path.write_text

    def read_only(self, *a, **k):
        if self.name == "metadata.json":
            raise PermissionError(13, "read-only")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "write_text", read_only)

    with caplog.at_level(logging.WARNING, logger="stemdeck"):
        _registry.restore(tmp_path)

    job = _registry.get(OTHER)
    assert job is not None
    assert job.title == f"Recovered track {OTHER[:6]}"
    assert not (job_dir / "metadata.json").exists()
    assert f"could not write recovery metadata for {OTHER}" in caplog.text
