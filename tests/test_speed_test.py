"""The hardware speed test (Settings, Separation): the recommendation, the
estimates, staleness, the disk test, the worker protocol and the routes.

No test runs a real model. Every subprocess the test starts is a stub
(speed_test.worker_cmd is replaced for every test), the jobs folder is the
pytest temporary one, and the disk test writes a few MiB rather than 128.
"""

from __future__ import annotations

import io
import json
import os
import sys
import threading
import time
from contextlib import redirect_stdout
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.core.config as cfg
from app.api import speed_test as api
from app.core import device_reason, stem_models
from app.core import settings as _settings
from app.core import speed_test as st
from app.core.stem_models import MODELS, SELECTABLE_STEMS
from app.main import app
from app.pipeline import jobqueue
from app.pipeline import speed_test_worker as worker

KIM, SIX, FT = "kim_mel_roformer", "htdemucs_6s", "htdemucs_ft"
# Taken before the autouse fixture replaces it, for the tests that need the
# real availability check.
REAL_MODEL_AVAILABLE = stem_models.model_available
GPU = {"name": "NVIDIA GeForce RTX 3080", "driver": "616.64", "cuda": "13.4", "memory_mb": 10240}
NO_DISK_ISSUE = {"write_mb_s": 900.0, "skipped": None, "slow": False}
SLOW_DISK = {"write_mb_s": 25.0, "skipped": None, "slow": True}


@pytest.fixture(autouse=True)
def _machine(monkeypatch):
    """A fixed machine: a named CPU, a known torch, an NVIDIA GPU with 10 GB,
    CPU and CUDA usable, every registry model installed. No real worker can
    start: the default worker command exits at once."""
    monkeypatch.setattr(st, "cpu_name", lambda: "Test CPU 9000")
    monkeypatch.setattr(st, "torch_version", lambda: "2.5.1+cu121")
    monkeypatch.setattr(st, "_available_devices", lambda: ["cuda", "cpu"])
    monkeypatch.setattr(cfg, "available_torch_devices", lambda: ["cuda", "cpu"])
    monkeypatch.setattr(device_reason, "nvidia_gpu", lambda: dict(GPU))
    monkeypatch.setattr(stem_models, "model_available", lambda model_id: model_id in MODELS)
    monkeypatch.setattr(
        st, "worker_cmd", lambda device: [sys.executable, "-c", "raise SystemExit(3)"]
    )
    monkeypatch.setattr(st, "DISK_TEST_BYTES", 4 * 1024 * 1024)
    monkeypatch.setattr(st, "DISK_CHUNK_BYTES", 1024 * 1024)
    api.reset_state()
    yield
    api.cancel_running()
    api.reset_state()


def _tables(**rtf6s: float) -> dict:
    """Each device's table from a measured 6s real-time factor."""
    return {d: st.device_table(d, {SIX: {"rtf": r, "peak_mb": None}}) for d, r in rtf6s.items()}


def _hw(**over) -> dict:
    return {**st.current_hw(), **over}


def _result(failed: dict | None = None, **rtf6s: float) -> dict:
    tables = {d: {SIX: {"rtf": r, "peak_mb": 600}} for d, r in rtf6s.items()}
    kim = {"cuda"} & set(tables)
    return st.build_result(
        hw=_hw(),
        disk=NO_DISK_ISSUE,
        measured=tables,
        failed=failed or {},
        kim_devices=kim,
        ts=1_700_000_000,
    )


# ── the recommendation ──


def test_a_fast_gpu_gets_kim_because_the_whole_plan_fits():
    rec = st.recommend(_tables(cpu=0.5, cuda=0.068), NO_DISK_ISSUE, {"cuda"})
    assert rec["device"] == "cuda"
    assert rec["stems"] == {**dict.fromkeys(SELECTABLE_STEMS, SIX), "vocals": KIM}
    # (0.143 + 0.068) x 4 minutes.
    assert rec["estimate_minutes_4min"] == pytest.approx(0.84, abs=0.01)
    assert rec["budget_minutes"] == 3.0
    assert rec["reason"] == "fits"
    assert rec["reasons"] == ["fits"]


def test_a_slow_gpu_gets_the_6s_alone_as_over_budget():
    # Five times slower than the registry: Kim + 6s is 3.46 min, over 3.0.
    rec = st.recommend(_tables(cpu=2.5, cuda=0.34), NO_DISK_ISSUE, {"cuda"})
    assert rec["device"] == "cuda"
    assert rec["stems"] == dict.fromkeys(SELECTABLE_STEMS, SIX)
    assert rec["estimate_minutes_4min"] == pytest.approx(1.36)
    assert rec["reason"] == "over-budget"


def test_cpu_only_gets_the_6s_with_no_gpu():
    rec = st.recommend(_tables(cpu=0.5), NO_DISK_ISSUE, set())
    assert rec["device"] == "cpu"
    assert rec["stems"] == dict.fromkeys(SELECTABLE_STEMS, SIX)
    assert rec["estimate_minutes_4min"] == pytest.approx(2.0)
    assert rec["reason"] == "no-gpu"


def test_a_cpu_too_slow_even_for_the_6s_is_cpu_over_budget():
    rec = st.recommend(_tables(cpu=1.5), NO_DISK_ISSUE, set())
    assert (rec["device"], rec["reason"]) == ("cpu", "cpu-over-budget")
    assert rec["estimate_minutes_4min"] == pytest.approx(6.0)


def test_a_gpu_slower_than_the_cpu_recommends_the_cpu():
    rec = st.recommend(_tables(cpu=0.5, cuda=0.9), NO_DISK_ISSUE, {"cuda"})
    assert rec["device"] == "cpu"
    assert rec["stems"] == dict.fromkeys(SELECTABLE_STEMS, SIX)
    assert rec["reason"] == "gpu-slower-than-cpu"


def test_a_slow_disk_adds_its_reason_and_changes_no_plan():
    tables = _tables(cpu=0.5, cuda=0.068)
    fast, slow = (st.recommend(tables, disk, {"cuda"}) for disk in (NO_DISK_ISSUE, SLOW_DISK))
    assert slow["device"] == fast["device"]
    assert slow["stems"] == fast["stems"]
    assert slow["estimate_minutes_4min"] == fast["estimate_minutes_4min"]
    assert slow["reason"] == "disk-slow"
    assert slow["reasons"] == ["fits", "disk-slow"]


