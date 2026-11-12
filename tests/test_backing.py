"""Streamer Mode's backing track: a song without one stem, mixed on demand.

GET /api/jobs/{id}/backing/{stem}.wav: validation (job id, stem whitelist,
job state, traversal), caching (built once, never rebuilt while its stems are
unchanged, rebuilt when they change, one build under concurrent requests),
the mix (equal to the sum of the other stems, never clipped), Voice with and
without the lead/backing split; and the take render leaving the same stem out.
"""

from __future__ import annotations

import asyncio
import os
import time

import httpx
import numpy as np
import pytest
import soundfile as sf

from app.api import stems as stems_mod
from app.api import takes as takes_mod
from app.core.models import Job
from app.core.registry import _jobs
from app.pipeline import backing as backing_mod

JOB = "bacc0ba0c001"
SR = 44100
SEC = 2.0
FREQ = {"vocals": 440, "drums": 90, "bass": 55, "guitar": 330, "piano": 262, "other": 600}


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


def _make_job(jobs_dir, amp=0.1, names=tuple(FREQ), status="done", job_id=JOB) -> Job:
    job = Job(id=job_id, status=status, title="Song", duration_sec=SEC)
    _jobs[job.id] = job
    stems = jobs_dir / job_id / "stems"
    stems.mkdir(parents=True, exist_ok=True)
    t = np.arange(int(SR * SEC)) / SR
    for name in names:
        x = amp * np.sin(2 * np.pi * FREQ.get(name, 700) * t)
        sf.write(stems / f"{name}.wav", np.stack([x, x], 1), SR, subtype="PCM_16")
    return job


def _read(path):
    data, sr = sf.read(str(path), dtype="float64", always_2d=True)
    assert sr == SR
    return data


def _sum(jobs_dir, names):
    return sum(_read(jobs_dir / JOB / "stems" / f"{n}.wav") for n in names)


def _cache():
    """Where the job's sums are kept: the data cache, not the song folder."""
    return backing_mod.CACHE_ROOT / JOB


# ─── Validation ──────────────────────────────────────────────────────────


async def test_bad_requests_are_refused(jobs_dir, client):
    _make_job(jobs_dir)
    async with client as c:
        assert (await c.get(f"/api/jobs/{JOB}/backing/kazoo.wav")).status_code == 422
        assert (await c.get(f"/api/jobs/{JOB}/backing/original.wav")).status_code == 422
        assert (await c.get("/api/jobs/not-a-job/backing/bass.wav")).status_code == 404
        assert (await c.get("/api/jobs/..%2F..%2Fetc/backing/bass.wav")).status_code == 404
        assert (await c.get("/api/jobs/ffffffffffff/backing/bass.wav")).status_code == 404
        r = await c.get(f"/api/jobs/{JOB}/backing/..%2Fbass.wav")
        assert r.status_code in (404, 422)
    assert not list((jobs_dir / JOB).glob("backing_without_*"))


async def test_a_job_still_processing_has_no_backing(jobs_dir, client):
    _make_job(jobs_dir, status="processing")
    async with client as c:
        r = await c.get(f"/api/jobs/{JOB}/backing/bass.wav")
    assert r.status_code == 404 and "not ready" in r.json()["detail"]


async def test_a_job_without_stems_says_so(jobs_dir, client):
    _make_job(jobs_dir, names=())
    async with client as c:
        r = await c.get(f"/api/jobs/{JOB}/backing/bass.wav")
    assert r.status_code == 404
    assert str(jobs_dir) not in r.text, "no paths in errors"


# ─── The mix ─────────────────────────────────────────────────────────────


async def test_the_backing_is_the_sum_of_the_other_stems(jobs_dir, client):
    _make_job(jobs_dir)
    async with client as c:
        r = await c.get(f"/api/jobs/{JOB}/backing/bass.wav")
        assert r.status_code == 200 and r.headers["content-type"] == "audio/wav"
        ranged = await c.get(f"/api/jobs/{JOB}/backing/bass.wav", headers={"Range": "bytes=0-99"})
        assert ranged.status_code == 206 and len(ranged.content) == 100
    out = _read(_cache() / "backing_without_bass.wav")
    want = _sum(jobs_dir, [n for n in FREQ if n != "bass"])
    assert out.shape == want.shape
    assert np.max(np.abs(out - want)) < 2 / 32768, "the same as summing the other five"
    # No bass left: its 55 Hz line is gone, the others' are there.
    spec = np.abs(np.fft.rfft(out[:, 0]))
    freqs = np.fft.rfftfreq(len(out), 1 / SR)
    at = lambda f: spec[np.argmin(np.abs(freqs - f))]  # noqa: E731
    assert at(55) < at(90) / 1000


