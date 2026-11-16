"""Tags (#1075): named points on a song's timeline that a click seeks to.

A section always loops and cannot hold two start points, so Tags are their own
layer, saved per song the way sections are: the whole list, in metadata.json
and the registry, through PATCH /api/jobs/{id}/timeline-tags.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core import registry as _registry
from app.core.models import Job
from app.core.registry import registry_path
from app.main import app
from app.pipeline.runner import _write_metadata

SONG = "c1c2c3d4e5f6"
FIXTURE_REGISTRY = Path(__file__).parent / "fixtures" / "upgrade-0.20.0" / "library"


def _tag(index: int, time: float | None = None, name: str = "Tag") -> dict:
    return {"id": f"t{index}", "name": name, "time": float(index if time is None else time)}


@pytest.fixture
def jobs_dir() -> Path:
    """The jobs folder the API writes to (the conftest points it at a temp dir)."""
    from app.core import config

    return config.JOBS_DIR


@pytest.fixture(autouse=True)
def _empty_registry():
    _registry._jobs.clear()
    _registry._deleted.clear()
    yield
    _registry._jobs.clear()
    _registry._deleted.clear()


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _finished(jobs_dir: Path, **fields) -> Job:
    """A finished song as the pipeline leaves it: stems, metadata.json, and a
    registry entry."""
    stems = jobs_dir / SONG / "stems"
    stems.mkdir(parents=True)
    for name in ("vocals", "drums"):
        (stems / f"{name}.wav").write_bytes(b"RIFF")
    job = Job(id=SONG, status="done", progress=1.0, title="Song", **fields)
    _write_metadata(job, jobs_dir / SONG)
    _registry.register(job)
    _registry.persist(jobs_dir)
    return job


def _restart(jobs_dir: Path) -> Job:
    _registry._jobs.clear()
    _registry.restore(jobs_dir)
    job = _registry.get(SONG)
    assert job is not None and job.status == "done"
    return job


def _meta(jobs_dir: Path) -> dict:
    return json.loads((jobs_dir / SONG / "metadata.json").read_text(encoding="utf-8"))


# ── the endpoint ─────────────────────────────────────────────────────────────


def test_tags_are_saved_in_time_order_to_the_job_and_its_folder(client, jobs_dir):
    _finished(jobs_dir)
    payload = {"timeline_tags": [_tag(1, 4.25, "Solo"), _tag(2, 1.5, "Verse two")]}

    r = client.patch(f"/api/jobs/{SONG}/timeline-tags", json=payload)

    assert r.status_code == 200
    body = r.json()
    assert body["job_id"] == SONG
    assert [t["name"] for t in body["timeline_tags"]] == ["Verse two", "Solo"]
    assert _registry.get(SONG).timeline_tags == body["timeline_tags"]
    assert _meta(jobs_dir)["timeline_tags"] == body["timeline_tags"]
    # What the page reads when it opens the song.
    state = client.get(f"/api/jobs/{SONG}").json()
    assert state["timeline_tags"] == body["timeline_tags"]


def test_saving_tags_leaves_the_sections_as_they_were(client, jobs_dir):
    """Tags have their own endpoint so that saving one never marks automatic
    sections as edited by hand, nor needs them sent again."""
    sections = [{"id": "auto-001", "kind": "verse", "start": 0.0, "end": 4.0, "color": "#fff"}]
    _finished(jobs_dir, sections=sections, sections_source="automatic")

    r = client.patch(f"/api/jobs/{SONG}/timeline-tags", json={"timeline_tags": [_tag(1)]})

    assert r.status_code == 200
    job = _registry.get(SONG)
    assert job.sections == sections
    assert job.sections_source == "automatic"
    assert _meta(jobs_dir)["sections_source"] == "automatic"


def test_an_empty_list_removes_every_tag(client, jobs_dir):
    _finished(jobs_dir, timeline_tags=[_tag(1)])

    r = client.patch(f"/api/jobs/{SONG}/timeline-tags", json={"timeline_tags": []})

    assert r.status_code == 200
    assert _registry.get(SONG).timeline_tags == []
    assert _meta(jobs_dir)["timeline_tags"] == []


def test_a_blank_name_becomes_tag_and_a_long_one_is_cut(client, jobs_dir):
    _finished(jobs_dir)
    payload = {"timeline_tags": [_tag(1, name="   "), _tag(2, name="  " + "x" * 100)]}

    saved = client.patch(f"/api/jobs/{SONG}/timeline-tags", json=payload).json()["timeline_tags"]

    assert saved[0]["name"] == "Tag"
    assert saved[1]["name"] == "x" * 64


@pytest.mark.parametrize(
    "tags",
    [
        [{"id": "has space", "name": "x", "time": 1.0}],
        [{"id": "t1", "name": "x", "time": -0.5}],
        [{"id": "t1", "name": "x", "time": 86400.0}],
        [{"id": "t1", "name": "x"}],
        [_tag(1, 1.0), _tag(1, 2.0)],
    ],
    ids=["bad id", "before the start", "past a day", "no time", "duplicate id"],
)
def test_an_invalid_tag_is_refused_and_nothing_changes(client, jobs_dir, tags):
    _finished(jobs_dir, timeline_tags=[_tag(9)])

    r = client.patch(f"/api/jobs/{SONG}/timeline-tags", json={"timeline_tags": tags})

    assert r.status_code == 422
    assert _registry.get(SONG).timeline_tags == [_tag(9)]


def test_a_list_past_the_cap_is_refused(client, jobs_dir):
    import app.api.jobs as jobs_mod

    _finished(jobs_dir)
    payload = {"timeline_tags": [_tag(i) for i in range(jobs_mod._MAX_TIMELINE_TAGS + 1)]}

    r = client.patch(f"/api/jobs/{SONG}/timeline-tags", json=payload)

    assert r.status_code == 422
    assert _registry.get(SONG).timeline_tags is None


def test_an_unknown_job_is_404(client):
    r = client.patch("/api/jobs/000000000000/timeline-tags", json={"timeline_tags": []})
    assert r.status_code == 404


@pytest.mark.parametrize("job_id", ["BADID", "%2e%2e", "abcdefabcdef0"])
def test_a_malformed_job_id_is_404(client, job_id):
    r = client.patch(f"/api/jobs/{job_id}/timeline-tags", json={"timeline_tags": []})
    assert r.status_code == 404


@pytest.mark.parametrize("job_id", ["../../etc/passwd", "..%2F..%2Fetc", "aaaa/../aaaa"])
def test_a_traversing_id_never_writes(client, jobs_dir, job_id):
    """Some of these are collapsed before routing and nothing answers PATCH
    there (405); the rest arrive as a job id and are refused (404). Neither
    may be a 500, and nothing may be written."""
    r = client.patch(f"/api/jobs/{job_id}/timeline-tags", json={"timeline_tags": [_tag(1)]})
    assert r.status_code in (404, 405)
    assert not list(jobs_dir.parent.rglob("metadata.json"))


def test_a_failed_write_leaves_the_live_job_as_it_was(client, jobs_dir, monkeypatch):
    import app.api.jobs as jobs_mod

    _finished(jobs_dir, timeline_tags=[_tag(1)])

    def fail_write(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(jobs_mod, "_write_json_atomic", fail_write)
    r = client.patch(f"/api/jobs/{SONG}/timeline-tags", json={"timeline_tags": [_tag(2)]})

    assert r.status_code == 500
    assert "disk" not in r.json()["detail"]
    assert _registry.get(SONG).timeline_tags == [_tag(1)]


@pytest.mark.parametrize("content", ["[1, 2]", "5", '"text"', "null"])
def test_a_metadata_file_that_is_not_an_object_is_refused_and_left_alone(client, jobs_dir, content):
    """Valid JSON that is not an object has no key to set. It is answered with
    the same 500 as an unreadable file, not a bare TypeError."""
    _finished(jobs_dir, timeline_tags=[_tag(1)])
    meta_path = jobs_dir / SONG / "metadata.json"
    meta_path.write_text(content, encoding="utf-8")

    r = client.patch(f"/api/jobs/{SONG}/timeline-tags", json={"timeline_tags": [_tag(2)]})

    assert r.status_code == 500
    assert r.json()["detail"] == "failed to save tags"
    assert meta_path.read_text(encoding="utf-8") == content
    assert _registry.get(SONG).timeline_tags == [_tag(1)]


# ── surviving restarts, rebuilds and a downgrade ─────────────────────────────


def test_tags_survive_a_restart(client, jobs_dir):
    _finished(jobs_dir)
    client.patch(f"/api/jobs/{SONG}/timeline-tags", json={"timeline_tags": [_tag(1, 2.5)]})

    assert _restart(jobs_dir).timeline_tags == [_tag(1, 2.5)]


def test_tags_survive_a_registry_rebuilt_from_the_folder(client, jobs_dir):
    _finished(jobs_dir)
    client.patch(f"/api/jobs/{SONG}/timeline-tags", json={"timeline_tags": [_tag(1, 2.5)]})
    registry_path(jobs_dir).unlink()

    assert _restart(jobs_dir).timeline_tags == [_tag(1, 2.5)]


@pytest.mark.parametrize("value", ["tags", 5, {"id": "t1"}, [1, 2], ["t1"]])
def test_a_hand_edited_tag_list_of_the_wrong_shape_is_dropped(jobs_dir, value):
    _finished(jobs_dir)
    meta = _meta(jobs_dir)
    meta["timeline_tags"] = value
    (jobs_dir / SONG / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    registry_path(jobs_dir).unlink()

    assert _restart(jobs_dir).timeline_tags is None


def test_a_downgrade_to_0_20_0_and_back_keeps_the_tags(client, jobs_dir):
    """0.20.0 does not know Tags, so its first save drops them from the
    registry and metadata.json is their only copy. Without reading them back,
    the next tag saved would replace them all on disk."""
    _finished(jobs_dir)
    client.patch(f"/api/jobs/{SONG}/timeline-tags", json={"timeline_tags": [_tag(1, 2.5)]})
    fields_0_20_0 = set(
        json.loads((FIXTURE_REGISTRY / "registry.json").read_text(encoding="utf-8"))["jobs"][0]
    )
    assert "timeline_tags" not in fields_0_20_0
    path = registry_path(jobs_dir)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["version"] = 1
    data["jobs"] = [{k: v for k, v in r.items() if k in fields_0_20_0} for r in data["jobs"]]
    path.write_text(json.dumps(data), encoding="utf-8")

    assert _restart(jobs_dir).timeline_tags == [_tag(1, 2.5)]


@pytest.mark.parametrize("value", [[1, 2], ["t1"], [{"id": "t1"}, 3]])
def test_after_a_downgrade_a_malformed_list_in_the_folder_is_dropped(client, jobs_dir, value):
    """The downgrade path reads metadata.json back into the record. It goes
    through the same check as a rebuild from the folder, so a list that is not
    of objects is dropped there too, instead of loaded and sent to the page."""
    _finished(jobs_dir)
    fields_0_20_0 = set(
        json.loads((FIXTURE_REGISTRY / "registry.json").read_text(encoding="utf-8"))["jobs"][0]
    )
    path = registry_path(jobs_dir)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["version"] = 1
    data["jobs"] = [{k: v for k, v in r.items() if k in fields_0_20_0} for r in data["jobs"]]
    path.write_text(json.dumps(data), encoding="utf-8")
    meta = _meta(jobs_dir)
    meta["timeline_tags"] = value
    (jobs_dir / SONG / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")

    job = _restart(jobs_dir)

    assert job.timeline_tags is None
    # The song still opens.
    assert client.get(f"/api/jobs/{SONG}").json()["timeline_tags"] is None


def test_a_registry_record_with_a_malformed_list_is_reset(jobs_dir):
    """registry.json goes through the same check as the folder."""
    _finished(jobs_dir)
    path = registry_path(jobs_dir)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["jobs"][0]["timeline_tags"] = [1, 2]
    path.write_text(json.dumps(data), encoding="utf-8")

    assert _restart(jobs_dir).timeline_tags is None


def test_the_finishing_metadata_write_carries_the_tags(jobs_dir):
    """The runner writes metadata.json whole when a run finishes, so a field it
    leaves out is gone from the folder."""
    job = Job(id=SONG, status="done", title="Song", timeline_tags=[_tag(1, 2.5)])
    (jobs_dir / SONG).mkdir(parents=True)

    _write_metadata(job, jobs_dir / SONG)

    assert _meta(jobs_dir)["timeline_tags"] == [_tag(1, 2.5)]
