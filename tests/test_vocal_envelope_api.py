"""The vocals stem's level over time, for the lyrics wipe (#699)."""

from __future__ import annotations

import json
import os

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from app.core.models import Job
from app.core.registry import _jobs
from app.pipeline.audio_stats import ENVELOPE_FLOOR_DB, vocal_envelope

RATE = 44100


@pytest.fixture(autouse=True)
def _isolate_registry():
    _jobs.clear()
    yield
    _jobs.clear()


@pytest.fixture
def client(tmp_path, monkeypatch):
    from app.api import stems as stems_mod

    monkeypatch.setattr(stems_mod, "JOBS_DIR", tmp_path)
    from app.main import app

    return TestClient(app)


def _sung_then_silent(path, sung_sec=1.0, silent_sec=1.0, amplitude=0.5):
    """A stereo stem: a tone for `sung_sec`, then digital silence."""
    t = np.arange(int(RATE * sung_sec)) / RATE
    tone = amplitude * np.sin(2 * np.pi * 220 * t)
    mono = np.concatenate([tone, np.zeros(int(RATE * silent_sec))]).astype(np.float32)
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.stack([mono, mono], axis=1), RATE, subtype="PCM_16")


def _done_job(job_id: str) -> Job:
    job = Job(id=job_id)
    job.status = "done"
    _jobs[job.id] = job
    return job


def test_envelope_levels_follow_the_audio(tmp_path):
    path = tmp_path / "vocals.wav"
    _sung_then_silent(path, sung_sec=1.0, silent_sec=1.02)

    hop, levels = vocal_envelope(path, 0.04)

    assert hop == pytest.approx(0.04, abs=1e-4)
    # 2.02 s in 40 ms hops: 50 whole hops and the partial one at the end.
    assert len(levels) == 51
    # A sine at 0.5 has an RMS of 0.354, about -9 dBFS.
    assert all(-10 <= v <= -8 for v in levels[1:24])
    assert all(v == ENVELOPE_FLOOR_DB for v in levels[26:])


def test_endpoint_serves_and_keeps_the_envelope(client, tmp_path):
    job = _done_job("abcdefabcf01")
    _sung_then_silent(tmp_path / job.id / "stems" / "vocals.wav")

    r = client.get(f"/api/jobs/{job.id}/vocal-envelope")

    assert r.status_code == 200
    assert r.headers["content-type"] == "application/json"
    body = r.json()
    assert body["hop"] == pytest.approx(0.04, abs=1e-4)
    assert body["db"][5] > -12
    assert body["db"][-1] == ENVELOPE_FLOOR_DB
    kept = tmp_path / job.id / "stems" / "vocal_envelope.json"
    assert json.loads(kept.read_text(encoding="utf-8")) == body
    assert not list(kept.parent.glob("*.tmp"))


def test_endpoint_recomputes_when_the_vocals_are_newer(client, tmp_path):
    job = _done_job("abcdefabcf02")
    stems = tmp_path / job.id / "stems"
    _sung_then_silent(stems / "vocals.wav")
    kept = stems / "vocal_envelope.json"
    kept.write_text(json.dumps({"hop": 0.04, "db": [1, 2, 3]}), encoding="utf-8")
    old = (stems / "vocals.wav").stat().st_mtime - 60
    os.utime(kept, (old, old))

    r = client.get(f"/api/jobs/{job.id}/vocal-envelope")

    assert r.status_code == 200
    assert len(r.json()["db"]) > 3


def test_endpoint_uses_what_it_kept(client, tmp_path):
    job = _done_job("abcdefabcf03")
    stems = tmp_path / job.id / "stems"
    _sung_then_silent(stems / "vocals.wav")
    kept = stems / "vocal_envelope.json"
    kept.write_text(json.dumps({"hop": 0.04, "db": [-20, -90]}), encoding="utf-8")
    later = (stems / "vocals.wav").stat().st_mtime + 60
    os.utime(kept, (later, later))

    r = client.get(f"/api/jobs/{job.id}/vocal-envelope")

    assert r.json() == {"hop": 0.04, "db": [-20, -90]}


def test_endpoint_404_without_a_vocals_stem(client, tmp_path):
    job = _done_job("abcdefabcf04")
    (tmp_path / job.id / "stems").mkdir(parents=True)

    r = client.get(f"/api/jobs/{job.id}/vocal-envelope")

    assert r.status_code == 404
    assert "abcdefabcf04" not in r.text


def test_endpoint_404_for_unknown_or_unfinished_jobs(client, tmp_path):
    assert client.get("/api/jobs/abcdefabcf05/vocal-envelope").status_code == 404
    job = Job(id="abcdefabcf06")
    job.status = "separating"
    _jobs[job.id] = job
    _sung_then_silent(tmp_path / job.id / "stems" / "vocals.wav")
    assert client.get(f"/api/jobs/{job.id}/vocal-envelope").status_code == 404


@pytest.mark.parametrize(
    "bad_id", ["../etc", "..%2F..%2Fetc", "ABC", "abcdefabcdef0", "abcd-efabcdef"]
)
def test_endpoint_rejects_malformed_job_ids(client, bad_id):
    assert client.get(f"/api/jobs/{bad_id}/vocal-envelope").status_code == 404


def test_endpoint_500_is_generic_when_the_stem_is_unreadable(client, tmp_path):
    job = _done_job("abcdefabcf07")
    stems = tmp_path / job.id / "stems"
    stems.mkdir(parents=True)
    (stems / "vocals.wav").write_bytes(b"RIFF not really a wav")

    r = client.get(f"/api/jobs/{job.id}/vocal-envelope")

    assert r.status_code == 500
    assert r.json() == {"detail": "vocal envelope unavailable"}
