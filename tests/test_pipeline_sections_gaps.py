"""Section-analysis branches the existing suite does not reach.

test_pipeline_sections.py covers normalize_sections well from the outside, but
three things underneath it had no coverage: the short-fragment merge's two
asymmetric cases, the rejections that keep untrusted model output from becoming
section records, and _run_worker's reading of what the worker actually said.

_run_worker matters most. Everything it can be handed -- a non-zero exit, no
output line, several output lines, a line that is not JSON -- has to become
"no sections" rather than an exception, because the sections stage is the last
thing the pipeline does and a raise there fails a job whose stems are already
finished and correct.
"""

from __future__ import annotations

import json

import pytest

from app.core.models import Job
from app.pipeline import sections as sec


def _seg(start, end, label="verse"):
    return {"start": start, "end": end, "label": label}


# --------------------------------------------------------------------------
# the short-fragment merge
# --------------------------------------------------------------------------


def test_a_short_fragment_between_matching_neighbours_dissolves_into_them():
    """The model often emits a one-frame sliver inside a repeated chorus.
    Both the sliver and the duplicate boundary it created are absorbed, so the
    two chorus halves come back as one section."""
    out = sec.normalize_sections(
        [
            _seg(0, 30, "chorus"),
            _seg(30, 30.2, "verse"),
            _seg(30.2, 50, "chorus"),
            _seg(50, 60, "verse"),
        ],
        60,
    )

    assert [s["kind"] for s in out] == ["chorus", "verse"]
    assert out[0]["start"] == 0
    assert out[0]["end"] == pytest.approx(50, abs=0.2)


def test_a_merge_that_leaves_only_one_section_is_refused():
    """A single span covering the whole song is not an analysis, so the merge
    collapsing everything into one is treated as no result rather than as a
    one-section track."""
    out = sec.normalize_sections(
        [_seg(0, 30, "chorus"), _seg(30, 30.2, "verse"), _seg(30.2, 60, "chorus")],
        60,
    )

    assert out == []


def test_a_short_fragment_with_differing_neighbours_joins_the_one_before_it():
    out = sec.normalize_sections(
        [_seg(0, 30, "verse"), _seg(30, 30.2, "break"), _seg(30.2, 60, "chorus")],
        60,
    )

    assert [s["kind"] for s in out] == ["verse", "chorus"]
    # The sliver's time is absorbed rather than dropped: sections stay gap-free.
    assert out[0]["end"] == pytest.approx(out[1]["start"])


def test_a_short_fragment_at_the_very_start_joins_the_one_after_it():
    """There is no preceding section to extend, so the merge has to go the
    other way -- and the track must still start at zero."""
    out = sec.normalize_sections(
        [_seg(0, 0.2, "intro"), _seg(0.2, 30, "verse"), _seg(30, 60, "chorus")],
        60,
    )

    assert [s["kind"] for s in out] == ["verse", "chorus"]
    assert out[0]["start"] == 0


def test_output_that_is_nothing_but_slivers_is_refused_entirely():
    """Merging down to one or two sections that are still too short means the
    model produced nothing usable; a two-section "analysis" of a whole song is
    worse than none."""
    assert sec.normalize_sections([_seg(0, 0.1, "verse"), _seg(0.1, 0.2, "chorus")], 60) == []


def test_a_track_too_short_to_hold_two_sections_is_refused():
    assert sec.normalize_sections([_seg(0, 0.4, "verse"), _seg(0.4, 0.8, "chorus")], 0.8) == []


# --------------------------------------------------------------------------
# rejecting untrusted model output
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, why",
    [
        ([_seg(0, 30), "not-a-dict"], "an entry that is not an object"),
        ([_seg(0, 30), {"start": 30, "end": 60, "label": 7}], "a label that is not a string"),
        ([_seg(0, 30), {"start": 30, "end": 60, "label": "kazoo-solo"}], "an unknown label"),
        ([_seg(0, 30), {"start": 30, "end": 60}], "an entry with no label at all"),
    ],
)
def test_one_bad_entry_rejects_the_whole_analysis(raw, why):
    """Partial acceptance would leave a gap in the middle of the track that the
    editor renders as a section with no name. All or nothing is the contract."""
    assert sec.normalize_sections(raw, 60) == [], why


def test_a_label_is_matched_case_and_space_insensitively():
    out = sec.normalize_sections([_seg(0, 30, "  Chorus  "), _seg(30, 60, "VERSE")], 60)

    assert [s["kind"] for s in out] == ["chorus", "verse"]


def test_a_duration_that_is_not_a_number_yields_nothing():
    assert sec.normalize_sections([_seg(0, 30), _seg(30, 60)], "sixty") == []
    assert sec.normalize_sections([_seg(0, 30), _seg(30, 60)], None) == []


# --------------------------------------------------------------------------
# _run_worker -- reading what the worker said
# --------------------------------------------------------------------------


@pytest.fixture
def job():
    return Job(id="abcdefabcdef")


def _fake_run(monkeypatch, returncode, stdout, stderr=()):
    seen = {}

    def _run(job, cmd):
        seen["cmd"] = cmd
        return returncode, list(stdout), list(stderr)

    monkeypatch.setattr(sec, "_run_registered_process", _run)
    return seen


def test_a_worker_that_answers_cleanly_is_parsed(job, tmp_path, monkeypatch):
    payload = {"segments": [{"start": 0.0, "end": 9.0, "label": "intro"}]}
    _fake_run(monkeypatch, 0, [json.dumps(payload)])

    assert sec._run_worker(job, tmp_path) == payload


