from __future__ import annotations

import dataclasses
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from app.core.config import AUDIO_TAG_LYRICS_MAX_CHARS, AUDIO_TAG_MAX_CHARS


class JobCancelled(Exception):
    """Raised inside a pipeline stage when the job's cancel flag is set."""


JobStatus = Literal[
    "queued", "downloading", "analyzing", "separating", "processing", "done", "error", "cancelled"
]

# A Wikidata item id. Anything else is not a band this app found.
_WIKIDATA_ID_RE = re.compile(r"^Q\d{1,12}$")
# A band's name, as kept on the job. Longer than this is not a name.
_ARTIST_NAME_MAX_CHARS = 300


def clean_artist(value: Any) -> dict[str, str] | None:
    """A band as kept on a job, {"id", "name", "englishName"}, or None.

    For anything read back from disk (the registry, metadata.json), where a
    hand-edited or damaged file must not put a malformed band in front of the
    page: the id has to be a Wikidata item, the names plain strings.
    """
    if not isinstance(value, dict):
        return None
    band_id = value.get("id")
    if not isinstance(band_id, str) or not _WIKIDATA_ID_RE.match(band_id):
        return None
    names = {}
    for key in ("name", "englishName"):
        name = value.get(key)
        names[key] = name.strip()[:_ARTIST_NAME_MAX_CHARS] if isinstance(name, str) else ""
    if not names["name"] and not names["englishName"]:
        return None
    return {"id": band_id, **names}


# The tags a file or a video names (audio_tags.py), and how long each may be:
# the lyrics a tag can carry are longer than any name.
_AUDIO_TAG_LIMITS = {
    "artist": AUDIO_TAG_MAX_CHARS,
    "title": AUDIO_TAG_MAX_CHARS,
    "album": AUDIO_TAG_MAX_CHARS,
    "lyrics": AUDIO_TAG_LYRICS_MAX_CHARS,
}


def clean_audio_tags(value: Any) -> dict[str, str] | None:
    """A job's tags as read back from disk, or None: known keys only, each a
    non-empty string within its limit. A damaged file must not reach the
    lyrics lookup, which reads them as a dict of strings."""
    if not isinstance(value, dict):
        return None
    tags = {
        key: text[:limit]
        for key, limit in _AUDIO_TAG_LIMITS.items()
        if isinstance(text := value.get(key), str) and text.strip()
    }
    return tags or None


# A MusicBrainz id (recording, artist, release group): a lower-case UUID.
MBID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
# "lrclib": named by LRCLIB's artist and album for a title MusicBrainz could
# not place (title_parse.py, lyrics_lookup.identify_on_lrclib).
IDENTITY_SOURCES = ("acoustid", "musicbrainz", "lrclib", "tags")
# More artists than this on one recording is not a credit worth keeping.
_IDENTITY_MAX_ARTISTS = 20
_IDENTITY_MAX_TYPES = 12
_IDENTITY_MAX_TITLE_ALIASES = 6
# Before sound recording, a year is a typo or a placeholder.
_IDENTITY_MIN_YEAR = 1860


def _identity_text(value: Any) -> str:
    return value.strip()[:_ARTIST_NAME_MAX_CHARS] if isinstance(value, str) else ""


def _identity_mbid(value: Any) -> str | None:
    return value if isinstance(value, str) and MBID_RE.match(value) else None