def test_a_slow_disk_never_hides_a_plan_reason():
    rec = st.recommend(_tables(cpu=2.5, cuda=0.34), SLOW_DISK, {"cuda"})
    assert rec["reason"] == "over-budget"
    assert rec["reasons"] == ["over-budget", "disk-slow"]


def test_exactly_on_the_budget_fits_and_just_over_does_not():
    on = {SIX: {"rtf": 0.30, "measured": True}, KIM: {"rtf": 0.45, "measured": False}}
    over = {SIX: {"rtf": 0.30, "measured": True}, KIM: {"rtf": 0.4501, "measured": False}}
    assert st.recommend({"cuda": on}, NO_DISK_ISSUE, {"cuda"})["stems"]["vocals"] == KIM
    rec = st.recommend({"cuda": over}, NO_DISK_ISSUE, {"cuda"})
    assert rec["stems"]["vocals"] == SIX
    assert rec["reason"] == "over-budget"


def test_every_plan_over_the_budget_takes_the_fastest_one():
    rec = st.recommend(_tables(cpu=1.0, cuda=0.8), NO_DISK_ISSUE, {"cuda"})
    assert rec["device"] == "cuda"
    assert rec["stems"]["vocals"] == SIX
    assert rec["estimate_minutes_4min"] == pytest.approx(3.2)
    assert rec["reason"] == "over-budget"


def test_kim_is_never_picked_where_the_static_rule_rules_it_out():
    # A GPU fast enough for Kim, but too little memory (or Kim not installed).
    rec = st.recommend(_tables(cpu=0.5, cuda=0.068), NO_DISK_ISSUE, set())
    assert rec["stems"]["vocals"] == SIX
    assert rec["reason"] == "fits"


def test_on_a_tie_the_gpu_wins():
    assert st.recommend(_tables(cpu=0.3, cuda=0.3), NO_DISK_ISSUE, set())["device"] == "cuda"


def test_no_measured_device_is_an_error_not_a_recommendation():
    with pytest.raises(ValueError):
        st.recommend({}, NO_DISK_ISSUE, set())


# ── estimates ──


def test_estimates_scale_the_registry_by_the_measured_6s():
    table = st.device_table("cuda", {SIX: {"rtf": 0.136, "peak_mb": 619}})
    # Measured 0.136 against the registry's 0.068: this GPU is 2x slower.
    assert table[SIX] == {"rtf": 0.136, "peak_mb": 619, "measured": True}
    assert table[KIM] == {
        "rtf": pytest.approx(0.286),
        "measured": False,
        "estimated_from": SIX,
    }
    assert table[FT]["rtf"] == pytest.approx(0.29)
    cpu = st.device_table("cpu", {SIX: {"rtf": 0.25, "peak_mb": None}})
    # Twice as fast as the registry's CPU numbers.
    assert cpu[KIM]["rtf"] == pytest.approx(1.70)
    assert cpu[FT]["rtf"] == pytest.approx(0.92)


def test_a_model_measured_for_real_is_kept_as_measured():
    table = st.device_table(
        "cuda", {SIX: {"rtf": 0.07, "peak_mb": 600}, FT: {"rtf": 0.2, "peak_mb": 580}}
    )
    assert table[FT] == {"rtf": 0.2, "peak_mb": 580, "measured": True}
    assert table[KIM]["measured"] is False


def test_only_a_quick_model_is_measured_for_real():
    # htdemucs_ft: about 5 s on a registry-speed GPU, about 66 s on the CPU.
    assert st.cheap_to_measure(FT, "cuda", 1.0) is True
    assert st.cheap_to_measure(FT, "cpu", 1.0) is False
    assert st.cheap_to_measure(FT, "cuda", 10.0) is False


# ── the saved result and staleness ──


def test_a_saved_result_survives_a_settings_reload():
    saved = _settings.set_speed_test(_result(cpu=0.5, cuda=0.068))
    _settings._state = None
    assert _settings.get_speed_test() == saved
    on_disk = json.loads(Path(_settings._SETTINGS_PATH).read_text(encoding="utf-8"))
    assert on_disk["speed_test"]["recommendation"]["device"] == "cuda"


def test_a_result_that_does_not_check_is_refused_and_nothing_is_written():
    bad = _result(cpu=0.5)
    bad["recommendation"]["stems"]["guitar"] = "made_up_model"
    with pytest.raises(ValueError):
        _settings.set_speed_test(bad)
    assert not Path(_settings._SETTINGS_PATH).exists()


@pytest.mark.parametrize(
    "edit",
    [
        lambda r: r.update(version=2),
        lambda r: r["devices"]["cpu"][SIX].update(rtf=float("nan")),
        lambda r: r["devices"]["cpu"][SIX].update(measured=False),
        lambda r: r["recommendation"].update(device="tpu"),
        lambda r: r["recommendation"].update(reason="because"),
        lambda r: r["disk"].update(skipped="/etc/passwd"),
        lambda r: r["hw"].update(devices=["cpu", "quantum"]),
        # Unhashable or mistyped values where strings and numbers belong.
        lambda r: r["recommendation"].update(device=["cuda"]),
        lambda r: r["recommendation"].update(device={"cuda": 1}),
        lambda r: r["recommendation"].update(reason=["fits"]),
        lambda r: r["recommendation"].update(reasons=[["fits"]]),
        lambda r: r["recommendation"]["stems"].update(vocals=[SIX]),
        lambda r: r["hw"].update(devices=[["cpu"]]),
        lambda r: r["disk"].update(skipped=["low-space"]),
        lambda r: r.update(version=True),
        lambda r: r.update(ts=10**400),
        lambda r: r["devices"]["cpu"][SIX].update(rtf=10**400),
        lambda r: r.update(devices=[["cpu"]]),
    ],
)
def test_a_hand_edited_result_reads_as_never_run(edit):
    raw = _result(cpu=0.5)
    edit(raw)
    _settings._ensure()["speed_test"] = raw
    assert _settings.get_speed_test() is None
    assert _settings.get_speed_test_public() is None


def test_a_fresh_result_is_not_stale():
    _settings.set_speed_test(_result(cpu=0.5, cuda=0.068))
    assert _settings.get_speed_test_public()["stale"] is False


