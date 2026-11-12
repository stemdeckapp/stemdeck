"""Effects chain prints: upload in parts, checks on finish, and the export
using a print in place of the stem (app/core/fx_prints.py, app/api/stems.py).

The browser side (rendering the print) is tested in the browser:
tests/e2e/fx-chain-dsp.spec.mjs and tests/e2e/fx-chain-studio.spec.mjs.
"""

from __future__ import annotations

import io
import json

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from app.core import fx_prints
from app.core.models import Job
from app.core.registry import _jobs
from tests.ffmpeg_probe import skip_without_ffmpeg

JOB = "abcdefabcf01"
RATE = 44100
SECONDS = 1.0
CHAIN = json.dumps({"r": 1, "inst": "bass", "trim": 0, "bpm": None, "pedals": [{"type": "delay"}]})


@pytest.fixture(autouse=True)
def _isolate_registry():
    _jobs.clear()
    yield
    _jobs.clear()


@pytest.fixture
def client(tmp_path, monkeypatch):
    from app.api import stems as stems_mod

    monkeypatch.setattr(stems_mod, "JOBS_DIR", tmp_path)
    monkeypatch.setattr(stems_mod, "_MIXDOWN_CACHE_DIR", tmp_path / "cache" / "mixdown")
    monkeypatch.setattr(stems_mod, "_FX_PRINT_DIR", tmp_path / "cache" / "fx-print")
    from app.main import app

    return TestClient(app)


def _job(tmp_path, job_id=JOB):
    stems = tmp_path / job_id / "stems"
    stems.mkdir(parents=True, exist_ok=True)
    t = np.arange(int(RATE * SECONDS)) / RATE
    tone = (0.25 * np.sin(2 * np.pi * 110 * t)).astype(np.float32)
    for name in ("bass", "vocals"):
        sf.write(stems / f"{name}.wav", np.stack([tone, tone], axis=1), RATE, subtype="PCM_16")
    job = Job(id=job_id)
    job.status = "done"
    _jobs[job_id] = job
    return stems