def test_a_worker_that_exits_non_zero_yields_no_sections(job, tmp_path, monkeypatch, caplog):
    """The stems are already finished by this point. A failed section pass
    costs the song-structure lane, never the job."""
    _fake_run(monkeypatch, 1, [], ["Traceback...", "RuntimeError: boom"])

    assert sec._run_worker(job, tmp_path) is None
    assert "section model failed" in caplog.text
    # The diagnostic reaches the log, or there is nothing to debug from.
    assert "RuntimeError: boom" in caplog.text


def test_heartbeats_are_not_mistaken_for_diagnostics(job, tmp_path, monkeypatch, caplog):
    """The worker prints a heartbeat every 10 s so a slow CPU pass is not taken
    for a hang. On failure those lines would crowd the real error out of the
    last five."""
    _fake_run(
        monkeypatch,
        1,
        [],
        [f"{sec._HEARTBEAT_PREFIX}"] * 20 + ["ValueError: the actual problem"],
    )

    sec._run_worker(job, tmp_path)

    assert "ValueError: the actual problem" in caplog.text
    assert sec._HEARTBEAT_PREFIX not in caplog.text


def test_a_worker_that_said_nothing_yields_no_sections(job, tmp_path, monkeypatch, caplog):
    _fake_run(monkeypatch, 0, [])

    assert sec._run_worker(job, tmp_path) is None
    assert "unexpected output" in caplog.text


def test_a_worker_that_said_too_much_yields_no_sections(job, tmp_path, monkeypatch, caplog):
    """Exactly one compact JSON line is the contract. More than one means
    something else reached stdout, and guessing which line is the data is how a
    diagnostic gets parsed as section records."""
    _fake_run(monkeypatch, 0, ['{"segments": []}', "some stray output"])

    assert sec._run_worker(job, tmp_path) is None
    assert "unexpected output" in caplog.text


def test_a_worker_line_that_is_not_json_yields_no_sections(job, tmp_path, monkeypatch, caplog):
    _fake_run(monkeypatch, 0, ["not json at all"])

    assert sec._run_worker(job, tmp_path) is None
    assert "invalid JSON" in caplog.text


def test_the_worker_is_told_about_a_beat_grid_when_there_is_one(job, tmp_path, monkeypatch):
    """Refinement snaps section boundaries to beats; without the grid the
    worker silently produces less accurate boundaries."""
    (tmp_path / "beats.json").write_text(json.dumps({"beats": [0.5]}), encoding="utf-8")
    seen = _fake_run(monkeypatch, 0, ['{"segments": []}'])

    sec._run_worker(job, tmp_path)

    assert "--beat-grid" in seen["cmd"]
    assert str(tmp_path / "beats.json") in seen["cmd"]


def test_no_beat_grid_argument_when_there_is_no_grid(job, tmp_path, monkeypatch):
    seen = _fake_run(monkeypatch, 0, ['{"segments": []}'])

    sec._run_worker(job, tmp_path)

    assert "--beat-grid" not in seen["cmd"]


def test_the_worker_is_invoked_as_a_module_with_the_job_identity(job, tmp_path, monkeypatch):
    seen = _fake_run(monkeypatch, 0, ['{"segments": []}'])

    sec._run_worker(job, tmp_path)

    cmd = seen["cmd"]
    assert cmd[1:3] == ["-m", "app.pipeline.section_worker"]
    assert job.id in cmd
    assert str(tmp_path) in cmd


# --------------------------------------------------------------------------
# _mix_other_stems
# --------------------------------------------------------------------------


def test_the_section_mix_is_skipped_when_a_source_stem_is_missing(job, tmp_path):
    """All three go into one input for the model. A missing one is a job that
    separated with fewer stems, not an error."""
    stems = tmp_path / "stems"
    stems.mkdir()
    (stems / "other.wav").write_bytes(b"RIFF")
    work = tmp_path / "work"
    work.mkdir()

    assert sec._mix_other_stems(job, stems, work) is None


def test_a_failed_section_mix_cleans_up_and_reports(job, tmp_path, monkeypatch, caplog):
    """A zero-byte output would be handed to the model as if it were audio."""
    stems = tmp_path / "stems"
    stems.mkdir()
    for name in ("other", "guitar", "piano"):
        (stems / f"{name}.wav").write_bytes(b"RIFF")
    work = tmp_path / "work"
    work.mkdir()

    def _run(job_, cmd):
        # ffmpeg "succeeded" but produced an empty file.
        (work / "other.wav").write_bytes(b"")
        return 0, [], ["Output file is empty"]

    monkeypatch.setattr(sec, "_run_registered_process", _run)

    assert sec._mix_other_stems(job, stems, work) is None
    assert not (work / "other.wav").exists()
    assert "could not prepare section stems" in caplog.text


def test_a_failed_mix_with_no_diagnostics_still_says_something(job, tmp_path, monkeypatch, caplog):
    stems = tmp_path / "stems"
    stems.mkdir()
    for name in ("other", "guitar", "piano"):
        (stems / f"{name}.wav").write_bytes(b"RIFF")
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setattr(sec, "_run_registered_process", lambda j, c: (1, [], []))

    assert sec._mix_other_stems(job, stems, work) is None
    assert "no diagnostic output" in caplog.text