@pytest.mark.parametrize(
    "change",
    [
        lambda mp: mp.setattr(st, "cpu_name", lambda: "Another CPU"),
        lambda mp: mp.setattr(os, "cpu_count", lambda: 3),
        lambda mp: mp.setattr(device_reason, "nvidia_gpu", lambda: {**GPU, "name": "RTX 5090"}),
        lambda mp: mp.setattr(device_reason, "nvidia_gpu", lambda: {**GPU, "memory_mb": 4096}),
        lambda mp: mp.setattr(device_reason, "nvidia_gpu", lambda: None),
        lambda mp: mp.setattr(st, "_available_devices", lambda: ["cpu"]),
        lambda mp: mp.setattr(st, "torch_version", lambda: "2.6.0+cu124"),
    ],
)
def test_a_hardware_change_makes_the_result_stale(monkeypatch, change):
    _settings.set_speed_test(_result(cpu=0.5, cuda=0.068))
    change(monkeypatch)
    assert _settings.get_speed_test_public()["stale"] is True


def test_resolve_uses_a_fresh_result_and_falls_back_when_it_goes_stale(monkeypatch):
    static = _settings.resolve_stem_models("cuda")
    assert static["vocals"] == KIM  # 10 GB of VRAM: the static rule picks Kim
    # A slow GPU: the test says Kim + 6s is over the budget.
    _settings.set_speed_test(_result(cpu=2.5, cuda=0.34))
    assert _settings.resolve_stem_models("cuda") == dict.fromkeys(SELECTABLE_STEMS, SIX)
    assert _settings.recommended_stem_models("cuda") == dict.fromkeys(SELECTABLE_STEMS, SIX)
    # The GPU is swapped: the result no longer describes this machine.
    monkeypatch.setattr(device_reason, "nvidia_gpu", lambda: {**GPU, "name": "RTX 5090"})
    assert _settings.resolve_stem_models("cuda") == static


def test_resolve_keeps_an_explicit_choice_over_the_test():
    _settings.set_speed_test(_result(cpu=2.5, cuda=0.34))
    _settings.set_stem_model_choices({"vocals": KIM})
    assert _settings.resolve_stem_models("cuda")["vocals"] == KIM


def test_resolve_for_a_device_the_test_never_measured_is_the_static_rule():
    _settings.set_speed_test(_result(cpu=2.5))
    assert _settings.resolve_stem_models("cuda")["vocals"] == KIM


def test_resolve_never_follows_the_test_to_a_model_this_install_lacks(monkeypatch):
    _settings.set_speed_test(_result(cpu=0.5, cuda=0.068))
    monkeypatch.setattr(stem_models, "model_available", lambda model_id: model_id == SIX)
    assert _settings.resolve_stem_models("cuda")["vocals"] == SIX


# ── the disk test ──


def _leftovers(folder: Path) -> list[str]:
    return [p.name for p in folder.iterdir() if p.name.startswith(st.DISK_TEMP_PREFIX)]


def test_the_disk_test_reports_write_speed_and_leaves_nothing(tmp_path):
    clock = iter([0.0, 0.5])
    disk = st.measure_disk(tmp_path, clock=lambda: next(clock))
    assert disk == {"write_mb_s": 8.0, "skipped": None, "slow": True}
    assert _leftovers(tmp_path) == []


def test_a_fast_folder_is_not_slow(tmp_path):
    clock = iter([0.0, 0.01])
    disk = st.measure_disk(tmp_path, clock=lambda: next(clock))
    assert disk["write_mb_s"] == pytest.approx(400.0)
    assert disk["slow"] is False


def test_the_temp_file_is_removed_when_the_sync_fails(tmp_path, monkeypatch):
    def broken_fsync(fd):
        assert len(_leftovers(tmp_path)) == 1  # it was really written
        raise OSError("disk went away")

    monkeypatch.setattr(os, "fsync", broken_fsync)
    disk = st.measure_disk(tmp_path)
    assert disk == {"write_mb_s": None, "skipped": "failed", "slow": False}
    assert _leftovers(tmp_path) == []


def test_the_temp_file_is_removed_even_when_the_write_raises_something_else(tmp_path, monkeypatch):
    def broken_write(fd, data):
        raise RuntimeError("not an OSError")

    monkeypatch.setattr(os, "write", broken_write)
    with pytest.raises(RuntimeError):
        st.measure_disk(tmp_path)
    assert _leftovers(tmp_path) == []


def test_an_existing_file_with_the_same_name_is_never_touched(tmp_path, monkeypatch):
    name = f"{st.DISK_TEMP_PREFIX}0123456789abcdef{st.DISK_TEMP_SUFFIX}"
    existing = tmp_path / name
    existing.write_bytes(b"someone else's file")
    monkeypatch.setattr(st, "_new_temp_name", lambda: name)
    disk = st.measure_disk(tmp_path)
    assert disk["skipped"] == "name-taken"
    assert existing.read_bytes() == b"someone else's file"


def test_the_disk_test_is_skipped_under_512_mb_free(tmp_path, monkeypatch):
    monkeypatch.setattr(st, "_free_bytes", lambda folder: 511 * 1024 * 1024)
    folder = tmp_path / "jobs"
    folder.mkdir()
    disk = st.measure_disk(folder)
    assert disk == {"write_mb_s": None, "skipped": "low-space", "slow": False}
    assert list(folder.iterdir()) == []


def test_a_missing_jobs_folder_skips_the_disk_test(tmp_path):
    assert st.measure_disk(tmp_path / "nope")["skipped"] == "unavailable"


def test_a_cancel_stops_the_disk_test_and_removes_its_file(tmp_path):
    cancel = threading.Event()
    cancel.set()
    assert st.measure_disk(tmp_path, cancel)["skipped"] == "failed"
    assert _leftovers(tmp_path) == []


# ── the worker protocol ──


@pytest.mark.parametrize(
    ("line", "parsed"),
    [
        ("@@STEP@@cuda\n", ("step", "cuda")),
        ("@@STEP@@tpu", None),
        ("@@PCT@@42", ("pct", 42)),
        ("@@PCT@@150", ("pct", 100)),
        ("@@PCT@@-3", None),
        ("@@PCT@@abc", None),
        ('@@RESULT@@{"device": "cpu"}', ("result", {"device": "cpu"})),
        ("@@RESULT@@[1, 2]", None),
        ("@@RESULT@@{not json", None),
        ("UserWarning: something from a library", None),
        ("", None),
    ],
)
def test_protocol_lines(line, parsed):
    assert st.parse_line(line) == parsed