async def test_a_loud_backing_is_scaled_never_clipped(jobs_dir):
    _make_job(jobs_dir, amp=0.35)  # five at 0.35 in phase at t=0 would pass full scale
    path, built = backing_mod.ensure_backing(JOB, jobs_dir / JOB, "bass")
    assert built
    out = _read(path)
    assert np.max(np.abs(out)) <= 0.9991
    meta = (path.with_suffix(".json")).read_text(encoding="utf-8")
    assert '"gain": 1.0' not in meta


# ─── Caching ─────────────────────────────────────────────────────────────


async def test_a_second_request_does_not_rebuild(jobs_dir, client, monkeypatch):
    _make_job(jobs_dir)
    builds = []
    real = backing_mod._mix

    def counted(paths, out):
        builds.append(out)
        return real(paths, out)

    monkeypatch.setattr(backing_mod, "_mix", counted)
    async with client as c:
        assert (await c.get(f"/api/jobs/{JOB}/backing/drums.wav")).status_code == 200
        assert (await c.get(f"/api/jobs/{JOB}/backing/drums.wav")).status_code == 200
    assert len(builds) == 1


async def test_concurrent_requests_build_once(jobs_dir, client, monkeypatch):
    _make_job(jobs_dir)
    builds = []
    real = backing_mod._mix

    def slow(paths, out):
        builds.append(out)
        import time

        time.sleep(0.3)
        return real(paths, out)

    monkeypatch.setattr(backing_mod, "_mix", slow)
    async with client as c:
        rs = await asyncio.gather(*(c.get(f"/api/jobs/{JOB}/backing/guitar.wav") for _ in range(4)))
    assert [r.status_code for r in rs] == [200] * 4
    assert len(builds) == 1
    assert len({r.content for r in rs}) == 1
    assert not list(_cache().glob(".backing_without_*.tmp.wav"))


def test_changed_stems_make_a_new_backing(jobs_dir):
    _make_job(jobs_dir)
    path, built = backing_mod.ensure_backing(JOB, jobs_dir / JOB, "piano")
    assert built
    assert backing_mod.ensure_backing(JOB, jobs_dir / JOB, "piano") == (path, False)
    # A re-split writes new stems: the backing follows.
    drums = jobs_dir / JOB / "stems" / "drums.wav"
    t = np.arange(int(SR * SEC)) / SR
    x = 0.05 * np.sin(2 * np.pi * 120 * t)
    sf.write(drums, np.stack([x, x], 1), SR, subtype="PCM_16")
    os.utime(drums, ns=(drums.stat().st_atime_ns, drums.stat().st_mtime_ns + 10_000_000))
    _, rebuilt = backing_mod.ensure_backing(JOB, jobs_dir / JOB, "piano")
    assert rebuilt


# ─── Voice ───────────────────────────────────────────────────────────────


def test_voice_is_the_vocals_stem(jobs_dir):
    _make_job(jobs_dir, names=(*FREQ, "lead_vocals", "backing_vocals"))
    stems = jobs_dir / JOB / "stems"
    names = [p.name for p in backing_mod.backing_inputs(stems, "vocals")]
    assert "vocals.wav" not in names and "lead_vocals.wav" not in names
    assert "backing_vocals.wav" not in names
    # vocals.wav (lead and backing together) is used for the others; the
    # split halves are never added on top of it.
    names = [p.name for p in backing_mod.backing_inputs(stems, "bass")]
    assert "vocals.wav" in names and "lead_vocals.wav" not in names


def test_without_vocals_wav_the_split_stands_in(jobs_dir):
    _make_job(
        jobs_dir,
        names=("drums", "bass", "guitar", "piano", "other", "lead_vocals", "backing_vocals"),
    )
    stems = jobs_dir / JOB / "stems"
    names = {p.name for p in backing_mod.backing_inputs(stems, "bass")}
    assert {"lead_vocals.wav", "backing_vocals.wav"} <= names
    names = {p.name for p in backing_mod.backing_inputs(stems, "vocals")}
    assert not names & {"lead_vocals.wav", "backing_vocals.wav", "vocals.wav"}


