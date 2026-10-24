"""Whether Lead + Backing can be made on this install (#832).

A failed setup warmup used to reach setup.log and nowhere else. It is now
remembered, and /api/health says so before anyone picks Lead + Backing.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.core.vocal_split_outcome import (
    DEPENDENCY_MISSING,
    MODEL_UNAVAILABLE,
    OUT_OF_MEMORY,
    WORKER_FAILED,
)
from app.pipeline import vocal_split_status as vss


@pytest.mark.parametrize(
    ("phase", "exc", "code"),
    [
        ("import", ImportError("No module named 'torchvision'"), DEPENDENCY_MISSING),
        ("import", RuntimeError("anything at all"), DEPENDENCY_MISSING),
        ("load", OSError("libcudart.so.12: cannot open shared object file"), DEPENDENCY_MISSING),
        ("load", RuntimeError("operator torchvision::nms does not exist"), DEPENDENCY_MISSING),
        (
            "load",
            RuntimeError("Failed to download file from x, response code: 404"),
            MODEL_UNAVAILABLE,
        ),
        ("load", ValueError("Unsupported Model File: parameters for MD5 hash"), MODEL_UNAVAILABLE),
        ("separate", MemoryError(), OUT_OF_MEMORY),
        ("separate", RuntimeError("MPS backend out of memory"), OUT_OF_MEMORY),
        ("separate", ValueError("bad shape"), WORKER_FAILED),
        ("separate", ConnectionError("Max retries exceeded"), MODEL_UNAVAILABLE),
    ],
)
def test_failure_code(phase, exc, code):
    assert vss.failure_code(phase, exc) == code


def test_failure_code_from_text_alone():
    assert vss.failure_code("unknown", text="") == WORKER_FAILED
    assert vss.failure_code("unknown", text="No module named 'onnx2torch'") == DEPENDENCY_MISSING
    assert vss.failure_code("unknown", text="CUDA out of memory") == OUT_OF_MEMORY


def test_nothing_recorded_reads_as_available():
    assert vss.capability() == {"available": True, "reason": None}


def test_a_missing_dependency_makes_it_unavailable_and_a_success_clears_it():
    vss.record(False, DEPENDENCY_MISSING, source="warmup")
    assert vss.capability() == {"available": False, "reason": DEPENDENCY_MISSING}

    vss.record(True, source="split")
    assert vss.capability() == {"available": True, "reason": None}


def test_the_record_survives_a_restart():
    vss.record(False, DEPENDENCY_MISSING, source="warmup")
    vss.reset_state()  # a new process reads the file
    assert vss.capability()["available"] is False


def test_a_model_that_did_not_download_does_not_switch_it_off():
    """The next attempt can still download it."""
    vss.record(False, MODEL_UNAVAILABLE, source="warmup")
    assert vss.capability()["available"] is True


@pytest.mark.parametrize("code", [OUT_OF_MEMORY, WORKER_FAILED, "timeout", "cancelled"])
def test_a_failure_of_one_run_leaves_the_record_alone(code):
    vss.record(False, DEPENDENCY_MISSING, source="warmup")
    vss.record(False, code, source="split")
    assert vss.capability()["reason"] == DEPENDENCY_MISSING


def test_a_record_from_other_packages_is_ignored(monkeypatch):
    """The startup repair (#778) swaps torchvision for the right build: the
    failure it fixed must not keep Lead + Backing switched off."""
    vss.record(False, DEPENDENCY_MISSING, source="warmup")
    data = json.loads(vss._status_path.read_text(encoding="utf-8"))
    data["packages"]["torchvision"] = "0.21.0+cu124"
    vss._status_path.write_text(json.dumps(data), encoding="utf-8")
    vss.reset_state()

    assert vss.capability() == {"available": True, "reason": None}


def test_an_unreadable_record_reads_as_available():
    vss._status_path.write_text("{not json", encoding="utf-8")
    assert vss.capability()["available"] is True


def test_health_reports_the_capability():
    from app.main import app

    with TestClient(app) as client:
        assert client.get("/api/health").json()["vocal_split"] == {
            "available": True,
            "reason": None,
        }
        vss.record(False, DEPENDENCY_MISSING, source="warmup")
        body = client.get("/api/health").json()
    assert body["vocal_split"] == {"available": False, "reason": DEPENDENCY_MISSING}
    # Booleans and a code: no package names, versions or paths.
    assert set(body["vocal_split"]) == {"available", "reason"}


def test_a_failed_warmup_is_remembered(monkeypatch, capsys):
    from app.pipeline import warmup

    def broken():
        raise ImportError("No module named 'torchvision'")

    monkeypatch.setattr(warmup, "_STEPS", (("vocal_split", broken),))
    assert warmup.main() == 0

    assert "WARMUP_FAILED vocal_split" in capsys.readouterr().out
    vss.reset_state()
    assert vss.capability() == {"available": False, "reason": DEPENDENCY_MISSING}


def test_a_good_warmup_clears_an_old_failure(monkeypatch):
    from app.pipeline import warmup

    vss.record(False, DEPENDENCY_MISSING, source="warmup")
    monkeypatch.setattr(warmup, "_STEPS", (("vocal_split", lambda: None),))
    warmup.main()

    vss.reset_state()
    assert vss.capability()["available"] is True