def test_a_worker_result_is_checked_before_it_is_used():
    ok = {"device": "cpu", "torch": "2.5.1", "models": {SIX: {"rtf": 0.5, "peak_mb": 900}}}
    assert st.check_worker_result("cpu", ok) == (
        {SIX: {"rtf": 0.5, "peak_mb": 900}},
        None,
        "2.5.1",
    )
    assert st.check_worker_result("cuda", ok)[1] == "failed"  # another device's answer
    assert st.check_worker_result("cpu", {"device": "cpu", "error": "rm -rf"})[1] == "failed"
    assert st.check_worker_result("cpu", {"device": "cpu", "error": "model-missing"})[1] == (
        "model-missing"
    )
    nan = {"device": "cpu", "models": {SIX: {"rtf": float("inf")}}}
    assert st.check_worker_result("cpu", nan)[1] == "failed"
    junk = {"device": "cpu", "models": {SIX: {"rtf": 0.5}, "evil": {"rtf": 1.0}}}
    assert set(st.check_worker_result("cpu", junk)[0]) == {SIX}


def _stub(monkeypatch, code: str) -> None:
    """Every device's worker is ``code``, run with the device as argv[1]."""
    monkeypatch.setattr(st, "worker_cmd", lambda device: [sys.executable, "-c", code, device])


_GOOD_WORKER = """
import json, sys
device = sys.argv[1]
print("some library noise", flush=True)
print("@@STEP@@" + device, flush=True)
print("@@PCT@@50", flush=True)
if device == "cuda":
    print("@@RESULT@@" + json.dumps({"device": device, "torch": "2.5.1+cu121",
                                     "error": "failed"}), flush=True)
else:
    print("@@RESULT@@" + json.dumps({"device": device, "torch": "2.5.1+cu121",
          "models": {"htdemucs_6s": {"rtf": 0.5, "peak_mb": 1400}}}), flush=True)
"""

_SLOW_WORKER = """
import sys, time
print("@@STEP@@" + sys.argv[1], flush=True)
time.sleep(60)
"""


def test_run_device_parses_the_protocol_from_a_subprocess(monkeypatch):
    _stub(monkeypatch, _GOOD_WORKER)
    steps, pcts, procs = [], [], []
    run = st.run_device(
        "cpu",
        on_step=steps.append,
        on_pct=pcts.append,
        cancel=threading.Event(),
        register=procs.append,
    )
    assert run.measured == {SIX: {"rtf": 0.5, "peak_mb": 1400}}
    assert (run.error, run.torch) == (None, "2.5.1+cu121")
    assert steps == ["cpu"] and pcts == [50]
    assert run.tail == ["some library noise"]
    assert procs[-1] is None and procs[0].poll() is not None


@pytest.mark.parametrize(("timeout", "stall", "why"), [(1, 30, "timeout"), (30, 1, "stalled")])
def test_run_device_ends_a_worker_that_takes_too_long(monkeypatch, timeout, stall, why):
    _stub(monkeypatch, _SLOW_WORKER)
    procs: list = []
    started = time.monotonic()
    run = st.run_device(
        "cpu",
        on_step=lambda s: None,
        on_pct=lambda p: None,
        cancel=threading.Event(),
        register=procs.append,
        timeout=timeout,
        stall=stall,
    )
    assert run.error == why and run.measured is None
    assert time.monotonic() - started < 20
    assert procs[0].poll() is not None


def test_a_worker_that_dies_without_a_result_is_a_failed_device(monkeypatch):
    _stub(monkeypatch, "import sys; sys.exit(1)")
    run = st.run_device(
        "cpu",
        on_step=lambda s: None,
        on_pct=lambda p: None,
        cancel=threading.Event(),
        register=lambda p: None,
    )
    assert (run.measured, run.error) == (None, "failed")


# ── the worker's own pieces (no torch, no model) ──


def test_the_warm_up_run_is_discarded():
    now = [0.0]
    calls = [0]

    def run():
        calls[0] += 1
        # The first run pays for CUDA start and the model transfer.
        now[0] += 30.0 if calls[0] == 1 else 1.5

    assert worker.time_runs(run, clock=lambda: now[0], runs=2) == [1.5, 1.5]
    assert calls[0] == 3


def test_the_clip_is_deterministic_stereo_at_44_1_khz():
    a, b = worker.synthetic_clip(), worker.synthetic_clip()
    assert a.shape == (2, 12 * 44_100)
    assert a.dtype.name == "float32"
    assert (a == b).all()
    assert abs(float(abs(a).max()) - 0.5) < 1e-6
    assert not (worker.synthetic_clip(seed=1) == a).all()


def test_the_worker_refuses_an_unknown_device_without_importing_torch():
    out = io.StringIO()
    with redirect_stdout(out):
        assert worker.main(["speed_test_worker", "tpu"]) == 0
    kind, value = st.parse_line(out.getvalue().strip().splitlines()[-1])
    assert kind == "result" and value["error"] == "device-unavailable"


def test_the_worker_blocks_every_download():
    class Hub:
        def download_url_to_file(self, *args):
            raise AssertionError("must not be reached")

    class FakeTorch:
        hub = Hub()

    fake = FakeTorch()
    original = fake.hub.download_url_to_file
    with worker._no_downloads(fake), pytest.raises(worker._DownloadRefused):
        fake.hub.download_url_to_file("https://example.invalid/x.th", "x.th")
    assert fake.hub.download_url_to_file == original


# ── the routes ──


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _wait_until_settled(client, limit: float = 30.0) -> dict:
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        body = client.get("/api/speed-test").json()
        if body["state"] != "running":
            return body
        time.sleep(0.05)
    raise AssertionError("the speed test did not finish")


def test_get_before_any_run_is_idle_and_settings_say_never_run(client):
    assert client.get("/api/speed-test").json() == {
        "state": "idle",
        "step": None,
        "pct": 0,
        "error": None,
        "error_code": None,
        "result": None,
    }
    assert client.get("/api/settings").json()["speed_test"] is None


