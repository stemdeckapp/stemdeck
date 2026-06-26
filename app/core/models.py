from __future__ import annotations

import dataclasses
import time
from dataclasses import dataclass, field
from typing import Any, Literal


class JobCancelled(Exception):
    """Raised inside a pipeline stage when the job's cancel flag is set."""


JobStatus = Literal[
    "queued", "downloading", "analyzing", "separating", "processing", "done", "error", "cancelled"
]


def _set(job: Job, **fields: object) -> None:
    """Mutate Job fields. SSE polling picks up the change automatically."""
    for k, v in fields.items():
        if k == "stage":
            job.stage_message = v  # type: ignore[assignment]
        else:
            setattr(job, k, v)


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
    sections: list[dict] | None = None  # [{id, name, start, end, color}]
    tags: list[str] | None = None  # YouTube tags + categories, lowercased, max 8
    stems: list[dict[str, str]] = field(default_factory=list)
    # Subset of stems the user chose at submit. The pipeline produces all
    # 6 regardless (Demucs htdemucs_6s is fixed), but after collect we
    # mix down only the selected ones into mix.wav so the user can
    # download a single track containing just their chosen stems.
    selected_stems: list[str] = field(default_factory=list)
    mix_url: str | None = None  # populated when a strict subset was selected
    source_url: str | None = None  # original URL or "local:<filename>" for file uploads
    # True when a silent video track (video.mp4) was preserved from an .mp4
    # upload, enabling the "Export Mix (with video)" MP4 export.
    has_video: bool = False
    error: str | None = None
    # Set by POST /api/jobs/{id}/cancel; consumed by pipeline stages.
    # Not surfaced via to_state() -- it's internal control state.
    cancel_requested: bool = False
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
            "tags": self.tags,
            "stems": self.stems,
            "selected_stems": self.selected_stems,
            "mix_url": self.mix_url,
            "source_url": self.source_url,
            "has_video": self.has_video,
            "error": self.error,
            "created_at": self.created_at,
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
        job.cancel_requested = False
        return job


_JOB_FIELDS = frozenset(f.name for f in dataclasses.fields(Job) if f.name != "cancel_requested")
