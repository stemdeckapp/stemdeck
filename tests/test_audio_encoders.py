"""The audio export capability probe (app/pipeline/audio_encoders.py, #824).

libvorbis is optional: an ffmpeg without it runs StemDeck normally and only
OGG export is unavailable. These tests drive both answers on any machine by
feeding the parser real listings and by monkeypatching the probe, so neither
path depends on what the ffmpeg here happens to have.
"""

from __future__ import annotations

import subprocess

import pytest

from app.pipeline import audio_encoders as ae
from tests.ffmpeg_probe import ffmpeg_available, vorbis_available

# `ffmpeg -hide_banner -encoders` from Homebrew's plain ffmpeg 9.0.2 (trimmed):
# the experimental native "vorbis" encoder, no libvorbis.
HOMEBREW_LISTING = """\
Encoders:
 V..... = Video
 A..... = Audio
 S..... = Subtitle
 .F.... = Frame-level multithreading
 ..S... = Slice-level multithreading
 ...X.. = Codec is experimental
 ....B. = Supports draw_horiz_band
 .....D = Supports direct rendering method 1
 ------
 V....D libx264              libx264 H.264 / AVC / MPEG-4 AVC / MPEG-4 part 10 (codec h264)
 A....D aac                  AAC (Advanced Audio Coding)
 A..... aac_at               aac (AudioToolbox) (codec aac)
 A....D flac                 FLAC (Free Lossless Audio Codec)
 A....D libmp3lame           libmp3lame MP3 (MPEG audio layer 3) (codec mp3)
 A....D pcm_s16le            PCM signed 16-bit little-endian
 A..X.D vorbis               Vorbis
 S..... srt                  SubRip subtitle
"""

FULL_LISTING = HOMEBREW_LISTING.replace(
    " A..X.D vorbis               Vorbis\n",
    " A..X.D vorbis               Vorbis\n A....D libvorbis            libvorbis (codec vorbis)\n",
)


@pytest.fixture(autouse=True)
def _fresh_cache():
    ae.reset_cache()
    yield
    ae.reset_cache()


def test_parse_finds_names_and_skips_the_legend():
    names = ae.parse_encoder_names(HOMEBREW_LISTING)
    assert {"aac", "flac", "libmp3lame", "pcm_s16le", "vorbis", "libx264", "srt"} <= names
    assert "libvorbis" not in names
    assert "=" not in names
    assert "libvorbis" in ae.parse_encoder_names(FULL_LISTING)
    assert ae.parse_encoder_names("") == frozenset()


def _fake_probe(monkeypatch, listing: str, ident: str = "ident-a", returncode: int = 0):
    calls = []

    def run(cmd, **_kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode, stdout=listing, stderr="")

    monkeypatch.setattr(ae, "ffmpeg_identity", lambda: (ident, "ffmpeg version test"))
    monkeypatch.setattr(ae.subprocess, "run", run)
    return calls


def test_export_formats_without_libvorbis(monkeypatch):
    _fake_probe(monkeypatch, HOMEBREW_LISTING)
    assert ae.export_formats() == {"wav": True, "mp3": True, "flac": True, "ogg": False}
    assert ae.unavailable_detail("ogg") == ae.OGG_UNAVAILABLE_DETAIL
    for fmt in ("wav", "mp3", "flac"):
        assert ae.unavailable_detail(fmt) is None


def test_export_formats_with_libvorbis(monkeypatch):
    _fake_probe(monkeypatch, FULL_LISTING)
    assert ae.export_formats() == {"wav": True, "mp3": True, "flac": True, "ogg": True}
    assert ae.unavailable_detail("ogg") is None


def test_the_listing_runs_once_per_ffmpeg_binary(monkeypatch):
    calls = _fake_probe(monkeypatch, HOMEBREW_LISTING)
    for _ in range(5):
        ae.export_formats()
    assert len(calls) == 1
    assert calls[0][1:] == ["-hide_banner", "-encoders"]
    # A different binary (path, size, mtime or version) is probed again.
    monkeypatch.setattr(ae, "ffmpeg_identity", lambda: ("ident-b", "other"))
    ae.export_formats()
    assert len(calls) == 2


def test_unknown_is_not_unavailable(monkeypatch):
    # No ffmpeg to run at all: unknown, and nothing is refused.
    monkeypatch.setattr(ae, "ffmpeg_identity", lambda: ("", ""))
    assert ae.export_formats() is None
    assert ae.unavailable_detail("ogg") is None


def test_a_failed_listing_is_unknown_and_not_retried_per_request(monkeypatch):
    calls = _fake_probe(monkeypatch, "", returncode=1)
    assert ae.export_formats() is None
    assert ae.export_formats() is None
    assert ae.unavailable_detail("ogg") is None
    assert len(calls) == 1


def test_a_listing_that_cannot_run_is_unknown(monkeypatch):
    def boom(cmd, **_kwargs):
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(ae, "ffmpeg_identity", lambda: ("ident-c", "v"))
    monkeypatch.setattr(ae.subprocess, "run", boom)
    assert ae.ffmpeg_encoders() is None


def test_the_detail_names_no_path():
    assert "/" not in ae.OGG_UNAVAILABLE_DETAIL
    assert "libvorbis" in ae.OGG_UNAVAILABLE_DETAIL
    assert ae.OGG_UNAVAILABLE_DETAIL.isascii()


def test_the_real_probe_agrees_with_the_test_skip_helper():
    # The same question tests/ffmpeg_probe.py asks for its skips.
    if not ffmpeg_available():
        pytest.skip("ffmpeg not available (not bundled and not on PATH)")
    formats = ae.export_formats()
    assert formats is not None
    assert formats["wav"] and formats["flac"]
    assert formats["ogg"] is vorbis_available()
