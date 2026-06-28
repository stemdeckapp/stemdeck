from __future__ import annotations

import pytest

from app.pipeline.download import InvalidYouTubeURL, validate_youtube_url


@pytest.mark.parametrize(
    "url,expected",
    [
        (
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        ),
        (
            "https://youtu.be/dQw4w9WgXcQ",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        ),
        (
            "https://m.youtube.com/watch?v=dQw4w9WgXcQ&list=PLfoo",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        ),
        (
            "https://music.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        ),
        (
            "  https://www.youtube.com/watch?v=dQw4w9WgXcQ  ",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        ),
        (
            "https://www.youtube.com/shorts/dQw4w9WgXcQ",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        ),
        (
            "https://m.youtube.com/shorts/dQw4w9WgXcQ",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        ),
    ],
)
def test_accepts_youtube_urls(url: str, expected: str) -> None:
    assert validate_youtube_url(url) == expected


@pytest.mark.parametrize(
    "url,reason_substring",
    [
        ("", "required"),
        ("   ", "required"),
        ("not a url", "http"),
        ("ftp://youtube.com/watch?v=dQw4w9WgXcQ", "http"),
        ("https://example.com/foo", "unsupported host"),
        ("https://www.youtube.com/playlist?list=PLfoo", "video ID"),
        ("https://evil.com/watch?v=dQw4w9WgXcQ", "unsupported host"),
        # The on.soundcloud.com share shortener is rejected — it redirects to
        # arbitrary targets, an SSRF vector once handed to yt-dlp (#173).
        ("https://on.soundcloud.com/abc123", "unsupported host"),
    ],
)
def test_rejects_bad_urls(url: str, reason_substring: str) -> None:
    with pytest.raises(InvalidYouTubeURL) as exc:
        validate_youtube_url(url)
    assert reason_substring in str(exc.value)


@pytest.mark.parametrize(
    "url",
    [
        "https://soundcloud.com/artist/track",
        "https://www.soundcloud.com/artist/track",
        "  https://soundcloud.com/artist/track  ",
    ],
)
def test_accepts_soundcloud_urls(url: str) -> None:
    result = validate_youtube_url(url)
    assert result == url.strip()