def clean_identity(value: Any) -> dict[str, Any] | None:
    """What a job was identified as, in the one shape the design fixes, or None.

    {"source", "score", "recording_mbid", "title", "artist", "artist_mbids",
    "album", "release_group_mbid", "release_group_type", "secondary_types",
    "year", "duration"}, and "title_aliases" when there are any, with "year"
    the album's first release. Used for everything written (the pipeline
    builds identities through it) and everything read back from disk, so a damaged or
    hand-edited record never reaches the page, the band lookup or the lyrics
    lookup in any other shape. A title and an artist are required: an identity
    without both names nothing.
    """
    if not isinstance(value, dict) or value.get("source") not in IDENTITY_SOURCES:
        return None
    title = _identity_text(value.get("title"))
    artist = _identity_text(value.get("artist"))
    if not title or not artist:
        return None
    score = value.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
        score = 0.0
    duration = value.get("duration")
    if (
        isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not 0 < duration < 86400
    ):
        duration = None
    year = value.get("year")
    if (
        isinstance(year, bool)
        or not isinstance(year, int)
        or not _IDENTITY_MIN_YEAR <= year <= 9999
    ):
        year = None
    mbids = value.get("artist_mbids")
    types = value.get("secondary_types")
    aliases: list[str] = []
    for alias in (
        value.get("title_aliases") or [] if isinstance(value.get("title_aliases"), list) else []
    ):
        text = _identity_text(alias)
        if text and text != title and text not in aliases:
            aliases.append(text)
    aliases = aliases[:_IDENTITY_MAX_TITLE_ALIASES]
    return {
        "source": value["source"],
        "score": round(max(0.0, min(1.0, float(score))), 4),
        "recording_mbid": _identity_mbid(value.get("recording_mbid")),
        "title": title,
        "artist": artist,
        "artist_mbids": [m for m in mbids if _identity_mbid(m)][:_IDENTITY_MAX_ARTISTS]
        if isinstance(mbids, list)
        else [],
        "album": _identity_text(value.get("album")) or None,
        "release_group_mbid": _identity_mbid(value.get("release_group_mbid")),
        "release_group_type": _identity_text(value.get("release_group_type")) or None,
        "secondary_types": [t for t in (_identity_text(t) for t in types) if t][
            :_IDENTITY_MAX_TYPES
        ]
        if isinstance(types, list)
        else [],
        "year": year,
        "duration": round(float(duration), 3) if duration is not None else None,
        # Other titles the song goes by, when any are known: the upload's own
        # ("Good Day (좋은 날)") and its MusicBrainz work's. For the lyrics
        # lookup, since LRCLIB files a song under any of them. Left out when
        # there are none, so an identity without them keeps its old shape.
        **({"title_aliases": aliases} if aliases else {}),
    }


# What a work (app/pipeline/work_lookup.py) can be.
WORK_KINDS = ("musical", "film", "tv", "other")


def clean_work(value: Any) -> dict[str, str] | None:
    """The work a job's song is from, {"id", "kind", "name", "englishName"},
    or None. clean_artist's rules, and a kind from WORK_KINDS."""
    work = clean_artist(value)
    if work is None or not isinstance(value, dict) or value.get("kind") not in WORK_KINDS:
        return None
    return {
        "id": work["id"],
        "kind": value["kind"],
        "name": work["name"],
        "englishName": work["englishName"],
    }


def _set(job: Job, **fields: object) -> None:
    """Mutate Job fields, then bump job.version so the SSE stream (#289) can
    detect the change with a cheap int compare instead of re-serializing on
    every tick. Incrementing after every field is written also closes #285:
    a snapshot taken mid-call (torn read) sees a version that doesn't match
    the version read before serializing, so the SSE loop discards it and
    re-serializes once this call has fully landed."""
    for k, v in fields.items():
        if k == "stage":
            job.stage_message = v  # type: ignore[assignment]
        else:
            setattr(job, k, v)
    job.version += 1


