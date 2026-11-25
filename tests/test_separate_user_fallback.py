"""separate() and a model the person added that cannot run (My models).

The worker says so on its last line ("user model failed: <id>: <code>"), and
separate() runs the job again from scratch on the SAME device with
htdemucs_6s for that model's stems, recording which stems were stood in for,
for which model and why (Job.stem_model_fallbacks). A fill (a given plan)
never stands another model in: its stems must add up with the ones on disk.

The worker is a stub process (the _spawn_worker_cmd seam, as in
test_separate_fallback.py and test_separate_plan.py) that writes each request
it was sent into the job folder, so what separate() really dispatched is what
is checked.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from app.core import stem_models
from app.core.models import Job, JobCancelled
from app.pipeline import separate as sep_mod
from app.pipeline.errors import SeparationError

SIX = "htdemucs_6s"
USER_A = "user_0123456789ab"
USER_B = "user_ba9876543210"
ALL_6S = dict.fromkeys(("vocals", "drums", "bass", "guitar", "piano"), SIX)

# Records each request (with the device and whether the last attempt's
# partial output was still there), then: fails as the first model of
# FAIL_USER the plan names, the way demucs_worker does (a cause line, then the
# failure line, then exit 1); or fails as a CUDA OOM on OOM_DEVICE; or, with
# TRICK_TAIL, writes the failure words on an earlier line and fails on another
# last line; or succeeds.
_WORKER = """
import json, os, sys
device = sys.argv[1]
fail = json.loads(os.environ.get("FAIL_USER", "{}"))
sys.stderr.write("@@READY@@\\n")
sys.stderr.flush()
for line in sys.stdin:
    req = json.loads(line)
    d = req["job_dir"]
    partial = os.path.join(d, "htdemucs_6s", "partial.txt")
    n = len([f for f in os.listdir(d) if f.startswith("request-")]) + 1
    req["device"] = device
    req["saw_partial"] = os.path.exists(partial)
    with open(os.path.join(d, "request-%02d.json" % n), "w") as f:
        json.dump(req, f)
    plan = req.get("plan") or {}
    failing = sorted(m for m in set(plan.values()) if m in fail)
    if failing:
        os.makedirs(os.path.dirname(partial), exist_ok=True)
        open(partial, "w").write("x")
        m = failing[0]
        sys.stderr.write("user model %s could not run (%s): it broke\\n" % (m, fail[m]))
        msg = "user model failed: %s: %s" % (m, fail[m])
        sys.stderr.write("@@ERROR@@" + json.dumps(msg) + "\\n")
        sys.stderr.flush()
        sys.exit(1)
    if os.environ.get("TRICK_TAIL"):
        sys.stderr.write("user model failed: %s: changed\\n" % os.environ["TRICK_TAIL"])
        sys.stderr.write("@@ERROR@@" + json.dumps("CUDA out of memory. Tried 2 GiB") + "\\n")
        sys.stderr.flush()
        sys.exit(1)
    if device == os.environ.get("OOM_DEVICE"):
        sys.stderr.write("@@ERROR@@" + json.dumps("CUDA out of memory. Tried 2 GiB") + "\\n")
        sys.stderr.flush()
        sys.exit(1)
    out = os.path.join(d, "htdemucs_6s", "source")
    os.makedirs(out, exist_ok=True)
    open(os.path.join(out, "vocals.wav"), "wb").write(b"RIFF")
    sys.stderr.write("100%\\n@@DONE@@\\n")
    sys.stderr.flush()
