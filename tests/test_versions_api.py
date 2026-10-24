"""Versions of a song (#804) and Extract on a song that is already separated.

The data model (app/core/versions.py): a song without versions reads as one
implicit default built from its import selection, with nothing rewritten; a
damaged record loses only its bad entries, never the song. The endpoints:
list, create, rename, delete and select, each validated before any I/O, none
of them touching a stem file. fill-stems: only the stems a song has no file
for are separated, with the song's own model and quality, and a stem it has
is never replaced.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.core import registry as _registry
from app.core.config import DEMUCS_MODEL, STEM_NAMES
from app.core.models import Job
from app.core.registry import _jobs
from app.core.registry import persist as persist_registry
from app.core.registry import restore as restore_registry
from app.core.versions import DEFAULT_VERSION_ID, MAX_VERSIONS, clean_versions
from app.pipeline import stem_fill as fill_mod

JOB = "a1b2c3d4e5f6"


def _app():
    from app.main import app

    return app


@pytest.fixture
def jobs_dir():
    from app.core import config

    yield config.JOBS_DIR
    _jobs.pop(JOB, None)


@pytest.fixture
def client(jobs_dir):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app()), base_url="http://testserver"
    )


def _make_job(jobs_dir: Path, names=STEM_NAMES, status="done", **fields) -> Job:
    job = Job(id=JOB, status=status, title="Song", selected_stems=["vocals", "drums"], **fields)
    stems = jobs_dir / JOB / "stems"
    stems.mkdir(parents=True, exist_ok=True)
    for name in names:
        (stems / f"{name}.wav").write_bytes(f"RIFF-{name}-original".encode())
        job.stems.append({"name": name, "url": f"/api/jobs/{JOB}/stems/{name}.wav"})
    _jobs[job.id] = job
    return job


def _stem_bytes(jobs_dir: Path) -> dict[str, bytes]:
    stems = jobs_dir / JOB / "stems"
    return {p.name: p.read_bytes() for p in sorted(stems.iterdir()) if p.is_file()}


# -- Migration


def test_a_song_without_versions_has_one_default_built_from_its_selection():
    job = Job.from_record({"id": JOB, "status": "done", "selected_stems": ["drums", "vocals"]})
    state = job.to_state()
    assert job.versions is None  # nothing is written until a version changes
    assert state["active_version"] == DEFAULT_VERSION_ID
    assert state["versions"] == [
        {
            "id": DEFAULT_VERSION_ID,
            "name": None,
            "stems": ["vocals", "drums"],
            "created_at": job.created_at,
        }
    ]


def test_a_song_selected_with_nothing_defaults_to_every_stem():
    job = Job.from_record({"id": JOB, "status": "done", "selected_stems": []})
    assert job.to_state()["versions"][0]["stems"] == list(STEM_NAMES)


def test_damaged_versions_lose_only_the_bad_entries():
    raw = [
        {"id": "main", "name": "Karaoke", "stems": ["vocals", "kazoo"], "created_at": 1.0},
        {"id": "../etc", "name": "bad id"},
        "not a version",
        {"id": "main", "name": "duplicate"},
        {"id": "b2", "name": "\x00Guitar\nstudy  ", "stems": "drums", "created_at": "x"},
    ]
    job = Job.from_record({"id": JOB, "status": "done", "versions": raw, "active_version": 7})
    assert job.versions == [
        {"id": "main", "name": "Karaoke", "stems": ["vocals"], "created_at": 1.0},
        {"id": "b2", "name": "Guitar study", "stems": list(STEM_NAMES), "created_at": 0.0},
    ]
    assert job.active_version is None
    assert job.to_state()["active_version"] == "main"


@pytest.mark.parametrize("value", [{}, "main", 3, [], [None], [{"id": ""}]])
def test_unusable_versions_read_as_none(value):
    assert clean_versions(value) is None


def test_an_active_version_that_is_gone_falls_back_to_the_first():
    job = Job.from_record(
        {
            "id": JOB,
            "status": "done",
            "versions": [{"id": "aa", "stems": ["bass"]}, {"id": "bb", "stems": ["drums"]}],
            "active_version": "zz",
        }
    )
    assert job.to_state()["active_version"] == "aa"


def test_a_running_fill_is_not_left_running_by_a_restart():
    job = Job.from_record({"id": JOB, "status": "done", "stem_fill": "running"})
    assert job.stem_fill == "error"


def test_versions_survive_a_restart_and_a_bad_record_keeps_the_song(tmp_path: Path):
    _jobs.clear()
    _registry._pending_resume.clear()
    good = Job(id="abcdef00aa01", status="done", title="Two versions")
    good.versions = [
        {"id": "main", "name": None, "stems": ["vocals"], "created_at": 1.0},
        {"id": "c3", "name": "Guitar", "stems": ["drums", "guitar"], "created_at": 2.0},
    ]
    good.active_version = "c3"
    _jobs[good.id] = good
    persist_registry(tmp_path)
    data = json.loads((tmp_path / "registry.json").read_text())
    data["jobs"].append(
        {"id": "abcdef00aa02", "status": "done", "title": "Damaged", "versions": {"x": 1}}
    )
    (tmp_path / "registry.json").write_text(json.dumps(data))
    _jobs.clear()
    try:
        restore_registry(tmp_path)
        assert _jobs["abcdef00aa01"].versions == good.versions
        assert _jobs["abcdef00aa01"].active_version == "c3"
        damaged = _jobs["abcdef00aa02"]
        assert damaged.versions is None
        assert damaged.to_state()["versions"][0]["id"] == DEFAULT_VERSION_ID
    finally:
        _jobs.clear()
        _registry._pending_resume.clear()


# -- The endpoints


async def test_create_rename_select_and_delete(jobs_dir, client):
    _make_job(jobs_dir)
    before = _stem_bytes(jobs_dir)
    async with client as c:
        r = await c.get(f"/api/jobs/{JOB}/versions")
        assert r.status_code == 200
        assert [v["id"] for v in r.json()["versions"]] == [DEFAULT_VERSION_ID]

        r = await c.post(
            f"/api/jobs/{JOB}/versions",
            json={"name": "Guitar study", "stems": ["guitar", "drums", "kazoo"], "select": True},
        )
        assert r.status_code == 201
        body = r.json()
        new = body["version"]
        assert new["name"] == "Guitar study"
        assert new["stems"] == ["drums", "guitar"]
        assert body["active_version"] == new["id"]
        assert [v["id"] for v in body["versions"]] == [DEFAULT_VERSION_ID, new["id"]]

        r = await c.patch(
            f"/api/jobs/{JOB}/versions/{DEFAULT_VERSION_ID}", json={"name": "Karaoke"}
        )
        assert r.status_code == 200
        assert r.json()["versions"][0]["name"] == "Karaoke"

        r = await c.post(f"/api/jobs/{JOB}/versions/{DEFAULT_VERSION_ID}/select")
        assert r.status_code == 200 and r.json()["active_version"] == DEFAULT_VERSION_ID

        # The job state carries them too, for the library.
        state = (await c.get(f"/api/jobs/{JOB}")).json()
        assert [v["name"] for v in state["versions"]] == ["Karaoke", "Guitar study"]
        assert state["active_version"] == DEFAULT_VERSION_ID

        r = await c.delete(f"/api/jobs/{JOB}/versions/{DEFAULT_VERSION_ID}")
        assert r.status_code == 200
        assert [v["id"] for v in r.json()["versions"]] == [new["id"]]
        # The one that opened with the song is gone: the first left opens now.
        assert r.json()["active_version"] == new["id"]
    assert _stem_bytes(jobs_dir) == before


async def test_deleting_versions_never_deletes_a_stem(jobs_dir, client):
    _make_job(jobs_dir)
    before = _stem_bytes(jobs_dir)
    async with client as c:
        made = []
        for stems in (["vocals", "drums"], ["drums", "guitar"]):
            r = await c.post(f"/api/jobs/{JOB}/versions", json={"stems": stems})
            made.append(r.json()["version"]["id"])
        for vid in made:
            assert (await c.delete(f"/api/jobs/{JOB}/versions/{vid}")).status_code == 200
    assert _stem_bytes(jobs_dir) == before


async def test_the_last_version_cannot_be_deleted(jobs_dir, client):
    _make_job(jobs_dir)
    async with client as c:
        r = await c.delete(f"/api/jobs/{JOB}/versions/{DEFAULT_VERSION_ID}")
    assert r.status_code == 409
    assert _jobs[JOB].to_state()["versions"][0]["id"] == DEFAULT_VERSION_ID


async def test_bad_bodies_are_422_not_500(jobs_dir, client):
    _make_job(jobs_dir)
    async with client as c:
        url = f"/api/jobs/{JOB}/versions"
        for body in (
            {},
            {"stems": []},
            {"stems": "vocals"},
            {"stems": [1, 2]},
            {"stems": ["kazoo"]},
            {"stems": ["vocals"] * 7},
            {"stems": ["vocals"], "name": "x" * 1000},
            {"stems": ["vocals"], "select": "yes"},
        ):
            assert (await c.post(url, json=body)).status_code == 422, body
        r = await c.post(url, content=b"not json", headers={"content-type": "application/json"})
        assert r.status_code == 422
        rename = f"{url}/{DEFAULT_VERSION_ID}"
        for body in ({}, {"name": ""}, {"name": "   \n"}, {"name": 5}):
            assert (await c.patch(rename, json=body)).status_code == 422, body
        fill = f"/api/jobs/{JOB}/fill-stems"
        for body in ({}, {"stems": []}, {"stems": ["kazoo"]}, {"stems": "drums"}):
            assert (await c.post(fill, json=body)).status_code == 422, body


async def test_unknown_jobs_versions_and_traversal_are_404(jobs_dir, client):
    _make_job(jobs_dir)
    async with client as c:
        for path in (
            "/api/jobs/ffffffffffff/versions",
            "/api/jobs/not-a-job/versions",
            "/api/jobs/..%2F..%2Fetc/versions",
            f"/api/jobs/{JOB}/versions/nope0000/select",
            f"/api/jobs/{JOB}/versions/..%2F..%2Fetc/select",
            f"/api/jobs/{JOB}/versions/BAD!/select",
        ):
            r = await c.post(path, json={"stems": ["vocals"]})
            assert r.status_code in (404, 405), path
            r = await c.get(path)
            assert r.status_code in (404, 405), path
        assert (await c.get("/api/jobs/ffffffffffff/versions")).status_code == 404
        r = await c.patch(f"/api/jobs/{JOB}/versions/zz99", json={"name": "x"})
        assert r.status_code == 404
        assert (await c.delete(f"/api/jobs/{JOB}/versions/zz99")).status_code == 404
        r = await c.delete(f"/api/jobs/{JOB}/versions/..%2F..%2Fregistry.json")
        assert r.status_code in (404, 405)
        r = await c.post("/api/jobs/..%2F..%2Fetc/fill-stems", json={"stems": ["drums"]})
        assert r.status_code in (404, 405)
        r = await c.post("/api/jobs/ffffffffffff/fill-stems", json={"stems": ["drums"]})
        assert r.status_code == 404
    assert (jobs_dir / JOB / "stems" / "vocals.wav").is_file()


async def test_a_song_still_processing_cannot_change_versions(jobs_dir, client):
    _make_job(jobs_dir, status="separating")
    async with client as c:
        assert (await c.get(f"/api/jobs/{JOB}/versions")).status_code == 200
        r = await c.post(f"/api/jobs/{JOB}/versions", json={"stems": ["vocals"]})
        assert r.status_code == 409
        r = await c.post(f"/api/jobs/{JOB}/fill-stems", json={"stems": ["vocals"]})
        assert r.status_code == 409


async def test_versions_are_capped(jobs_dir, client):
    _make_job(jobs_dir)
    async with client as c:
        for _ in range(MAX_VERSIONS - 1):
            r = await c.post(f"/api/jobs/{JOB}/versions", json={"stems": ["vocals"]})
            assert r.status_code == 201
        r = await c.post(f"/api/jobs/{JOB}/versions", json={"stems": ["vocals"]})
    assert r.status_code == 409
    assert len(_jobs[JOB].versions) == MAX_VERSIONS


# -- Extract again: fill only the missing stems


def _fake_separate(calls: list):
    """A stand-in for Demucs: writes all six stems, with new bytes, where the
    real worker would, and records the settings it was asked for."""

    def run(job, source, job_dir, quality=None):
        calls.append({"job": job, "source": source, "quality": quality})
        root = job_dir / DEMUCS_MODEL / source.stem
        root.mkdir(parents=True, exist_ok=True)
        for name in STEM_NAMES:
            (root / f"{name}.wav").write_bytes(f"RIFF-{name}-new".encode())
        return root

    return run


@pytest.fixture
def no_peaks(monkeypatch):
    monkeypatch.setattr(fill_mod, "merge_stem_peaks", lambda stems_dir, names: {})


async def test_nothing_missing_separates_nothing(jobs_dir, client, monkeypatch):
    _make_job(jobs_dir)

    def boom(*a, **k):
        raise AssertionError("a song with every stem must not be separated again")

    monkeypatch.setattr(fill_mod, "separate", boom)
    before = _stem_bytes(jobs_dir)
    async with client as c:
        r = await c.post(f"/api/jobs/{JOB}/fill-stems", json={"stems": ["drums", "guitar"]})
    assert r.status_code == 200
    assert r.json()["filled"] == []
    assert _stem_bytes(jobs_dir) == before


async def test_only_missing_stems_are_added_with_the_songs_settings(
    jobs_dir, client, monkeypatch, no_peaks
):
    job = _make_job(
        jobs_dir,
        names=("vocals", "drums", "bass", "other"),
        source_url="local:Song",
        separation_model=DEMUCS_MODEL,
        separation_quality="best",
    )
    (jobs_dir / JOB / "source.wav").write_bytes(b"RIFF-source")
    calls: list = []
    monkeypatch.setattr(fill_mod, "separate", _fake_separate(calls))
    before = _stem_bytes(jobs_dir)
    async with client as c:
        r = await c.post(
            f"/api/jobs/{JOB}/fill-stems", json={"stems": ["drums", "guitar", "vocals"]}
        )
    assert r.status_code == 200, r.text
    assert r.json()["filled"] == ["guitar"]
    after = _stem_bytes(jobs_dir)
    # Every stem the song had is exactly as it was; only the asked-for
    # missing one is new, and piano (missing, not asked for) still is.
    for name, data in before.items():
        assert after[name] == data
    assert after["guitar.wav"] == b"RIFF-guitar-new"
    assert "piano.wav" not in after
    assert calls[0]["quality"] == "best"
    assert calls[0]["job"] is not job  # the song itself is never "separating"
    assert job.status == "done" and job.stem_fill == "done"
    assert [s["name"] for s in job.stems][-1] == "guitar"
    # The scratch folder is gone, and the upload is still there.
    assert not list((jobs_dir / JOB).glob(".fill-*"))
    assert (jobs_dir / JOB / "source.wav").read_bytes() == b"RIFF-source"


async def test_a_song_without_recorded_settings_fills_at_standard(
    jobs_dir, client, monkeypatch, no_peaks
):
    _make_job(jobs_dir, names=("vocals",), source_url="local:Song")
    (jobs_dir / JOB / "source.wav").write_bytes(b"RIFF-source")
    calls: list = []
    monkeypatch.setattr(fill_mod, "separate", _fake_separate(calls))
    async with client as c:
        r = await c.post(f"/api/jobs/{JOB}/fill-stems", json={"stems": ["bass"]})
    assert r.status_code == 200 and r.json()["filled"] == ["bass"]
    assert calls[0]["quality"] == "standard"


async def test_a_link_is_downloaded_again_into_scratch(jobs_dir, client, monkeypatch, no_peaks):
    _make_job(jobs_dir, names=("vocals",), source_url="https://www.youtube.com/watch?v=abc")
    seen = {}

    def fake_download(job, url, job_dir):
        seen["url"], seen["dir"] = url, job_dir
        src = job_dir / "source.wav"
        src.write_bytes(b"RIFF")
        return src

    monkeypatch.setattr(fill_mod, "download", fake_download)
    monkeypatch.setattr(fill_mod, "separate", _fake_separate([]))
    async with client as c:
        r = await c.post(f"/api/jobs/{JOB}/fill-stems", json={"stems": ["drums"]})
    assert r.status_code == 200 and r.json()["filled"] == ["drums"]
    assert seen["url"] == "https://www.youtube.com/watch?v=abc"
    assert seen["dir"].name.startswith(".fill-") and not seen["dir"].exists()


async def test_an_upload_without_its_source_is_refused(jobs_dir, client, monkeypatch):
    _make_job(jobs_dir, names=("vocals",), source_url="local:Song")
    monkeypatch.setattr(fill_mod, "separate", _fake_separate([]))
    async with client as c:
        r = await c.post(f"/api/jobs/{JOB}/fill-stems", json={"stems": ["drums"]})
    assert r.status_code == 409
    assert "no source" in r.json()["detail"]


async def test_another_model_is_refused(jobs_dir, client, monkeypatch):
    _make_job(jobs_dir, names=("vocals",), source_url="local:Song", separation_model="htdemucs")
    (jobs_dir / JOB / "source.wav").write_bytes(b"RIFF-source")
    monkeypatch.setattr(fill_mod, "separate", _fake_separate([]))
    async with client as c:
        r = await c.post(f"/api/jobs/{JOB}/fill-stems", json={"stems": ["drums"]})
    assert r.status_code == 409
    assert not (jobs_dir / JOB / "stems" / "drums.wav").exists()


async def test_a_failed_fill_keeps_every_stem_and_says_so(jobs_dir, client, monkeypatch):
    _make_job(jobs_dir, names=("vocals",), source_url="local:Song")
    (jobs_dir / JOB / "source.wav").write_bytes(b"RIFF-source")

    def fail(job, source, job_dir, quality=None):
        (job_dir / "partial.wav").write_bytes(b"x")
        raise RuntimeError("demucs failed: /secret/path")

    monkeypatch.setattr(fill_mod, "separate", fail)
    before = _stem_bytes(jobs_dir)
    async with client as c:
        r = await c.post(f"/api/jobs/{JOB}/fill-stems", json={"stems": ["drums"]})
    assert r.status_code == 500
    assert "secret" not in r.text
    assert _stem_bytes(jobs_dir) == before
    assert _jobs[JOB].stem_fill == "error" and _jobs[JOB].status == "done"
    assert not list((jobs_dir / JOB).glob(".fill-*"))


def test_a_stem_that_appears_meanwhile_is_never_replaced(tmp_path):
    src = tmp_path / "new.wav"
    src.write_bytes(b"new")
    dest = tmp_path / "drums.wav"
    dest.write_bytes(b"kept")
    assert fill_mod._place(src, dest) is False
    assert dest.read_bytes() == b"kept"
    assert not list(tmp_path.glob(".*fill*"))


def test_separate_records_the_model_and_quality(monkeypatch, tmp_path):
    from app.pipeline import separate as sep_mod

    seen = {}

    def fake_run(job, source, job_dir, device):
        seen["quality"] = job.separation_quality
        (job_dir / DEMUCS_MODEL / source.stem).mkdir(parents=True)
        return 0, []

    monkeypatch.setattr(sep_mod, "_run_demucs", fake_run)
    monkeypatch.setattr(sep_mod, "get_demucs_device", lambda: "cpu")
    monkeypatch.setattr(sep_mod, "get_separation_quality", lambda: "best")
    job = Job(id="abcdefabc804")
    sep_mod.separate(job, tmp_path / "source.wav", tmp_path)
    assert (job.separation_model, job.separation_quality) == (DEMUCS_MODEL, "best")
    assert seen["quality"] == "best"
    job2 = Job(id="abcdefabc805")
    sep_mod.separate(job2, tmp_path / "source.wav", tmp_path / "x", quality="standard")
    assert job2.separation_quality == "standard"
