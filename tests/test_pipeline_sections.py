from __future__ import annotations

import math
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from app.core.models import Job, JobCancelled
from app.pipeline.section_refine import refine_segments
from app.pipeline.sections import normalize_sections, sweep_orphaned_workspaces

MODEL_LABELS = (
    "start",
    "end",
    "intro",
    "outro",
    "break",
    "bridge",
    "inst",
    "solo",
    "verse",
    "chorus",
)


def _refinement_evidence(seconds: int, fps: int = 2):
    frames = seconds * fps
    activations = {
        "segment": np.zeros(frames, dtype=float),
        "label": np.zeros((len(MODEL_LABELS), frames), dtype=float),
    }
    embeddings = np.zeros((4, frames, 2), dtype=float)
    return activations, embeddings, fps


def _set_label(activations, label: str, start: int, end: int, fps: int, value: float = 0.9):
    activations["label"][MODEL_LABELS.index(label), start * fps : end * fps] = value


def test_normalize_sections_produces_gap_free_deterministic_records():
    raw = {
        "segments": [
            {"start": 0.0, "end": 0.4, "label": "start"},
            {"start": 0.4, "end": 12.0, "label": "intro"},
            {"start": 12.0, "end": 36.0, "label": "verse"},
            {"start": 36.0, "end": 60.0, "label": "chorus"},
            {"start": 60.0, "end": 84.0, "label": "verse"},
            {"start": 84.0, "end": 99.6, "label": "chorus"},
            {"start": 99.6, "end": 100.0, "label": "end"},
        ]
    }

    sections = normalize_sections(raw, 100.0)

    assert [s["id"] for s in sections] == [
        "auto-001",
        "auto-002",
        "auto-003",
        "auto-004",
        "auto-005",
    ]
    assert [s["kind"] for s in sections] == ["intro", "verse", "chorus", "verse", "chorus"]
    assert sections[0]["start"] == 0.0
    assert sections[-1]["end"] == 100.0
    assert all(
        left["end"] == right["start"] for left, right in zip(sections, sections[1:], strict=False)
    )
    assert sections[1]["color"] == sections[3]["color"]
    assert sections[2]["color"] == sections[4]["color"]


def test_normalize_sections_preserves_model_boundaries_and_merges_only_short_fragments():
    raw = [
        {"start": 0.0, "end": 8.0, "label": "intro"},
        {"start": 8.0, "end": 20.0, "label": "verse"},
        {"start": 20.0, "end": 20.3, "label": "break"},
        {"start": 20.3, "end": 32.0, "label": "verse"},
        {"start": 32.0, "end": 48.0, "label": "chorus"},
        {"start": 48.0, "end": 64.0, "label": "chorus"},
    ]

    sections = normalize_sections(raw, 64.0)

    assert [(s["kind"], s["start"], s["end"]) for s in sections] == [
        ("intro", 0.0, 8.0),
        ("verse", 8.0, 32.0),
        ("chorus", 32.0, 48.0),
        ("chorus", 48.0, 64.0),
    ]


def test_normalize_sections_accepts_a_neutral_low_confidence_part():
    raw = [
        {"start": 0.0, "end": 12.0, "label": "verse"},
        {"start": 12.0, "end": 24.0, "label": "part"},
    ]

    sections = normalize_sections(raw, 24.0)

    assert [section["kind"] for section in sections] == ["verse", "part"]
    assert sections[1]["name"] == "Part"


def test_normalize_sections_keeps_a_song_whose_model_output_ends_past_the_duration():
    """duration_sec is rounded; the model reads the stems and overhangs it.

    Observed on a real 484-second track, which emitted a 10 ms "start" span
    after its final section. Clamping collapsed that span to zero length and
    the whole song lost every section.
    """
    raw = {
        "segments": [
            {"start": 0.0, "end": 0.53, "label": "start"},
            {"start": 0.53, "end": 30.45, "label": "verse"},
            {"start": 30.45, "end": 74.04, "label": "chorus"},
            {"start": 74.04, "end": 484.41, "label": "verse"},
            {"start": 484.41, "end": 484.42, "label": "start"},
        ]
    }

    sections = normalize_sections(raw, 484)

    assert [section["kind"] for section in sections] == ["verse", "chorus", "verse"]
    assert sections[0]["start"] == 0.0
    assert sections[-1]["end"] == 484