def test_a_run_measures_skips_a_failed_device_and_saves_the_result(client, monkeypatch):
    _stub(monkeypatch, _GOOD_WORKER)
    resp = client.post("/api/speed-test")
    assert (resp.status_code, resp.json()) == (202, {"state": "running"})
    body = _wait_until_settled(client)
    assert body["state"] == "done" and body["pct"] == 100 and body["error"] is None
    result = body["result"]
    # cuda failed: left out of the devices, reported, and the CPU still counts.
    assert set(result["devices"]) == {"cpu"}
    assert result["failed"] == {"cuda": "failed"}
    assert result["devices"]["cpu"][SIX] == {"rtf": 0.5, "peak_mb": 1400, "measured": True}
    assert result["devices"]["cpu"][KIM]["measured"] is False
    assert result["hw"] == {
        "cpu": "Test CPU 9000",
        "cores": os.cpu_count(),
        "gpu": GPU["name"],
        "vram_mb": GPU["memory_mb"],
        "devices": ["cpu", "cuda"],
        "torch": "2.5.1+cu121",
    }
    assert result["disk"]["skipped"] is None and result["disk"]["write_mb_s"] > 0
    assert result["recommendation"]["device"] == "cpu"
    assert result["stale"] is False
    # Settings serve the same result, and it is on disk.
    assert client.get("/api/settings").json()["speed_test"] == result
    _settings._state = None
    assert _settings.get_speed_test_public() == result
    # The disk test left nothing in the jobs folder.
    assert _leftovers(cfg.JOBS_DIR) == []


def test_a_run_where_no_device_works_fails_with_a_fixed_sentence(client, monkeypatch):
    _stub(
        monkeypatch,
        'import json, sys; print("@@RESULT@@" + json.dumps('
        '{"device": sys.argv[1], "error": "model-missing"}), flush=True)',
    )
    assert client.post("/api/speed-test").status_code == 202
    body = _wait_until_settled(client)
    assert body["state"] == "failed"
    assert body["error"] == st.ERROR_MODEL
    assert body["error_code"] == "model-missing"
    assert body["result"] is None
    assert _settings.get_speed_test() is None


def test_a_new_run_replaces_the_old_result(client, monkeypatch):
    _settings.set_speed_test(_result(cpu=2.5, cuda=0.34))
    _stub(monkeypatch, _GOOD_WORKER)
    client.post("/api/speed-test")
    assert _wait_until_settled(client)["result"]["devices"]["cpu"][SIX]["rtf"] == 0.5


def _assert_busy(client) -> None:
    resp = client.post("/api/speed-test")
    assert resp.status_code == 409
    assert resp.json() == {"detail": api.BUSY_DETAIL, "code": "busy"}
    assert client.get("/api/speed-test").json()["state"] == "idle"


def test_busy_while_a_job_runs(client, monkeypatch):
    started = []
    monkeypatch.setattr(api, "run_test", lambda cancel: started.append(1))
    jobqueue._running_id = "a1b2c3d4e5f6"
    _assert_busy(client)
    assert started == []


def test_busy_while_a_job_waits_in_the_queue(client, monkeypatch):
    started = []
    monkeypatch.setattr(api, "run_test", lambda cancel: started.append(1))
    jobqueue._queue.append("a1b2c3d4e5f6")
    _assert_busy(client)
    assert started == []


def test_busy_while_the_pipeline_lock_is_held(client, monkeypatch):
    import asyncio

    started = []
    monkeypatch.setattr(api, "run_test", lambda cancel: started.append(1))
    monkeypatch.setattr("app.pipeline.runner._pipeline_lock", asyncio.Semaphore(0))
    _assert_busy(client)
    assert started == []


def test_busy_while_a_test_runs_and_only_one_runs(client, monkeypatch):
    _stub(monkeypatch, _SLOW_WORKER)
    assert client.post("/api/speed-test").status_code == 202
    resp = client.post("/api/speed-test")
    assert resp.status_code == 409 and resp.json()["code"] == "busy"
    client.post("/api/speed-test/cancel")


def test_cancel_ends_the_worker_and_saves_nothing(client, monkeypatch):
    _stub(monkeypatch, _SLOW_WORKER)
    procs: list = []
    real_register = api._register

    def register(proc):
        if proc is not None:
            procs.append(proc)
        real_register(proc)

    monkeypatch.setattr(api, "_register", register)
    assert client.post("/api/speed-test").status_code == 202
    deadline = time.monotonic() + 30
    while client.get("/api/speed-test").json()["step"] != "cpu":
        assert time.monotonic() < deadline
        time.sleep(0.05)
    body = client.post("/api/speed-test/cancel").json()
    assert body["state"] == "cancelled"
    assert body["result"] is None
    assert procs and all(p.poll() is not None for p in procs)
    assert _settings.get_speed_test() is None
    assert _leftovers(cfg.JOBS_DIR) == []
    # Idle again: the next run may start.
    _stub(monkeypatch, _GOOD_WORKER)
    assert client.post("/api/speed-test").status_code == 202
    assert _wait_until_settled(client)["state"] == "done"


def test_cancel_with_nothing_running_answers_the_state(client):
    resp = client.post("/api/speed-test/cancel")
    assert resp.status_code == 200
    assert resp.json()["state"] == "idle"


def test_settings_payload_marks_a_stale_result(client, monkeypatch):
    _settings.set_speed_test(_result(cpu=0.5, cuda=0.068))
    body = client.get("/api/settings").json()
    assert body["speed_test"]["stale"] is False
    assert body["speed_test"]["recommendation"]["stems"]["vocals"] == KIM
    monkeypatch.setattr(st, "_available_devices", lambda: ["cpu"])
    assert client.get("/api/settings").json()["speed_test"]["stale"] is True


def test_settings_resolved_models_follow_a_fresh_result(client, monkeypatch):
    monkeypatch.setattr("app.main.get_demucs_device", lambda: "cuda")
    _settings.set_speed_test(_result(cpu=2.5, cuda=0.34))
    body = client.get("/api/settings").json()
    assert body["stem_models_resolved"] == dict.fromkeys(SELECTABLE_STEMS, SIX)
    assert body["stem_models_recommended"] == dict.fromkeys(SELECTABLE_STEMS, SIX)