def test_unknown_stems_are_refused_by_the_builder(jobs_dir):
    with pytest.raises(ValueError):
        backing_mod.backing_inputs(jobs_dir, "../bass")


# ─── The take render leaves the same stem out ─────────────────────────────


def test_render_body_takes_the_removed_stem():
    assert takes_mod.RenderBody(remove_stem="bass").remove_stem == "bass"
    assert takes_mod.RenderBody().remove_stem is None
    with pytest.raises(ValueError):
        takes_mod.RenderBody(remove_stem="kazoo")
    assert takes_mod.removed_stems("vocals") == {"vocals", "lead_vocals", "backing_vocals"}
    assert takes_mod.removed_stems("bass") == {"bass"}
    assert takes_mod.removed_stems(None) == frozenset()


def test_the_render_rebuilds_original_without_the_removed_stem(jobs_dir):
    _make_job(jobs_dir, names=(*FREQ, "original"))
    names = ["original", "vocals"]
    lanes, _ = stems_mod._expand_render_lanes(JOB, names, [1.0, 0.8], [0, 0], frozenset({"bass"}))
    got = {(lane.name, lane.path.name) for lane in lanes}
    # "original" is the complement of the vocals lane, without the bass.
    assert ("original", "bass.wav") not in got
    assert {("original", f"{n}.wav") for n in ("drums", "guitar", "piano", "other")} <= got
    assert ("vocals", "vocals.wav") in got
    assert all(lane.gain == 1.0 for lane in lanes if lane.name == "original")
    # Without a removed stem, nothing changes: the rendered original.wav.
    plain, _ = stems_mod._expand_render_lanes(JOB, names, [1.0, 0.8], [0, 0])
    assert [(lane.name, lane.path.name) for lane in plain] == [
        ("original", "original.wav"),
        ("vocals", "vocals.wav"),
    ]
    # A removed stem that is a lane of its own is dropped.
    lanes, _ = stems_mod._expand_render_lanes(JOB, names, [1.0, 0.8], [0, 0], frozenset({"vocals"}))
    assert all(lane.name != "vocals" for lane in lanes)
    assert all(lane.path.name != "vocals.wav" for lane in lanes)


# ─── Errors from the builder, for the backing and the rest ──────────────────
#
# Both endpoints map what the builder raises to a status and a sentence that
# never names a path. Each case is driven through the real endpoint with the
# builder replaced, because a timeout or a disk error cannot be produced on
# demand by real stems.

_URLS = {
    "backing": f"/api/jobs/{JOB}/backing/bass.wav",
    "rest": f"/api/jobs/{JOB}/rest/drums-other.wav",
}
_BUILDERS = {"backing": "ensure_backing", "rest": "ensure_rest"}


def _builder(monkeypatch, which, fn):
    from app.api import backing as backing_api

    monkeypatch.setattr(backing_api, _BUILDERS[which], fn)


@pytest.mark.parametrize("which", ["backing", "rest"])
async def test_a_registered_job_without_its_folder_is_a_404(jobs_dir, client, which):
    _jobs[JOB] = Job(id=JOB, status="done")
    assert not (jobs_dir / JOB).exists()
    async with client as c:
        r = await c.get(_URLS[which])
    assert r.status_code == 404 and r.json()["detail"] == "job not found"


@pytest.mark.parametrize("which", ["backing", "rest"])
async def test_a_disk_error_while_mixing_is_a_500_without_a_path(
    jobs_dir, client, monkeypatch, which
):
    _make_job(jobs_dir, names=("drums",))

    def broken(*_a):
        raise PermissionError(13, "denied", str(jobs_dir / JOB / "secret"))

    _builder(monkeypatch, which, broken)
    async with client as c:
        r = await c.get(_URLS[which])
    assert r.status_code == 500
    assert "could not be made" in r.json()["detail"]
    assert str(jobs_dir) not in r.text


@pytest.mark.parametrize("which", ["backing", "rest"])
async def test_a_mix_that_takes_too_long_is_a_500(jobs_dir, client, monkeypatch, which):
    import time

    from app.api import backing as backing_api

    _make_job(jobs_dir, names=("drums",))
    monkeypatch.setattr(backing_api, "TIMEOUT_BACKING", 0.05)

    def slow(*_a):
        time.sleep(0.5)
        raise AssertionError("the endpoint must not wait for this")

    _builder(monkeypatch, which, slow)
    async with client as c:
        r = await c.get(_URLS[which])
    assert r.status_code == 500
    assert "took too long" in r.json()["detail"]