def test_normalize_sections_neutralizes_a_sentinel_predicted_mid_song():
    """``start`` and ``end`` are ordinary classes, not only bracket markers.

    A real track was labelled ``start`` for 34 seconds in its middle. That is
    the model failing to name a real span, which is what ``part`` is for.
    """
    raw = {
        "segments": [
            {"start": 0.0, "end": 12.0, "label": "verse"},
            {"start": 12.0, "end": 46.0, "label": "start"},
            {"start": 46.0, "end": 60.0, "label": "chorus"},
        ]
    }

    sections = normalize_sections(raw, 60.0)

    assert [section["kind"] for section in sections] == ["verse", "part", "chorus"]
    assert sections[1]["name"] == "Part"


def test_refinement_preserves_adjacent_equal_label_boundaries():
    raw = [
        {"start": 0.0, "end": 12.0, "label": "verse"},
        {"start": 12.0, "end": 24.0, "label": "verse"},
        {"start": 24.0, "end": 36.0, "label": "chorus"},
    ]
    activations, embeddings, fps = _refinement_evidence(36)
    _set_label(activations, "verse", 0, 24, fps)
    _set_label(activations, "chorus", 24, 36, fps)

    refined = refine_segments(raw, activations, embeddings, fps, None, MODEL_LABELS)

    assert [(item["start"], item["end"], item["label"]) for item in refined] == [
        (0.0, 12.0, "verse"),
        (12.0, 24.0, "verse"),
        (24.0, 36.0, "chorus"),
    ]


def test_refinement_adds_only_an_activation_peak_with_embedding_novelty():
    raw = [
        {"start": 0.0, "end": 24.0, "label": "intro"},
        {"start": 24.0, "end": 48.0, "label": "verse"},
    ]
    activations, embeddings, fps = _refinement_evidence(48)
    _set_label(activations, "intro", 0, 24, fps)
    _set_label(activations, "verse", 24, 48, fps)
    activations["segment"][12 * fps] = 0.8
    embeddings[:, : 12 * fps] = -1.0
    embeddings[:, 12 * fps : 24 * fps] = 1.0
    embeddings[:, 24 * fps : 36 * fps] = -1.0
    embeddings[:, 36 * fps :] = 1.0
    grid = {"confidence": 90, "beats": [index / fps for index in range(48 * fps)]}

    refined = refine_segments(raw, activations, embeddings, fps, grid, MODEL_LABELS)

    assert [item["start"] for item in refined] == [0.0, 12.0, 24.0]
    assert [item["label"] for item in refined] == ["intro", "intro", "verse"]


def test_refinement_finds_a_boundary_without_a_trustworthy_beat_grid():
    """A beat grid aligns candidates; it must never gate whether they are found.

    Rubato, live, and free-time material is where the upstream spans most need
    splitting, and it is exactly the material whose grid confidence is lowest.
    """
    raw = [
        {"start": 0.0, "end": 24.0, "label": "intro"},
        {"start": 24.0, "end": 48.0, "label": "verse"},
    ]

    def refined_for(grid):
        activations, embeddings, fps = _refinement_evidence(48)
        _set_label(activations, "intro", 0, 24, fps)
        _set_label(activations, "verse", 24, 48, fps)
        activations["segment"][12 * fps] = 0.8
        embeddings[:, : 12 * fps] = -1.0
        embeddings[:, 12 * fps : 24 * fps] = 1.0
        embeddings[:, 24 * fps : 36 * fps] = -1.0
        embeddings[:, 36 * fps :] = 1.0
        return refine_segments(raw, activations, embeddings, fps, grid, MODEL_LABELS)

    beats = [index / 2 for index in range(96)]
    for grid in (None, {"confidence": 20, "beats": beats}, {"confidence": 90, "beats": beats}):
        result = refined_for(grid)
        assert [item["start"] for item in result] == [0.0, 12.0, 24.0], grid
        assert [item["label"] for item in result] == ["intro", "intro", "verse"], grid