def test_a_library_move_waits_for_a_running_test(tmp_path, monkeypatch):
    """The disk test holds a file open in the jobs folder; a move would carry
    it, or on Windows fail to rename it, part way."""
    import app.main as main_mod

    monkeypatch.setenv("STEMDECK_DESKTOP", "1")
    jobs = tmp_path / "current"
    (jobs / "aaaaaaaaaaaa").mkdir(parents=True)
    monkeypatch.setattr(main_mod, "JOBS_DIR", jobs)
    with TestClient(app) as client:
        api._state["state"] = "running"
        r = client.post("/api/settings/stems-location", json={"path": str(tmp_path / "new")})
        api._state["state"] = "idle"
        assert r.status_code == 409
        assert r.json()["detail"] == (
            "Wait for the benchmark to finish before moving the stems folder"
        )
        assert (jobs / "aaaaaaaaaaaa").is_dir()
        # The door is open again: the refusal did not leave a move half begun.
        assert client.post("/api/speed-test/cancel").status_code == 200


# ── fix round: hysteresis, failed devices, the new reasons ──


@pytest.mark.parametrize(
    ("cuda", "device"),
    [(0.52, "cuda"), (0.549, "cuda"), (0.55, "cpu"), (0.9, "cpu")],
)
def test_the_cpu_wins_only_when_the_gpu_is_ten_percent_slower(cuda, device):
    rec = st.recommend(_tables(cpu=0.5, cuda=cuda), NO_DISK_ISSUE, {"cuda"})
    assert rec["device"] == device
    if device == "cpu":
        assert rec["reason"] == "gpu-slower-than-cpu"


def test_a_gpu_that_failed_to_measure_is_gpu_failed_not_no_gpu():
    rec = st.recommend(_tables(cpu=0.5), NO_DISK_ISSUE, set(), {"cuda": "timeout"})
    assert (rec["device"], rec["reason"]) == ("cpu", "gpu-failed")
    # A failed GPU is the reason even when the CPU is also over the budget.
    rec = st.recommend(_tables(cpu=1.5), NO_DISK_ISSUE, set(), {"cuda": "stalled"})
    assert rec["reason"] == "gpu-failed"


def test_a_gpu_without_kim_and_over_budget_is_cpu_over_budget():
    rec = st.recommend(_tables(cpu=1.0, cuda=0.8), NO_DISK_ISSUE, set())
    assert (rec["device"], rec["reason"]) == ("cuda", "cpu-over-budget")
    assert rec["stems"]["vocals"] == SIX


def test_a_failed_device_resolves_to_the_safe_plan_until_the_test_runs_again():
    assert _settings.resolve_stem_models("cuda")["vocals"] == KIM  # the static rule
    saved = _settings.set_speed_test(_result(failed={"cuda": "timeout"}, cpu=0.5))
    assert saved["failed"] == {"cuda": "timeout"}
    assert _settings.resolve_stem_models("cuda") == dict.fromkeys(SELECTABLE_STEMS, SIX)
    assert _settings.recommended_stem_models("cuda") == dict.fromkeys(SELECTABLE_STEMS, SIX)
    # A run in which cuda measured replaces it.
    _settings.set_speed_test(_result(cpu=0.5, cuda=0.068))
    assert _settings.resolve_stem_models("cuda")["vocals"] == KIM


def test_a_failed_entry_for_a_measured_device_is_dropped():
    raw = _result(cpu=0.5, cuda=0.068)
    raw["failed"] = {"cuda": "timeout", "mps": ["not", "a", "code"]}
    assert st.sanitize(raw)["failed"] == {}


def test_sanitize_never_raises(monkeypatch):
    def broken(raw):
        raise RecursionError("deep")

    raw = _result(cpu=0.5)
    monkeypatch.setattr(st, "_clean_hw", broken)
    assert st.sanitize(raw) is None


def test_separation_still_runs_with_a_hostile_saved_result(tmp_path, monkeypatch):
    """A hand-edited speed_test with a list where the device belongs: the job
    resolves its plan (the static rule) and separates."""
    from app.core.models import Job
    from app.pipeline import separate as sep_mod

    raw = _result(cpu=0.5, cuda=0.068)
    raw["recommendation"]["device"] = ["cuda"]
    Path(_settings._SETTINGS_PATH).write_text(json.dumps({"speed_test": raw}), encoding="utf-8")
    _settings._state = None
    echo = (
        "import json, os, sys\n"
        "sys.stderr.write('@@READY@@\\n'); sys.stderr.flush()\n"
        "for line in sys.stdin:\n"
        "    req = json.loads(line)\n"
        "    d = os.path.join(req['job_dir'], 'htdemucs_6s', 'source')\n"
        "    os.makedirs(d, exist_ok=True)\n"
        "    open(os.path.join(d, 'vocals.wav'), 'wb').write(b'RIFF')\n"
        "    sys.stderr.write('@@DONE@@\\n'); sys.stderr.flush()\n"
    )
    monkeypatch.setattr(sep_mod, "_spawn_worker_cmd", lambda d: [sys.executable, "-c", echo])
    monkeypatch.setattr(sep_mod, "get_demucs_device", lambda: "cpu")
    sep_mod._worker.clear()
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    job = Job(id="abcdefabc999")
    try:
        sep_mod.separate(job, tmp_path / "source.wav", job_dir)
    finally:
        sep_mod._kill_worker()
    assert job.stem_models == dict.fromkeys(SELECTABLE_STEMS, SIX)


# ── fix round: the clip is scaled to a song ──


def test_demucs_cuts_the_clip_and_the_song_into_whole_segments():
    # 7.8 s segments, one every 0.75 x 343980 = 257985 samples.
    assert st.demucs_chunks(12.0) == 3
    assert st.demucs_chunks(240.0) == 42
    # The 0.5 s shift never changes either count.
    assert st.demucs_chunks(12.5) == 3 and st.demucs_chunks(240.5) == 42


def test_a_clip_run_is_scaled_to_a_four_minute_song():
    # 3 s for a run on the clip is 1 s a segment; a song is 42 segments.
    assert st.song_rtf(3.0) == pytest.approx(42 / 240)
    # Dividing by the clip's 12 s would have said 0.25: 43 % too slow.
    assert 3.0 / 12.0 / st.song_rtf(3.0) == pytest.approx(1.43, abs=0.01)
    assert st.clip_run_seconds(st.song_rtf(3.0)) == pytest.approx(3.0)