"""


@pytest.fixture(autouse=True)
def _reset_worker():
    sep_mod._worker.clear()
    yield
    sep_mod._kill_worker()


@pytest.fixture
def worker(monkeypatch):
    """The stub worker on whatever device is asked; returns the devices of
    the workers started."""
    spawned: list[str] = []

    def spawn(device: str) -> list[str]:
        spawned.append(device)
        return [sys.executable, "-c", _WORKER, device]

    monkeypatch.setattr(sep_mod, "_spawn_worker_cmd", spawn)
    return spawned


@pytest.fixture
def store(monkeypatch):
    """Models the person added, as the separation sees them: each has a
    worker description and a name. Remove one from the dict to remove it."""
    models = {USER_A: "My drums", USER_B: "My bass"}

    def spec(model_id):
        if model_id not in models:
            raise KeyError(model_id)
        return {"id": model_id, "stems": ["drums", "bass"]}

    monkeypatch.setattr(stem_models, "user_worker_spec", spec)
    monkeypatch.setattr(stem_models, "user_model_name", lambda m: models.get(m))
    monkeypatch.setattr(stem_models, "user_model_rtf", lambda m, d: None)
    return models


def _settings_plan(monkeypatch, device: str, plan: dict[str, str]) -> None:
    """Settings resolve to ``plan`` on ``device``; no stem is on "auto"."""
    monkeypatch.setattr(sep_mod, "get_demucs_device", lambda: device)
    monkeypatch.setattr(sep_mod, "resolve_stem_models", lambda d: dict(plan))
    monkeypatch.setattr(sep_mod, "get_stem_model_choices", lambda: dict(plan))


def _requests(job_dir: Path) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(job_dir.glob("request-*.json"))]


def _job_dir(tmp_path: Path) -> Path:
    d = tmp_path / "job"
    d.mkdir()
    return d


def _record_stages(monkeypatch) -> list[str]:
    seen: list[str] = []
    real = sep_mod._set

    def record(job, **fields):
        if "stage" in fields:
            seen.append(fields["stage"])
        real(job, **fields)

    monkeypatch.setattr(sep_mod, "_set", record)
    return seen


def test_a_failing_user_model_is_stood_in_for_by_6s_on_the_same_device(
    worker, store, monkeypatch, tmp_path
):
    """F on the old code, which retried the whole job on the CPU."""
    plan = {**ALL_6S, "drums": USER_A, "bass": USER_A}
    _settings_plan(monkeypatch, "cuda", plan)
    monkeypatch.setenv("FAIL_USER", json.dumps({USER_A: "key_mismatch"}))
    stages = _record_stages(monkeypatch)
    job = Job(id="abcdefabc911")
    job_dir = _job_dir(tmp_path)

    root = sep_mod.separate(job, tmp_path / "source.wav", job_dir)

    assert root.is_dir()
    first, second = _requests(job_dir)
    assert first["plan"] == plan and first["device"] == "cuda"
    assert set(first["user_models"]) == {USER_A}
    assert second["plan"] == ALL_6S and second["device"] == "cuda"
    assert "user_models" not in second
    # The failed attempt's partial output was gone before the second began.
    assert second["saw_partial"] is False
    assert worker == ["cuda", "cuda"]
    assert job.gpu_fallback is False and job.compute_device == "cuda"
    assert job.stem_models == ALL_6S
    record = {"model": USER_A, "name": "My drums", "code": "key_mismatch"}
    assert job.stem_model_fallbacks == {"drums": record, "bass": record}
    assert job.to_state()["stem_model_fallbacks"] == job.stem_model_fallbacks
    assert "A model could not run; using HT Demucs 6 stems..." in stages


def test_two_failing_user_models_are_each_stood_in_for(worker, store, monkeypatch, tmp_path):
    plan = {**ALL_6S, "drums": USER_A, "bass": USER_B}
    _settings_plan(monkeypatch, "cpu", plan)
    monkeypatch.setenv("FAIL_USER", json.dumps({USER_A: "changed", USER_B: "non_finite"}))
    job = Job(id="abcdefabc912")
    job_dir = _job_dir(tmp_path)

    sep_mod.separate(job, tmp_path / "source.wav", job_dir)

    plans = [r["plan"] for r in _requests(job_dir)]
    assert plans == [plan, {**plan, "drums": SIX}, ALL_6S]
    assert job.stem_model_fallbacks == {
        "drums": {"model": USER_A, "name": "My drums", "code": "changed"},
        "bass": {"model": USER_B, "name": "My bass", "code": "non_finite"},
    }


def test_a_model_removed_before_the_job_reached_the_worker_is_stood_in_for(
    worker, store, monkeypatch, tmp_path
):
    plan = {**ALL_6S, "drums": USER_A}
    _settings_plan(monkeypatch, "cpu", plan)
    del store[USER_A]
    job = Job(id="abcdefabc913")
    job_dir = _job_dir(tmp_path)

    sep_mod.separate(job, tmp_path / "source.wav", job_dir)

    # The attempt that named it never reached a worker.
    assert [r["plan"] for r in _requests(job_dir)] == [ALL_6S]
    assert job.stem_model_fallbacks == {
        "drums": {"model": USER_A, "name": None, "code": "unavailable"}
    }


def test_a_fill_never_stands_another_model_in(worker, store, monkeypatch, tmp_path):
    """The stems on disk were made with the user model: 6s ones would not add
    up with them. The fill fails, on the GPU and then the CPU, with the plan
    it was given."""
    plan = {**ALL_6S, "drums": USER_A}
    monkeypatch.setattr(sep_mod, "get_demucs_device", lambda: "cuda")
    monkeypatch.setenv("FAIL_USER", json.dumps({USER_A: "load_failed"}))
    job = Job(id="abcdefabc914")
    job_dir = _job_dir(tmp_path)

    with pytest.raises(SeparationError) as exc_info:
        sep_mod.separate(job, tmp_path / "source.wav", job_dir, plan=plan)

    assert [r["plan"] for r in _requests(job_dir)] == [plan, plan]
    assert [r["device"] for r in _requests(job_dir)] == ["cuda", "cpu"]
    assert job.stem_model_fallbacks is None
    assert job.stem_models == plan
    assert f"user model failed: {USER_A}: load_failed" in str(exc_info.value)


def test_only_the_last_line_counts(worker, store, monkeypatch, tmp_path):
    """The failure words earlier in a tail never pick this branch: the GPU
    failure is retried on the CPU with the same plan, as before."""
    plan = {**ALL_6S, "drums": USER_A}
    _settings_plan(monkeypatch, "cuda", plan)
    monkeypatch.setenv("TRICK_TAIL", USER_A)
    job = Job(id="abcdefabc915")
    job_dir = _job_dir(tmp_path)

    with pytest.raises(SeparationError):
        sep_mod.separate(job, tmp_path / "source.wav", job_dir)

    assert [(r["device"], r["plan"]) for r in _requests(job_dir)] == [
        ("cuda", plan),
        ("cpu", plan),
    ]
    assert job.stem_model_fallbacks is None
    assert job.gpu_fallback is True


def test_the_cpu_retry_after_a_stand_in_keeps_the_stand_in(worker, store, monkeypatch, tmp_path):
    plan = {**ALL_6S, "drums": USER_A}
    _settings_plan(monkeypatch, "cuda", plan)
    monkeypatch.setenv("FAIL_USER", json.dumps({USER_A: "forward_failed"}))
    monkeypatch.setenv("OOM_DEVICE", "cuda")
    job = Job(id="abcdefabc916")
    job_dir = _job_dir(tmp_path)

    sep_mod.separate(job, tmp_path / "source.wav", job_dir)

    assert [(r["device"], r["plan"]) for r in _requests(job_dir)] == [
        ("cuda", plan),
        ("cuda", ALL_6S),
        ("cpu", ALL_6S),
    ]
    assert job.gpu_fallback is True
    assert job.stem_model_fallbacks["drums"]["code"] == "forward_failed"


def test_a_cancel_between_attempts_stops_the_job(worker, store, monkeypatch, tmp_path):
    plan = {**ALL_6S, "drums": USER_A}
    _settings_plan(monkeypatch, "cpu", plan)
    monkeypatch.setenv("FAIL_USER", json.dumps({USER_A: "changed"}))
    job = Job(id="abcdefabc917")
    job_dir = _job_dir(tmp_path)
    real = sep_mod.shutil.rmtree

    def rmtree_then_cancel(path, *a, **kw):
        real(path, *a, **kw)
        job.cancel_requested = True

    monkeypatch.setattr(sep_mod.shutil, "rmtree", rmtree_then_cancel)

    with pytest.raises(JobCancelled):
        sep_mod.separate(job, tmp_path / "source.wav", job_dir)

    assert len(_requests(job_dir)) == 1


def test_stand_ins_end_after_the_limit(worker, store, monkeypatch, tmp_path):
    ids = ["user_00000000000a", "user_00000000000b", "user_00000000000c", "user_00000000000d"]
    for model_id in ids:
        store[model_id] = model_id
    plan = {"vocals": ids[0], "drums": ids[1], "bass": ids[2], "guitar": ids[3], "piano": SIX}
    _settings_plan(monkeypatch, "cpu", plan)
    monkeypatch.setenv("FAIL_USER", json.dumps(dict.fromkeys(ids, "load_failed")))
    job = Job(id="abcdefabc918")
    job_dir = _job_dir(tmp_path)

    with pytest.raises(SeparationError) as exc_info:
        sep_mod.separate(job, tmp_path / "source.wav", job_dir)

    assert len(_requests(job_dir)) == 1 + sep_mod.MAX_STAND_INS
    assert len(job.stem_model_fallbacks) == sep_mod.MAX_STAND_INS
    assert f"user model failed: {ids[3]}: load_failed" in str(exc_info.value)


@pytest.mark.parametrize(
    ("line", "found"),
    [
        (f"user model failed: {USER_A}: key_mismatch", (USER_A, "key_mismatch")),
        (f"  user model failed: {USER_A}: changed  ", (USER_A, "changed")),
        (f"user model failed: {USER_A}: key_mismatch; and more", None),
        (f"oops user model failed: {USER_A}: changed", None),
        ("user model failed: user_0123456789AB: changed", None),
        ("user model failed: htdemucs_6s: changed", None),
        (f"user model failed: {USER_A}: Not-A-Code", None),
        ("CUDA out of memory. Tried 2 GiB", None),
    ],
)
def test_the_failure_line_is_matched_whole(line, found):
    assert sep_mod.user_failure(["something before", line]) == found
    assert sep_mod.user_failure([line, "CUDA out of memory"]) is None
    assert sep_mod.user_failure([]) is None


def test_the_worker_and_the_parent_agree_on_the_failure_line():
    from app.pipeline import demucs_worker

    text = str(demucs_worker.UserModelFailed(USER_A, "gates_missing"))
    assert sep_mod.user_failure([text]) == (USER_A, "gates_missing")