def _print_wav(frames: int, subtype: str = "PCM_24", channels: int = 2, rate: int = RATE) -> bytes:
    t = np.arange(frames) / rate
    x = (0.5 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    data = np.stack([x] * channels, axis=1)
    buf = io.BytesIO()
    sf.write(buf, data, rate, subtype=subtype, format="WAV")
    return buf.getvalue()


def _upload(client, body: bytes, lane="bass", gain=2.0, part=1_000_000, chain=CHAIN):
    r = client.post(f"/api/jobs/{JOB}/fx-prints", json={"lane": lane, "chain": chain})
    assert r.status_code == 200, r.text
    info = r.json()
    if info["done"]:
        return info, None
    for seq, i in enumerate(range(0, len(body), part)):
        rp = client.put(
            f"/api/jobs/{JOB}/fx-prints/{info['id']}/parts",
            params={"seq": seq},
            content=body[i : i + part],
        )
        assert rp.status_code == 200, rp.text
    rf = client.post(
        f"/api/jobs/{JOB}/fx-prints/{info['id']}/finish", json={"lane": lane, "gain": gain}
    )
    return info, rf


# 1. Happy path


def test_upload_in_parts_then_reuse(client, tmp_path):
    _job(tmp_path)
    frames = int(RATE * SECONDS) + RATE  # the lane plus one second of tail
    info, rf = _upload(client, _print_wav(frames))
    assert info["rate"] == RATE and info["channels"] == 2 and info["frames"] == int(RATE * SECONDS)
    assert fx_prints.PRINT_ID_RE.match(info["id"])
    assert rf.status_code == 200, rf.text
    assert rf.json() == {"id": info["id"], "done": True, "frames": frames}
    # Stored in the cache folder, never the jobs folder (D6).
    assert (tmp_path / "cache" / "fx-print" / f"{info['id']}.wav").is_file()
    assert not list((tmp_path / JOB).rglob("*.part"))
    assert not list((tmp_path / JOB).rglob(f"{info['id']}*"))
    # The same chain on the same file is printed once.
    again = client.post(f"/api/jobs/{JOB}/fx-prints", json={"lane": "bass", "chain": CHAIN})
    assert again.json()["done"] is True and again.json()["id"] == info["id"]
    # Another chain is another print.
    other = client.post(f"/api/jobs/{JOB}/fx-prints", json={"lane": "bass", "chain": CHAIN + " "})
    assert other.json()["id"] != info["id"]


def test_repeated_last_part_is_a_no_op(client, tmp_path):
    _job(tmp_path)
    body = _print_wav(int(RATE * SECONDS))
    r = client.post(f"/api/jobs/{JOB}/fx-prints", json={"lane": "bass", "chain": CHAIN})
    pid = r.json()["id"]
    url = f"/api/jobs/{JOB}/fx-prints/{pid}/parts"
    assert client.put(url, params={"seq": 0}, content=body[:1000]).json()["duplicate"] is False
    dup = client.put(url, params={"seq": 0}, content=body[:1000]).json()
    assert dup["duplicate"] is True and dup["bytes"] == 1000


def test_mixdown_plays_the_print_times_its_gain(client, tmp_path):
    skip_without_ffmpeg()
    _job(tmp_path)
    frames = int(RATE * SECONDS) + RATE // 2
    body = _print_wav(frames)
    info, rf = _upload(client, body, gain=0.5)
    assert rf.status_code == 200
    r = client.get(
        f"/api/jobs/{JOB}/mixdown.wav",
        params={"stems": "bass", "gains": "1.000", "prints": f"bass:{info['id']}"},
    )
    assert r.status_code == 200, r.text
    out, rate = sf.read(io.BytesIO(r.content), dtype="float32")
    expect, _ = sf.read(io.BytesIO(body), dtype="float32")
    assert rate == RATE
    # The tail past the song's end is kept, and the level is the print's
    # times its gain (16-bit export: about -96 dB of quantisation).
    assert out.shape[0] == frames
    err = out - expect * 0.5
    assert 20 * np.log10(np.sqrt(np.mean(err**2)) / np.sqrt(np.mean((expect * 0.5) ** 2))) < -60
    # Without prints= the raw stem plays: a different file.
    raw = client.get(f"/api/jobs/{JOB}/mixdown.wav", params={"stems": "bass", "gains": "1.000"})
    assert raw.status_code == 200 and raw.content != r.content


# 2. Validation


def test_rejects_original_and_unknown_lanes(client, tmp_path):
    _job(tmp_path)
    for lane in ("original", "mix", "banjo", "../etc"):
        r = client.post(f"/api/jobs/{JOB}/fx-prints", json={"lane": lane, "chain": CHAIN})
        assert r.status_code in (404, 422), (lane, r.status_code)
        assert r.status_code != 500


def test_rejects_an_oversized_chain_key(client, tmp_path):
    _job(tmp_path)
    r = client.post(f"/api/jobs/{JOB}/fx-prints", json={"lane": "bass", "chain": "x" * 9000})
    assert r.status_code == 422


@pytest.mark.parametrize(
    ("kind", "detail"),
    [
        ("pcm16", "24-bit"),
        ("mono", "format"),
        ("rate", "format"),
        ("short", "length"),
        ("long", "length"),
        ("junk", "WAV"),
    ],
)
def test_finish_checks_the_file(client, tmp_path, kind, detail):
    _job(tmp_path)
    n = int(RATE * SECONDS)
    body = {
        "pcm16": lambda: _print_wav(n, subtype="PCM_16"),
        "mono": lambda: _print_wav(n, channels=1),
        "rate": lambda: _print_wav(int(48000 * SECONDS), rate=48000),
        "short": lambda: _print_wav(n - 10),
        "long": lambda: _print_wav(n + 5 * RATE + 10),
        "junk": lambda: b"RIFF" + b"\0" * 2000,
    }[kind]()
    if kind == "long":
        # The parts themselves already refuse anything past the cap.
        r = client.post(f"/api/jobs/{JOB}/fx-prints", json={"lane": "bass", "chain": CHAIN})
        pid = r.json()["id"]
        rp = client.put(f"/api/jobs/{JOB}/fx-prints/{pid}/parts", params={"seq": 0}, content=body)
        assert rp.status_code in (413,)
        return
    _info, rf = _upload(client, body)
    assert rf.status_code == 422, rf.text
    assert detail in rf.json()["detail"]
    # A refused upload leaves nothing behind.
    assert not list((tmp_path / "cache" / "fx-print").glob(".*.part"))


def test_finish_rejects_a_non_finite_or_out_of_range_gain(client, tmp_path):
    _job(tmp_path)
    r = client.post(f"/api/jobs/{JOB}/fx-prints", json={"lane": "bass", "chain": CHAIN})
    pid = r.json()["id"]
    url = f"/api/jobs/{JOB}/fx-prints/{pid}/finish"
    assert (
        client.post(
            url,
            content=b'{"lane": "bass", "gain": Infinity}',
            headers={"content-type": "application/json"},
        ).status_code
        == 422
    )
    assert client.post(url, json={"lane": "bass", "gain": 1000.0}).status_code == 422
    assert client.post(url, json={"lane": "bass", "gain": 0}).status_code == 422


def test_prints_param_is_validated(client, tmp_path):
    _job(tmp_path)
    base = {"stems": "bass", "gains": "1.000"}
    for bad in (
        "bass",
        "bass:xyz",
        "original:" + "0" * 40,
        "bass:" + "0" * 40 + ",bass:" + "1" * 40,
    ):
        r = client.get(f"/api/jobs/{JOB}/mixdown.wav", params={**base, "prints": bad})
        assert r.status_code == 422, (bad, r.status_code)
    # A print for a lane that is not in the mix.
    r = client.get(f"/api/jobs/{JOB}/mixdown.wav", params={**base, "prints": "vocals:" + "0" * 40})
    assert r.status_code == 422


# 3. Not found


def test_unknown_job_is_404(client):
    r = client.post("/api/jobs/abcdefabc404/fx-prints", json={"lane": "bass", "chain": CHAIN})
    assert r.status_code == 404
    r = client.put(
        "/api/jobs/abcdefabc404/fx-prints/" + "0" * 40 + "/parts", params={"seq": 0}, content=b"x"
    )
    assert r.status_code == 404
    r = client.post(
        "/api/jobs/abcdefabc404/fx-prints/" + "0" * 40 + "/finish", json={"lane": "bass", "gain": 1}
    )
    assert r.status_code == 404


def test_part_for_an_upload_that_was_never_started_is_404(client, tmp_path):
    _job(tmp_path)
    r = client.put(f"/api/jobs/{JOB}/fx-prints/{'a' * 40}/parts", params={"seq": 0}, content=b"x")
    assert r.status_code == 404


# 4. Conflict and capacity


def test_out_of_order_part_is_409(client, tmp_path):
    _job(tmp_path)
    r = client.post(f"/api/jobs/{JOB}/fx-prints", json={"lane": "bass", "chain": CHAIN})
    pid = r.json()["id"]
    r = client.put(f"/api/jobs/{JOB}/fx-prints/{pid}/parts", params={"seq": 3}, content=b"abc")
    assert r.status_code == 409


def test_a_part_over_the_limit_is_413(client, tmp_path):
    _job(tmp_path)
    r = client.post(f"/api/jobs/{JOB}/fx-prints", json={"lane": "bass", "chain": CHAIN})
    pid = r.json()["id"]
    from app.core.config import FX_PRINT_PART_MAX_BYTES

    r = client.put(
        f"/api/jobs/{JOB}/fx-prints/{pid}/parts",
        params={"seq": 0},
        content=b"\0" * (FX_PRINT_PART_MAX_BYTES + 1),
    )
    assert r.status_code == 413


def test_a_print_of_a_changed_file_is_409(client, tmp_path):
    stems = _job(tmp_path)
    info, rf = _upload(client, _print_wav(int(RATE * SECONDS)))
    assert rf.status_code == 200
    # The lane's file changes (a take re-rendered, a stem replaced).
    data, _ = sf.read(stems / "bass.wav", dtype="float32")
    sf.write(stems / "bass.wav", data * 0.9, RATE, subtype="PCM_16")
    r = client.get(
        f"/api/jobs/{JOB}/mixdown.wav",
        params={"stems": "bass", "gains": "1.000", "prints": f"bass:{info['id']}"},
    )
    assert r.status_code == 409
    assert info["id"] not in r.json()["detail"]


# 5. Path traversal


def test_traversal_is_404_not_500(client, tmp_path):
    _job(tmp_path)
    # Slashes and dot segments in the job id: the path no longer names a POST
    # route (the router decodes %2F), so it is refused (405) before any
    # handler runs. Never a 500, never a file.
    for url in ("/api/jobs/..%2F..%2Fetc/fx-prints", "/api/jobs/../../etc/fx-prints"):
        r = client.post(url, json={"lane": "bass", "chain": CHAIN})
        assert r.status_code in (404, 405), (url, r.status_code)
    # A job id of the right length that is not hex reaches the handler: 404.
    r = client.post("/api/jobs/....etc.....x/fx-prints", json={"lane": "bass", "chain": CHAIN})
    assert r.status_code == 404, r.status_code
    # The same for a print id with a slash in it; one without reaches the
    # handler and is refused by the id check.
    r = client.put(
        f"/api/jobs/{JOB}/fx-prints/..%2F..%2Fregistry/parts", params={"seq": 0}, content=b"x"
    )
    assert r.status_code in (404, 405), r.status_code
    for pid in ("....", "..", "A" * 40, "0" * 39 + "g"):
        r = client.put(f"/api/jobs/{JOB}/fx-prints/{pid}/parts", params={"seq": 0}, content=b"x")
        assert r.status_code in (404, 405), (pid, r.status_code)
        r = client.post(f"/api/jobs/{JOB}/fx-prints/{pid}/finish", json={"lane": "bass", "gain": 1})
        assert r.status_code in (404, 405), (pid, r.status_code)
    # Nothing was written outside the print folder.
    assert not (tmp_path / "registry").exists()


def test_storage_refuses_a_malformed_id_without_the_route(tmp_path):
    # The storage layer checks the id itself, so a caller that skipped the
    # route's check still cannot name a file outside the print folder.
    root = tmp_path / "fx"
    root.mkdir()
    source = fx_prints.Source(
        path=tmp_path / "x.wav", identity="stem:bass@1:1", rate=RATE, frames=1
    )
    for pid in ("../../escape", "..", "0" * 39 + "/", "A" * 40):
        with pytest.raises(fx_prints.PrintError) as exc:
            fx_prints.append_part(root, JOB, pid, 0, b"x")
        assert exc.value.status == 404
        with pytest.raises(fx_prints.PrintError) as exc:
            fx_prints.finish(root, JOB, pid, 1.0, source, "bass")
        assert exc.value.status == 404
    assert not (tmp_path / "escape").exists()
    assert list(root.iterdir()) == []


# Cache key and storage


def test_cache_key_is_unchanged_without_prints():
    from app.api.stems import _mixdown_cache_key

    base = _mixdown_cache_key(JOB, "wav", ["bass"], [1.0], None, None)
    assert _mixdown_cache_key(JOB, "wav", ["bass"], [1.0], None, None, prints_fragment="") == base
    assert (
        _mixdown_cache_key(
            JOB, "wav", ["bass"], [1.0], None, None, prints_fragment="fxp:bass=" + "0" * 40
        )
        != base
    )


def test_prune_keeps_the_budget_and_the_newest(tmp_path, monkeypatch):
    monkeypatch.setattr(fx_prints, "FX_PRINT_CACHE_MAX_FILES", 3)
    root = tmp_path / "fx"
    root.mkdir()
    import os
    import time

    for i in range(5):
        (root / f"{i:040x}.wav").write_bytes(b"x" * 10)
        (root / f"{i:040x}.json").write_text("{}")
        os.utime(root / f"{i:040x}.wav", (time.time() - 100 + i, time.time() - 100 + i))
    fx_prints.prune(root, keep=f"{0:040x}")
    left = sorted(p.stem for p in root.glob("*.wav"))
    assert len(left) == 3
    assert f"{0:040x}" in left and f"{4:040x}" in left
    assert not (root / f"{1:040x}.json").exists()


def test_abandoned_uploads_are_swept(tmp_path):
    root = tmp_path / "fx"
    root.mkdir()
    (root / ".abc.part").write_bytes(b"x")
    (root / ".abc.state").write_text(json.dumps({"created": 0}))
    (root / ".new.part").write_bytes(b"x")
    (root / ".new.state").write_text(json.dumps({"created": 10_000}))
    fx_prints.sweep_sessions(root, now=10_000 + 10)
    assert not (root / ".abc.part").exists() and not (root / ".abc.state").exists()
    assert (root / ".new.part").exists()


# The cross-site guard (app/core/csrf.py): a part is raw WAV bytes, sent as
# application/octet-stream; any other non-JSON type is refused like every
# other /api body.


def test_part_upload_passes_the_body_type_guard_only_as_octet_stream(client, tmp_path):
    _job(tmp_path)
    r = client.post(f"/api/jobs/{JOB}/fx-prints", json={"lane": "bass", "chain": CHAIN})
    pid = r.json()["id"]
    url = f"/api/jobs/{JOB}/fx-prints/{pid}/parts"
    ok = client.put(
        url,
        params={"seq": 0},
        content=b"RIFF",
        headers={"content-type": "application/octet-stream"},
    )
    assert ok.status_code == 200, ok.text
    for ctype in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data"):
        r = client.put(url, params={"seq": 1}, content=b"RIFF", headers={"content-type": ctype})
        assert r.status_code == 415, (ctype, r.status_code)