@pytest.mark.parametrize("which", ["backing", "rest"])
async def test_a_built_file_outside_the_library_is_never_served(
    jobs_dir, client, monkeypatch, tmp_path, which
):
    _make_job(jobs_dir, names=("drums",))
    outside = tmp_path / "elsewhere.wav"
    outside.write_bytes(b"RIFF")
    _builder(monkeypatch, which, lambda *_a: (outside, True))
    async with client as c:
        r = await c.get(_URLS[which])
    assert r.status_code == 404
    assert r.content != b"RIFF"


async def test_rest_refuses_a_set_spelled_two_ways(jobs_dir, client):
    _make_job(jobs_dir)
    async with client as c:
        unsorted = await c.get(f"/api/jobs/{JOB}/rest/other-drums.wav")
        unknown = await c.get(f"/api/jobs/{JOB}/rest/drums-kazoo.wav")
        bad_id = await c.get("/api/jobs/not-a-job/rest/drums.wav")
    assert unsorted.status_code == unknown.status_code == 422
    assert bad_id.status_code == 404


async def test_rest_of_a_job_still_processing_is_a_404(jobs_dir, client):
    _make_job(jobs_dir, status="processing")
    async with client as c:
        r = await c.get(f"/api/jobs/{JOB}/rest/drums.wav")
    assert r.status_code == 404 and "not ready" in r.json()["detail"]


async def test_rest_without_its_stems_says_so(jobs_dir, client):
    _make_job(jobs_dir, names=("vocals",))
    async with client as c:
        r = await c.get(f"/api/jobs/{JOB}/rest/drums-other.wav")
    assert r.status_code == 404
    assert "missing" in r.json()["detail"]
    assert str(jobs_dir) not in r.text


# ─── The data cache: its budget, eviction and rebuild ───────────────────────


def _age(path, seconds):
    then = time.time() - seconds
    os.utime(path, (then, then))


def _job_locks():
    return [key for key in backing_mod._locks if key[0] == JOB]


def test_the_sums_are_kept_in_the_cache_not_with_the_song(jobs_dir):
    _make_job(jobs_dir)
    job_dir = jobs_dir / JOB
    backing, _ = backing_mod.ensure_backing(JOB, job_dir, "bass")
    rest, _ = backing_mod.ensure_rest(JOB, job_dir, ("drums",))
    assert not list(job_dir.glob("backing_without_*")), "the song folder holds no sum"
    assert not list(job_dir.glob("rest_*"))
    assert backing == _cache() / "backing_without_bass.wav"
    assert rest == _cache() / "rest_drums.wav"
    assert backing.with_suffix(".json").is_file() and rest.with_suffix(".json").is_file()