def test_refinement_does_not_turn_a_beat_alone_into_a_boundary():
    raw = [
        {"start": 0.0, "end": 24.0, "label": "intro"},
        {"start": 24.0, "end": 48.0, "label": "verse"},
    ]
    activations, embeddings, fps = _refinement_evidence(48)
    _set_label(activations, "intro", 0, 24, fps)
    _set_label(activations, "verse", 24, 48, fps)
    embeddings[:, : 12 * fps] = -1.0
    embeddings[:, 12 * fps :] = 1.0
    grid = {"confidence": 90, "beats": [index / fps for index in range(48 * fps)]}

    refined = refine_segments(raw, activations, embeddings, fps, grid, MODEL_LABELS)

    assert [item["start"] for item in refined] == [0.0, 24.0]


def test_refinement_snaps_only_to_a_trustworthy_nearby_beat():
    raw = [
        {"start": 0.0, "end": 12.05, "label": "verse"},
        {"start": 12.05, "end": 24.0, "label": "chorus"},
    ]
    activations, embeddings, fps = _refinement_evidence(24, fps=20)
    _set_label(activations, "verse", 0, 12, fps)
    _set_label(activations, "chorus", 12, 24, fps)

    trusted = refine_segments(
        raw,
        activations,
        embeddings,
        fps,
        {"confidence": 90, "beats": [float(index) for index in range(25)]},
        MODEL_LABELS,
    )
    untrusted = refine_segments(
        raw,
        activations,
        embeddings,
        fps,
        {"confidence": 20, "beats": [float(index) for index in range(25)]},
        MODEL_LABELS,
    )

    assert trusted[0]["end"] == trusted[1]["start"] == 12.0
    assert untrusted[0]["end"] == untrusted[1]["start"] == 12.05


def test_refinement_uses_part_for_an_ambiguous_semantic_label():
    raw = [
        {"start": 0.0, "end": 12.0, "label": "verse"},
        {"start": 12.0, "end": 24.0, "label": "chorus"},
    ]
    activations, embeddings, fps = _refinement_evidence(24)
    _set_label(activations, "verse", 0, 12, fps, 0.52)
    _set_label(activations, "chorus", 0, 12, fps, 0.48)
    _set_label(activations, "chorus", 12, 24, fps)

    refined = refine_segments(raw, activations, embeddings, fps, None, MODEL_LABELS)

    assert [item["label"] for item in refined] == ["part", "chorus"]


def test_refinement_regularizes_a_neutral_repeated_region():
    raw = [
        {"start": 0.0, "end": 8.0, "label": "chorus"},
        {"start": 8.0, "end": 16.0, "label": "verse"},
        {"start": 16.0, "end": 24.0, "label": "verse"},
        {"start": 24.0, "end": 32.0, "label": "outro"},
    ]
    activations, embeddings, fps = _refinement_evidence(32)
    _set_label(activations, "chorus", 0, 8, fps)
    _set_label(activations, "verse", 8, 16, fps)
    _set_label(activations, "verse", 16, 24, fps, 0.52)
    _set_label(activations, "chorus", 16, 24, fps, 0.48)
    _set_label(activations, "outro", 24, 32, fps)
    embeddings[:, 0 : 8 * fps, 0] = 2.0
    embeddings[:, 8 * fps : 16 * fps, 1] = 2.0
    embeddings[:, 16 * fps : 24 * fps, 0] = 2.0
    embeddings[:, 24 * fps :, :] = -2.0

    refined = refine_segments(raw, activations, embeddings, fps, None, MODEL_LABELS)

    assert refined[2]["label"] == "chorus"


