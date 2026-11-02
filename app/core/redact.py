"""Stripping locally- or personally-identifying text before it can reach a
public report.

The notification centre's "Report on GitHub"/"Report on Discord" flow and its
opt-in "include recent logs" button both end with this text pasted somewhere
public. Three things in particular have no business being there:

  - The reporter's home directory. On Windows the Python install path alone
    (`C:\\Users\\<name>\\...`) carries the OS username.
  - A track's source URL. `download.py` logs it for every job
    ("download starting: <url>"), so a raw log tail carries the YouTube/
    SoundCloud link for everything the reporter has ever imported in the
    fetched window -- not just the one that failed. The per-job failure
    report has always kept this out deliberately (see
    app/api/jobs.py's _FAILURE_PUBLIC_KEYS); a raw log has no such allowlist
    of its own, so it is enforced here instead.
  - An IP address. The mobile UI talks to this backend over the LAN when
    network access is on, so uvicorn's access log (captured into
    backend.log on desktop) can carry another device's address on the
    reporter's home network, not just their own.

Paths are matched in any spelling a log carries them in (either slash,
doubled backslashes from repr(), any case) and replaced by a placeholder
that says which folder it was, so the rest of the path stays readable. The
user name is only removed as a whole path segment: replacing the bare word
would rewrite every word that contains it, and a short or common name would
shred the whole log.

Every place that can hand text to that report flow -- the per-job failure
evidence (runner.py), the Settings -> Logs viewer and the Export logs zip
(main.py) -- redacts through this one function rather than each carrying
its own copy.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.core.config import CACHE_DIR, DATA_DIR, JOBS_DIR, MODELS_DIR

# Mirrors app/pipeline/download.py's _YOUTUBE_HOSTS | _SOUNDCLOUD_HOSTS. Kept
# as a literal copy rather than importing it: this module is used from
# main.py's request path, and pipeline.download pulls in yt_dlp, which is not
# something a log-tail redaction pass should need to import.
_SOURCE_URL_RE = re.compile(
    r"https?://(?:[\w-]+\.)*"
    r"(?:youtube\.com|youtu\.be|youtube-nocookie\.com|soundcloud\.com)\S*",
    re.IGNORECASE,
)

# IPv4 only. The app's own LAN detection (app/main.py's _is_lan_ipv4) is
# IPv4-only too, and a loose IPv6 pattern is a real hazard here: something
# like "(?:[0-9a-f]{1,4}:){2,7}[0-9a-f]{1,4}" also matches an HH:MM:SS
# timestamp (every log line has one), which would mangle every line's time
# instead of catching the rare home IPv6 address.
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")

# Every path pattern starts on a plain character, with any lookbehind after it
# and ignore-case scoped to the rest: a leading lookbehind or a global
# ignore-case flag stops the scan skipping ahead, about 4x slower on a 5 MB log.
_SEP = r"[\\/]+"
# Not glued to a name before it: /data, but not the end of /srv/data.
_AT_START = r"(?<![\w.~-].)"
# The segment ends here: jobs and "jobs." at the end of a sentence, but not
# jobs2 or jobs.json.
_AT_END = r"(?![\w-]|\.\w)"
# One path segment: anything up to the next separator, quote, space or a
# character no file name can hold. It does not end on a dot (Windows forbids
# that), so a sentence's full stop stays. A name with a space in it
# (C:\Users\John Smith) keeps its second word, unless it is this process's
# own home, which the exact folder match covers.
_SEGMENT = r"[^\\/\s\"'<>|:*?]*[^\\/\s\"'<>|:*?.]"
# Anyone's home, not only this process's: another account, a log from another
# machine, an 8.3 short name. The folder after Users or home is the user name.
# The drive letter must not end a word, so https://users/... is not a drive.
_WIN_HOME_RE = re.compile(
    rf"[A-Za-z](?<!\w.):{_SEP}(?i:Users|Documents and Settings){_SEP}{_SEGMENT}"
)
# Also the WSL, MSYS and Cygwin spellings of a Windows drive.
_POSIX_HOME_RE = re.compile(rf"/{_AT_START}(?:(?:mnt/|cygdrive/)?[a-z]/)?(?:Users|home)/{_SEGMENT}")
# \\server\share, //server/share and \\?\UNC\server\share. The lookbehind
# keeps a URL's // and a doubled separator inside a repr() path
# (C:\\StemDeck\\jobs) from reading as one.
_UNC_RE = re.compile(rf"[\\/](?<![\w:\\/].)[\\/]+(?:\?[\\/]+UNC[\\/]+)?{_SEGMENT}[\\/]+{_SEGMENT}")


def _path_re(path: Path) -> re.Pattern[str]:
    """`path` in either slash, any case, with repr()'s doubled backslashes,
    from a segment start to a segment end."""
    head, *rest = re.split(r"[\\/]+", str(path))
    # head is the drive (C:), or empty for a POSIX root or a UNC share.
    if head:
        start = rf"[{head[0].upper()}{head[0].lower()}]{_AT_START}{re.escape(head[1:])}{_SEP}"
    else:
        # Only the first separator of a run may start a match; otherwise
        # every slash in a long run rescans the rest of it (quadratic).
        start = r"[\\/](?<![\w.~\\/-].)[\\/]*"
    return re.compile(rf"{start}(?i:{_SEP.join(map(re.escape, rest))}){_AT_END}")


def redact(text: str) -> str:
    """Strip the app's folders, any user's home folder and name, network
    shares, any YouTube/SoundCloud source URL, and any IPv4 address from
    `text`."""
    home = Path.home()
    folders = [
        (JOBS_DIR, "<jobs>"),
        (CACHE_DIR, "<cache>"),
        (MODELS_DIR, "<models>"),
        (DATA_DIR, "<data>"),
        (home, "<home>"),
    ]
    # Longest first, so a folder inside another (jobs inside data, anything
    # inside home) keeps its own name.
    for folder, placeholder in sorted(folders, key=lambda f: len(str(f[0])), reverse=True):
        if folder.name:
            text = _path_re(folder).sub(placeholder, text)
    text = _WIN_HOME_RE.sub("<home>", text)
    text = _POSIX_HOME_RE.sub("<home>", text)
    text = _UNC_RE.sub("<unc>", text)
    if home.name:
        text = re.sub(rf"([\\/])(?i:{re.escape(home.name)}){_AT_END}", r"\1<user>", text)
    text = _SOURCE_URL_RE.sub("<source-url-redacted>", text)
    text = _IPV4_RE.sub("<ip>", text)
    return text