def test_the_cache_stays_under_its_budget_least_recently_used_first(jobs_dir, monkeypatch):
    _make_job(jobs_dir)
    job_dir = jobs_dir / JOB
    paths = [backing_mod.ensure_backing(JOB, job_dir, s)[0] for s in ("bass", "drums", "guitar")]
    for i, path in enumerate(paths):
        _age(path, 3600 - 60 * i)  # bass the oldest, guitar the newest
    # Asked for again: bass is the most recent now, drums the oldest.
    assert backing_mod.ensure_backing(JOB, job_dir, "bass") == (paths[0], False)
    size = paths[0].stat().st_size
    monkeypatch.setattr(backing_mod, "CACHE_MAX_BYTES", 2 * size + size // 2)
    backing_mod.ensure_backing(JOB, job_dir, "piano")
    left = sorted(p.name for p in _cache().glob("*.wav"))
    assert left == ["backing_without_bass.wav", "backing_without_piano.wav"]
    assert not (_cache() / "backing_without_drums.json").exists(), "its signature goes with it"
    assert not (_cache() / "backing_without_guitar.json").exists()


def test_a_sum_in_use_is_never_evicted(jobs_dir, monkeypatch):
    _make_job(jobs_dir)
    job_dir = jobs_dir / JOB
    monkeypatch.setattr(backing_mod, "CACHE_MAX_BYTES", 1)
    first, _ = backing_mod.ensure_backing(JOB, job_dir, "bass")
    second, _ = backing_mod.ensure_backing(JOB, job_dir, "drums")
    assert first.is_file() and second.is_file(), "both were served a moment ago"
    # Not served for an hour, but a request holds its lock: still kept.
    _age(first, 3600)
    with backing_mod._lock(JOB, first.name):
        backing_mod.ensure_backing(JOB, job_dir, "guitar")
        assert first.is_file()
    # Old and free: it goes with the next build.
    backing_mod.ensure_backing(JOB, job_dir, "piano")
    assert not first.exists()
    assert second.is_file()


async def test_an_evicted_sum_is_built_again(jobs_dir, client, monkeypatch):
    _make_job(jobs_dir)
    async with client as c:
        before = await c.get(f"/api/jobs/{JOB}/rest/bass-drums.wav")
        _age(_cache() / "rest_bass-drums.wav", 3600)
        monkeypatch.setattr(backing_mod, "CACHE_MAX_BYTES", 1)
        assert (await c.get(f"/api/jobs/{JOB}/rest/guitar.wav")).status_code == 200
        assert not (_cache() / "rest_bass-drums.wav").exists()
        after = await c.get(f"/api/jobs/{JOB}/rest/bass-drums.wav")
    assert before.status_code == after.status_code == 200
    assert after.content == before.content


async def test_a_song_folder_from_an_earlier_build_still_plays(jobs_dir, client):
    """Builds before the cache kept the sums in the song folder. They are made
    again in the cache, and the old files in the song folder go."""
    _make_job(jobs_dir)
    job_dir = jobs_dir / JOB
    for name in ("backing_without_bass", "rest_drums"):
        (job_dir / f"{name}.wav").write_bytes(b"RIFF old")
        (job_dir / f"{name}.json").write_text('{"sources": {}}', encoding="utf-8")
    async with client as c:
        backing = await c.get(f"/api/jobs/{JOB}/backing/bass.wav")
        rest = await c.get(f"/api/jobs/{JOB}/rest/drums.wav")
    assert backing.status_code == rest.status_code == 200
    assert backing.content != b"RIFF old" and rest.content != b"RIFF old"
    assert not list(job_dir.glob("backing_without_*")) and not list(job_dir.glob("rest_*"))


# ─── A deleted song takes its sums and its locks with it ─────────────────────


async def test_deleting_the_song_drops_its_sums_and_locks(jobs_dir, client):
    _make_job(jobs_dir)
    async with client as c:
        assert (await c.get(f"/api/jobs/{JOB}/backing/bass.wav")).status_code == 200
        assert (await c.get(f"/api/jobs/{JOB}/rest/drums.wav")).status_code == 200
        assert _job_locks()
        assert (await c.delete(f"/api/jobs/{JOB}")).status_code == 200
    assert not _cache().exists()
    assert not _job_locks()


async def test_a_held_lock_outlives_the_delete(jobs_dir, client):
    _make_job(jobs_dir)
    lock = backing_mod._lock(JOB, "backing_without_bass.wav")
    try:
        with lock:
            async with client as c:
                assert (await c.delete(f"/api/jobs/{JOB}")).status_code == 200
            assert backing_mod._locks.get((JOB, "backing_without_bass.wav")) is lock
    finally:
        backing_mod._locks.pop((JOB, "backing_without_bass.wav"), None)


async def test_songs_made_and_deleted_leave_no_locks_behind(jobs_dir, client):
    before = len(backing_mod._locks)
    async with client as c:
        for i in range(10):
            job_id = f"bacc0ba0c1{i:02d}"
            _make_job(jobs_dir, job_id=job_id)
            assert (await c.get(f"/api/jobs/{job_id}/backing/bass.wav")).status_code == 200
            assert (await c.get(f"/api/jobs/{job_id}/rest/drums.wav")).status_code == 200
            assert (await c.delete(f"/api/jobs/{job_id}")).status_code == 200
    assert len(backing_mod._locks) == before


async def test_a_reset_drops_every_sum_and_lock(jobs_dir, client):
    _make_job(jobs_dir)
    async with client as c:
        assert (await c.get(f"/api/jobs/{JOB}/backing/bass.wav")).status_code == 200
        assert (await c.post("/api/reset")).status_code == 200
    assert not _cache().exists()
    assert not _job_locks()


def test_the_auto_delete_drops_the_sums_of_what_it_removes(jobs_dir):
    from app.pipeline.collect import sweep_old_jobs

    _make_job(jobs_dir).created_at = 0
    backing_mod.ensure_backing(JOB, jobs_dir / JOB, "bass")
    sweep_old_jobs(jobs_dir, ttl_seconds=60)
    assert not (jobs_dir / JOB).exists()
    assert not _cache().exists()
    assert not _job_locks()
