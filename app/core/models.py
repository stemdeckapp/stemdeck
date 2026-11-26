from __future__ import annotations

import dataclasses
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from app.core.config import (
    AUDIO_TAG_LYRICS_MAX_CHARS,
    AUDIO_TAG_MAX_CHARS,
    MIDI_PIANO_ENGINES,
    MIDI_STEMS,
)
from app.core.versions import VERSION_ID_RE, active_of, clean_versions, versions_of
from app.core.vocal_split_outcome import INTERRUPTED, message_for


class JobCancelled(Exception):
    """Raised inside a pipeline stage when the job's cancel flag is set."""


class JobBusy(Exception):
    """A finished song is being worked on (Job.post_hoc_running), so its folder
    must not be deleted, moved or reset away. Answered as 409 "job_busy"."""


JobStatus = Literal[
    "queued",
    "downloading",
    "analyzing",
    "separating",
    "processing",
    "done",
    "error",
    "cancelled",
    "stopped",
]

# Statuses no pipeline will move on from. "stopped" is a job halted because the
# user put it in the Trash (#748): unlike "cancelled", its files stay on disk,
# because only emptying the Trash deletes anything.
FINISHED_STATUSES = frozenset(("done", "error", "cancelled", "stopped"))

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


def settle_cancelled(job: Job) -> bool:
    """Mark a job whose cancel took effect. Returns whether its files go too.

    A plain cancel ends "cancelled" and its directory is removed. A stop that
    came from the Trash ends "stopped" with the directory kept (#748): trashing
    is reversible, and only emptying the Trash deletes files, the rule the
    finished tracks in the Trash have always followed."""
    if job.stop_requested:
        _set(job, status="stopped", stage="Stopped")
        return False
    _set(job, status="cancelled", stage="Cancelled")
    return True


# Bumped when the loudness card's measurement changes, so songs analysed with
# an older one are measured again. 1 (no field) was the mono 22 kHz, first 180 s
# reading; 2 is BS.1770-4 on the whole stereo mix (analyze.measure_loudness).
LOUDNESS_VERSION = 2
# What a song measured again was read from (Job.loudness_remeasured).
LOUDNESS_BASES = ("source", "stems")