def test_the_worker_reports_the_song_rtf_from_its_timed_runs():
    now = [0.0]
    calls = [0]

    def run():
        calls[0] += 1
        now[0] += 40.0 if calls[0] == 1 else 3.0

    times = worker.time_runs(run, clock=lambda: now[0], runs=2)
    assert worker.rtf_from_times(times, 7.8) == pytest.approx(42 / 240)


def test_the_segment_is_read_from_the_model_or_its_bag():
    from fractions import Fraction
    from types import SimpleNamespace

    assert worker.segment_of(SimpleNamespace(segment=Fraction(39, 5))) == 7.8
    bag = SimpleNamespace(models=[SimpleNamespace(segment=Fraction(39, 5))])
    assert worker.segment_of(bag) == 7.8
    assert worker.segment_of(SimpleNamespace()) == st.HTDEMUCS_SEGMENT


# ── fix round: the protocol, partial results, the kill ──


@pytest.mark.parametrize(
    "line",
    [
        "@@PCT@@²",
        "@@PCT@@٣",
        "@@RESULT@@" + "[" * 100_000 + "]" * 100_000,
        "@@RESULT@@" + json.dumps({"device": "cpu", "pad": "x" * (64 * 1024)}),
    ],
    ids=["superscript", "arabic-indic", "deep-json", "over-64-kib"],
)
def test_hostile_protocol_lines_are_dropped(line):
    assert st.parse_line(line) is None


def test_a_partial_line_is_parsed():
    assert st.parse_line('@@PARTIAL@@{"device": "cpu"}') == ("partial", {"device": "cpu"})


_PARTIAL_THEN_HANG = """
import json, sys, time
device = sys.argv[1]
print("@@STEP@@" + device, flush=True)
print("@@PARTIAL@@" + json.dumps({"device": device, "torch": "2.5.1",
      "models": {"htdemucs_6s": {"rtf": 0.4, "peak_mb": 900}}}), flush=True)
time.sleep(60)
"""


def test_a_timeout_after_the_6s_keeps_the_6s(monkeypatch):
    _stub(monkeypatch, _PARTIAL_THEN_HANG)
    run = st.run_device(
        "cpu",
        on_step=lambda s: None,
        on_pct=lambda p: None,
        cancel=threading.Event(),
        register=lambda p: None,
        timeout=1,
        grace=1,
    )
    assert run.measured == {SIX: {"rtf": 0.4, "peak_mb": 900}}
    assert (run.error, run.partial, run.torch) == (None, True, "2.5.1")


def test_a_worker_that_dies_after_the_6s_keeps_the_6s(monkeypatch):
    code = _PARTIAL_THEN_HANG.replace("time.sleep(60)", "sys.exit(1)")
    _stub(monkeypatch, code)
    run = st.run_device(
        "cpu",
        on_step=lambda s: None,
        on_pct=lambda p: None,
        cancel=threading.Event(),
        register=lambda p: None,
    )
    assert run.partial is True and run.measured[SIX]["rtf"] == 0.4


def test_a_worker_that_ignores_the_stop_is_killed(monkeypatch):
    """terminate() alone never escalated: a wedged worker kept the read, and
    the pipeline lock, forever."""
    _stub(monkeypatch, _SLOW_WORKER)
    calls: list[bool] = []
    real = st.signal_tree

    def stubborn(proc, *, hard):
        calls.append(hard)
        if hard:
            real(proc, hard=True)

    monkeypatch.setattr(st, "signal_tree", stubborn)
    started = time.monotonic()
    run = st.run_device(
        "cpu",
        on_step=lambda s: None,
        on_pct=lambda p: None,
        cancel=threading.Event(),
        register=lambda p: None,
        timeout=1,
        grace=1,
    )
    assert run.error == "timeout"
    assert calls[:2] == [False, True]
    assert time.monotonic() - started < 15


