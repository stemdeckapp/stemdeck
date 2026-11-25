"""separate() and the separation plan: the plan is resolved once per job when
separation starts, recorded on the job, sent to the worker, kept by the CPU
retry, and shown as one progress bar across the worker's passes.

The worker is a stub process (the _spawn_worker_cmd seam, as in
test_separate_fallback.py) that writes the request it was sent next to its
output, so what separate() really dispatched is what is checked.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
from pathlib import Path

import pytest

from app.core import settings
from app.core.models import Job
from app.pipeline import separate as sep_mod

SIX = "htdemucs_6s"
FT = "htdemucs_ft"
KIM = "kim_mel_roformer"
ALL_6S = {s: SIX for s in ("vocals", "drums", "bass", "guitar", "piano")}

# Writes each request to <job_dir>/request-<n>.json, waits for <job_dir>/go
# when the job folder's name holds "wait", then answers with the @@PHASE@@
# lines _phase_worker() gives it (none by default).
_ECHO_WORKER = """
import json, os, sys, time
sys.stderr.write("@@READY@@\\n")
sys.stderr.flush()
n = 0
for line in sys.stdin:
    req = json.loads(line)
    n += 1
    with open(os.path.join(req["job_dir"], "request-%d.json" % n), "w") as f:
        json.dump(req, f)
    if "wait" in os.path.basename(req["job_dir"]):
        while not os.path.exists(os.path.join(req["job_dir"], "go")):
            time.sleep(0.02)
    d = os.path.join(req["job_dir"], "htdemucs_6s", "source")
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "vocals.wav"), "wb").write(b"RIFF")
    for phase in req.get("phases_to_print", []):
        sys.stderr.write("@@PHASE@@" + phase + "\\n50%\\n100%\\n")
    sys.stderr.write("@@DONE@@\\n")
    sys.stderr.flush()
"""

# Fails the GPU attempt the way a CUDA OOM does; the CPU one echoes.
_FAIL_ON_GPU = """
import json, os, sys
sys.stderr.write("@@READY@@\\n")
sys.stderr.flush()
req = json.loads(sys.stdin.readline())
with open(os.path.join(req["job_dir"], "request-gpu.json"), "w") as f:
    json.dump(req, f)