def test_refinement_falls_back_when_evidence_is_malformed():
    raw = [
        {"start": 0.0, "end": 12.0, "label": "verse"},
        {"start": 12.0, "end": 24.0, "label": "chorus"},
    ]

    assert refine_segments(raw, {}, None, 100.0, None, MODEL_LABELS) == raw


@pytest.mark.parametrize(
    "raw",
    [
        [{"start": 0.0, "end": 10.0, "label": "verse"}],
        [
            {"start": 0.0, "end": 10.0, "label": "verse"},
            {"start": 9.0, "end": 20.0, "label": "chorus"},
        ],
        [
            {"start": 0.0, "end": 10.0, "label": "verse"},
            {"start": 12.0, "end": 20.0, "label": "unknown"},
            {"start": 20.0, "end": 30.0, "label": "chorus"},
        ],
        [
            {"start": 0.0, "end": math.nan, "label": "verse"},
            {"start": 10.0, "end": 20.0, "label": "chorus"},
        ],
        [
            {"start": 10.0, "end": 0.0, "label": "verse"},
            {"start": 10.0, "end": 20.0, "label": "chorus"},
        ],
    ],
)
def test_normalize_sections_rejects_untrustworthy_output(raw):
    assert normalize_sections(raw, 30.0) == []


def test_normalize_sections_accepts_small_rounding_gaps_at_a_shared_boundary():
    raw = [
        {"start": 0.0, "end": 12.0, "label": "intro"},
        {"start": 12.08, "end": 30.0, "label": "verse"},
        {"start": 30.0, "end": 45.0, "label": "chorus"},
    ]

    sections = normalize_sections(raw, 45.0)

    assert sections[0]["end"] == sections[1]["start"] == 12.04


def test_detect_sections_skips_when_required_stems_are_missing(tmp_path: Path, monkeypatch):
    from app.pipeline import sections as module

    stems_dir = tmp_path / "stems"
    stems_dir.mkdir()
    (stems_dir / "vocals.wav").write_bytes(b"RIFF")
    called = False

    def unexpected(*_args, **_kwargs):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(module, "_run_worker", unexpected)

    assert module.detect_sections(Job(id="abcdefabcdef"), stems_dir, 60.0) is None
    assert called is False


def test_detect_sections_cleans_temporary_other_mix(tmp_path: Path, monkeypatch):
    from app.pipeline import sections as module

    stems_dir = tmp_path / "stems"
    stems_dir.mkdir()
    for name in ("vocals", "drums", "bass", "guitar", "piano", "other"):
        (stems_dir / f"{name}.wav").write_bytes(b"RIFF")
    (stems_dir / "beats.json").write_text('{"confidence":90,"beats":[0,1]}', encoding="utf-8")
    workspace = None

    def fake_mix(_job, _stems_dir, work_dir):
        nonlocal workspace
        workspace = work_dir
        temp_other = work_dir / "other.wav"
        temp_other.write_bytes(b"RIFF-float")
        return temp_other

    monkeypatch.setattr(module, "_mix_other_stems", fake_mix)

    def fake_worker(_job, work_dir):
        assert (work_dir / "beats.json").read_text(encoding="utf-8") == (
            '{"confidence":90,"beats":[0,1]}'
        )
        return {
            "segments": [
                {"start": 0.0, "end": 20.0, "label": "verse"},
                {"start": 20.0, "end": 40.0, "label": "chorus"},
            ]
        }

    monkeypatch.setattr(module, "_run_worker", fake_worker)

    sections = module.detect_sections(Job(id="abcdefabcdef"), stems_dir, 40.0)

    assert sections is not None
    assert [section["kind"] for section in sections] == ["verse", "chorus"]
    assert workspace is not None
    assert not workspace.exists()


def test_detect_sections_preserves_cancellation(tmp_path: Path):
    from app.pipeline import sections as module

    job = Job(id="abcdefabcdef", cancel_requested=True)

    with pytest.raises(JobCancelled):
        module.detect_sections(job, tmp_path, 60.0)


