"""Lyrics timed line by line to the vocals (app/pipeline/lyrics_retime.py).

No model runs here: what Whisper heard is written out as words, the vocals
stem's envelope as levels, and the worker, where one runs, is a stub script
behind transcribe._spawn_worker_cmd (conftest's _no_whisper keeps every other
test away from the real one)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from app.core import settings as settings_mod
from app.core.models import Job, JobCancelled
from app.pipeline import lyrics_retime as rt
from app.pipeline.lyrics_lookup import clean_lyrics, lyrics_path, read_lyrics, write_lyrics

HOP = 0.04

# ── tokens ──


def keys(text: str) -> list[str]:
    return [t[0] for t in rt.tokens_of(text)]


def test_latin_words_fold_case_accents_and_apostrophes():
    assert keys("Don't STOP me, Łódź café") == ["dont", "stop", "me", "lodz", "cafe"]


def test_polish_and_german_letters_without_a_base_letter_fold():
    assert keys("Straße Łza") == ["strasse", "lza"]


def test_chinese_is_one_token_a_character_traditional_as_simplified():
    assert keys("紅豆 红豆") == ["红", "豆", "红", "豆"]


def test_kana_is_one_token_a_character_katakana_as_hiragana():
    assert keys("カタ かた") == ["か", "た", "か", "た"]


def test_hangul_is_one_token_a_syllable():
    assert keys("좋은 날") == ["좋", "은", "날"]


def test_mixed_scripts_split_where_the_script_changes():
    assert keys("愛してるyou") == ["爱", "し", "て", "る", "you"]


def pieces(text: str) -> list[str]:
    return [text[a:b] for a, b in rt.pieces_of(text)]


def test_pieces_are_spaced_words_and_each_chinese_character():
    assert pieces("(Don't) stop!") == ["(Don't)", "stop!"]
    assert pieces("月亮代表") == ["月", "亮", "代", "表"]
    assert pieces("좋은 날") == ["좋은", "날"]
    assert pieces("愛してるyou") == ["愛", "し", "て", "る", "you"]


def test_punctuation_alone_joins_a_word():
    assert pieces("... hello , world") == ["... hello ,", "world"]


def test_script_language():
    assert rt.script_language("좋은 날 you") == "ko"
    assert rt.script_language("夜に駆ける") == "ja"
    assert rt.script_language("月亮代表我的心") == "zh"
    assert rt.script_language("W biegu") is None


# ── the alignment ──


def test_alignment_pairs_in_order_and_skips_what_does_not_match():
    pairs = rt.align_tokens(list("abcxdef"), list("zzabcdqqdef"))
    assert [(i, j) for i, j, _s in pairs] == [(0, 2), (1, 3), (2, 4), (4, 8), (5, 9), (6, 10)]


def test_a_misheard_word_still_matches_a_little_less():
    pairs = rt.align_tokens(["zatrzymaj", "mnie"], ["zatrzymać", "mnie"])
    assert [(i, j) for i, j, _s in pairs] == [(0, 0), (1, 1)]
    assert 0 < pairs[0][2] < 1


def test_a_short_word_only_matches_itself():
    assert rt.similarity("to", "ta") == 0.0
    assert rt.similarity("do", "do") == 1.0


def test_a_hangul_syllable_one_letter_off_matches():
    pairs = rt.align_tokens(["적", "막"], ["정", "맥"])
    assert len(pairs) == 2


def test_a_chorus_sung_twice_but_written_once_keeps_one_whole_run():
    chorus = ["to", "ja", "zatrzymaj", "mnie"]
    heard = chorus + ["la", "la"] + chorus
    pairs = rt.align_tokens(chorus, heard)
    js = [j for _i, j, _s in pairs]
    # One occurrence, whole: the run bonus keeps the words together.
    assert js in ([0, 1, 2, 3], [6, 7, 8, 9])


def test_repeated_choruses_each_take_their_own_occurrence():
    chorus = ["to", "ja", "zatrzymaj", "mnie"]
    lyric = chorus + ["verse"] + chorus
    heard = chorus + ["verse"] + chorus
    pairs = rt.align_tokens(lyric, heard)
    assert [(i, j) for i, j, _s in pairs] == [(k, k) for k in range(9)]


def test_nothing_to_pair_or_too_much_is_empty(monkeypatch):
    assert rt.align_tokens([], ["a"]) == []
    monkeypatch.setattr(rt, "LYRICS_RETIME_MAX_CELLS", 3)
    assert rt.align_tokens(["a", "b"], ["a", "b"]) == []


# ── the voice ──


def voice_of(spans: list[tuple[float, float]], length: float | None = None) -> rt.Voice:
    """An envelope singing at -15 dB over ``spans`` and silent elsewhere, to
    a little after the last: the voice's loud parts are its 95th percentile,
    and a stem silent 95% of the time has none."""
    length = length or max(b for _a, b in spans) + 3
    db = [-80] * int(length / HOP)
    for a, b in spans:
        for f in range(int(a / HOP), int(b / HOP)):
            db[f] = -15
    return rt.Voice.of(HOP, db)


def test_a_word_stretched_over_a_pause_starts_after_it():
    voice = voice_of([(9.0, 10.0), (11.0, 12.0)])
    # Whisper starts the word at 10 (the end of the note before) and ends it
    # at 11.6: it is sung from 11.
    assert rt.voiced_start(10.0, 11.6, voice) == pytest.approx(11.0, abs=HOP)
    # One sung straight through keeps its start.
    assert rt.voiced_start(11.0, 11.8, voice) == 11.0


def test_a_line_start_on_a_held_note_moves_to_the_rise():
    voice = voice_of([(9.0, 10.8), (11.0, 12.0)])
    assert rt.Voice.rise_start(voice, 10.0, 11.5) == pytest.approx(11.0, abs=0.1)
    assert rt.Voice.rise_start(voice, 11.0, 11.5) == 11.0


# ── retiming ──


def words(*timed: tuple[str, float, float]) -> list[dict]:
    return [{"text": t, "start": a, "end": b} for t, a, b in timed]


def sing(line: str, start: float, pace: float = 0.4) -> list[dict]:
    """A line heard word by word from ``start``, ``pace`` seconds a word."""
    out = []
    for k, w in enumerate(line.split()):
        out.append({"text": w, "start": start + k * pace, "end": start + (k + 1) * pace - 0.05})
    return out


LINES = [
    "znowu to samo znowu biegnę",
    "chciałabym bardzo choć na chwilę",
    "zobaczyć cię biegnę za szybko",
    "nic się nie stało mówią",
]


def test_lines_start_where_they_are_heard_whatever_their_stamps_say():
    heard = sing(LINES[0], 20) + sing(LINES[1], 25) + sing(LINES[2], 31) + sing(LINES[3], 36)
    voice = voice_of([(20, 22), (25, 27.5), (31, 33.5), (36, 38)])
    pairs = [(50.0 + 3 * k, text) for k, text in enumerate(LINES)]
    result = rt.retime_lines(pairs, heard, voice, 60)
    assert [round(t.start, 1) for t in result.timed] == [20.0, 25.0, 31.0, 36.0]
    assert result.lines_matched == 4
    assert result.matched == pytest.approx(1.0)
    assert rt.retime_gate(result)


def test_word_stamps_follow_the_heard_words():
    heard = sing(LINES[0], 20, pace=0.5)
    result = rt.retime_lines([(0.0, LINES[0])], heard, voice_of([(20, 23)]), 60)
    assert result.timed[0].words == pytest.approx([20.0, 20.5, 21.0, 21.5, 22.0])


def test_a_line_the_track_does_not_sing_is_passed_quickly_and_the_rest_kept():
    extra = "tego wiersza nikt nie śpiewa"
    heard = sing(LINES[0], 20) + sing(LINES[1], 25)
    pairs = [(10.0, LINES[0]), (13.0, extra), (16.0, LINES[1])]
    result = rt.retime_lines(pairs, heard, voice_of([(20, 22), (25, 27.5)]), 60)
    first, skipped, second = (t.start for t in result.timed)
    assert (round(first, 1), round(second, 1)) == (20.0, 25.0)
    assert first < skipped < second
    assert not result.timed[1].anchored


def test_an_unheard_line_between_anchors_lands_on_the_singing_not_midway():
    # The line between is sung at 40, after a long instrumental: not at the
    # midpoint (32.5) the stamps would give on a straight line.
    mumble = "la la la"
    heard = sing(LINES[0], 20) + sing(LINES[1], 45)
    voice = voice_of([(20, 22), (40, 41.5), (45, 47.5)])
    pairs = [(0.0, LINES[0]), (5.0, mumble), (10.0, LINES[1])]
    result = rt.retime_lines(pairs, heard, voice, 60)
    assert result.timed[1].start == pytest.approx(40.0, abs=0.2)


def test_stamps_between_anchors_that_agree_with_them_are_kept():
    heard = sing(LINES[0], 20) + sing(LINES[3], 32)
    voice = voice_of([(20, 22), (26, 28), (29, 31), (32, 34)])
    pairs = [(10.0, LINES[0]), (16.0, "one mumbled"), (19.0, "two mumbled"), (22.0, LINES[3])]
    result = rt.retime_lines(pairs, heard, voice, 60)
    assert [round(t.start, 1) for t in result.timed] == [20.0, 26.0, 29.0, 32.0]


def test_a_translation_stamped_with_its_line_is_shown_with_it():
    heard = sing(LINES[0], 20) + sing(LINES[1], 25)
    pairs = [(1.0, LINES[0]), (1.0, "Again the same"), (4.0, LINES[1])]
    result = rt.retime_lines(pairs, heard, voice_of([(20, 22), (25, 27.5)]), 60)
    assert result.timed[1].start == result.timed[0].start
    assert [text for _t, text in rt.synced_lines(rt.to_lrc(result))] == [p[1] for p in pairs]


def test_words_heard_in_a_silence_anchor_nothing():
    heard = sing(LINES[0], 20)
    result = rt.retime_lines([(0.0, LINES[0])], heard, voice_of([(40, 42)]), 60)
    assert result.lines_matched == 0
    assert not rt.retime_gate(result)


def test_a_line_matched_to_a_later_repeat_keeps_its_main_group():
    # "niż wcale" is heard again 10 s later: the line keeps its first group.
    heard = sing("lepiej późno niż wcale", 70) + sing("niż wcale", 82)
    voice = voice_of([(70, 72), (82, 83)])
    result = rt.retime_lines([(0.0, "lepiej późno niż wcale")], heard, voice, 100)
    assert result.timed[0].words[-1] < 72
    assert result.timed[0].end < 73


def test_a_first_word_held_long_stays_with_its_line():
    # Piaf's "Non" is held two seconds; Whisper ends it long before "rien",
    # but the voice goes on between them, so the line starts at "Non".
    heard = words(
        ("non", 11.3, 11.7), ("rien", 13.8, 14.0), ("de", 14.0, 14.2), ("rien", 14.2, 14.8)
    )
    heard += sing("non je ne regrette rien", 16.5)
    voice = voice_of([(11.3, 15.0), (16.5, 18.5)])
    lines = [(11.0, "Non, rien de rien"), (16.0, "Non, je ne regrette rien")]
    result = rt.retime_lines(lines, heard, voice, 30)
    assert result.timed[0].start == pytest.approx(11.3, abs=0.1)
    # With the voice silent in between it is a word heard apart, as before.
    silent = voice_of([(11.3, 11.7), (13.8, 15.0), (16.5, 18.5)])
    result = rt.retime_lines(lines, heard, silent, 30)
    assert result.timed[0].start > 13


def test_lyrics_nothing_like_the_singing_fail_the_gate():
    heard = sing("completely other words here", 10)
    result = rt.retime_lines([(0.0, LINES[0]), (3.0, LINES[1])], heard, voice_of([(10, 12)]), 60)
    assert not rt.retime_gate(result)


def test_plain_lyrics_get_timed_too():
    heard = sing(LINES[0], 20) + sing(LINES[1], 25)
    pairs = [(None, LINES[0]), (None, LINES[1])]
    result = rt.retime_lines(pairs, heard, voice_of([(20, 22), (25, 27.5)]), 60)
    assert [round(t.start, 1) for t in result.timed] == [20.0, 25.0]


# ── LRC out ──


def test_the_result_is_enhanced_lrc_of_the_same_lines():
    heard = sing(LINES[0], 20) + sing(LINES[1], 30)
    pairs = [(0.0, LINES[0]), (5.0, LINES[1])]
    lrc = rt.to_lrc(rt.retime_lines(pairs, heard, voice_of([(20, 22), (30, 33)]), 60))
    rows = lrc.splitlines()
    assert rows[0].startswith("[00:20.00]<00:20.00>znowu <00:20.40>to ")
    assert rows[0].endswith(">")
    # A long pause after a line clears it.
    assert rows[1].startswith("[00:2") and rows[1].endswith("]")
    assert [text for _t, text in rt.synced_lines(lrc)] == [p[1] for p in pairs]


# ── keeping it ──


def entry(**fields) -> dict:
    base = {
        "v": 1,
        "source": "lrclib",
        "track": "W biegu",
        "artist": "Natalia Kukulska",
        "album": "",
        "duration": 238.0,
        "synced": "[00:50.00]znowu to samo\n[00:55.00]chciałabym bardzo",
        "plain": "znowu to samo\nchciałabym bardzo",
        "instrumental": False,
        "lrclib_id": 1,
        "timing": "unverified",
        "others": [],
    }
    return {**base, **fields}


ALIGNED = {
    "synced": "[00:20.00]<00:20.00>znowu <00:20.40>to <00:20.80>samo<00:21.20>\n"
    "[00:25.00]<00:25.00>chciałabym <00:25.60>bardzo<00:26.00>",
    "method": "whisper",
    "matched": 1.0,
    "lines_matched": 2,
    "lines": 2,
    "at": 1700000000.0,
}


def test_old_lyrics_read_back_unchanged():
    old = entry()
    cleaned = clean_lyrics(old)
    assert "aligned" not in cleaned and "user_synced" not in cleaned
    assert cleaned["synced"] == old["synced"]


def test_a_timing_of_these_lines_is_kept():
    cleaned = clean_lyrics(entry(aligned=ALIGNED))
    assert cleaned["aligned"] == ALIGNED


@pytest.mark.parametrize(
    "broken",
    [
        {**ALIGNED, "method": "guess"},
        {**ALIGNED, "matched": 1.5},
        {**ALIGNED, "matched": float("nan")},
        {**ALIGNED, "lines_matched": 3},
        {**ALIGNED, "lines": True},
        {**ALIGNED, "synced": "[00:20.00]another song"},
        {**ALIGNED, "synced": 5},
        "aligned",
    ],
)
def test_a_malformed_or_foreign_timing_is_dropped_not_the_lyrics(broken):
    cleaned = clean_lyrics(entry(aligned=broken))
    assert cleaned is not None and "aligned" not in cleaned


LINES2 = ["znowu to samo", "chciałabym bardzo"]


@pytest.mark.parametrize(
    "lrc",
    [
        "[00:20.00]znowu to samo\n[00:25.00]chciałabym bardzo",
        "[00:20.00]<00:20.00>znowu to samo\n\n[00:22.00]\n[00:25.00]chciałabym  bardzo",
        "[00:20.00]znowu to samo\n[00:20.00]chciałabym bardzo",
    ],
)
def test_a_manual_timing_of_these_lines_is_accepted(lrc):
    assert rt.clean_user_synced(lrc, LINES2) is not None


@pytest.mark.parametrize(
    "lrc",
    [
        # Another text, a line more or less, another order.
        "[00:20.00]znowu to samo\n[00:25.00]<script>alert(1)</script>",
        "[00:20.00]znowu to samo",
        "[00:20.00]znowu to samo\n[00:25.00]chciałabym bardzo\n[00:30.00]i jeszcze",
        "[00:20.00]chciałabym bardzo\n[00:25.00]znowu to samo",
        # Stamps back in time, malformed, or a tag that is not a stamp.
        "[00:25.00]znowu to samo\n[00:20.00]chciałabym bardzo",
        "[00:75.00]znowu to samo\n[01:20.00]chciałabym bardzo",
        "znowu to samo\n[00:25.00]chciałabym bardzo",
        "[ar:someone]\n[00:20.00]znowu to samo\n[00:25.00]chciałabym bardzo",
        "[00:20.00][00:30.00]znowu to samo\n[00:25.00]chciałabym bardzo",
        "[00:20.00]znowu to samo <00:99.00>\n[00:25.00]chciałabym bardzo",
        "",
        None,
        5,
    ],
)
def test_anything_else_is_refused(lrc):
    assert rt.clean_user_synced(lrc, LINES2) is None


def test_a_manual_timing_too_long_is_refused(monkeypatch):
    monkeypatch.setattr(rt, "LYRICS_USER_SYNCED_MAX_LINES", 1)
    assert (
        rt.clean_user_synced("[00:20.00]znowu to samo\n[00:25.00]chciałabym bardzo", LINES2) is None
    )


def test_the_lines_a_timing_must_give_are_one_a_stamp_in_time_order():
    lrc = "[00:30.00][00:10.00]refren\n[00:20.00]zwrotka\n[00:40.00]"
    assert rt.lyric_line_texts({"synced": lrc, "plain": ""}) == ["refren", "zwrotka", "refren"]
    assert rt.lyric_line_texts({"synced": "", "plain": "a\n\nb"}) == ["a", "b"]


# ── the transcript ──


def test_a_transcript_is_kept_and_read_back(tmp_path):
    answer = {
        "language": "pl",
        "model": "turbo",
        "segments": [
            {"start": 1, "end": 2, "words": [{"word": " znowu", "start": 1.0, "end": 1.5}]},
            # Taken for silence by Whisper: not kept.
            {
                "start": 5,
                "end": 6,
                "no_speech_prob": 0.9,
                "avg_logprob": -2.0,
                "words": [{"word": " dzięki", "start": 5.0, "end": 5.5}],
            },
            # A chorus compresses like a loop and is kept all the same.
            {
                "start": 7,
                "end": 8,
                "compression_ratio": 3.0,
                "words": [{"word": " to", "start": 7.0, "end": 7.2}],
            },
        ],
    }
    rt.save_transcript(tmp_path, answer)
    kept = rt.read_transcript(tmp_path)
    assert kept["language"] == "pl"
    assert [w["text"] for w in kept["words"]] == ["znowu", "to"]


def test_a_transcript_of_other_vocals_is_heard_afresh(tmp_path):
    stems = tmp_path / "stems"
    stems.mkdir()
    (stems / "vocals.wav").write_bytes(b"RIFF1")
    rt.save_transcript(tmp_path, words=[{"start": 1.0, "end": 1.5, "text": "znowu"}], language="pl")
    assert rt.read_transcript(tmp_path) is not None
    # The stem made again, in place: the transcript is of other audio.
    (stems / "vocals.wav").write_bytes(b"RIFF22")
    assert rt.read_transcript(tmp_path) is None


def test_a_transcript_kept_before_the_stamp_is_taken_as_it_is(tmp_path):
    rt.transcript_path(tmp_path).write_text(
        json.dumps({"v": 1, "language": "pl", "words": [[1.0, 1.5, "znowu"]]}), encoding="utf-8"
    )
    assert [w["text"] for w in rt.read_transcript(tmp_path)["words"]] == ["znowu"]


@pytest.mark.parametrize("content", ["{", '{"v": 2, "words": []}', '{"v": 1, "words": 3}'])
def test_a_damaged_transcript_is_none(tmp_path, content):
    rt.transcript_path(tmp_path).write_text(content, encoding="utf-8")
    assert rt.read_transcript(tmp_path) is None


def test_bad_rows_of_a_transcript_are_skipped(tmp_path):
    data = {"v": 1, "words": [[1, 2, "a"], [1, "x", "b"], [float("inf"), 2, "c"], [1, 2]]}
    rt.transcript_path(tmp_path).write_text(json.dumps(data), encoding="utf-8")
    assert [w["text"] for w in rt.read_transcript(tmp_path)["words"]] == ["a"]


# ── running it on a job ──


def job_dir_with(tmp_path: Path, lyrics: dict, voiced: list[tuple[float, float]]) -> Path:
    job_dir = tmp_path / "abcdef123456"
    (job_dir / "stems").mkdir(parents=True)
    (job_dir / "stems" / "vocals.wav").write_bytes(b"RIFF")
    db = [-80] * int(max(40, max(b for _a, b in voiced) + 3) / HOP)
    for a, b in voiced:
        for f in range(int(a / HOP), int(b / HOP)):
            db[f] = -15
    # Kept envelope newer than the stem: read instead of the stem.
    (job_dir / "stems" / "vocal_envelope.json").write_text(
        json.dumps({"hop": HOP, "db": db}), encoding="utf-8"
    )
    lyrics_path(job_dir).write_text(json.dumps(lyrics), encoding="utf-8")
    return job_dir


def make_job(**fields) -> Job:
    job = Job(id="abcdef123456", status="done", title="W biegu", duration_sec=60.0)
    job.compute_device = "cuda"
    for k, v in fields.items():
        setattr(job, k, v)
    return job


def stub_worker(monkeypatch, tmp_path: Path, heard: list[dict], log: list | None = None):
    """Point the worker at a script that answers ``heard`` as one segment."""
    answer = {
        "language": "pl",
        "language_probability": 1.0,
        "model": "stub",
        "device": "cpu",
        "segments": [
            {
                "start": heard[0]["start"],
                "end": heard[-1]["end"],
                "text": " ".join(w["text"] for w in heard),
                "words": [
                    {"word": " " + w["text"], "start": w["start"], "end": w["end"]} for w in heard
                ],
            }
        ],
        "peak_vram_mb": None,
    }
    out = tmp_path / "answer.json"
    out.write_text(json.dumps(answer), encoding="utf-8")
    script = f"import sys; sys.stdout.write(open({str(out)!r}, encoding='utf-8').read())"

    def cmd(vocals, device):
        if log is not None:
            log.append(device)
        return [sys.executable, "-c", script]

    monkeypatch.setattr("app.pipeline.transcribe._spawn_worker_cmd", cmd)


HEARD = sing("znowu to samo", 20) + sing("chciałabym bardzo", 25)
VOICED = [(20, 22), (25, 27)]


def test_a_job_gets_its_lyrics_timed_and_the_source_is_kept(monkeypatch, tmp_path):
    job_dir = job_dir_with(tmp_path, entry(), VOICED)
    stub_worker(monkeypatch, tmp_path, HEARD)
    outcome = rt.retime_job(make_job(), job_dir, cancelled=lambda: False)
    assert outcome.state == "done"
    kept = read_lyrics(job_dir)
    assert kept["synced"] == entry()["synced"]
    assert kept["aligned"]["lines_matched"] == 2
    assert kept["aligned"]["synced"].startswith("[00:20.00]")
    assert rt.transcript_path(job_dir).is_file()


def test_a_second_run_reuses_the_transcript(monkeypatch, tmp_path):
    job_dir = job_dir_with(tmp_path, entry(), VOICED)
    calls: list[str] = []
    stub_worker(monkeypatch, tmp_path, HEARD, calls)
    rt.retime_job(make_job(), job_dir, cancelled=lambda: False)
    rt.retime_job(make_job(), job_dir, cancelled=lambda: False)
    assert calls == ["cuda"]


def test_an_unsure_result_leaves_the_lyrics_alone(monkeypatch, tmp_path):
    job_dir = job_dir_with(tmp_path, entry(), VOICED)
    stub_worker(monkeypatch, tmp_path, sing("nothing like it at all", 20))
    before = lyrics_path(job_dir).read_bytes()
    assert rt.retime_job(make_job(), job_dir, cancelled=lambda: False).state == "unsure"
    assert lyrics_path(job_dir).read_bytes() == before


def test_a_cancel_leaves_the_lyrics_alone(monkeypatch, tmp_path):
    job_dir = job_dir_with(tmp_path, entry(), VOICED)
    stub_worker(monkeypatch, tmp_path, HEARD)
    before = lyrics_path(job_dir).read_bytes()
    with pytest.raises(JobCancelled):
        rt.retime_job(make_job(), job_dir, cancelled=lambda: True)
    assert lyrics_path(job_dir).read_bytes() == before


def test_a_failed_transcription_is_a_failure(tmp_path):
    job_dir = job_dir_with(tmp_path, entry(), VOICED)
    with pytest.raises(rt.RetimeFailed):
        rt.retime_job(make_job(), job_dir, cancelled=lambda: False)


def test_no_lyrics_is_a_failure(tmp_path):
    job_dir = job_dir_with(tmp_path, entry(), VOICED)
    lyrics_path(job_dir).unlink()
    with pytest.raises(rt.RetimeFailed):
        rt.retime_job(make_job(), job_dir, cancelled=lambda: False)


STARTS = [5.0, 8.5, 14.0, 17.0, 22.5, 26.0, 31.5, 35.0]


def test_a_timing_the_voice_confirms_is_not_replaced_by_a_worse_one():
    # The copy's own stamps sit on the voice's rises, and a whole-song shift
    # of 0 fits them; a timing a second later, where the voice does not
    # rise, is worse.
    synced = chr(10).join(f"[00:{t:05.2f}]zwrotka numer {k}" for k, t in enumerate(STARTS))
    voice = voice_of([(t, t + 2) for t in STARTS])
    pairs = rt.synced_lines(synced)
    later = rt.Retimed(
        rt.build_lyric(pairs).lines,
        [rt.Timed(t + 1.0, t + 1.8, [t + 1.0] * 3, True) for t in STARTS],
        1.0,
        len(STARTS),
    )
    assert not rt.no_worse(entry(synced=synced, timing="exact"), later, voice)
    same = rt.retime_lines(
        pairs, [w for t in STARTS for w in sing("zwrotka numer x", t)], voice, 40
    )
    assert rt.no_worse(entry(synced=synced, timing="exact"), same, voice)


def test_a_line_whose_first_words_whisper_missed_starts_with_its_phrase(tmp_path):
    # Heard from its third word, a second into a phrase nothing else was
    # heard in: the line starts where the phrase does.
    heard = sing("samo znowu biegnę", 21.0)
    result = rt.retime_lines([(0.0, LINES[0])], heard, voice_of([(20.0, 23.0)]), 40)
    assert result.timed[0].start == pytest.approx(20.0, abs=0.1)


# ── after separation ──


@pytest.mark.parametrize(
    ("fields", "setting", "device", "wanted"),
    [
        ({}, "auto", "cuda", True),
        ({}, "auto", "cpu", False),
        ({}, "on", "cpu", True),
        ({}, "off", "cuda", False),
        ({"offset_sec": 1.0}, "auto", "cuda", False),
        ({"aligned": ALIGNED}, "auto", "cuda", False),
        ({"timing": "shifted"}, "auto", "cuda", True),
        ({"source": "whisper"}, "auto", "cuda", False),
    ],
)
def test_who_gets_timed_after_separation(tmp_path, fields, setting, device, wanted):
    settings_mod.set_transcribe_lyrics(setting)
    job_dir = job_dir_with(tmp_path, entry(**fields), VOICED)
    assert rt.wants_retime(make_job(compute_device=device), job_dir) is wanted


def test_after_separation_it_never_raises_but_a_cancel(monkeypatch, tmp_path):
    job_dir = job_dir_with(tmp_path, entry(), VOICED)
    # The worker fails (conftest): no lyrics timed, no error.
    assert rt.retime_after_separation(make_job(), job_dir) is False
    with pytest.raises(JobCancelled):
        rt.retime_after_separation(make_job(cancel_requested=True), job_dir)


def test_after_separation_it_times_the_lyrics(monkeypatch, tmp_path):
    job_dir = job_dir_with(tmp_path, entry(), VOICED)
    stub_worker(monkeypatch, tmp_path, HEARD)
    assert rt.retime_after_separation(make_job(), job_dir) is True
    assert "aligned" in read_lyrics(job_dir)


def test_writing_lyrics_again_keeps_a_timing_of_the_same_lines(tmp_path):
    job_dir = tmp_path / "j"
    job_dir.mkdir()
    job = make_job()
    assert write_lyrics(job, job_dir, entry(aligned=ALIGNED))
    assert read_lyrics(job_dir)["aligned"] == ALIGNED


def test_the_language_of_latin_lyrics_comes_from_their_commonest_words():
    german = "Ich geh mit dir wohin du willst und nicht mit mir, das ist die Nacht " * 2
    assert rt.text_language(german) == "de"
    assert rt.text_language("Eu não sei o que você quer, é meu e com mais " * 2) == "pt"
    assert rt.text_language("la la la") is None


def test_words_in_brackets_are_a_backing_vocal_and_match_nothing():
    lyric = rt.build_lyric([(0.0, "(you're gonna wish) the scars of your love")])
    assert lyric.aside[:3] == [True, True, True] and not any(lyric.aside[3:])
    # A line all in brackets is the line itself.
    assert not any(rt.build_lyric([(0.0, "(whoa oh oh)")]).aside)
    heard = sing("you're gonna wish", 18) + sing("the scars of your love", 20)
    result = rt.retime_lines(
        [(0.0, "(you're gonna wish) the scars of your love")], heard, voice_of([(18, 22)]), 30
    )
    assert result.timed[0].start == pytest.approx(20.0, abs=0.05)


def test_a_line_sung_again_much_later_is_taken_where_it_follows_the_one_before():
    # The last line is heard right after the line before and again a minute
    # later, a little more clearly; it is the first time.
    heard = sing(LINES[0], 20) + sing("chciałabym xyz choć na", 24)
    heard += [w for k in range(30) for w in sing(f"inne{k}", 30 + k * 2)]
    heard += sing(LINES[1], 95)
    voice = voice_of([(20, 22), (24, 26), (30, 90), (95, 98)])
    result = rt.retime_lines([(0.0, LINES[0]), (4.0, LINES[1])], heard, voice, 100)
    assert result.timed[1].start == pytest.approx(24.0, abs=0.3)


# ── second round: credits, pause marks, missed lines heard again ──


def test_credits_at_the_top_are_not_timed_and_come_before_the_singing():
    pairs = [
        (0.5, "晴天 - 周傑倫"),
        (1.0, "詞：周傑倫"),
        (1.5, "曲：周傑倫"),
        (30.0, LINES[0]),
        (35.0, LINES[1]),
    ]
    assert rt._credits(pairs) == {0, 1, 2}
    heard = sing(LINES[0], 20) + sing(LINES[1], 25)
    result = rt.retime_lines(pairs, heard, voice_of([(20, 22), (25, 27.5)]), 40)
    starts = [t.start for t in result.timed]
    assert starts[3] == pytest.approx(20.0, abs=0.05)
    assert starts[0] <= starts[1] <= starts[2] < starts[3]
    assert result.counted == 2
    assert [text for _t, text in rt.synced_lines(rt.to_lrc(result))] == [p[1] for p in pairs]


def test_a_line_that_only_sings_the_word_music_is_not_a_credit():
    assert rt._credits([(1.0, "Music is my life"), (3.0, "la la")]) == set()


def test_a_pause_mark_keeps_its_place_after_the_line_before():
    pairs = [(10.0, LINES[0]), (14.0, "♪"), (20.0, LINES[1])]
    heard = sing(LINES[0], 30) + sing(LINES[1], 40)
    result = rt.retime_lines(pairs, heard, voice_of([(30, 32), (40, 42.5)]), 60)
    mark = result.timed[1]
    assert mark.start == pytest.approx(34.0, abs=0.1)
    assert result.counted == 2


def test_a_first_word_held_over_the_note_before_starts_at_the_stronger_rise():
    # Singing from 9 s with a mild rise; the line's word, heard from 10 s,
    # is sung from the sharp rise at 11 s after a dip.
    db = [-80] * int(20 / HOP)
    for f in range(int(9 / HOP), int(13 / HOP)):
        db[f] = -24
    for f in range(int(10.0 / HOP), int(10.9 / HOP)):
        db[f] = -21
    for f in range(int(11.0 / HOP), int(13 / HOP)):
        db[f] = -10
    voice = rt.Voice.of(HOP, db)
    assert voice.rise_start(10.0, 12.0) == pytest.approx(11.0, abs=0.1)
    # A short word keeps its start.
    assert voice.rise_start(10.0, 10.5) == 10.0


def test_the_worker_clips_argument():
    from app.pipeline.transcribe_worker import parse_clips

    assert parse_clips("1:2,x,5:400,-1:3,3:2") == [(1.0, 2.0), (5.0, 125.0)]
    assert parse_clips("") == []


def test_missed_runs_become_clips_and_their_words_replace_the_first_ones():
    lines = [rt.Line("a", 0.0), rt.Line("b", 4.0), rt.Line("c", 8.0), rt.Line("d", 12.0)]
    timed = [
        rt.Timed(10.0, 11.0, [], True),
        rt.Timed(14.0, 14.5, [], False),
        rt.Timed(18.0, 18.5, [], False),
        rt.Timed(22.0, 23.0, [], True),
    ]
    result = rt.Retimed(lines, timed, 0.5, 2)
    voice = voice_of([(10, 23)])
    assert rt.missed_clips(result, voice, 30) == [(11.0, 22.0)]
    # Too little singing where the run goes: nothing to hear again.
    assert rt.missed_clips(result, voice_of([(10, 11), (22, 23)]), 30) == []
    words = [{"text": "x", "start": 5.0, "end": 5.5}, {"text": "y", "start": 15.0, "end": 15.5}]
    extra = [{"text": "z", "start": 16.0, "end": 16.5}, {"text": "w", "start": 40.0, "end": 41}]
    merged = rt.merge_words(words, extra, [(10.6, 22.4)])
    assert [w["text"] for w in merged] == ["x", "z"]


FOUR = [
    "pierwsza linia tutaj",
    "druga linia śpiewana",
    "trzecia linia słyszana",
    "czwarta już ostatnia",
]


def stub_two_answers(monkeypatch, tmp_path, first: list[dict], clips: list[dict], log: list):
    """A worker answering ``first`` for the whole stem and ``clips`` when it
    is asked for --clips."""

    def answer(heard: list[dict]) -> dict:
        return {
            "language": "pl",
            "language_probability": 1.0,
            "model": "stub",
            "device": "cpu",
            "segments": [
                {
                    "start": heard[0]["start"],
                    "end": heard[-1]["end"],
                    "text": "",
                    "words": [
                        {"word": " " + w["text"], "start": w["start"], "end": w["end"]}
                        for w in heard
                    ],
                }
            ],
            "peak_vram_mb": None,
        }

    (tmp_path / "full.json").write_text(json.dumps(answer(first)), encoding="utf-8")
    (tmp_path / "clips.json").write_text(json.dumps(answer(clips)), encoding="utf-8")
    script = (
        "import sys; name = 'clips.json' if '--clips' in sys.argv else 'full.json'; "
        f"sys.stdout.write(open({str(tmp_path)!r} + '/' + name, encoding='utf-8').read())"
    )

    def cmd(vocals, device):
        log.append(device)
        return [sys.executable, "-c", script]

    monkeypatch.setattr("app.pipeline.transcribe._spawn_worker_cmd", cmd)


def test_lines_missed_by_the_first_pass_are_heard_again_and_found(monkeypatch, tmp_path):
    stamps = [2.0, 6.0, 10.0, 14.0]
    synced = chr(10).join(f"[00:{t:05.2f}]{x}" for t, x in zip(stamps, FOUR, strict=True))
    voiced = [(t + 10, t + 12) for t in stamps]
    job_dir = job_dir_with(tmp_path, entry(synced=synced, plain=""), voiced)
    first = sing(FOUR[0], 12.0) + sing(FOUR[3], 24.0)
    again = sing(FOUR[1], 16.0) + sing(FOUR[2], 20.0)
    log: list = []
    stub_two_answers(monkeypatch, tmp_path, first, again, log)
    outcome = rt.retime_job(make_job(), job_dir, cancelled=lambda: False)
    assert outcome.state == "done" and outcome.lines_matched == 4
    assert len(log) == 2
    aligned = read_lyrics(job_dir)["aligned"]["synced"]
    assert [round(t) for t, _x in rt.synced_lines(aligned)] == [12, 16, 20, 24]
    kept = rt.read_transcript(job_dir)
    assert kept["refined"] is True
    # Timed again: the kept transcript, no worker at all.
    rt.retime_job(make_job(), job_dir, cancelled=lambda: False)
    assert len(log) == 2


SEVEN = FOUR + ["piąta dalej śpiewa", "szósta prawie koniec", "siódma na sam koniec"]


def test_lines_heard_again_where_the_copy_does_not_put_them_are_not_taken(monkeypatch, tmp_path):
    # Heard again, three missed lines turn up 9 s later than the copy and
    # the lines found around them put them: a chorus sung once more after a
    # break, not these. They keep the copy's own place.
    stamps = [2.0, 6.0, 10.0, 14.0, 18.0, 40.0, 44.0]
    synced = chr(10).join(f"[00:{t:05.2f}]{x}" for t, x in zip(stamps, SEVEN, strict=True))
    voiced = [(t + 10, t + 12) for t in stamps] + [(37, 39), (41, 43), (45, 47)]
    job_dir = job_dir_with(tmp_path, entry(synced=synced, plain=""), voiced)
    first = sing(SEVEN[0], 12.0) + sing(SEVEN[1], 16.0) + sing(SEVEN[5], 50.0)
    first += sing(SEVEN[6], 54.0)
    later = sing(SEVEN[2], 37.0) + sing(SEVEN[3], 41.0) + sing(SEVEN[4], 45.0)
    log: list = []
    stub_two_answers(monkeypatch, tmp_path, first, later, log)
    rt.retime_job(make_job(), job_dir, cancelled=lambda: False)
    assert len(log) == 2
    starts = [t for t, _x in rt.synced_lines(read_lyrics(job_dir)["aligned"]["synced"])]
    assert starts[2:5] == pytest.approx([20.0, 24.0, 28.0], abs=0.3)