sys.stderr.write("@@ERROR@@" + json.dumps("CUDA out of memory. Tried 2 GiB") + "\\n")
sys.stderr.flush()
sys.exit(1)
"""


@pytest.fixture(autouse=True)
def _reset_worker():
    sep_mod._worker.clear()
    yield
    sep_mod._kill_worker()


@pytest.fixture()
def echo(monkeypatch):
    monkeypatch.setattr(
        sep_mod, "_spawn_worker_cmd", lambda d: [sys.executable, "-c", _ECHO_WORKER]
    )
    monkeypatch.setattr(sep_mod, "get_demucs_device", lambda: "cpu")


def _requests(job_dir: Path) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(job_dir.glob("request-*.json"))]


def _job_dir(tmp_path: Path, name: str) -> Path:
    d = tmp_path / name
    d.mkdir()
    return d


def test_the_plan_is_resolved_recorded_and_sent(echo, tmp_path):
    job = Job(id="abcdefabc901")
    job_dir = _job_dir(tmp_path, "one")

    sep_mod.separate(job, tmp_path / "source.wav", job_dir)

    # Settings are all "auto"; on the CPU every stem resolves to htdemucs_6s.
    assert job.stem_models == ALL_6S
    assert _requests(job_dir)[0]["plan"] == ALL_6S


def test_a_given_plan_is_what_runs(echo, tmp_path):
    plan = {**ALL_6S, "bass": FT}
    job = Job(id="abcdefabc902")
    job_dir = _job_dir(tmp_path, "one")

    sep_mod.separate(job, tmp_path / "source.wav", job_dir, plan=plan)

    assert job.stem_models == plan and job.stem_models is not plan
    assert _requests(job_dir)[0]["plan"] == plan


def test_a_settings_change_during_a_job_waits_for_the_next_one(echo, tmp_path):
    """The running job keeps the plan it started with; the next job runs the
    new one. Settings are read once per job, when separation starts."""
    first = Job(id="abcdefabc903")
    first_dir = _job_dir(tmp_path, "first-wait")
    done = threading.Event()
    errors: list[BaseException] = []

    def run() -> None:
        try:
            sep_mod.separate(first, tmp_path / "source.wav", first_dir)
        except BaseException as e:  # noqa: BLE001 -- reported by the assert below
            errors.append(e)
        finally:
            done.set()

    threading.Thread(target=run, daemon=True).start()
    deadline = time.monotonic() + 30
    while not list(first_dir.glob("request-*.json")) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert first.stem_models == ALL_6S

    settings.set_stem_model_choices({"drums": FT})
    (first_dir / "go").write_text("")
    assert done.wait(30) and not errors, errors

    assert first.stem_models == ALL_6S
    assert _requests(first_dir)[0]["plan"] == ALL_6S

    second = Job(id="abcdefabc904")
    second_dir = _job_dir(tmp_path, "second")
    sep_mod.separate(second, tmp_path / "source.wav", second_dir)
    assert second.stem_models == {**ALL_6S, "drums": FT}
    assert _requests(second_dir)[0]["plan"] == {**ALL_6S, "drums": FT}


def test_a_new_separation_of_a_song_resolves_from_current_settings(echo, tmp_path):
    """Resplit and Extract after a stop run separate() with no plan: a job
    that already carries an older plan gets the current one."""
    job = Job(id="abcdefabc905", stem_models={**ALL_6S, "vocals": KIM})
    settings.set_stem_model_choices({"bass": FT})
    job_dir = _job_dir(tmp_path, "one")

    sep_mod.separate(job, tmp_path / "source.wav", job_dir)

    assert job.stem_models == {**ALL_6S, "bass": FT}
    assert _requests(job_dir)[0]["plan"] == {**ALL_6S, "bass": FT}


def test_the_cpu_retry_runs_a_given_plan_as_given(monkeypatch, tmp_path):
    plan = {**ALL_6S, "vocals": KIM}
    calls: list[str] = []

    def spawn(device: str) -> list[str]:
        calls.append(device)
        return [sys.executable, "-c", _FAIL_ON_GPU if device == "cuda" else _ECHO_WORKER]

    monkeypatch.setattr(sep_mod, "_spawn_worker_cmd", spawn)
    monkeypatch.setattr(sep_mod, "get_demucs_device", lambda: "cuda")
    # A fill's recorded plan: never re-resolved, whatever "auto" picks.
    monkeypatch.setattr(sep_mod, "resolve_stem_models", lambda device: dict(ALL_6S))
    job = Job(id="abcdefabc906")
    job_dir = _job_dir(tmp_path, "one")

    sep_mod.separate(job, tmp_path / "source.wav", job_dir, plan=plan)

    assert calls == ["cuda", "cpu"]
    assert job.gpu_fallback is True
    assert json.loads((job_dir / "request-gpu.json").read_text())["plan"] == plan
    assert _requests(job_dir)[0]["plan"] == plan
    assert job.stem_models == plan


def _record_stages(monkeypatch) -> list[tuple[str, float | None]]:
    seen: list[tuple[str, float | None]] = []
    real = sep_mod._set

    def record(job, **fields):
        if "stage" in fields:
            seen.append((fields["stage"], fields.get("progress")))
        real(job, **fields)

    monkeypatch.setattr(sep_mod, "_set", record)
    return seen


def _phase_worker(phases: list[str]) -> str:
    """The echo worker, told which @@PHASE@@ lines to print."""
    return _ECHO_WORKER.replace('req.get("phases_to_print", [])', repr(phases))


def test_passes_share_one_progress_bar_with_their_own_words(monkeypatch, tmp_path, caplog):
    plan = {**ALL_6S, "vocals": KIM, "bass": FT}
    stages = _record_stages(monkeypatch)
    worker = _phase_worker([KIM, SIX, FT])
    monkeypatch.setattr(sep_mod, "_spawn_worker_cmd", lambda d: [sys.executable, "-c", worker])
    monkeypatch.setattr(sep_mod, "get_demucs_device", lambda: "cpu")
    job = Job(id="abcdefabc907")

    with caplog.at_level(logging.INFO, logger="stemdeck.pipeline"):
        sep_mod.separate(job, tmp_path / "source.wav", _job_dir(tmp_path, "one"), plan=plan)

    shown = [(s, p) for s, p in stages if s.endswith("%")]
    assert [s for s, _ in shown] == [
        "Separating vocals 50%",
        "Separating vocals 100%",
        "Separating instruments 50%",
        "Separating instruments 100%",
        "Separating bass 50%",
        "Separating bass 100%",
    ]
    progress = [p for _, p in shown]
    assert progress == sorted(progress)
    assert progress[-1] == pytest.approx(1.0)
    # The vocal model is the slowest pass on the CPU, so it fills the most bar.
    assert progress[1] > 0.5
    assert "separate_startup" in (job.stage_timings or {})
    passes = [r.message for r in caplog.records if " pass " in r.message]
    assert [m.split(" pass ")[1].split(" ")[0] for m in passes] == [KIM, SIX, FT]
    assert all(" took " in m and " s on cpu" in m for m in passes)
    assert any("plan: vocals=kim_mel_roformer" in r.message for r in caplog.records)


def test_one_pass_still_reads_separating(monkeypatch, tmp_path):
    stages = _record_stages(monkeypatch)
    worker = _phase_worker([SIX])
    monkeypatch.setattr(sep_mod, "_spawn_worker_cmd", lambda d: [sys.executable, "-c", worker])
    monkeypatch.setattr(sep_mod, "get_demucs_device", lambda: "cpu")

    sep_mod.separate(Job(id="abcdefabc908"), tmp_path / "source.wav", _job_dir(tmp_path, "one"))

    assert [s for s, _ in stages if s.endswith("%")] == ["Separating 50%", "Separating 100%"]


_SCALE_WORKER = """
import json, os, sys
sys.stderr.write("@@READY@@\\n")
sys.stderr.flush()
for line in sys.stdin:
    req = json.loads(line)
    d = os.path.join(req["job_dir"], "htdemucs_6s", "source")
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "vocals.wav"), "wb").write(b"RIFF")
    sys.stderr.write("@@PHASE@@htdemucs_6s\\n100%\\n@@SCALE@@1.2500\\n@@DONE@@\\n")
    sys.stderr.flush()