@dataclass
class Job:
    id: str
    status: JobStatus = "queued"
    progress: float = 0.0
    stage_message: str = "Queued"
    title: str | None = None
    duration_sec: float | None = None
    thumbnail: str | None = None
    bpm: int | None = None
    key: str | None = None
    scale: str | None = None  # "Major" / "Natural Minor"
    key_confidence: int | None = None  # 0-100 percent
    lufs: float | None = None  # ITU-R BS.1770 integrated loudness (dB)
    peak_db: float | None = None  # sample peak in dBFS (close to true peak)
    dynamic_range: float | None = None  # peak_db - integrated LUFS (dB)
    tempo_stability: int | None = None  # 0-100, beat interval consistency
    stem_presence: dict[str, int] | None = None  # per-stem RMS 0-100
    sections: list[dict] | None = None  # [{id, name, kind?, start, end, color}]
    # Whether this job should run the automatic song-structure pass, captured
    # from the setting when the job is created rather than read when the stage
    # is reached. The stage runs at the very end of the pipeline, minutes after
    # submit, and the toggle is a per-import choice that clears itself: reading
    # it late let a job lose a pass the user had asked and waited for.
    auto_sections: bool = False
    sections_source: Literal["automatic", "manual"] | None = None
    tags: list[str] | None = None  # YouTube tags + categories, lowercased, max 8
    stems: list[dict[str, str]] = field(default_factory=list)
    # Subset of stems the user chose at submit. The pipeline produces all
    # 6 regardless (Demucs htdemucs_6s is fixed), but after collect we
    # mix down only the selected ones into mix.wav so the user can
    # download a single track containing just their chosen stems.
    selected_stems: list[str] = field(default_factory=list)
    mix_url: str | None = None  # populated when a strict subset was selected
    source_url: str | None = None  # original URL or "local:<filename>" for file uploads
    # An upload's file format ("wav", "mp3", ...), recorded from the extension
    # the upload was validated against. source_url cannot carry it: its title
    # has the extension removed, so "Hollow Veins.wav" is "local:Hollow Veins"
    # and anything that read the format out of it found nothing (#690). None
    # for a link, and for an upload older than this field until its state is
    # first served (see _job_state).
    source_format: str | None = None
    # What the source said about itself (#699): {"artist", "title", "album",
    # "lyrics"}, each only when present. An upload's container tags, read
    # before the upload is deleted; a link's music metadata from yt-dlp. The
    # Lyrics tab and the artist box fill themselves from it. None when the
    # source had none, and for anything imported before it was read.
    audio_tags: dict[str, str] | None = None
    # Which recording this is, found while the job ran (app/pipeline/identify.py):
    # by audio fingerprint on AcoustID when the user set a key, else a
    # confident MusicBrainz search by the tags, else the tags alone. The shape
    # is clean_identity's. None when nothing names the track.
    identity: dict[str, Any] | None = None
    # The band audio_tags' artist names, found on Wikidata while the job ran
    # (#699): {"id": "Q...", "name", "englishName"}. Only ever an exact match
    # for the tag, so a wrong band is never saved with nobody looking. None
    # when there was no artist tag, no such band, or no connection; the page
    # then looks for it itself, and a band saved there always wins over this.
    artist: dict[str, str] | None = None
    # The musical, film or series the song is from, when the recording is a
    # soundtrack or a cast recording (app/pipeline/work_lookup.py): clean_work's
    # {"id": "Q...", "kind", "name", "englishName"}. None for anything else.
    work: dict[str, str] | None = None
    # Whether the job has lyrics.json beside its stems (lyrics_lookup.py). The
    # lyrics themselves stay in that file: they run to kilobytes, and this
    # record is rewritten whole on every save.
    has_lyrics: bool = False
    # True when a silent video track (video.mp4) was preserved from an .mp4
    # upload, enabling the "Export Mix (with video)" MP4 export.
    has_video: bool = False
    # Why has_video is what it is (#436). None when video was never attempted
    # (SoundCloud, a non-mp4 upload); "ok" when a track was preserved;
    # "unavailable" when the source simply offers no video stream; "failed"
    # when the fetch or extract errored.
    #
    # has_video alone collapses the last two into the same silent absence, so a
    # user who imported a track specifically to export a karaoke video could not
    # tell "this never had video" from "the video fetch broke".
    video_status: str | None = None
    error: str | None = None
    # Classified failure cause + last stderr line (e.g. "out-of-memory — ...").
    # Shown by the UI as a secondary line under the generic error message so
    # failures are actionable instead of uniformly opaque.
    error_detail: str | None = None
    # Device the separation actually ran on ("cuda" / "mps" / "cpu"), recorded
    # per job for diagnostics -- settings may change between jobs.
    compute_device: str | None = None
    # True when a GPU separation attempt failed and the job completed on the
    # CPU fallback (#276) -- kept loud in state/metadata so the fallback is
    # never silent (the #247 lesson).
    gpu_fallback: bool = False
    # Wall-clock seconds per pipeline stage ({"download": 12.3, ...}); written
    # to metadata.json and the one-line completion summary in the log.
    stage_timings: dict[str, float] | None = None
    # On-demand lead/backing vocal split (#275) -- a post-hoc action on an
    # already-"done" job, not part of the main pipeline. "none" until the user
    # asks for it; "error" leaves the job's base stems untouched (see
    # app/pipeline/vocal_split.py) and is recorded in stems/vocal_split_error.txt,
    # not job.error_detail, since the job itself did not fail.
    vocal_split: Literal["none", "running", "done", "error"] = "none"
    # When the user put this job in the Trash, or None if they have not.
    #
    # Server-side on purpose. The Trash used to live only in the browser's
    # catalog store, which is per-device: a track deleted on the desktop was
    # still returned by GET /api/jobs, so the phone -- which builds its library
    # straight from that endpoint -- listed everything the user thought they
    # had thrown away. Two UIs, two answers to "what is in my library".
    #
    # A timestamp rather than a bool so the Trash can say when, and so a future
    # auto-purge has something to work from.
    trashed_at: float | None = None
    # Set by POST /api/jobs/{id}/cancel; consumed by pipeline stages.
    # Not surfaced via to_state() -- it's internal control state.
    cancel_requested: bool = False
    # Bumped by _set() on every field write (#289). Internal dirty-flag /
    # tear-detection state for the SSE stream -- not surfaced via to_state()
    # or persisted, same as cancel_requested.
    version: int = 0
    # Place in the waiting queue, rewritten whenever the queue changes. Only
    # exists so a reordered queue comes back in the user's order rather than
    # submission order after a restart; the position the UI shows is derived
    # from the live deque. Old records default to 0, where created_at decides.
    queue_position: int = 0
    # How many times a restart has put this job back in the queue. Persisted,
    # so a job that reliably kills the process is failed rather than retried on
    # every start. Old records without the field default to 0 via from_record.
    resume_attempts: int = 0
    # Wall-clock timestamps for metadata-based sweep -- more predictable
    # than directory mtime, which can be touched by unrelated FS events.
    created_at: float = field(default_factory=time.time)

    def to_state(self) -> dict[str, Any]:
        return {
            "job_id": self.id,
            "status": self.status,
            "progress": self.progress,
            "stage": self.stage_message,
            "title": self.title,
            "duration": self.duration_sec,
            "thumbnail": self.thumbnail,
            "bpm": self.bpm,
            "key": self.key,
            "scale": self.scale,
            "key_confidence": self.key_confidence,
            "lufs": self.lufs,
            "peak_db": self.peak_db,
            "dynamic_range": self.dynamic_range,
            "tempo_stability": self.tempo_stability,
            "stem_presence": self.stem_presence,
            "sections": self.sections,
            "sections_source": self.sections_source,
            "tags": self.tags,
            "stems": self.stems,
            "selected_stems": self.selected_stems,
            "mix_url": self.mix_url,
            "source_url": self.source_url,
            "source_format": self.source_format,
            "audio_tags": self.audio_tags,
            "identity": self.identity,
            "artist": self.artist,
            "work": self.work,
            "has_lyrics": self.has_lyrics,
            "has_video": self.has_video,
            "video_status": self.video_status,
            "error": self.error,
            "error_detail": self.error_detail,
            "compute_device": self.compute_device,
            "gpu_fallback": self.gpu_fallback,
            "stage_timings": self.stage_timings,
            "vocal_split": self.vocal_split,
            "trashed_at": self.trashed_at,
            "created_at": self.created_at,
        }

    def to_queue_state(self) -> dict[str, Any]:
        """The compact record the queue view needs. Deliberately not to_state():
        the queue stream carries every waiting job several times a second, and
        stems/sections/analysis are only meaningful once a job is done."""
        return {
            "job_id": self.id,
            "status": self.status,
            "progress": self.progress,
            "stage": self.stage_message,
            "title": self.title,
            "thumbnail": self.thumbnail,
            "source_url": self.source_url,
            "error": self.error,
        }

    def to_record(self) -> dict[str, Any]:
        return {field: getattr(self, field) for field in _JOB_FIELDS}

    @classmethod
    def from_record(cls, data: dict[str, Any]) -> Job:
        fields = {key: value for key, value in data.items() if key in _JOB_FIELDS}
        job_id = str(fields.pop("id", "")).strip()
        if not job_id:
            raise ValueError("job record missing id")
        job = cls(id=job_id)
        for key, value in fields.items():
            setattr(job, key, value)
        job.artist = clean_artist(job.artist)
        job.identity = clean_identity(job.identity)
        job.work = clean_work(job.work)
        job.audio_tags = clean_audio_tags(job.audio_tags)
        job.cancel_requested = False
        return job


_JOB_FIELDS = frozenset(
    f.name for f in dataclasses.fields(Job) if f.name not in ("cancel_requested", "version")
)
