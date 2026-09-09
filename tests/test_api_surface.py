"""API-layer branches the existing endpoint tests do not reach.

The happy paths and the common rejections are covered in test_jobs_api.py,
test_beats_api.py and friends. What was left were the endpoints nobody had
touched at all (/api/config, /api/qr) and the failure branches of the upload
path -- the ones that decide whether a rejected import leaves a half-written
job directory behind.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from app.core.models import Job
from app.core.registry import _jobs


@pytest.fixture(autouse=True)
def _isolate_registry():
    _jobs.clear()
    yield
    _jobs.clear()


@pytest.fixture
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def upload_client(tmp_path, monkeypatch):
    """Uploads with the *real* ffprobe: the duration check is the thing under
    test in several of these, and mocking it out is what left the rejection
    branches uncovered in the first place."""
    import app.api.jobs as jobs_mod
    import app.core.config as cfg

    monkeypatch.setattr(cfg, "JOBS_DIR", tmp_path)
    monkeypatch.setattr(jobs_mod, "JOBS_DIR", tmp_path)
    with patch("app.api.jobs.jobqueue.enqueue", lambda job_id: None):
        from app.main import app

        with TestClient(app) as c:
            yield c


def _wav_bytes(seconds: float = 1.0, rate: int = 8000) -> bytes:
    buf = io.BytesIO()
    samples = np.zeros(int(rate * seconds), dtype=np.float32)
    sf.write(buf, samples, rate, format="WAV", subtype="PCM_16")
    return buf.getvalue()


# --------------------------------------------------------------------------
# /api/config -- the lane vocabulary every client builds itself from
# --------------------------------------------------------------------------


def test_the_config_endpoint_publishes_both_stem_vocabularies(client):
    """extra_stem_names is kept separate on purpose: clients that assume "every
    job produces exactly these stems" predate the on-demand vocal split and
    must not see its outputs in the base list."""
    from app.core.config import EXTRA_STEM_NAMES, STEM_NAMES

    body = client.get("/api/config").json()

    assert body["stem_names"] == list(STEM_NAMES)
    assert body["extra_stem_names"] == list(EXTRA_STEM_NAMES)
    assert not set(body["stem_names"]) & set(body["extra_stem_names"])


# --------------------------------------------------------------------------
# /api/qr -- the desktop settings panel's "open on your phone" code
# --------------------------------------------------------------------------


def test_a_qr_code_is_returned_as_an_svg(client):
    r = client.get("/api/qr", params={"url": "https://192.168.1.50:8443/"})

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg+xml")
    assert r.text.lstrip().startswith("<?xml") or "<svg" in r.text


def test_the_qr_code_is_cacheable(client):
    """The panel re-renders on every open; the address rarely changes."""
    r = client.get("/api/qr", params={"url": "https://192.168.1.50:8443/"})

    assert "max-age" in r.headers.get("cache-control", "")


def test_a_qr_code_for_a_different_address_is_a_different_image(client):
    a = client.get("/api/qr", params={"url": "https://192.168.1.50:8443/"}).text
    b = client.get("/api/qr", params={"url": "https://192.168.1.99:8443/"}).text

    assert a != b


def test_an_empty_url_has_no_qr_code(client):
    r = client.get("/api/qr", params={"url": ""})

    assert r.status_code == 422


def test_a_url_too_long_to_encode_is_refused(client):
    """segno would otherwise spend real time failing to fit it into a symbol."""
    r = client.get("/api/qr", params={"url": "https://example.com/" + "x" * 500})

    assert r.status_code == 422


def test_a_qr_request_with_no_url_at_all_is_refused(client):
    assert client.get("/api/qr").status_code == 422


# --------------------------------------------------------------------------
# _probe_duration
# --------------------------------------------------------------------------


def test_the_duration_probe_reads_a_real_file(tmp_path):
    import app.api.jobs as jobs_mod

    path = tmp_path / "clip.wav"
    path.write_bytes(_wav_bytes(seconds=2.0))

    assert jobs_mod._probe_duration(path) == pytest.approx(2.0, abs=0.1)


def test_the_duration_probe_reports_a_file_ffprobe_cannot_read(tmp_path):
    """The caller turns this into a 422 naming the file; a bare crash would be
    a 500 on a perfectly ordinary "that isn't audio"."""
    import app.api.jobs as jobs_mod

    path = tmp_path / "not-audio.wav"
    path.write_bytes(b"this is not a wav file at all")

    with pytest.raises(RuntimeError, match="ffprobe"):
        jobs_mod._probe_duration(path)


def test_a_container_with_no_duration_is_reported_rather_than_returned(tmp_path, monkeypatch):
    """Some streams report "N/A". float() would raise ValueError, which is not
    what the upload path catches."""
    import subprocess

    import app.api.jobs as jobs_mod

    class _Result:
        returncode = 0
        stdout = "N/A\n"
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _Result())

    with pytest.raises(RuntimeError, match="non-numeric"):
        jobs_mod._probe_duration(tmp_path / "clip.wav")


# --------------------------------------------------------------------------
# upload rejections -- and what they leave behind
# --------------------------------------------------------------------------


def test_a_file_longer_than_the_limit_is_refused(upload_client, monkeypatch):
    import app.api.jobs as jobs_mod

    monkeypatch.setattr(jobs_mod, "get_max_duration_sec", lambda: 60)
    with patch("app.api.jobs._probe_duration", return_value=3600.0):
        r = upload_client.post("/api/jobs", files={"file": ("long.wav", _wav_bytes(), "audio/wav")})

    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "60 min" in detail or "min" in detail


def test_a_rejected_upload_leaves_no_job_directory_behind(upload_client, tmp_path, monkeypatch):
    """The file is copied to disk before the duration is known. A rejection
    that skipped the cleanup would accumulate 400 MB directories that no job
    ever references."""
    import app.api.jobs as jobs_mod

    monkeypatch.setattr(jobs_mod, "get_max_duration_sec", lambda: 60)
    before = set(tmp_path.iterdir())
    with patch("app.api.jobs._probe_duration", return_value=3600.0):
        upload_client.post("/api/jobs", files={"file": ("long.wav", _wav_bytes(), "audio/wav")})

    assert set(tmp_path.iterdir()) == before
    assert _jobs == {}


def test_a_file_ffprobe_cannot_read_is_refused_and_cleaned_up(upload_client, tmp_path):
    before = set(tmp_path.iterdir())

    r = upload_client.post(
        "/api/jobs", files={"file": ("broken.wav", b"not really a wav", "audio/wav")}
    )

    assert r.status_code == 422
    assert "duration" in r.json()["detail"].lower()
    assert set(tmp_path.iterdir()) == before


def test_an_obviously_oversized_upload_is_refused_from_its_header(upload_client):
    """Rejected on Content-Length, before the body is buffered to disk."""
    r = upload_client.post(
        "/api/jobs",
        content=b"x" * 32,
        headers={
            "content-type": "multipart/form-data; boundary=zz",
            "content-length": str(500 * 1024 * 1024),
        },
    )

    assert r.status_code == 422
    assert "400 MB" in r.json()["detail"]


def test_a_nonsense_content_length_does_not_decide_anything(upload_client):
    """A non-integer header is ignored rather than raising: the real size check
    happens after buffering anyway."""
    r = upload_client.post(
        "/api/jobs",
        files={"file": ("clip.wav", _wav_bytes(), "audio/wav")},
        headers={"content-length": "not-a-number"},
    )

    # httpx recomputes Content-Length, so this asserts the request is served
    # normally rather than 500ing on the header path.
    assert r.status_code in (200, 422)


def test_a_request_with_no_file_part_is_refused(upload_client):
    r = upload_client.post("/api/jobs", data={"stems": "[]"}, files={})

    assert r.status_code == 422


def test_a_selected_stem_list_is_honoured(upload_client):
    r = upload_client.post(
        "/api/jobs",
        files={"file": ("clip.wav", _wav_bytes(), "audio/wav")},
        data={"stems": json.dumps(["vocals", "drums"])},
    )

    assert r.status_code == 200
    job = _jobs[r.json()["job_id"]]
    assert job.selected_stems == ["vocals", "drums"]


def test_an_unparseable_stem_list_falls_back_to_every_stem(upload_client):
    """A malformed form field must not silently produce a one-stem separation."""
    from app.core.config import STEM_NAMES

    r = upload_client.post(
        "/api/jobs",
        files={"file": ("clip.wav", _wav_bytes(), "audio/wav")},
        data={"stems": "{not json"},
    )

    assert r.status_code == 200
    assert _jobs[r.json()["job_id"]].selected_stems == list(STEM_NAMES)


def test_a_stem_list_that_is_not_a_list_falls_back_to_every_stem(upload_client):
    from app.core.config import STEM_NAMES

    r = upload_client.post(
        "/api/jobs",
        files={"file": ("clip.wav", _wav_bytes(), "audio/wav")},
        data={"stems": '{"vocals": true}'},
    )

    assert r.status_code == 200
    assert _jobs[r.json()["job_id"]].selected_stems == list(STEM_NAMES)


def test_stem_names_that_do_not_exist_are_dropped(upload_client):
    from app.core.config import STEM_NAMES

    r = upload_client.post(
        "/api/jobs",
        files={"file": ("clip.wav", _wav_bytes(), "audio/wav")},
        data={"stems": json.dumps(["vocals", "kazoo"])},
    )

    assert r.status_code == 200
    job = _jobs[r.json()["job_id"]]
    assert job.selected_stems == ["vocals"]
    assert "kazoo" not in job.selected_stems
    assert set(job.selected_stems) <= set(STEM_NAMES)


def test_an_upload_is_titled_from_its_filename(upload_client):
    r = upload_client.post(
        "/api/jobs",
        files={"file": ("My   Favourite  Song.wav", _wav_bytes(), "audio/wav")},
    )

    job = _jobs[r.json()["job_id"]]
    assert job.title == "My Favourite Song"
    assert job.source_url == "local:My Favourite Song"


def test_a_very_long_filename_is_capped():
    import app.api.jobs as jobs_mod

    assert len(jobs_mod._sanitize_title("x" * 500 + ".wav")) == 120


# --------------------------------------------------------------------------
# _rmtree_job / _job_files_missing
# --------------------------------------------------------------------------


def test_removing_a_job_directory_that_is_not_there_is_a_success(monkeypatch, tmp_path):
    """Nothing on disk is the outcome the caller asked for."""
    import app.api.jobs as jobs_mod

    monkeypatch.setattr(jobs_mod, "JOBS_DIR", tmp_path)

    assert jobs_mod._rmtree_job("abcdefabcdef") is True


def test_a_directory_that_cannot_be_removed_is_reported(monkeypatch, tmp_path, caplog):
    """delete_job used to swallow this, drop the registry entry anyway, and let
    restore() re-adopt the surviving directory on the next start -- which is how
    deleted songs came back (#521)."""
    import shutil

    import app.api.jobs as jobs_mod

    monkeypatch.setattr(jobs_mod, "JOBS_DIR", tmp_path)
    (tmp_path / "abcdefabcdef").mkdir()
    monkeypatch.setattr(shutil, "rmtree", lambda *a, **kw: (_ for _ in ()).throw(OSError("busy")))

    assert jobs_mod._rmtree_job("abcdefabcdef") is False
    # Retried once before giving up: on macOS a .DS_Store appearing mid-rmtree
    # is a transient "Directory not empty".
    assert caplog.text.count("failed to remove job dir") == 2


def test_a_removal_that_succeeds_on_the_retry_is_a_success(monkeypatch, tmp_path):
    import shutil

    import app.api.jobs as jobs_mod

    monkeypatch.setattr(jobs_mod, "JOBS_DIR", tmp_path)
    target = tmp_path / "abcdefabcdef"
    target.mkdir()
    calls = {"n": 0}
    real = shutil.rmtree

    def _flaky(path, *a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("Directory not empty")
        return real(path, *a, **kw)

    monkeypatch.setattr(shutil, "rmtree", _flaky)

    assert jobs_mod._rmtree_job("abcdefabcdef") is True
    assert calls["n"] == 2


def test_a_stems_dir_pointing_outside_the_library_counts_as_missing(monkeypatch, tmp_path):
    """A job id that escapes JOBS_DIR must never be reported as present -- that
    is the check standing between a symlink and serving arbitrary files."""
    import app.api.jobs as jobs_mod

    library = tmp_path / "jobs"
    library.mkdir()
    outside = tmp_path / "outside"
    (outside / "stems").mkdir(parents=True)
    # Populated, so "missing" can only be the containment check answering --
    # not simply an absent directory.
    (outside / "stems" / "vocals.wav").write_bytes(b"x")
    (library / "escape").symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(jobs_mod, "JOBS_DIR", library)
    job = Job(id="escape")
    job.status = "done"

    assert jobs_mod._job_files_missing(job) is True


def test_an_empty_stems_dir_counts_as_missing(monkeypatch, tmp_path):
    import app.api.jobs as jobs_mod

    monkeypatch.setattr(jobs_mod, "JOBS_DIR", tmp_path)
    (tmp_path / "abcdefabcdef" / "stems").mkdir(parents=True)
    job = Job(id="abcdefabcdef")
    job.status = "done"

    assert jobs_mod._job_files_missing(job) is True


def test_nothing_is_missing_while_the_library_is_being_moved(monkeypatch, tmp_path):
    """A relocation is a known, temporary absence; flagging it would flap every
    row in the library to "unavailable" mid-move."""
    import app.api.jobs as jobs_mod
    from app.core import stems_location

    monkeypatch.setattr(jobs_mod, "JOBS_DIR", tmp_path)
    job = Job(id="abcdefabcdef")
    job.status = "done"

    stems_location.begin_relocation()
    try:
        assert jobs_mod._job_files_missing(job) is False
    finally:
        stems_location.abandon_relocation()


# --------------------------------------------------------------------------
# delete / trash / restore
# --------------------------------------------------------------------------


def test_deleting_a_job_whose_files_survive_is_an_error_not_a_silent_ok(
    client, tmp_path, monkeypatch
):
    import shutil

    import app.api.jobs as jobs_mod

    monkeypatch.setattr(jobs_mod, "JOBS_DIR", tmp_path)
    job = Job(id="abcdefabcdef")
    job.status = "done"
    _jobs[job.id] = job
    (tmp_path / job.id).mkdir()
    monkeypatch.setattr(shutil, "rmtree", lambda *a, **kw: (_ for _ in ()).throw(OSError("busy")))

    r = client.delete(f"/api/jobs/{job.id}")

    assert r.status_code == 500


def test_a_deleted_job_stays_out_of_the_registry_even_when_its_files_do_not(
    client, tmp_path, monkeypatch
):
    """The user asked for it to be gone. Without the record, restore() adopts
    the surviving directory on the next start and the track reappears (#521)."""
    import shutil

    import app.api.jobs as jobs_mod
    from app.core.registry import _deleted

    monkeypatch.setattr(jobs_mod, "JOBS_DIR", tmp_path)
    job = Job(id="abcdefabcdef")
    job.status = "done"
    _jobs[job.id] = job
    (tmp_path / job.id).mkdir()
    monkeypatch.setattr(shutil, "rmtree", lambda *a, **kw: (_ for _ in ()).throw(OSError("busy")))

    client.delete(f"/api/jobs/{job.id}")

    assert job.id not in _jobs
    assert job.id in _deleted


@pytest.mark.parametrize("path", ["trash", "restore"])
@pytest.mark.parametrize("job_id", ["zzzzzzzzzzzz", "abc", "ABCDEFABCDEF", "abcdefabcdef0"])
def test_a_malformed_job_id_is_not_found_rather_than_a_crash(client, path, job_id):
    """JOB_ID_RE is 12 lowercase hex characters. Anything else must not reach
    the registry lookup."""
    r = client.post(f"/api/jobs/{job_id}/{path}")

    assert r.status_code == 404


@pytest.mark.parametrize("path", ["trash", "restore"])
def test_trashing_an_unknown_job_is_a_404(client, path):
    r = client.post(f"/api/jobs/abcdefabcdef/{path}")

    assert r.status_code == 404


# --------------------------------------------------------------------------
# beats -- the parse failures
# --------------------------------------------------------------------------


@pytest.fixture
def done_job(client, tmp_path, monkeypatch):
    import app.api.jobs as jobs_mod

    monkeypatch.setattr(jobs_mod, "JOBS_DIR", tmp_path)
    job = Job(id="abcdefabcdef")
    job.status = "done"
    _jobs[job.id] = job
    stems = tmp_path / job.id / "stems"
    stems.mkdir(parents=True)
    (stems / "beats.json").write_text(
        json.dumps(
            {
                "version": 1,
                "source": "drums",
                "bpm": 120.0,
                "duration": 10.0,
                "confidence": 90,
                "beats": [0.0, 0.5, 1.0],
                "onsets": [0.0, 0.5],
            }
        ),
        encoding="utf-8",
    )
    return job


def test_a_beats_body_that_is_not_json_is_refused(client, done_job):
    r = client.patch(f"/api/jobs/{done_job.id}/beats", content=b"{nope")

    assert r.status_code == 422
    assert "invalid JSON" in r.json()["detail"]


def test_a_beats_body_that_is_not_an_object_is_refused(client, done_job):
    r = client.patch(f"/api/jobs/{done_job.id}/beats", content=b"[1, 2, 3]")

    assert r.status_code == 422
    assert "object" in r.json()["detail"]


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_beat_times_are_refused(client, done_job, literal):
    """json.loads accepts these by default. A NaN beat time would poison every
    downstream comparison in the editor."""
    r = client.patch(
        f"/api/jobs/{done_job.id}/beats",
        content=f'{{"beats": [0.5, {literal}], "bars": []}}'.encode(),
    )

    assert r.status_code == 422


def test_a_beats_rejection_never_echoes_the_submitted_values(client, done_job):
    r = client.patch(
        f"/api/jobs/{done_job.id}/beats",
        content=json.dumps({"beats": ["not-a-number"], "bars": []}).encode(),
    )

    assert r.status_code == 422
    assert "not-a-number" not in r.text


def test_beats_cannot_be_written_for_a_job_that_is_not_done(client, tmp_path, monkeypatch):
    import app.api.jobs as jobs_mod

    monkeypatch.setattr(jobs_mod, "JOBS_DIR", tmp_path)
    job = Job(id="abcdefabcdef")
    job.status = "separating"
    _jobs[job.id] = job

    r = client.patch(
        f"/api/jobs/{job.id}/beats", content=json.dumps({"beats": [0.5], "bars": []}).encode()
    )

    assert r.status_code == 404


def test_a_beat_grid_that_cannot_be_written_is_a_500(client, done_job, monkeypatch):
    """The editor has to know the correction did not land, or the user loses
    work they believe is saved."""

    def _boom(self, *a, **kw):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(Path, "write_text", _boom)

    r = client.patch(
        f"/api/jobs/{done_job.id}/beats",
        content=json.dumps({"beats": [0.5, 1.0], "bars": []}).encode(),
    )

    assert r.status_code == 500
    assert "failed to save" in r.json()["detail"]


def test_an_unreadable_computed_grid_reads_as_absent(client, done_job, tmp_path):
    """A truncated write leaves JSON that will not parse; the editor should see
    "no grid" rather than a 500."""
    (tmp_path / done_job.id / "stems" / "beats.json").write_text("{truncated", encoding="utf-8")

    r = client.get(f"/api/jobs/{done_job.id}/beats")

    assert r.status_code == 404


def test_resetting_beats_that_cannot_be_removed_is_a_500(client, done_job, monkeypatch):
    def _boom(self, **kw):
        raise OSError("busy")

    monkeypatch.setattr(Path, "unlink", _boom)

    r = client.delete(f"/api/jobs/{done_job.id}/beats")

    assert r.status_code == 500


def test_beats_cannot_be_reset_on_a_job_that_is_not_done(client, tmp_path, monkeypatch):
    import app.api.jobs as jobs_mod

    monkeypatch.setattr(jobs_mod, "JOBS_DIR", tmp_path)
    job = Job(id="abcdefabcdef")
    job.status = "queued"
    _jobs[job.id] = job

    assert client.delete(f"/api/jobs/{job.id}/beats").status_code == 404


# --------------------------------------------------------------------------
# the vocal split's diagnostic record
# --------------------------------------------------------------------------


def test_the_vocal_split_error_note_keeps_the_stderr_tail(tmp_path):
    import app.api.jobs as jobs_mod

    jobs_mod._write_vocal_split_error(tmp_path, "worker exited 1", ["line one", "line two"])

    written = (tmp_path / "vocal_split_error.txt").read_text(encoding="utf-8")
    assert "cause: worker exited 1" in written
    assert "line two" in written


def test_a_note_that_cannot_be_written_does_not_fail_the_job(tmp_path, caplog):
    """The job stays "done": this record is diagnostic-only, and losing it must
    not cost the user their stems."""
    import app.api.jobs as jobs_mod

    jobs_mod._write_vocal_split_error(tmp_path / "not-a-dir", "boom", [])

    assert "could not write vocal_split_error.txt" in caplog.text
