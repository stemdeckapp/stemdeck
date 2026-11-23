"""The opt-in for online sources (#1089): every use of yt-dlp passes here.

Search, its previews, importing a link, expanding a playlist, fetching a
link's tags and the download itself all reach YouTube or SoundCloud through
yt-dlp. They run only while the user has turned online sources on in
Settings, Experimental (app/core/settings.py, `online_sources`), read on each
call so turning it on or off applies to the next request without a restart.

Enforced in two places on purpose. Each endpoint that would start yt-dlp
refuses first, with 403 and the code below, so the page can say why. And
`_base_ydl_opts` in app/pipeline/download.py, which every YoutubeDL is built
from, refuses again: a link queued before the setting was turned off, or one
waiting in the registry from before the update, must not be fetched either.
Local file upload never comes here.
"""

from __future__ import annotations

from app.core.settings import get_online_sources

# The code the page reads to show its own sentence (static/js/errorReason.js).
ONLINE_SOURCES_OFF_CODE = "online_sources_off"
# The cause a link job carries in error_detail when its turn came while they
# were off: it is then "stopped", not failed (app/pipeline/runner.py), and the
# page tells the reason by this code (errorReason.js CAUSE_KEYS).
ONLINE_SOURCES_OFF_CAUSE = "online-sources-off"
# A fixed sentence, safe to show and to log. It names no path and no link.
ONLINE_SOURCES_OFF_DETAIL = (
    "Online search and link import are off. Turn them on in Settings, Experimental."
)


class OnlineSourcesOff(RuntimeError):
    """yt-dlp was about to run while online sources are off. Answered as 403
    "online_sources_off" by the handler in app/main.py."""

    def __init__(self) -> None:
        super().__init__(ONLINE_SOURCES_OFF_DETAIL)


def require_online_sources() -> None:
    """Raise OnlineSourcesOff unless the user turned online sources on."""
    if not get_online_sources():
        raise OnlineSourcesOff()
