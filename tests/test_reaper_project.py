"""The REAPER project text (app/pipeline/reaper_project.py). The zip
endpoint's ?reaper=1 is tested beside the zip's click in test_click_sounds.py,
which has the real-audio job those tests need."""

from __future__ import annotations

import re

import pytest

from app.pipeline.reaper_project import _quote, build_rpp, track_label


def rpp_files(rpp: str) -> list[str]:
    """The FILE of every source in a project, in order."""
    return re.findall(r'^\s*FILE "([^"]+)"$', rpp, re.M)


def test_build_rpp_is_balanced_and_names_every_file():
    text = build_rpp(
        [("lead_vocals", "Song_lead_vocals.flac"), ("drums", "Song_drums.flac")],
        "flac",
        48000,
        212.5,
        97.5,
    )
    assert text.startswith("<REAPER_PROJECT ")
    opens = sum(1 for line in text.splitlines() if line.strip().startswith("<"))
    closes = sum(1 for line in text.splitlines() if line.strip() == ">")
    assert opens == closes == 1 + 2 * 3  # project, and track/item/source per stem
    assert "  SAMPLERATE 48000 0 0" in text
    assert "  TEMPO 97.5 4 4" in text
    assert 'NAME "Lead Vocals"' in text
    assert "<SOURCE FLAC" in text
    assert "LENGTH 212.500000" in text
    assert rpp_files(text) == ["Song_lead_vocals.flac", "Song_drums.flac"]


@pytest.mark.parametrize("bpm", [None, 0, -3, 5000])
def test_build_rpp_falls_back_to_120_without_a_usable_tempo(bpm):
    assert "TEMPO 120 4 4" in build_rpp([("bass", "bass.wav")], "wav", 44100, 1.0, bpm)


@pytest.mark.parametrize(("fmt", "kind"), [("wav", "WAVE"), ("mp3", "MP3"), ("ogg", "VORBIS")])
def test_build_rpp_source_type_follows_the_format(fmt, kind):
    assert f"<SOURCE {kind}" in build_rpp([("bass", f"bass.{fmt}")], fmt, 44100, 1.0, 120)


def test_quote_and_label():
    assert _quote("a b") == '"a b"'
    assert _quote('say "hi"') == "'say \"hi\"'"
    assert track_label("backing_vocals") == "Backing Vocals"
    assert track_label("Take_1_t1a2b3") == "Take 1 T1a2b3"