"""


def test_a_shared_scale_above_one_is_logged(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(
        sep_mod, "_spawn_worker_cmd", lambda d: [sys.executable, "-c", _SCALE_WORKER]
    )
    monkeypatch.setattr(sep_mod, "get_demucs_device", lambda: "cpu")

    with caplog.at_level(logging.INFO, logger="stemdeck.pipeline"):
        sep_mod.separate(Job(id="abcdefabc909"), tmp_path / "source.wav", tmp_path)

    assert any("scaled down by 1.2500" in r.message for r in caplog.records)


# The parser: a vocal model download (its destination holds a "%"), then the
# vocal pass, then ft with four sweeps per pass. Every line in one stream.
_PARSER_WORKER = r"""
import json, os, sys
sys.stderr.write("@@READY@@\n")
sys.stderr.flush()
for line in sys.stdin:
    req = json.loads(line)
    d = os.path.join(req["job_dir"], "htdemucs_6s", "source")
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "vocals.wav"), "wb").write(b"RIFF")
    out = sys.stderr
    out.write("@@PHASE@@kim_mel_roformer\n")
    out.write('Downloading: "https://h/MelBandRoformer.ckpt" to /data/Stem 100%/models/m.ckpt\n')
    out.write(" 40%|####      |\r100%|##########|\n")
    out.write("@@SWEEPS@@1\n50%\n100%\n")
    out.write("@@PHASE@@htdemucs_6s\n@@SWEEPS@@1\n100%\n")
    out.write("@@PHASE@@htdemucs_ft\n@@SWEEPS@@4\n")
    for _ in range(4):
        out.write("  0%|          |\r 50%|#####     |\r100%|##########|\n")
    out.write("@@DONE@@\n")
    out.flush()