def public_stem_undo(undo: object) -> dict[str, dict[str, Any]]:
    """Which stems have an undo copy, as the page sees it: {stem: {"model",
    "saved_at"}}. The file names and gains stay on the server."""
    if not isinstance(undo, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for stem, entry in undo.items():
        if isinstance(stem, str) and isinstance(entry, dict):
            out[stem] = {"model": entry.get("model"), "saved_at": entry.get("saved_at")}
    return out


# MIDI from a stem (app/pipeline/midi.py): the stems it is made from, what
# a run can be in, and what the page is told about each.
MIDI_STEM_NAMES = MIDI_STEMS
MIDI_RUN_STATES = ("running", "done", "error", "cancelled")
MIDI_RUN_PHASES = ("download", "transcribe")
MIDI_ENGINE_IDS = MIDI_PIANO_ENGINES
_MIDI_CODE_RE = re.compile(r"^[a-z_]{1,32}\Z")
_MIDI_DETAIL_RE = re.compile(r"^[a-z]{1,16}\Z")
_MIDI_AT_MAX = 40


def clean_midi(value: Any) -> dict[str, dict[str, Any]] | None:
    """A job's MIDI runs, {stem: entry}, in the one shape the page reads, or
    None when there are none. For everything written and everything read back
    from disk, so a damaged record never reaches the page in another shape.

    An entry is {"state", "pct", "phase", "notes", "engine", "detail",
    "tempo_source", "at", "error_code", "has_file"}. The state, pct, phase
    (what a running run does: "download" its model, or "transcribe"; None
    otherwise), engine, detail, at and error_code describe the latest run;
    notes, tempo_source and has_file describe the file on disk, which a
    failed or cancelled run leaves as it was. An entry saved before there
    was a second engine reads as Basic Pitch's, the only one there was."""
    if not isinstance(value, dict):
        return None
    out: dict[str, dict[str, Any]] = {}
    for stem in MIDI_STEM_NAMES:
        entry = value.get(stem)
        if not isinstance(entry, dict) or entry.get("state") not in MIDI_RUN_STATES:
            continue
        pct = entry.get("pct")
        if isinstance(pct, bool) or not isinstance(pct, (int, float)) or not math.isfinite(pct):
            pct = 0
        notes = entry.get("notes")
        if isinstance(notes, bool) or not isinstance(notes, int) or notes < 0:
            notes = None
        detail = entry.get("detail")
        code = entry.get("error_code")
        at = entry.get("at")
        tempo = entry.get("tempo_source")
        engine = entry.get("engine")
        phase = entry.get("phase")
        running = entry["state"] == "running"
        out[stem] = {
            "state": entry["state"],
            "pct": int(max(0, min(100, pct))),
            "phase": phase if running and phase in MIDI_RUN_PHASES else None,
            "notes": notes,
            "engine": engine if engine in MIDI_ENGINE_IDS else "basic_pitch",
            "detail": detail if isinstance(detail, str) and _MIDI_DETAIL_RE.match(detail) else None,
            "tempo_source": tempo if tempo in ("song", "default") else None,
            "at": at if isinstance(at, str) and len(at) <= _MIDI_AT_MAX else None,
            "error_code": code if isinstance(code, str) and _MIDI_CODE_RE.match(code) else None,
            "has_file": entry.get("has_file") is True,
        }
    return out or None


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
    lufs: float | None = None  # ITU-R BS.1770-4 integrated loudness of the stereo mix (LUFS)
    peak_db: float | None = None  # true peak in dBTP
    dynamic_range: float | None = None  # peak_db - lufs: peak-to-loudness ratio (dB), not LRA
    # Which loudness measurement wrote the three fields above. Songs analysed
    # before 2 hold a mono 22 kHz first-180-s reading, about 4 LU too low; they
    # are measured again the first time they are opened (jobs._upgrade_loudness).
    loudness_version: int | None = None
    # What an older song's numbers were measured again from (#923): "source"
    # for its kept upload, "stems" for the sum of its stems (a link song keeps
    # no source). None when the numbers come from the song's own analysis.
    # Persisted, since the measurement runs after an open and the next open,
    # the first to show the new numbers, is often after a restart. The page
    # tells the user once per song why the card's numbers moved.
    # Here a string, in to_state() two keys: "loudness_remeasured" is a bool
    # (a known basis is stored) and "loudness_basis" carries the string.
    loudness_remeasured: str | None = None
    tempo_stability: int | None = None  # 0-100, beat interval consistency
    stem_presence: dict[str, int] | None = None  # per-stem RMS 0-100
    sections: list[dict] | None = None  # [{id, name, kind?, start, end, color}]
    # Whether this job should run the automatic song-structure pass, captured
    # from the setting when the job is created rather than read when the stage
    # is reached. The stage runs at the very end of the pipeline, minutes after
    # submit, and the toggle is a per-import choice that clears itself: reading
    # it late let a job lose a pass the user had asked and waited for.
    auto_sections: bool = False
    # Whether this job looks for lyrics (LRCLIB, the transcript, the retime),
    # captured from the Detect lyrics setting when the job is created. None
    # for a job made before the setting existed, which looks as it always did.
    auto_lyrics: bool | None = None
    sections_source: Literal["automatic", "manual"] | None = None
    # Points on the timeline the user saved to jump back to (#1075): Tags in
    # the studio, [{id, name, time}]. A section always loops and cannot hold
    # two start points, so these are their own layer, never derived from the
    # audio and never touched by the pipeline. Named for the timeline because
    # `tags` below is already the source's genre words. None until the first
    # one is saved.
    timeline_tags: list[dict] | None = None
    tags: list[str] | None = None  # YouTube tags + categories, lowercased, max 8
    stems: list[dict[str, str]] = field(default_factory=list)
    # Recorded takes, finished ones only, in creation order: the lanes the
    # studio adds after the stems. Each is app.core.takes.take_entry's shape.
    # The take itself (raw capture, sidecar, camera) stays in
    # takes/<take_id>/ beside the stems; this is the index the UI reads.
    takes: list[dict[str, Any]] = field(default_factory=list)
    # Recording tracks, sorted by order: each a mixer lane that plays its
    # active take. Mirrors takes/tracks.json (app.core.takes).
    tracks: list[dict[str, Any]] = field(default_factory=list)
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
    # How this song's stems are kept on disk (#783): "wav" or "mp3", set when
    # the job finishes from the Stem storage format setting of that moment,
    # with the MP3 bitrate (kbps) or None. None for a song finished before the
    # setting existed, whose stems are WAV. Readers do not branch on it: they
    # resolve each file through app.core.stem_files, which looks at the disk.
    stem_format: str | None = None
    stem_mp3_kbps: int | None = None
    # On-demand lead/backing vocal split (#275) -- a post-hoc action on an
    # already-"done" job, not part of the main pipeline. "none" until the user
    # asks for it; "error" leaves the job's base stems untouched (see
    # app/pipeline/vocal_split.py) and is recorded in stems/vocal_split_error.txt,
    # not job.error_detail, since the job itself did not fail.
    vocal_split: Literal["none", "running", "done", "error"] = "none"
    # Why the last split did not make its two stems, while vocal_split is
    # "error": one of app.core.vocal_split_outcome.CODES, so the client can say
    # what happened and what to do instead of showing one Vocals lane with no
    # word about it (#832). None otherwise.
    vocal_split_reason: str | None = None
    # Whether Lead + Backing was chosen when the job was submitted (#848). Kept
    # on the job rather than only in the page that asked, so a reload, a
    # restart or a second window still asks for the split once the job is
    # done. False for a record written before it existed.
    vocal_split_wanted: bool = False
    # Versions of the song (#804, app/core/versions.py): named choices of
    # stems over the one set of stem files. None until one is made, created,
    # renamed or chosen: the song then reads as its implicit default version.
    versions: list[dict[str, Any]] | None = None
    # The version that opens with the song, or None for the first one.
    active_version: str | None = None
    # How the stems were separated (#804), recorded when separation runs, so
    # a later Extract fills missing stems with exactly the same settings and
    # they add up to the song. None for a song separated before this existed.
    separation_model: str | None = None
    separation_quality: str | None = None
    # The model each selectable stem was separated with ({stem: model id},
    # app/core/stem_models.py), recorded when separation starts, so a later
    # Extract runs the same plan. separation_model stays the base Demucs model.
    # None for a song separated before this existed: every stem was
    # htdemucs_6s's.
    stem_models: dict[str, str] | None = None
    # The stems a model the person added was meant to separate and could not
    # (it failed to load or run), so HT Demucs 6 stems separated them instead,
    # on the same device: {stem: {"model": "<user id>", "name": "<its name
    # then>", "code": "<reason code>"}}. stem_models holds what really ran.
    # None when nothing stood in. Kept in metadata.json like stem_models.
    stem_model_fallbacks: dict[str, dict] | None = None
    # Separating the stems a song does not have yet (POST .../fill-stems), a
    # post-hoc action on a "done" job like vocal_split. "error" leaves every
    # stem the song had as it was.
    stem_fill: Literal["none", "running", "done", "error"] = "none"
    # Re-extract one stem (POST .../reextract, app/pipeline/stem_reextract.py):
    # the last run or undo on this song, {"stem", "model", "action", "state",
    # "phase", "progress", "code", "used_model", "fallback", "gain_db",
    # "started_at", "finished_at"}, or None when none ever ran. A post-hoc
    # action on a "done" job like stem_fill: "error" and "cancelled" leave
    # every stem as it was.
    stem_reextract: dict[str, Any] | None = None
    # The undo copy of each re-extracted stem, by stem: {"model", "fallback",
    # "dir", "file", "gain", "saved_at", "extras", "vocal_split"}. The files
    # live in <job>/stem-undo/<dir>/. None when there is none.
    stem_undo: dict[str, dict[str, Any]] | None = None
    # How many times each stem file was replaced in place by a re-extract or an
    # undo ({name: n}). Its URL carries "?v=<n>", so a browser never plays a
    # cached copy of the file it replaced, and the export caches miss. None
    # until the first replacement: every file is as the separation wrote it.
    stem_revisions: dict[str, int] | None = None
    # The level every stem was turned down by since the separation, so that a
    # re-extracted stem and the rebuilt "other" fit without clipping (the
    # shared-scale rule, app/pipeline/stem_plan.py). None is 1.0.
    stem_gain: float | None = None
    # MIDI made from the piano and bass stems (POST .../midi,
    # app/pipeline/midi.py): clean_midi's {stem: entry}, or None when none
    # was ever asked for. The files are <job>/midi/<stem>.mid and what made
    # each is in <job>/midi.json. A post-hoc action on a "done" job like
    # stem_fill: a run that fails or is cancelled leaves the file it had.
    midi: dict[str, dict[str, Any]] | None = None
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
    # Whether the user marked this track as a favourite, or None if no
    # client has said either way yet (#734).
    #
    # Server-side for the same reason as trashed_at: favourites lived only in
    # the desktop page's catalog store, so the phone, which builds its library
    # from GET /api/jobs, had no way to set one or to filter by them. None
    # rather than False lets the desktop tell "never recorded here" from
    # "taken back out", so it can hand up favourites it set before this field
    # existed without undoing one removed on the phone.
    favorite: bool | None = None
    # Set by POST /api/jobs/{id}/cancel; consumed by pipeline stages.
    # Not surfaced via to_state() -- it's internal control state.
    cancel_requested: bool = False
    # Set alongside cancel_requested when the stop comes from the Trash (#748).
    # The job then settles as "stopped" with its files kept, not "cancelled"
    # with its directory removed. Internal, like cancel_requested.
    stop_requested: bool = False
    # Bumped by _set() on every field write (#289). Internal dirty-flag /
    # tear-detection state for the SSE stream -- not surfaced via to_state()
    # or persisted, same as cancel_requested.
    version: int = 0
    # Place in the waiting queue, rewritten whenever the queue changes. Only
    # exists so a reordered queue comes back in the user's order rather than
    # submission order after a restart; the position the UI shows is derived
    # from the live deque. Old records default to 0, where created_at decides.
    queue_position: int = 0
    # How many times the queue has started this job, counted at the start and
    # never by a restart. Persisted, so a job that reliably kills the process
    # is failed rather than retried on every start. Old records without the
    # field default to 0 via from_record.
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
            "loudness_version": self.loudness_version,
            "loudness_remeasured": self.loudness_remeasured in LOUDNESS_BASES,
            "loudness_basis": self.loudness_remeasured
            if self.loudness_remeasured in LOUDNESS_BASES
            else None,
            "tempo_stability": self.tempo_stability,
            "stem_presence": self.stem_presence,
            "sections": self.sections,
            "sections_source": self.sections_source,
            "timeline_tags": self.timeline_tags,
            "auto_lyrics": self.auto_lyrics,
            "tags": self.tags,
            "stems": self.stems,
            "takes": self.takes,
            "tracks": self.tracks,
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
            "stem_format": self.stem_format,
            "stem_mp3_kbps": self.stem_mp3_kbps,
            "vocal_split": self.vocal_split,
            "vocal_split_reason": self.vocal_split_reason,
            # A fixed sentence per code, never the worker's own words.
            "vocal_split_message": message_for(self.vocal_split_reason),
            "vocal_split_wanted": self.vocal_split_wanted,
            "versions": versions_of(self),
            "active_version": active_of(self),
            "stem_fill": self.stem_fill,
            "stem_models": self.stem_models,
            "stem_model_fallbacks": self.stem_model_fallbacks,
            "stem_reextract": self.stem_reextract,
            "stem_undo": public_stem_undo(self.stem_undo),
            "midi": clean_midi(self.midi) or {},
            "trashed_at": self.trashed_at,
            "favorite": self.favorite,
            "created_at": self.created_at,
        }

    def post_hoc_running(self) -> bool:
        """A stem fill, a vocal split, a re-extract or a MIDI run is writing
        into this song's folder. The queue statuses do not show it: the song
        stays "done" meanwhile."""
        return (
            self.stem_fill == "running"
            or self.vocal_split == "running"
            or self.stem_reextract_running()
            or self.midi_running()
        )

    def midi_running(self, stem: str | None = None) -> bool:
        """A MIDI run is writing into this song's midi/ folder: for ``stem``,
        or for any stem when it is None."""
        runs = self.midi if isinstance(self.midi, dict) else {}
        stems = (stem,) if stem is not None else MIDI_STEM_NAMES
        return any(
            isinstance(runs.get(s), dict) and runs[s].get("state") == "running" for s in stems
        )

    def stem_reextract_running(self) -> bool:
        """A re-extract or its undo is writing into this song's folder."""
        run = self.stem_reextract
        return isinstance(run, dict) and run.get("state") == "running"

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
        job.stop_requested = False
        # Saved while a split ran, so the process ended before it did. Left
        # "running", every later attempt was refused as already running.
        if job.vocal_split == "running":
            job.vocal_split = "error"
            job.vocal_split_reason = INTERRUPTED
            # Saved as "Splitting lead/backing vocals...", which the song's row
            # would keep showing. The split's own failure path says "Done".
            job.stage_message = "Done"
        # Never fails the song: a damaged versions list loses only the
        # entries that are not a version (app/core/versions.py).
        job.versions = clean_versions(job.versions)
        if not isinstance(job.active_version, str) or not VERSION_ID_RE.match(job.active_version):
            job.active_version = None
        if job.stem_fill == "running":
            job.stem_fill = "error"
        # Saved while a re-extract ran: its files were never swapped in (the
        # swap is the run's last step, one rename per file), so the song is
        # as it was, and says why the run did not finish.
        if job.stem_reextract_running():
            job.stem_reextract = {
                **job.stem_reextract,
                "state": "error",
                "phase": None,
                "code": "interrupted",
            }
        # Saved while a MIDI run was writing: its file was written to a temp
        # name and never renamed, so the file the song had is still there.
        job.midi = clean_midi(job.midi)
        for entry in (job.midi or {}).values():
            if entry["state"] == "running":
                entry.update(state="error", pct=0, error_code="interrupted")
        return job


_JOB_FIELDS = frozenset(
    f.name
    for f in dataclasses.fields(Job)
    if f.name not in ("cancel_requested", "stop_requested", "version")
)