def test_a_child_holding_the_output_open_is_killed_too(monkeypatch):
    from app.core.process import process_exists

    code = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "print('child', child.pid, flush=True)\n"
        "time.sleep(60)\n"
    )
    _stub(monkeypatch, code)
    started = time.monotonic()
    run = st.run_device(
        "cpu",
        on_step=lambda s: None,
        on_pct=lambda p: None,
        cancel=threading.Event(),
        register=lambda p: None,
        timeout=1,
        grace=1,
    )
    assert run.error == "timeout"
    assert time.monotonic() - started < 20
    [pid] = [int(line.split()[1]) for line in run.tail if line.startswith("child ")]
    deadline = time.monotonic() + 10
    while process_exists(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not process_exists(pid)


@pytest.mark.parametrize(
    ("codes", "code", "text"),
    [
        (["torch-missing"], "torch-missing", st.ERROR_TORCH),
        (["torch-missing", "torch-missing"], "torch-missing", st.ERROR_TORCH),
        (["model-missing", "failed"], "model-missing", st.ERROR_MODEL),
        (["timeout"], "timeout", st.ERROR_TIMEOUT),
        (["stalled", "stalled"], "stalled", st.ERROR_TIMEOUT),
        (["timeout", "stalled"], "failed", st.ERROR_FAILED),
        (["device-unavailable"], "failed", st.ERROR_FAILED),
    ],
)
def test_a_failed_run_names_its_cause(codes, code, text):
    assert st.run_error(codes) == (code, text)
    assert code in st.RUN_ERRORS


# ── fix round: the disk step ──


def test_a_disk_that_hangs_is_given_up_on_as_timeout(tmp_path, monkeypatch):
    release = threading.Event()

    def hang(fd, data):
        release.wait(10)
        return os.write(fd, data)

    monkeypatch.setattr(st, "_write_chunk", hang)
    started = time.monotonic()
    disk = st.measure_disk_bounded(tmp_path, threading.Event(), timeout=0.5)
    assert disk == {"write_mb_s": None, "skipped": "timeout", "slow": False}
    assert time.monotonic() - started < 5
    # The thread given up on still removes its file once its write returns.
    release.set()
    deadline = time.monotonic() + 10
    while _leftovers(tmp_path) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _leftovers(tmp_path) == []


def test_a_bounded_disk_test_that_finishes_reports_its_speed(tmp_path):
    disk = st.measure_disk_bounded(tmp_path, threading.Event(), timeout=30)
    assert disk["skipped"] is None and disk["write_mb_s"] > 0


def _old(path: Path, data: bytes = b"x", age: float = 3600) -> Path:
    path.write_bytes(data)
    then = time.time() - age
    os.utime(path, (then, then))
    return path


def test_the_sweep_removes_only_stale_test_files(tmp_path, monkeypatch):
    monkeypatch.setattr(st, "DISK_TEST_BYTES", 16)
    stale = _old(tmp_path / ".stemdeck-speedtest-0123456789abcdef.tmp")
    fresh = _old(tmp_path / ".stemdeck-speedtest-1123456789abcdef.tmp", age=60)
    big = _old(tmp_path / ".stemdeck-speedtest-2123456789abcdef.tmp", b"x" * 17)
    other = _old(tmp_path / ".stemdeck-speedtest-8123456789ABCDEF.tmp")
    named = _old(tmp_path / "my-song.tmp")
    folder = tmp_path / ".stemdeck-speedtest-3123456789abcdef.tmp"
    folder.mkdir()
    nested = tmp_path / "sub"
    nested.mkdir()
    deep = _old(nested / ".stemdeck-speedtest-4123456789abcdef.tmp")
    assert st.sweep_stale_temp_files(tmp_path) == 1
    assert not stale.exists()
    for kept in (fresh, big, other, named, folder, deep):
        assert kept.exists(), kept.name


def test_the_sweep_never_follows_a_link(tmp_path):
    target = _old(tmp_path / "precious.wav")
    link = tmp_path / ".stemdeck-speedtest-5123456789abcdef.tmp"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("this account cannot create symlinks")
    then = time.time() - 3600
    os.utime(link, (then, then), follow_symlinks=False)
    st.sweep_stale_temp_files(tmp_path)
    assert link.is_symlink() and target.exists()


def test_the_server_start_sweeps_stale_test_files():
    stale = _old(cfg.JOBS_DIR / ".stemdeck-speedtest-6123456789abcdef.tmp")
    with TestClient(app):
        pass
    assert not stale.exists()


def test_a_run_sweeps_stale_test_files_first(client, monkeypatch):
    stale = _old(cfg.JOBS_DIR / ".stemdeck-speedtest-7123456789abcdef.tmp")
    _stub(monkeypatch, _GOOD_WORKER)
    client.post("/api/speed-test")
    _wait_until_settled(client)
    assert not stale.exists()


def test_cancel_during_the_disk_step_removes_its_file(client, monkeypatch):
    def slow(fd, data):
        time.sleep(0.3)
        return os.write(fd, data)

    monkeypatch.setattr(st, "_write_chunk", slow)
    assert client.post("/api/speed-test").status_code == 202
    deadline = time.monotonic() + 10
    while not _leftovers(cfg.JOBS_DIR):
        assert time.monotonic() < deadline
        time.sleep(0.02)
    assert client.get("/api/speed-test").json()["step"] == "disk"
    body = client.post("/api/speed-test/cancel").json()
    assert body["state"] == "cancelled"
    assert _leftovers(cfg.JOBS_DIR) == []
    assert _settings.get_speed_test() is None


# ── fix round: when a test may start ──


def test_a_paused_queue_does_not_block_the_test(client, monkeypatch):
    started = []
    monkeypatch.setattr(api, "run_test", lambda cancel: started.append(1))
    jobqueue._queue.append("a1b2c3d4e5f6")
    jobqueue._paused = True
    assert client.post("/api/speed-test").status_code == 202
    deadline = time.monotonic() + 10
    while not started:
        assert time.monotonic() < deadline
        time.sleep(0.02)


def test_busy_during_a_library_move(client, monkeypatch):
    started = []
    monkeypatch.setattr(api, "run_test", lambda cancel: started.append(1))
    monkeypatch.setattr(api, "is_relocating", lambda: True)
    _assert_busy(client)
    assert started == []


# ── fix round: what Settings say ──


def test_settings_reason_follows_the_effective_plan(client, monkeypatch):
    monkeypatch.setattr("app.main.get_demucs_device", lambda: "cuda")
    assert client.get("/api/settings").json()["stem_models_reason"] == "gpu"
    _settings.set_speed_test(_result(cpu=2.5, cuda=0.34))  # Kim + 6s over budget
    assert client.get("/api/settings").json()["stem_models_reason"] == "over-budget"
    _settings.set_speed_test(_result(cpu=0.5, cuda=0.068))  # Kim fits
    assert client.get("/api/settings").json()["stem_models_reason"] == "gpu"
    _settings.set_speed_test(_result(cpu=2.5, cuda=0.34))
    monkeypatch.setattr(st, "_available_devices", lambda: ["cpu"])  # stale
    assert client.get("/api/settings").json()["stem_models_reason"] == "gpu"


def test_auto_never_picks_a_model_this_install_cannot_run(client, monkeypatch):
    """The real availability check, not a patched one: Kim hidden the way an
    install without audio-separator hides it, and a fresh test that
    recommends Kim. Neither the resolved nor the recommended plan has Kim."""
    monkeypatch.setattr(stem_models, "model_available", REAL_MODEL_AVAILABLE)
    monkeypatch.setattr("app.main.get_demucs_device", lambda: "cuda")
    real_dirs = stem_models._package_dirs
    monkeypatch.setattr(
        stem_models,
        "_package_dirs",
        lambda name: [] if name == "audio_separator" else real_dirs(name),
    )
    saved = _settings.set_speed_test(_result(cpu=0.5, cuda=0.068))
    assert saved["recommendation"]["stems"]["vocals"] == KIM
    body = client.get("/api/settings").json()
    for plan in (body["stem_models_resolved"], body["stem_models_recommended"]):
        assert plan["vocals"] == SIX
        assert all(REAL_MODEL_AVAILABLE(model_id) for model_id in plan.values())


def test_auto_on_this_install_only_names_models_it_has(client, monkeypatch):
    monkeypatch.setattr(stem_models, "model_available", REAL_MODEL_AVAILABLE)
    monkeypatch.setattr("app.main.get_demucs_device", lambda: "cuda")
    _settings.set_speed_test(_result(cpu=0.5, cuda=0.068))
    body = client.get("/api/settings").json()
    for plan in (body["stem_models_resolved"], body["stem_models_recommended"]):
        assert all(REAL_MODEL_AVAILABLE(model_id) for model_id in plan.values())
