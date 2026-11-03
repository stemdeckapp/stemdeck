"""Write the 0.20.0 library fixture with the 0.20.0 code itself.

The acceptance check UPG-01 / UPG-02 (tests/acceptance/acceptance.spec.mjs)
opens a library the way 0.20.0 left it. Writing that shape by hand would test
what someone remembers 0.20.0 wrote; this runs the tagged code instead:

- registry.json comes from 0.20.0's registry.persist, so every field of the
  0.20.0 Job record is there with its 0.20.0 default;
- metadata.json from 0.20.0's runner._write_metadata;
- settings.json from 0.20.0's settings setters (what the package keeps in its
  data folder);
- user-data.json is the desktop library store as 0.20.0's catalog.js wrote it
  (STORAGE_KEY "stemdeck.folders", version 2), through tauri-plugin-store,
  which keeps one JSON object keyed by store key.

The stems themselves are not here: the acceptance test writes them as real
PCM16 WAVs when it copies this fixture, so the repository carries no audio.

    python tests/fixtures/upgrade-0.20.0/make.py

Needs git (it extracts `app/` at the v0.20.0 tag into a temporary folder) and
the project's Python environment. Paths written into settings.json are
placeholders ("@LIBRARY@"); the test puts the real throwaway path in.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
TAG = "v0.20.0"

JOB_ID = "0a20c0ffee01"
TITLE = "Old Song From 0.20.0"
DURATION_SEC = 4
STEMS = ["vocals", "drums", "bass", "other"]
# Thursday 1 October 2026, before 0.21.0 existed. Auto-delete is off, as it is
# by default, so an old date must not get the song swept.
CREATED_AT = 1790812800.0
LIBRARY = "@LIBRARY@"

# Every value differs from its default, so "kept" cannot pass by accident.
SETTINGS = {
    "max_duration_sec": 1200,
    "playlist_max_items": 25,
    "video_max_height": 720,
    "export_sample_rate": 48000,
    "separation_quality": "best",
    "transcribe_lyrics": "off",
    "auto_sections": False,
    "auto_delete_jobs": False,
}


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="stemdeck-v020-") as tmp:
        old = Path(tmp) / "old"
        old.mkdir()
        archive = subprocess.run(
            ["git", "-C", str(REPO), "archive", TAG, "app"], check=True, capture_output=True
        ).stdout
        subprocess.run(["tar", "-x", "-C", str(old)], input=archive, check=True)

        out_jobs = Path(tmp) / "jobs"
        out_data = Path(tmp) / "data"
        out_jobs.mkdir()
        out_data.mkdir()
        os.environ["STEMDECK_DATA_DIR"] = str(out_data)
        os.environ["STEMDECK_JOBS_DIR"] = str(out_jobs)
        os.environ.pop("STEMDECK_SETTINGS_MIRROR", None)
        sys.path.insert(0, str(old))

        from app.core import registry, settings  # noqa: PLC0415  (0.20.0's)
        from app.core.models import Job  # noqa: PLC0415
        from app.pipeline.runner import _write_metadata  # noqa: PLC0415

        assert Path(registry.__file__).is_relative_to(old), "imported the checkout, not 0.20.0"

        job = Job(id=JOB_ID)
        job.status = "done"
        job.progress = 1.0
        job.stage_message = "Done"
        job.title = TITLE
        job.duration_sec = float(DURATION_SEC)
        job.bpm = 120
        job.key = "A min"
        job.scale = "Natural Minor"
        job.key_confidence = 77
        job.stem_presence = {name: 70 for name in STEMS}
        job.sections = [
            {"id": "s-verse", "name": "Verse", "start": 0.5, "end": 2.0, "color": "#4a7fff"},
            {"id": "s-chorus", "name": "Chorus", "start": 2.0, "end": 3.5, "color": "#ff7a4a"},
        ]
        job.sections_source = "manual"
        job.stems = [{"name": n, "url": f"/api/jobs/{JOB_ID}/stems/{n}.wav"} for n in STEMS]
        job.selected_stems = list(STEMS)
        job.source_url = "local:Old Song From 0.20.0"
        job.source_format = "wav"
        job.created_at = CREATED_AT
        registry.register(job)
        registry.persist(out_jobs)
        (out_jobs / JOB_ID).mkdir()
        _write_metadata(job, out_jobs / JOB_ID)

        for key, value in SETTINGS.items():
            getattr(settings, f"set_{key}")(value)

        dest = HERE / "library"
        shutil.rmtree(dest, ignore_errors=True)
        (dest / JOB_ID).mkdir(parents=True)
        shutil.copy(out_jobs / "registry.json", dest / "registry.json")
        shutil.copy(out_jobs / JOB_ID / "metadata.json", dest / JOB_ID / "metadata.json")

        written = json.loads((out_data / "settings.json").read_text(encoding="utf-8"))
        # The library this install used: a folder chosen in Settings, as an
        # upgrader whose library is not inside the old package has it.
        written["jobs_dir"] = LIBRARY
        (HERE / "settings.json").write_text(json.dumps(written, indent=2) + "\n", encoding="utf-8")

    store = {
        "stemdeck.folders": {
            "v": 2,
            "folders": [
                {"id": "f-old", "name": "Practice 2026", "items": [JOB_ID], "color": "#4a7fff"},
                {"id": "trash", "name": "Trash", "items": [], "color": None},
            ],
            "tracks": {
                JOB_ID: {
                    "id": JOB_ID,
                    "title": TITLE,
                    "status": "done",
                    "stems": STEMS,
                    "sourceUrl": "local:Old Song From 0.20.0",
                    "createdAt": CREATED_AT,
                    "favorite": True,
                    "tags": ["warmup"],
                },
            },
        },
    }
    (HERE / "library" / "user-data.json").write_text(
        json.dumps(store, indent=2) + "\n", encoding="utf-8"
    )
    print(f"wrote {HERE}")


if __name__ == "__main__":
    main()