"""


def test_the_parser_reads_downloads_phases_and_sweeps_in_one_stream(monkeypatch, tmp_path):
    plan = {**ALL_6S, "vocals": KIM, "bass": FT}
    stages = _record_stages(monkeypatch)
    monkeypatch.setattr(
        sep_mod, "_spawn_worker_cmd", lambda d: [sys.executable, "-c", _PARSER_WORKER]
    )
    monkeypatch.setattr(sep_mod, "get_demucs_device", lambda: "cpu")
    job = Job(id="abcdefabc910")

    sep_mod.separate(job, tmp_path / "source.wav", _job_dir(tmp_path, "one"), plan=plan)

    shown = [s for s, _ in stages if s.endswith("%")]
    # A "%" in the destination is not progress: the line is the download's.
    assert shown[:2] == [
        "Downloading the separation model 40%",
        "Downloading the separation model 100%",
    ]
    assert shown[2:6] == [
        "Separating vocals 50%",
        "Separating vocals 100%",
        "Separating instruments 100%",
        "Separating bass 0%",
    ]
    # ft's four sweeps are one pass: its percentage only rises, to 100.
    bass = [int(s.split()[-1].rstrip("%")) for s in shown if s.startswith("Separating bass")]
    assert bass == [0, 12, 25, 25, 38, 50, 50, 62, 75, 75, 88, 100]
    progress = [p for s, p in stages if s.endswith("%") and not s.startswith("Downloading")]
    assert progress == sorted(progress) and progress[-1] == pytest.approx(1.0)


def test_a_download_line_with_a_percent_in_its_path_gets_the_short_stall_limit():
    """The download state is what the watchdog's short limit keys on."""
    line = 'Downloading: "https://h/m.ckpt" to /data/Stem 100%/models/m.ckpt'
    assert sep_mod._DOWNLOADING_RE.match(line) is not None
    assert sep_mod._PCT_RE.search(line) is not None  # why the order matters


def _retry_case(monkeypatch, choices: dict[str, str]):
    calls: list[str] = []

    def spawn(device: str) -> list[str]:
        calls.append(device)
        return [sys.executable, "-c", _FAIL_ON_GPU if device == "cuda" else _ECHO_WORKER]

    def resolve(device: str) -> dict[str, str]:
        on = {**ALL_6S, "vocals": KIM} if device == "cuda" else dict(ALL_6S)
        # An explicit choice holds on every device.
        return {s: (c if c != "auto" else on[s]) for s, c in choices.items()}

    monkeypatch.setattr(sep_mod, "_spawn_worker_cmd", spawn)
    monkeypatch.setattr(sep_mod, "get_demucs_device", lambda: "cuda")
    monkeypatch.setattr(sep_mod, "get_stem_model_choices", lambda: dict(choices))
    monkeypatch.setattr(sep_mod, "resolve_stem_models", resolve)
    return calls


def test_the_cpu_retry_gives_auto_stems_what_auto_picks_on_the_cpu(monkeypatch, tmp_path):
    """Kim on the CPU is 3.4 x real time; "auto" on the CPU is htdemucs_6s."""
    auto = {s: "auto" for s in ALL_6S}
    calls = _retry_case(monkeypatch, auto)
    job = Job(id="abcdefabc911")
    job_dir = _job_dir(tmp_path, "one")

    sep_mod.separate(job, tmp_path / "source.wav", job_dir)

    assert calls == ["cuda", "cpu"]
    assert json.loads((job_dir / "request-gpu.json").read_text())["plan"]["vocals"] == KIM
    assert _requests(job_dir)[0]["plan"] == ALL_6S
    assert job.stem_models == ALL_6S


def test_the_cpu_retry_keeps_a_model_chosen_in_settings(monkeypatch, tmp_path):
    chosen = {**{s: "auto" for s in ALL_6S}, "vocals": KIM}
    calls = _retry_case(monkeypatch, chosen)
    job = Job(id="abcdefabc912")
    job_dir = _job_dir(tmp_path, "one")

    sep_mod.separate(job, tmp_path / "source.wav", job_dir)

    assert calls == ["cuda", "cpu"]
    assert _requests(job_dir)[0]["plan"] == {**ALL_6S, "vocals": KIM}
    assert job.stem_models == {**ALL_6S, "vocals": KIM}