def test_registered_process_honors_inflight_cancellation():
    from app.pipeline import sections as module

    job = Job(id="abcdefabc116")

    def cancel_soon():
        time.sleep(0.2)
        job.cancel_requested = True

    thread = threading.Thread(target=cancel_soon)
    thread.start()
    try:
        with pytest.raises(JobCancelled):
            module._run_registered_process(
                job,
                [sys.executable, "-c", "import time; time.sleep(30)"],
            )
    finally:
        thread.join(timeout=2)


def test_registered_process_enforces_total_timeout(monkeypatch):
    from app.pipeline import sections as module

    monkeypatch.setattr(module, "TIMEOUT_SECTIONS", 0.2)
    monkeypatch.setattr(module, "TIMEOUT_SECTIONS_STALL", 30)
    started = time.monotonic()

    returncode, _stdout, _stderr = module._run_registered_process(
        Job(id="abcdefabc117"),
        [sys.executable, "-c", "import time; time.sleep(30)"],
    )

    assert returncode != 0
    assert time.monotonic() - started < 5


def test_mix_other_stems_uses_all_sources_and_float_output(tmp_path: Path, monkeypatch):
    from app.pipeline import sections as module

    stems_dir = tmp_path / "stems"
    work_dir = stems_dir / ".sections-work-test"
    work_dir.mkdir(parents=True)
    for name in ("other", "guitar", "piano"):
        (stems_dir / f"{name}.wav").write_bytes(b"RIFF")
    captured = []

    def fake_process(_job, cmd):
        captured.extend(cmd)
        (work_dir / "other.wav").write_bytes(b"RIFFDATA")
        return 0, [], []

    monkeypatch.setattr(module, "_run_registered_process", fake_process)
    output = module._mix_other_stems(Job(id="abcdefabc118"), stems_dir, work_dir)

    assert output == work_dir / "other.wav"
    assert any("amix=inputs=3:normalize=0:duration=longest" in arg for arg in captured)
    assert "pcm_f32le" in captured
    assert all(str(stems_dir / f"{name}.wav") in captured for name in ("other", "guitar", "piano"))


def test_sweep_removes_a_workspace_a_dead_process_left_behind(tmp_path: Path):
    """A force quit bypasses detect_sections' finally, and nothing else in the
    codebase has ever heard of the prefix. What is stranded is a pcm_f32le mix
    of three stems: about 1.27 GB for a 60-minute track, hidden behind a dot
    (#483)."""
    jobs_dir = tmp_path / "jobs"
    stems_dir = jobs_dir / "abcdefabcdef" / "stems"
    stems_dir.mkdir(parents=True)
    orphan = stems_dir / ".sections-work-dead"
    orphan.mkdir()
    (orphan / "other.wav").write_bytes(b"x" * 1024)
    keeper = stems_dir / "drums.wav"
    keeper.write_bytes(b"audio")

    removed = sweep_orphaned_workspaces(jobs_dir)

    assert removed == 1
    assert not orphan.exists()
    assert keeper.is_file(), "a real stem was deleted"


def test_sweep_leaves_everything_that_is_not_a_workspace(tmp_path: Path):
    """It runs against the user's library, so the prefix and the parent are
    both load-bearing. Anything else in a stems folder must survive."""
    jobs_dir = tmp_path / "jobs"
    stems_dir = jobs_dir / "abcdefabcdef" / "stems"
    stems_dir.mkdir(parents=True)
    survivors = [
        stems_dir / "htdemucs_6s",  # a real demucs output directory
        stems_dir / ".cache",  # dot-prefixed, but not ours
        stems_dir / "sections-work-no-dot",  # close, but missing the leading dot
    ]
    for path in survivors:
        path.mkdir()
        (path / "keep.wav").write_bytes(b"x")

    assert sweep_orphaned_workspaces(jobs_dir) == 0
    for path in survivors:
        assert (path / "keep.wav").is_file(), f"{path.name} was deleted"


def test_sweep_survives_a_library_it_cannot_read(tmp_path: Path):
    """Tidying up must never be the thing that breaks startup."""
    assert sweep_orphaned_workspaces(tmp_path / "does-not-exist") == 0
