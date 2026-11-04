"""scripts/bench_list_jobs.py builds 300 songs wherever it is pointed, so the
folder checks that keep it out of a real library are tested here (#976)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "bench_list_jobs.py"


@pytest.fixture(scope="module")
def bench():
    spec = importlib.util.spec_from_file_location("bench_list_jobs", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    return root


def _fake_real_library(folder: Path, *, with_registry: bool) -> Path:
    folder.mkdir(parents=True)
    song = folder / "0123456789ab"
    song.mkdir()
    (song / "metadata.json").write_text(json.dumps({"title": "A real song"}))
    if with_registry:
        (folder / "registry.json").write_text("{}")
    return folder


def test_a_new_folder_inside_the_repo_is_accepted(bench, repo):
    assert (
        bench.check_library(repo / "data" / "f49" / "jobs", repo, allow_outside_repo=False) is None
    )


def test_a_folder_outside_the_repo_is_refused_without_the_flag(bench, repo, tmp_path):
    refusal = bench.check_library(tmp_path / "nas" / "bench", repo, allow_outside_repo=False)
    assert refusal is not None and "outside the repository" in refusal


def test_a_new_folder_outside_the_repo_is_accepted_with_the_flag(bench, repo, tmp_path):
    assert bench.check_library(tmp_path / "nas" / "bench", repo, allow_outside_repo=True) is None


@pytest.mark.parametrize("with_registry", [True, False])
def test_a_new_folder_under_a_real_library_is_refused_even_with_the_flag(
    bench, repo, tmp_path, with_registry
):
    real = _fake_real_library(
        tmp_path / "Documents" / "StemDeck" / "jobs", with_registry=with_registry
    )
    refusal = bench.check_library(real / "bench", repo, allow_outside_repo=True)
    assert refusal is not None and "looks like a StemDeck library" in refusal


def test_a_real_library_inside_the_repo_is_refused_too(bench, repo):
    real = _fake_real_library(repo / "jobs", with_registry=True)
    refusal = bench.check_library(real / "bench", repo, allow_outside_repo=False)
    assert refusal is not None


def test_an_existing_folder_without_the_marker_is_refused(bench, repo, tmp_path):
    existing = tmp_path / "nas" / "music"
    existing.mkdir(parents=True)
    refusal = bench.check_library(existing, repo, allow_outside_repo=True)
    assert refusal is not None and "not a library this script made" in refusal


def test_a_real_library_itself_is_refused(bench, repo, tmp_path):
    real = _fake_real_library(tmp_path / "jobs", with_registry=True)
    assert bench.check_library(real, repo, allow_outside_repo=True) is not None


def test_a_marked_folder_with_a_registry_is_refused(bench, repo):
    marked = repo / "data" / "f49" / "jobs"
    marked.mkdir(parents=True)
    (marked / bench.MARKER).write_text("x")
    assert bench.check_library(marked, repo, allow_outside_repo=False) is None
    (marked / "registry.json").write_text("{}")
    assert bench.check_library(marked, repo, allow_outside_repo=False) is not None


def test_the_app_data_folder_is_always_under_the_repo(bench, repo):
    data = bench.data_dir_for(repo)
    assert data.is_relative_to(repo.resolve())
    assert data.parent.name == "data"
