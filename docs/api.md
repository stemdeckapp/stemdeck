# HTTP API

<sub>[Back to the README](../README.md)</sub>

The endpoints the web app itself uses. They are not a stable public interface and can change between releases.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | Server health and version info |
| POST | `/api/jobs` | JSON `{url, stems?}` or multipart `file + stems` → `{job_id}` |
| GET | `/api/jobs` | List completed (library) jobs |
| GET | `/api/jobs/{id}` | Job state snapshot |
| GET | `/api/jobs/{id}/events` | SSE stream of job state |
| POST | `/api/jobs/{id}/cancel` | Terminate active subprocess and cancel job |
| PATCH | `/api/jobs/{id}/sections` | Save waveform section markers for a job |
| PATCH | `/api/jobs/{id}/timeline-tags` | Save the timeline markers for a job: JSON `{timeline_tags: [{id, name, time}]}`, the whole list, stored in time order (#1075) |
| POST | `/api/jobs/{id}/lyrics/import` | Keep the lyrics from a `.lrc` or `.txt` the user picked, decoded by the page: JSON `{text}`, at most 100,000 characters → `lyrics.json`, source `file`, answered as `GET .../lyrics` then serves it, plus `dropped_lines` (a count) when untimed lines with words were dropped. Timed when the text has `[mm:ss.xx]` stamps (tags, `[offset:]`, multi-stamp lines and `<mm:ss.xx>` word stamps kept, other lines dropped), plain otherwise (LRC ID tags dropped; plain lyrics cannot be aligned or synced line by line). Replaces the old lyrics with their Align offset, own timing and vocal timing, and LRCLIB's other versions; the page asks first when any of those three is set. The page reads a file as UTF-8 (or UTF-16 with a byte order mark), else GB18030, so a Windows-1252, Shift-JIS or Big5 file can come out as garbled letters: save it as UTF-8 first. 409 while the job is not finished, 422 for no words, too long text or text UTF-8 cannot hold. An Align offset (`.../lyrics/offset`, Auto-detect) answers 409 when an import replaced the lyrics it was set for (#1083) |
| GET | `/api/jobs/{id}/stems/{name}.wav` | Stream a single stem as the song stores it: WAV, or MP3 (`audio/mpeg`) for a song stored as MP3 (#783) |
| GET | `/api/jobs/{id}/stems/{name}.mp3` | A stem as MP3: the stored file for a song stored as MP3, else a cached transcode |
| GET | `/api/jobs/{id}/stems/all.zip` | The chosen stems (and takes) as one ZIP, named after the song. Query: `format`, `stems`, and the click options. `reaper=1` (default off) adds `<song>.rpp`, a REAPER project with one track per file in the zip (the click too), at the stems' sample rate and the beat grid's median tempo (120 without a grid), each file named relative to the project; the download is then named `<song>_reaper.zip`. Without it the zip is unchanged (#1071) |
| GET | `/api/jobs/{id}/video.mp4` | Mux the current mix with the source video (MP4 upload or YouTube) into an MP4 |
| DELETE | `/api/jobs/{id}` | Remove job dir from disk (terminal jobs only) |
