"""A source's own artist, title, album and lyrics, read at import (#699).

The Lyrics tab and the artist box fill themselves from these, so what matters
is that each container's way of spelling a tag comes out the same, that
nothing a tag carries can be more than plain text of a sane length, and that a
source with no tags, or one ffprobe cannot read, changes nothing about the
import.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from app.core.config import AUDIO_TAG_LYRICS_MAX_CHARS, AUDIO_TAG_MAX_CHARS
from app.core.models import Job
from app.pipeline.audio_tags import probe_tags, tags_from_probe, tags_from_ytdlp
from tests.ffmpeg_probe import skip_without_ffmpeg

# ── tags_from_probe ──


def test_an_mp3s_lyrics_frame_is_read_whatever_its_language():
    tags = tags_from_probe(
        {"artist": "Dream Theater", "title": "Pull Me Under", "lyrics-eng": "Lost in the sky"}
    )
    assert tags == {
        "artist": "Dream Theater",
        "title": "Pull Me Under",
        "lyrics": "Lost in the sky",
    }


def test_flac_tags_are_read_whatever_their_case():
    tags = tags_from_probe({"ARTIST": "Queen", "ALBUM": "Jazz", "UNSYNCEDLYRICS": "Words"})
    assert tags == {"artist": "Queen", "album": "Jazz", "lyrics": "Words"}


def test_album_artist_stands_in_when_there_is_no_artist():
    assert tags_from_probe({"album_artist": "Adele", "title": "Hello"}) == {
        "artist": "Adele",
        "title": "Hello",
    }


def test_no_useful_tags_gives_none():
    assert tags_from_probe({"encoder": "Lavf60", "comment": "ripped"}) is None
    assert tags_from_probe({}) is None
    assert tags_from_probe(None) is None


def test_tag_text_is_plain_and_capped():
    tags = tags_from_probe(
        {
            "artist": "  Band\x00\x1bName  ",
            "title": "x" * (AUDIO_TAG_MAX_CHARS + 50),
            "lyrics": "line one\r\nline two" + "y" * AUDIO_TAG_LYRICS_MAX_CHARS,
        }
    )
    assert tags["artist"] == "BandName"
    assert len(tags["title"]) == AUDIO_TAG_MAX_CHARS
    assert tags["lyrics"].startswith("line one\nline two")
    assert len(tags["lyrics"]) == AUDIO_TAG_LYRICS_MAX_CHARS


# ── tags_from_ytdlp ──


def test_youtube_music_metadata_is_read():
    info = {"artist": "Dream Theater", "track": "Metropolis", "album": "Images and Words"}
    assert tags_from_ytdlp(info) == {
        "artist": "Dream Theater",
        "title": "Metropolis",
        "album": "Images and Words",
    }


def test_the_first_of_several_artists_when_there_is_no_artist_field():
    assert tags_from_ytdlp({"artists": ["Jay-Z", "Alicia Keys"], "track": "Empire"}) == {
        "artist": "Jay-Z",
        "title": "Empire",
    }


def test_the_channel_is_not_taken_for_the_artist():
    info = {"uploader": "Roadrunner Records", "channel": "Roadrunner Records", "title": "x"}
    assert tags_from_ytdlp(info) is None


def test_an_album_alone_is_not_enough():
    assert tags_from_ytdlp({"album": "Greatest Hits"}) is None


# The channel, when the video corroborates it. Found live on
# https://www.youtube.com/watch?v=pkcJEvMcnEg: no music fields at all.


def test_a_title_that_starts_with_the_channel_names_the_artist():
    info = {
        "artist": None,
        "artists": None,
        "track": None,
        "album": None,
        "channel": "Nirvana",
        "uploader": "Nirvana",
        "title": "Nirvana - Lithium (Official Music Video)",
    }
    # The bracketed noise stays: the page cleans titles, this only splits.
    assert tags_from_ytdlp(info) == {"artist": "Nirvana", "title": "Lithium (Official Music Video)"}


def test_a_vevo_channel_is_its_artist():
    info = {"channel": "TaylorSwiftVEVO", "title": "Taylor Swift \N{EN DASH} Shake It Off"}
    assert tags_from_ytdlp(info) == {"artist": "Taylor Swift", "title": "Shake It Off"}


@pytest.mark.parametrize(
    ("channel", "title"),
    [
        ("Nirvana Official", "NIRVANA | Lithium"),
        ("NirvanaVEVO", "Nirvana \N{EM DASH} Lithium"),
        ("Guns N' Roses", "Guns N Roses: Patience"),
    ],
)
def test_the_channel_name_is_compared_loosely(channel, title):
    tags = tags_from_ytdlp({"channel": channel, "title": title})
    assert tags is not None
    assert tags["title"] in ("Lithium", "Patience")


def test_a_topic_channel_is_its_artist():
    info = {"channel": "Nirvana - Topic", "title": "Lithium"}
    assert tags_from_ytdlp(info) == {"artist": "Nirvana", "title": "Lithium"}


def test_a_label_channel_is_not_the_artist_of_what_it_uploads():
    info = {"channel": "Roadrunner Records", "title": "Slipknot - Duality"}
    assert tags_from_ytdlp(info) is None


@pytest.mark.parametrize(
    ("channel", "title", "tags"),
    [
        # The song in marks straight after the name.
        (
            "周杰倫 Jay Chou",
            "周杰倫 Jay Chou【晴天 Sunny Day】-Official Music Video",
            {"artist": "周杰倫 Jay Chou", "title": "【晴天 Sunny Day】-Official Music Video"},
        ),
        ("YOASOBI", "YOASOBI「群青」Official Music Video", {"artist": "YOASOBI"}),
        # What a Japanese or Chinese channel adds to the name.
        ("King Gnu official YouTube channel", "King Gnu - 白日", {"artist": "King Gnu"}),
        ("宇多田ヒカル 公式チャンネル", "宇多田ヒカル - First Love", {"artist": "宇多田ヒカル"}),
        ("Edith Piaf Officiel", "Edith Piaf - La vie en rose", {"artist": "Edith Piaf"}),
        # East Asian separators.
        ("IU", "IU _ Palette", {"artist": "IU", "title": "Palette"}),
        ("YOASOBI", "YOASOBI／群青", {"artist": "YOASOBI", "title": "群青"}),
        # A title typed without its letters: the channel's own spelling.
        (
            "Dawid Podsiadło",
            "Dawid Podsiadlo - Małomiasteczkowy",
            {"artist": "Dawid Podsiadło", "title": "Małomiasteczkowy"},
        ),
        # A handle that runs the words together keeps the title's spacing:
        # "NataliaKukulska" found nothing on MusicBrainz or LRCLIB.
        (
            "NataliaKukulska",
            "Natalia Kukulska - W Biegu",
            {"artist": "Natalia Kukulska", "title": "W Biegu"},
        ),
        # Accents without the spaces: the readable name wins.
        ("DawidPodsiadło", "Dawid Podsiadlo - Małomiasteczkowy", {"artist": "Dawid Podsiadlo"}),
        # A suffix that is part of the name is not lost.
        ("Roxy Music", "Roxy Music - More Than This", {"artist": "Roxy Music"}),
    ],
)
def test_an_artists_own_channel_is_read_in_every_script(channel, title, tags):
    found = tags_from_ytdlp({"channel": channel, "title": title})
    assert found is not None
    assert {k: found[k] for k in tags} == tags


def test_a_k_pop_label_channel_is_not_the_artist():
    for channel in ("HYBE LABELS", "1theK (원더케이)", "SMTOWN", "JVR Music"):
        info = {"channel": channel, "title": "NewJeans (뉴진스) 'Ditto' Official MV (side A)"}
        assert tags_from_ytdlp(info) is None


def test_a_fan_upload_is_not_corroborated():
    info = {"channel": "grungefan1991", "uploader": "grungefan1991", "title": "Nirvana - Lithium"}
    assert tags_from_ytdlp(info) is None
    # Nor is a channel that only starts the way the title does.
    info = {"channel": "Nirvana", "title": "Nirvana Tribute Band - Lithium"}
    assert tags_from_ytdlp(info) is None


def test_the_music_fields_still_win_over_the_channel():
    info = {
        "artist": "Nirvana",
        "track": "Lithium",
        "channel": "Nirvana",
        "title": "Nirvana - Lithium (Live at Reading)",
    }
    assert tags_from_ytdlp(info) == {"artist": "Nirvana", "title": "Lithium"}


# ── probe_tags ──


def test_probe_reads_a_real_files_tags(tmp_path):
    skip_without_ffmpeg()
    from app.core.config import ffmpeg_executable

    path = tmp_path / "tagged.mp3"
    subprocess.run(
        [
            ffmpeg_executable(),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-metadata",
            "artist=Dream Theater",
            "-metadata",
            "title=Pull Me Under",
            "-metadata",
            "album=Images and Words",
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    assert probe_tags(path) == {
        "artist": "Dream Theater",
        "title": "Pull Me Under",
        "album": "Images and Words",
    }


def test_probe_never_raises(tmp_path):
    junk = tmp_path / "junk.mp3"
    junk.write_bytes(b"not audio")
    assert probe_tags(junk) is None
    with patch("app.pipeline.audio_tags.subprocess.run", side_effect=OSError("no ffprobe")):
        assert probe_tags(junk) is None
    with patch(
        "app.pipeline.audio_tags.subprocess.run",
        side_effect=subprocess.TimeoutExpired("ffprobe", 1),
    ):
        assert probe_tags(junk) is None
    # JSON, but not the shape ffprobe writes.
    for stdout in ("[]", '{"format": []}', '{"format": {"tags": "x"}}'):
        done = subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")
        with patch("app.pipeline.audio_tags.subprocess.run", return_value=done):
            assert probe_tags(junk) is None


# ── on the job ──


def test_tags_are_in_the_state_the_page_reads_and_the_record_kept():
    job = Job(id="abc123abc123", audio_tags={"artist": "Queen"})
    assert job.to_state()["audio_tags"] == {"artist": "Queen"}
    assert Job.from_record(job.to_record()).audio_tags == {"artist": "Queen"}


@pytest.fixture
def upload_client(tmp_path, monkeypatch):
    import app.core.config as cfg

    monkeypatch.setattr(cfg, "JOBS_DIR", tmp_path)
    with (
        patch("app.api.jobs.jobqueue.enqueue", lambda job_id: None),
        patch("app.api.jobs._probe_duration", return_value=60.0),
        patch(
            "app.api.jobs.probe_tags",
            return_value={"artist": "Dream Theater", "title": "Pull Me Under"},
        ),
    ):
        from fastapi.testclient import TestClient

        from app.main import app

        with TestClient(app) as c:
            yield c


def test_an_upload_keeps_its_tags(upload_client):
    r = upload_client.post(
        "/api/jobs",
        files={"file": ("song.mp3", b"ID3 fake bytes", "audio/mpeg")},
    )
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    state = upload_client.get(f"/api/jobs/{job_id}").json()
    assert state["audio_tags"] == {"artist": "Dream Theater", "title": "Pull Me Under"}
