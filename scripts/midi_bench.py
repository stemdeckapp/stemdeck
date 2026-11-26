"""Measure StemDeck's MIDI transcription against ground-truth MIDI.

Runs the code the app runs: the separation worker (app/pipeline/demucs_worker.py)
on the device the app would pick, and the MIDI worker (app/pipeline/midi_worker.py)
with midi.run_params for each detail preset, writing the .mid the app writes and
reading it back the way the studio does (midi_file.read_notes). The notes are
then scored with mir_eval.transcription (vendored in app/_vendor/transcribe).

Everything it reads and writes lives under one folder, data/midi-bench by default
(git-ignored), and it holds no data itself. The datasets are fetched by hand,
each into its own folder:

    data/midi-bench/dl-babyslakh/babyslakh_16k/Track000NN/...   BabySlakh (CC BY 4.0)
    data/midi-bench/dl-maestro-hf/<year>/<name>.{wav,midi}      MAESTRO v3 test pieces
    data/midi-bench/dl-maestro-official/maestro-v3.0.0.csv      (CC BY-NC-SA 4.0)

Then:

    uv run python scripts/midi_bench.py prepare      # items, references, mp3 copies
    uv run python scripts/midi_bench.py separate     # Demucs, as the app runs it
    uv run python scripts/midi_bench.py cache        # model activations per input
    uv run python scripts/midi_bench.py score --tag baseline
    uv run python scripts/midi_bench.py score --tag check --worker   # spawn the worker

The second piano engine (app/pipeline/piano_hr.py) takes ``--engine piano_hr``
on cache, score and sweep, piano items only. Its checkpoint is the one the app
downloads (piano_hr.checkpoint_path(), under the bench's own app data):

    uv run python scripts/midi_bench.py fetch-piano-hr    # the app's download step
    uv run python scripts/midi_bench.py --engine piano_hr cache --split tune
    uv run python scripts/midi_bench.py --engine piano_hr score --tag hr --worker

Its cache holds every activation the model outputs; ``score`` decodes them with
piano_hr.output_notes, as the worker does. The model runs on the device the app
would pick (``--device`` to choose).

YourMT3+ (app/pipeline/ymt3.py) and Basic Pitch on every stem are measured on
the engine comparison's held-out sets (data/midi-bench/eng/inputs.json, made
by data/midi-bench/engine-scripts/build_jobs.py; their scores are in
data/midi-bench/results/engines-per-stem.md) with ``eng``, which spawns the
real worker for each input exactly as midi.py does, reads its .mid back and
scores it as engine-scripts/score.py did, beside that comparison's own score
for the same input:

    uv run python scripts/midi_bench.py fetch-ymt3 --from-file <last.ckpt>
    uv run python scripts/midi_bench.py --engine ymt3 eng --tag ymt3-worker
    uv run python scripts/midi_bench.py eng --tag bp-worker --sets guitartechs vocadito

fetch-ymt3 is the app's own first-use step (download, then the conversion
process, then the checks, then the checkpoint removed), into
data/ymt3-bench-models (STEMDECK_MODELS_DIR for every ymt3 command, so the
removal never touches data/midi-bench). ``--from-file`` puts a copy of a
checkpoint already on this computer where the download would, still checked
against the pinned size and SHA-256. YourMT3+ is not offered for the vocals
(config.MIDI_ENGINES), so the worker refuses them; ``eng`` measures it on the
vocals with the worker's own functions in this process instead (rows marked
"in_process").

``score`` decodes the cached activations in this process with the worker's own
functions (midi_worker.output_notes), writes and reads the .mid like the worker,
and so gives the worker's notes without running the model again; ``--worker``
spawns the worker exactly as midi.py does instead, to prove the two agree.
``sweep`` tries parameter sets on the tuning split only. ``compare`` scores both
piano engines per song and tests whether a signal known at run time predicts
which one wins (fitted on the tuning split, checked on held-out).

Splits: BabySlakh tracks 1 to 10 are the tuning split, 11 to 20 and every MAESTRO
piece are held out. Nothing is ever tuned on a held-out item.

Conditions: "clean" is the ground-truth stem (BabySlakh: the sum of the stems of
that instrument family; MAESTRO: the recording), "sep" is mix -> Demucs -> stem,
what a person actually gets. Each runs from a wav and from a 128 kbps mp3.

References: note offsets are held by the sustain pedal (CC 64), as the MAESTRO
evaluation does. BabySlakh's bass stems sound an octave below their MIDI, so a
bass reference is moved by the octave pYIN measures on that stem's audio
(_octave_shift), and a track whose bass notes do not agree on one octave is
left out (Track00012 and Track00013 at the time of writing).

Scores: note precision, recall and F1 with onsets within 50 ms ("F1"), and
with offsets too (within 20% of the note or 50 ms, "F1 on+off"); the same with
every pitch moved into one octave ("F1 chroma"); and the share of reference
notes missed where a note an octave or more away was heard ("octave err").
Each row also counts the missed and the extra notes by what was there instead.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BENCH = ROOT / "data" / "midi-bench"

# GM programs (0-based) per stem: Piano family and Bass family.
PROGRAMS = {"piano": range(0, 8), "bass": range(32, 40)}
TUNE_TRACKS = {f"Track{n:05d}" for n in range(1, 11)}
MAESTRO_START = 10.0
MAESTRO_SECONDS = 75.0
MP3_BITRATE = "128k"
ONSET_TOL = 0.05
FORMATS = ("wav", "mp3")
CONDITIONS = ("clean", "sep")


# YourMT3+'s models folder for the bench: the app's first-use step removes the
# checkpoint after its conversion, and nothing under data/midi-bench is ever
# removed, so its files live beside it (git-ignored, inside the repository).
YMT3_MODELS = ROOT / "data" / "ymt3-bench-models"


def _setup_env(bench: Path, ymt3_models: bool = False) -> None:
    """App data, cache and jobs for anything this process or its workers
    import: under the bench folder, never a real library."""
    bench = bench.resolve()
    if ROOT.resolve() not in bench.parents:
        sys.exit(f"the bench folder must be inside the repository: {bench}")
    app = bench / "app-data"
    for name, sub in (
        ("STEMDECK_DATA_DIR", "data"),
        ("STEMDECK_CACHE_DIR", "cache"),
        ("STEMDECK_JOBS_DIR", "jobs"),
    ):
        (app / sub).mkdir(parents=True, exist_ok=True)
        os.environ[name] = str(app / sub)
    if ymt3_models:
        YMT3_MODELS.mkdir(parents=True, exist_ok=True)
        os.environ["STEMDECK_MODELS_DIR"] = str(YMT3_MODELS)
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))


def _vendor_path(engine: str = "basic_pitch") -> None:
    """The vendored engines on this process's path, as the worker puts them
    (with the same packages blocked): Basic Pitch and piano_hr, or for
    ymt3 its own folders, whose einops would try a blocked tensorflow."""
    from app.pipeline import piano_hr, ymt3
    from app.pipeline.midi_file import VENDOR_DIR
    from app.pipeline.midi_worker import _install_engine

    if engine == "ymt3":
        _install_engine(VENDOR_DIR, [ymt3.VENDOR_DIR / "shim", ymt3.VENDOR_DIR / "src"], "ymt3")
    else:
        _install_engine(VENDOR_DIR, [piano_hr.VENDOR_DIR])


def _device(asked: str | None) -> str:
    """The device a torch engine runs on here: ``asked``, or what the app picks."""
    if asked:
        return asked
    from app.pipeline.midi import torch_engine_device

    return torch_engine_device()


def _ffmpeg() -> str:
    found = os.environ.get("STEMDECK_FFMPEG_DIR")
    if found:
        exe = Path(found) / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
        if exe.is_file():
            return str(exe)
    exe = shutil.which("ffmpeg")
    if not exe:
        sys.exit("ffmpeg not found: set STEMDECK_FFMPEG_DIR")
    return exe


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=1) + "\n", encoding="utf-8")


# -- references


def _midi_notes(path: Path, programs: range | None, t0: float = 0.0, t1: float | None = None):
    """[onset, offset, pitch, velocity] of every non-drum note in ``path``
    (of ``programs`` when given), offsets extended while the sustain pedal
    (CC 64 >= 64) holds them, as the MAESTRO and Onsets and Frames
    evaluations do. Cropped to notes whose onset is in [t0, t1), times
    shifted by -t0 and offsets clipped to t1."""
    import pretty_midi

    pm = pretty_midi.PrettyMIDI(str(path))
    out = []
    for inst in pm.instruments:
        if inst.is_drum or (programs is not None and inst.program not in programs):
            continue
        pedal = sorted((c.time, c.value >= 64) for c in inst.control_changes if c.number == 64)
        downs = []  # (start, end) of each held stretch
        start = None
        for t, down in pedal:
            if down and start is None:
                start = t
            elif not down and start is not None:
                downs.append((start, t))
                start = None
        if start is not None:
            downs.append((start, float("inf")))
        notes = sorted(inst.notes, key=lambda n: (n.start, n.pitch))
        for i, n in enumerate(notes):
            end = n.end
            for a, b in downs:
                if a <= end < b:
                    end = b
                    break
            # A held note ends where the same key is struck again.
            for m in notes[i + 1 :]:
                if m.pitch == n.pitch and m.start > n.start:
                    end = min(end, m.start)
                    break
            if end == float("inf"):
                end = max(n.end, pm.get_end_time())
            out.append(
                [n.start, max(end, n.end if end > n.start else n.start + 0.01), n.pitch, n.velocity]
            )
    sel = []
    for on, off, p, v in out:
        if on < t0 or (t1 is not None and on >= t1):
            continue
        off = min(off, t1) if t1 is not None else off
        sel.append([round(on - t0, 5), round(max(off - t0, on - t0 + 0.01), 5), int(p), int(v)])
    sel.sort()
    return sel


def _octave_shift(audio: Path, ref: list, memo: Path) -> int | None:
    """How many semitones the rendered bass sounds away from its MIDI: a
    whole number of octaves, from pYIN's f0 in the middle of every reference
    note of 150 ms or more in the first 120 s (or the whole stem).

    Measured on BabySlakh: every bass stem sounds an octave below its MIDI
    (pYIN says -12 on 433 of 446 notes over Track00001 to 6), so the MIDI is
    not the pitch that is heard. The reference is moved by what is measured,
    never by an assumption, and a stem whose notes do not agree on one octave
    (70%; pYIN's own misses are mostly one to three octaves low) is None:
    its MIDI is not what was rendered, and the track is left out of the bass
    set (Track00012's S10, a lead line in 58..77 on a bass patch)."""
    if memo.is_file():
        return json.loads(memo.read_text())["shift"]
    shift, detail = _measure_octave(audio, ref)
    _write_json(memo, {"shift": shift, **detail})
    return shift


def _measure_octave(audio: Path, ref: list) -> tuple[int | None, dict]:
    import librosa
    import numpy as np
    import soundfile as sf

    info = sf.info(str(audio))
    diffs: list[float] = []
    # The first 120 s, or the whole stem when that holds fewer than 10
    # notes to measure (a bass that enters late, or plays short notes).
    for seconds in (120.0, info.duration + 1):
        y, sr = sf.read(str(audio), dtype="float32", frames=int(seconds * info.samplerate))
        if y.ndim > 1:
            y = y.mean(axis=1)
        f0, voiced, _ = librosa.pyin(
            y, fmin=30, fmax=1000, sr=sr, frame_length=4096, hop_length=256
        )
        t = librosa.times_like(f0, sr=sr, hop_length=256)
        diffs = []
        for on, off, pitch, _v in ref:
            if off > seconds or off - on < 0.15:
                continue
            sel = (t > on + 0.03) & (t < off - 0.03) & voiced
            if sel.sum() >= 3:
                diffs.append(float(np.median(librosa.hz_to_midi(f0[sel]))) - pitch)
        if len(diffs) >= 10:
            break
    octaves = [12 * round(d / 12) for d in diffs if abs(d - 12 * round(d / 12)) < 0.5]
    if not octaves:
        return None, {"why": "no voiced reference note"}
    values, counts = np.unique(octaves, return_counts=True)
    shift = int(values[np.argmax(counts)])
    agree = float(counts.max() / len(diffs))
    detail = {
        "agree": round(agree, 3),
        "notes": len(diffs),
        "octaves": {int(v): int(c) for v, c in zip(values, counts, strict=True)},
    }
    if agree < 0.7:
        return None, {"why": "the notes do not agree on one octave", **detail}
    return shift, detail


def _slakh_items(bench: Path, ffmpeg: str) -> list[dict]:
    import numpy as np
    import soundfile as sf
    import yaml

    src = bench / "dl-babyslakh" / "babyslakh_16k"
    items = []
    for track in sorted(p for p in src.iterdir() if p.is_dir() and p.name.startswith("Track")):
        meta = yaml.safe_load((track / "metadata.yaml").read_text(encoding="utf-8"))
        out = bench / "items" / track.name
        out.mkdir(parents=True, exist_ok=True)
        mix = out / "mix.wav"
        if not mix.is_file():
            shutil.copyfile(track / "mix.wav", mix)
        for stem, programs in PROGRAMS.items():
            ids = [
                sid
                for sid, s in (meta.get("stems") or {}).items()
                # BabySlakh's audio_rendered flags are false for every stem,
                # rendered or not: a stem counts when its wav and mid exist.
                if not s.get("is_drum")
                and s.get("program_num") in programs
                and (track / "stems" / f"{sid}.wav").is_file()
                and (track / "MIDI" / f"{sid}.mid").is_file()
            ]
            if not ids:
                continue
            ref = []
            shifts = {}
            for sid in ids:
                notes = _midi_notes(track / "MIDI" / f"{sid}.mid", None)
                shift = 0
                if stem == "bass":
                    # Each stem on its own: two basses may sit in two octaves.
                    shift = _octave_shift(
                        track / "stems" / f"{sid}.wav", notes, out / f"f0_octave_{sid}.json"
                    )
                shifts[sid] = shift
                if shift is not None:
                    ref += [[a, b, p + shift, v] for a, b, p, v in notes]
            ref.sort()
            if None in shifts.values():
                print(
                    f"{track.name} {stem}: left out, a stem's rendered pitch is not its MIDI ({shifts})"
                )
                continue
            if not ref:
                continue
            clean = out / f"clean_{stem}.wav"
            if not clean.is_file():
                total, rate = None, None
                for sid in ids:
                    y, rate = sf.read(str(track / "stems" / f"{sid}.wav"), dtype="float32")
                    total = y if total is None else total + y
                sf.write(str(clean), total.astype(np.float32), rate, subtype="FLOAT")
            _write_json(out / f"ref_{stem}.json", ref)
            items.append(
                {
                    "id": f"{track.name}-{stem}",
                    "ref_transpose": shifts,
                    "set": "slakh",
                    "track": track.name,
                    "split": "tune" if track.name in TUNE_TRACKS else "held",
                    "stem": stem,
                    "programs": [meta["stems"][s].get("program_num") for s in ids],
                    "clean": str(clean.relative_to(bench)),
                    "mix": str(mix.relative_to(bench)),
                    "ref": str((out / f"ref_{stem}.json").relative_to(bench)),
                    "seconds": round(sf.info(str(mix)).duration, 3),
                    "ref_notes": len(ref),
                }
            )
    return items


def _maestro_items(bench: Path) -> list[dict]:
    import soundfile as sf

    src = bench / "dl-maestro-hf"
    table = bench / "dl-maestro-official" / "maestro-v3.0.0.csv"
    rows = {r["audio_filename"]: r for r in csv.DictReader(table.open(encoding="utf-8"))}
    items = []
    for wav in sorted(src.glob("*/*.wav")):
        rel = f"{wav.parent.name}/{wav.name}"
        row = rows.get(rel)
        if row is None or row["split"] != "test":
            sys.exit(f"{rel} is not a MAESTRO v3 test piece")
        midi = wav.with_suffix(".midi")
        if midi.name != Path(row["midi_filename"]).name:
            sys.exit(f"{rel}: MIDI name does not match maestro-v3.0.0.csv")
        info = sf.info(str(wav))
        if info.duration < float(row["duration"]) - 1.0:  # truncated audio
            sys.exit(f"{rel}: {info.duration:.1f} s, the csv says {row['duration']}")
        t0 = MAESTRO_START
        t1 = min(info.duration, t0 + MAESTRO_SECONDS)
        key = hashlib.sha1(rel.encode()).hexdigest()[:8]
        name = f"maestro-{wav.parent.name}-{key}"
        out = bench / "items" / name
        out.mkdir(parents=True, exist_ok=True)
        clip = out / "clean_piano.wav"
        if not clip.is_file():
            y, rate = sf.read(
                str(wav),
                dtype="float32",
                start=int(t0 * info.samplerate),
                stop=int(t1 * info.samplerate),
            )
            sf.write(str(clip), y, rate, subtype="PCM_16")
        ref = _midi_notes(midi, None, t0, t1)
        _write_json(out / "ref_piano.json", ref)
        items.append(
            {
                "id": f"{name}-piano",
                "set": "maestro",
                "track": name,
                "split": "held",
                "stem": "piano",
                "title": f"{row['canonical_composer']}: {row['canonical_title']}",
                "source": rel,
                "excerpt": [t0, round(t1, 3)],
                "clean": str(clip.relative_to(bench)),
                # Solo piano: the mix is the recording itself.
                "mix": str(clip.relative_to(bench)),
                "ref": str((out / "ref_piano.json").relative_to(bench)),
                "seconds": round(t1 - t0, 3),
                "ref_notes": len(ref),
            }
        )
    return items


def _mp3(ffmpeg: str, src: Path) -> Path:
    dst = src.with_suffix(".mp3")
    if not dst.is_file():
        subprocess.run(
            [
                ffmpeg,
                "-v",
                "error",
                "-y",
                "-i",
                str(src),
                "-codec:a",
                "libmp3lame",
                "-b:a",
                MP3_BITRATE,
                str(dst),
            ],
            check=True,
        )
    return dst


def cmd_prepare(args) -> None:
    bench = args.bench
    ffmpeg = _ffmpeg()
    _vendor_path()
    items = _slakh_items(bench, ffmpeg) + _maestro_items(bench)
    for item in items:
        for key in ("clean", "mix"):
            _mp3(ffmpeg, bench / item[key])
    _write_json(bench / "items.json", items)
    for item in items:
        print(
            item["id"], item["split"], item["ref_notes"], item["seconds"], item.get("programs", "")
        )
    print(len(items), "items")


# -- inputs per condition


def _input(bench: Path, item: dict, cond: str, fmt: str) -> Path:
    if cond == "clean":
        path = bench / item["clean"]
        return path if fmt == "wav" else path.with_suffix(".mp3")
    mix = bench / item["mix"]
    source = mix if fmt == "wav" else mix.with_suffix(".mp3")
    from app.core.config import DEMUCS_MODEL

    return bench / "sep" / fmt / item["track"] / DEMUCS_MODEL / source.stem / f"{item['stem']}.wav"


def _items(bench: Path, split: str | None = None, only: str | None = None) -> list[dict]:
    items = json.loads((bench / "items.json").read_text(encoding="utf-8"))
    if split:
        items = [i for i in items if i["split"] == split]
    if only:
        items = [i for i in items if only in i["id"]]
    return items


# -- separation, as the app runs it


def cmd_separate(args) -> None:
    from app.core.settings import get_demucs_device, get_separation_quality
    from app.pipeline import separate as sep_mod

    bench = args.bench
    device = get_demucs_device()
    quality = get_separation_quality()
    shifts = 2 if quality == "best" else 1
    env = os.environ.copy()
    ffmpeg_dir = str(Path(_ffmpeg()).parent)
    env["PATH"] = ffmpeg_dir + os.pathsep + env.get("PATH", "")
    env["PYTHONIOENCODING"] = "utf-8:replace"
    cmd = sep_mod._spawn_worker_cmd(device)
    print("device", device, "quality", quality, "shifts", shifts, "cmd", cmd)
    proc = subprocess.Popen(
        cmd,
        cwd=ROOT,
        env=env,
        stdin=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )

    def wait_for(*markers: str) -> str:
        assert proc.stderr is not None
        buf = ""
        while True:
            ch = proc.stderr.read(1)
            if not ch:
                raise RuntimeError("worker exited")
            if ch in "\r\n":
                line, buf = buf, ""
                for m in markers:
                    if line.startswith(m):
                        return line
                if line.startswith("@@ERROR@@"):
                    raise RuntimeError(line)
            else:
                buf += ch

    wait_for("@@READY@@")
    timings = {}
    tracks = {}
    for item in _items(bench):
        tracks[item["track"]] = item
    for fmt in args.formats:
        for track, item in tracks.items():
            mix = bench / item["mix"]
            source = mix if fmt == "wav" else mix.with_suffix(".mp3")
            job_dir = bench / "sep" / fmt / track
            out = _input(bench, {**item, "stem": "piano"}, "sep", fmt)
            if out.is_file() and out.with_name("bass.wav").is_file():
                continue
            job_dir.mkdir(parents=True, exist_ok=True)
            started = time.monotonic()
            assert proc.stdin is not None
            proc.stdin.write(
                json.dumps({"source": str(source), "job_dir": str(job_dir), "shifts": shifts})
                + "\n"
            )
            proc.stdin.flush()
            wait_for("@@DONE@@")
            took = time.monotonic() - started
            timings[f"{fmt}/{track}"] = round(took, 2)
            print(fmt, track, f"{took:.1f}s", flush=True)
    proc.stdin.close()
    proc.wait(timeout=60)
    meta_path = bench / "sep" / "separation.json"
    old = json.loads(meta_path.read_text()) if meta_path.is_file() else {}
    old.setdefault("timings", {}).update(timings)
    old.update({"device": device, "quality": quality, "shifts": shifts})
    _write_json(meta_path, old)


# -- model activations


def _cache_path(bench: Path, item: dict, cond: str, fmt: str, engine: str = "basic_pitch") -> Path:
    stem = item["stem"] if cond == "sep" else "clean"
    folder = "cache" if engine == "basic_pitch" else f"cache-{engine}"
    return bench / folder / cond / fmt / f"{item['id']}-{stem}.npz"


def _engine_items(items: list[dict], engine: str) -> list[dict]:
    """piano_hr hears the piano only."""
    return items if engine == "basic_pitch" else [i for i in items if i["stem"] == "piano"]


def cmd_fetch_piano_hr(args) -> None:
    """The piano_hr checkpoint into the bench's models folder, through the
    app's own download step (app/core/model_fetch.py), then checked."""
    from app.pipeline import midi as midi_run
    from app.pipeline import piano_hr

    dst = piano_hr.checkpoint_path()
    if piano_hr.checkpoint_ok(dst):
        print("already there", dst)
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    from app.core.config import (
        PIANO_HR_CHECKPOINT_SHA256,
        PIANO_HR_CHECKPOINT_SIZE,
        PIANO_HR_CHECKPOINT_URL,
    )

    cmd = midi_run._spawn_fetch_cmd(
        dst, PIANO_HR_CHECKPOINT_URL, PIANO_HR_CHECKPOINT_SHA256, PIANO_HR_CHECKPOINT_SIZE
    )
    if args.from_url:
        # Another copy of the same bytes (a local server): still checked
        # against the pinned size and SHA-256.
        cmd[cmd.index("--url") + 1] = args.from_url
    started = time.monotonic()
    result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    took = time.monotonic() - started
    events = [e for e in (midi_run.parse_event(x) for x in result.stdout.splitlines()) if e]
    print("exit", result.returncode, "events", len(events), "last", events[-1:], f"{took:.1f}s")
    if not piano_hr.checkpoint_ok(dst):
        sys.exit("the checkpoint did not arrive whole: " + result.stderr[-800:])
    print("checked", dst)


def _run_protocol(cmd: list[str]) -> tuple[list[dict], subprocess.CompletedProcess, float]:
    from app.pipeline import midi as midi_run

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8:replace"
    started = time.monotonic()
    result = subprocess.run(
        cmd, cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8"
    )
    events = [e for e in (midi_run.parse_event(x) for x in result.stdout.splitlines()) if e]
    return events, result, time.monotonic() - started


def cmd_fetch_ymt3(args) -> None:
    """YourMT3+'s weights, made the way a first run makes them: the pinned
    checkpoint downloaded (or ``--from-file``, a copy already here, put
    where the download would put it), checked, converted by the app's own
    process (ymt3_convert), the result checked, the checkpoint removed."""
    from app.core.config import YMT3_CHECKPOINT_SHA256, YMT3_CHECKPOINT_SIZE, YMT3_CHECKPOINT_URL
    from app.pipeline import midi as midi_run
    from app.pipeline import ymt3

    weights = ymt3.weights_path()
    if ymt3.weights_ok(weights):
        print("already there", weights)
        return
    ckpt = ymt3.checkpoint_path()
    ckpt.parent.mkdir(parents=True, exist_ok=True)
    if not ymt3.checkpoint_ok(ckpt):
        if args.from_file:
            shutil.copyfile(args.from_file, ckpt)
        else:
            cmd = midi_run._spawn_fetch_cmd(
                ckpt, YMT3_CHECKPOINT_URL, YMT3_CHECKPOINT_SHA256, YMT3_CHECKPOINT_SIZE
            )
            events, result, took = _run_protocol(cmd)
            print("download: exit", result.returncode, events[-1:], f"{took:.1f}s")
    if not ymt3.checkpoint_ok(ckpt):
        sys.exit("the checkpoint is not the pinned file")
    events, result, took = _run_protocol(midi_run._spawn_convert_cmd(ckpt, weights))
    print("convert: exit", result.returncode, events[-1:], f"{took:.1f}s")
    if not ymt3.weights_ok(weights):
        sys.exit("the weights are not the pinned file: " + result.stderr[-800:])
    ckpt.unlink()
    print("checked", weights)


def _cache_piano_hr(args, items: list[dict]) -> None:
    import numpy as np

    from app.core.config import PIANO_HR_BATCH_CPU, PIANO_HR_BATCH_GPU
    from app.pipeline import piano_hr

    bench = args.bench
    device = _device(args.device)
    model = piano_hr.load_model(piano_hr.checkpoint_path(), device)
    batch = PIANO_HR_BATCH_GPU if device == "cuda" else PIANO_HR_BATCH_CPU
    for item in items:
        for cond in args.conditions:
            for fmt in args.formats:
                dst = _cache_path(bench, item, cond, fmt, "piano_hr")
                if dst.is_file():
                    continue
                src = _input(bench, item, cond, fmt)
                if not src.is_file():
                    print("missing", src)
                    continue
                started = time.monotonic()
                audio = piano_hr.read_audio(src)
                out = piano_hr.model_output(model, audio, device, batch)
                took = time.monotonic() - started
                dst.parent.mkdir(parents=True, exist_ok=True)
                np.savez_compressed(dst, took=took, **out)
                print(item["id"], cond, fmt, device, f"{took:.1f}s", flush=True)


def cmd_cache(args) -> None:
    import numpy as np

    from app.pipeline import midi_worker

    if args.engine == "piano_hr":
        _cache_piano_hr(args, _engine_items(_items(args.bench, args.split, args.only), "piano_hr"))
        return
    from app.pipeline.midi_file import MODEL_PATH

    _vendor_path()
    bench = args.bench
    model = midi_worker.load_model(MODEL_PATH)
    for item in _items(bench, args.split, args.only):
        for cond in args.conditions:
            for fmt in args.formats:
                dst = _cache_path(bench, item, cond, fmt)
                if dst.is_file():
                    continue
                src = _input(bench, item, cond, fmt)
                if not src.is_file():
                    print("missing", src)
                    continue
                started = time.monotonic()
                out = midi_worker.model_output(model, src)
                took = time.monotonic() - started
                dst.parent.mkdir(parents=True, exist_ok=True)
                np.savez_compressed(dst, took=took, **out)
                print(item["id"], cond, fmt, f"{took:.1f}s", flush=True)


# -- scoring


def _load_mir_eval():
    from app.pipeline.midi_file import VENDOR_DIR

    # mir_eval is in Basic Pitch's folder, which every engine's path holds.
    if str(VENDOR_DIR) not in sys.path:
        _vendor_path()
    import mir_eval.transcription as tr

    return tr


def _arrays(notes):
    import numpy as np

    if not notes:
        return np.zeros((0, 2)), np.zeros(0)
    iv = np.array([[n[0], max(n[1], n[0] + 1e-3)] for n in notes], dtype=float)
    hz = 440.0 * 2 ** ((np.array([n[2] for n in notes], dtype=float) - 69) / 12)
    return iv, hz


def score_notes(ref: list, est: list, stem: str) -> dict:
    """mir_eval note scores, plus the counts the diagnosis needs."""
    import numpy as np

    tr = _load_mir_eval()
    riv, rhz = _arrays(ref)
    eiv, ehz = _arrays(est)
    out: dict = {"n_ref": len(ref), "n_est": len(est)}
    if not ref or not est:
        out.update({k: 0.0 for k in ("p", "r", "f1", "p_off", "r_off", "f1_off", "f1_chroma")})
        out["octave_err"] = 0.0
        return out
    p, r, f, _ = tr.precision_recall_f1_overlap(
        riv, rhz, eiv, ehz, onset_tolerance=ONSET_TOL, offset_ratio=None
    )
    po, ro, fo, _ = tr.precision_recall_f1_overlap(
        riv, rhz, eiv, ehz, onset_tolerance=ONSET_TOL, offset_ratio=0.2, offset_min_tolerance=0.05
    )
    # Pitch class only: every note moved into one octave.
    rc = 440.0 * 2 ** (((np.array([n[2] for n in ref]) % 12) + 60 - 69) / 12)
    ec = 440.0 * 2 ** (((np.array([n[2] for n in est]) % 12) + 60 - 69) / 12)
    _, _, fc, _ = tr.precision_recall_f1_overlap(
        riv, rc, eiv, ec, onset_tolerance=ONSET_TOL, offset_ratio=None
    )
    matched = tr.match_notes(riv, rhz, eiv, ehz, onset_tolerance=ONSET_TOL, offset_ratio=None)
    mr = {a for a, _ in matched}
    me = {b for _, b in matched}
    ron = riv[:, 0]
    eon = eiv[:, 0]
    rp = np.array([n[2] for n in ref])
    ep = np.array([n[2] for n in est])
    # Each missed reference note, by what was heard instead within 50 ms.
    fn = {"octave": 0, "semitone": 0, "other_pitch": 0, "nothing": 0, "soft": 0}
    for i in range(len(ref)):
        if i in mr:
            continue
        near = np.where(np.abs(eon - ron[i]) <= ONSET_TOL)[0]
        d = np.abs(ep[near] - rp[i])
        if near.size and np.any((d % 12 == 0) & (d > 0)):
            fn["octave"] += 1
        elif near.size and np.any(d == 1):
            fn["semitone"] += 1
        elif near.size:
            fn["other_pitch"] += 1
        else:
            fn["nothing"] += 1
        if ref[i][3] < 50:
            fn["soft"] += 1
    # Each extra estimated note, by what the reference has there.
    fp = {
        "octave": 0,
        "fragment": 0,
        "harmonic": 0,
        "semitone": 0,
        "early_late": 0,
        "no_ref_near": 0,
    }
    roff = riv[:, 1]
    for j in range(len(est)):
        if j in me:
            continue
        near = np.where(np.abs(ron - eon[j]) <= ONSET_TOL)[0]
        d = ep[j] - rp[near]
        held = np.where((ron < eon[j] - ONSET_TOL) & (roff > eon[j]) & (rp == ep[j]))[0]
        close = np.where((np.abs(ron - eon[j]) <= 0.15) & (rp == ep[j]))[0]
        if near.size and np.any((np.abs(d) % 12 == 0) & (d != 0)):
            fp["octave"] += 1
        elif held.size:
            fp["fragment"] += 1
        elif near.size and np.any(np.isin(d, (19, 28, 31, 7))):
            fp["harmonic"] += 1
        elif near.size and np.any(np.abs(d) == 1):
            fp["semitone"] += 1
        elif close.size:
            fp["early_late"] += 1
        else:
            fp["no_ref_near"] += 1
    dt = [eon[b] - ron[a] for a, b in matched]
    out.update(
        {
            "p": p,
            "r": r,
            "f1": f,
            "p_off": po,
            "r_off": ro,
            "f1_off": fo,
            "f1_chroma": fc,
            "octave_err": fn["octave"] / len(ref),
            "fn": fn,
            "fp": fp,
            "onset_dt_ms": round(1000 * float(np.median(dt)), 2) if dt else None,
            "onset_abs_dt_ms": round(1000 * float(np.mean(np.abs(dt))), 2) if dt else None,
        }
    )
    return out


def _roundtrip(notes, stem: str, workdir: Path) -> list:
    """The notes as the app's .mid stores them: written by midi_file.write_midi
    at the default tempo, read back by midi_file.read_notes."""
    from app.pipeline.midi_file import default_tempo, read_notes, write_midi

    workdir.mkdir(parents=True, exist_ok=True)
    path = workdir / f"{stem}-{os.getpid()}.mid"
    write_midi(notes, path, stem, default_tempo())
    notes = read_notes(path)["notes"]
    path.unlink()
    return notes


# The activations piano_hr's post-processing reads (piano_hr.model_output).
_HR_KEYS = (
    "reg_onset_output",
    "reg_offset_output",
    "frame_output",
    "velocity_output",
    "reg_pedal_onset_output",
    "reg_pedal_offset_output",
    "pedal_frame_output",
)


def _decode(cache: Path, params: dict, engine: str = "basic_pitch") -> list:
    import numpy as np

    if engine == "piano_hr":
        from app.pipeline import piano_hr

        with np.load(cache) as z:
            output = {k: np.array(z[k]) for k in _HR_KEYS}
        return piano_hr.output_notes(output, params)

    from app.pipeline import midi_worker

    with np.load(cache) as z:
        output = {k: np.array(z[k]) for k in ("note", "onset", "contour")}
    return midi_worker.output_notes(output, params)


def _worker_notes(
    src: Path,
    stem: str,
    params: dict,
    workdir: Path,
    engine: str = "basic_pitch",
    device: str = "cpu",
) -> tuple[list, float, str | None]:
    """Spawn the MIDI worker exactly as midi.py does and read its file:
    (notes, seconds the worker took, the device it said it ran on)."""
    from app.pipeline import midi as midi_run
    from app.pipeline.midi_file import default_tempo, read_notes

    workdir.mkdir(parents=True, exist_ok=True)
    out = workdir / f"{stem}-{os.getpid()}.mid"
    cmd = midi_run._spawn_worker_cmd(src, out, stem, params, default_tempo(), engine, device)
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8:replace"
    started = time.monotonic()
    result = subprocess.run(
        cmd, cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8"
    )
    took = time.monotonic() - started
    events = [midi_run.parse_event(line) for line in result.stdout.splitlines()]
    events = [e for e in events if e]
    if not events or events[-1]["event"] != "done":
        raise RuntimeError(f"worker failed on {src}: {events[-1:]} {result.stderr[-800:]}")
    notes = read_notes(out)["notes"]
    out.unlink()
    return notes, took, events[-1].get("device")


def _mean(rows: list[dict], key: str) -> float:
    vals = [r[key] for r in rows if isinstance(r.get(key), (int, float))]
    return sum(vals) / len(vals) if vals else 0.0


def _group_key(row: dict) -> str:
    split = "tune" if row["split"] == "tune" else "held"
    return f"{row['set']}/{split}/{row['stem']}/{row['cond']}/{row['fmt']}"


def summarize(rows: list[dict]) -> dict:
    groups: dict[str, list] = {}
    for row in rows:
        groups.setdefault(_group_key(row), []).append(row)
    out = {}
    for key, rs in sorted(groups.items()):
        fn: dict = {}
        fp: dict = {}
        for r in rs:
            for k, v in (r.get("fn") or {}).items():
                fn[k] = fn.get(k, 0) + v
            for k, v in (r.get("fp") or {}).items():
                fp[k] = fp.get(k, 0) + v
        out[key] = {
            "items": len(rs),
            **{
                k: round(_mean(rs, k), 4)
                for k in ("p", "r", "f1", "p_off", "r_off", "f1_off", "f1_chroma", "octave_err")
            },
            "n_ref": sum(r["n_ref"] for r in rs),
            "n_est": sum(r["n_est"] for r in rs),
            "fn": fn,
            "fp": fp,
        }
    return out


def _params_for(stem: str, preset: str, override: dict | None, engine: str = "basic_pitch") -> dict:
    from app.pipeline.midi import run_params

    params = run_params(stem, preset, engine)
    if override:
        params = {**params, **override.get("all", {}), **override.get(stem, {})}
    return params


def _score_one(task: tuple) -> dict | None:
    """One (item, preset, condition, format): its notes, scored. Top level, so
    a process pool can run it."""
    bench, item, preset, params, cond, fmt, worker, est_dir, engine, device = task
    work = bench / "work"
    ref = json.loads((bench / item["ref"]).read_text(encoding="utf-8"))
    took = None
    ran_on = None
    if est_dir is not None:
        # Another engine's notes, [onset, offset, pitch, ...].
        path = est_dir / cond / fmt / f"{item['id']}.json"
        if not path.is_file():
            return None
        notes = json.loads(path.read_text(encoding="utf-8"))
    elif worker:
        src = _input(bench, item, cond, fmt)
        if not src.is_file():
            return None
        notes, took, ran_on = _worker_notes(src, item["stem"], params, work, engine, device)
    else:
        cache = _cache_path(bench, item, cond, fmt, engine)
        if not cache.is_file():
            return None
        notes = _roundtrip(_decode(cache, params, engine), item["stem"], work)
    s = score_notes(ref, notes, item["stem"])
    row = {
        "id": item["id"],
        "set": item["set"],
        "split": item["split"],
        "stem": item["stem"],
        "cond": cond,
        "fmt": fmt,
        "preset": preset,
        "engine": engine,
        "seconds": item["seconds"],
        **s,
    }
    if took is not None:
        row["worker_s"] = round(took, 2)
        row["device"] = ran_on
    return row


def _pool_init(bench: Path) -> None:
    _setup_env(bench)
    _vendor_path()
    # Everything a decode and a score import, once per process, before any
    # task: sixteen processes importing scipy at once mid-task have failed
    # on Windows with EINVAL reading a module file.
    import mir_eval.transcription  # noqa: F401
    from basic_pitch import note_creation  # noqa: F401


def run_score(
    bench: Path,
    items: list[dict],
    presets,
    conditions,
    formats,
    worker: bool,
    override=None,
    quiet=False,
    est_dir: Path | None = None,
    jobs: int = 1,
    pool=None,
    engine: str = "basic_pitch",
    device: str = "cpu",
):
    from concurrent.futures import ProcessPoolExecutor

    tasks = [
        (
            bench,
            item,
            preset,
            _params_for(item["stem"], preset, override, engine),
            cond,
            fmt,
            worker,
            est_dir,
            engine,
            device,
        )
        for preset in presets
        for item in _engine_items(items, engine)
        for cond in conditions
        for fmt in formats
    ]
    if pool is not None and not worker:
        results = list(pool.map(_score_one, tasks, chunksize=1))
    elif jobs > 1 and not worker:
        with ProcessPoolExecutor(jobs, initializer=_pool_init, initargs=(bench,)) as own:
            results = list(own.map(_score_one, tasks, chunksize=1))
    else:
        results = [_score_one(t) for t in tasks]
    rows = [r for r in results if r is not None]
    if not quiet:
        for r in rows:
            print(
                f"{r['preset']:9s} {r['id']:34s} {r['cond']:5s} {r['fmt']} F1 {r['f1']:.3f} "
                f"F1off {r['f1_off']:.3f} n {r['n_est']}/{r['n_ref']}"
            )
    return rows


def _table(summary_by_preset: dict) -> str:
    lines = [
        "| preset | set/split/stem/cond/fmt | items | P | R | F1 | F1 on+off | F1 chroma | octave err |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for preset, summary in summary_by_preset.items():
        for key, s in summary.items():
            lines.append(
                f"| {preset} | {key} | {s['items']} | {s['p']:.3f} | {s['r']:.3f} | {s['f1']:.3f} | "
                f"{s['f1_off']:.3f} | {s['f1_chroma']:.3f} | {s['octave_err']:.3f} |"
            )
    return "\n".join(lines) + "\n"


def cmd_score(args) -> None:
    bench = args.bench
    override = json.loads(args.override) if args.override else None
    items = _items(bench, args.split, args.only)
    est_dir = args.est_dir.resolve() if args.est_dir else None
    rows = run_score(
        bench,
        items,
        args.presets,
        args.conditions,
        args.formats,
        args.worker,
        override,
        est_dir=est_dir,
        jobs=args.jobs,
        engine=args.engine,
        device=_device(args.device) if args.engine == "piano_hr" else "cpu",
    )
    by_preset = {p: summarize([r for r in rows if r["preset"] == p]) for p in args.presets}
    results = bench / "results"
    timed = [r for r in rows if "worker_s" in r]
    speed = None
    if timed:
        # Seconds of work per minute of audio through the worker (its start,
        # the model's load, the run and the file), per device it ran on.
        speed = {}
        for dev in sorted({str(r.get("device")) for r in timed}):
            rs = [r for r in timed if str(r.get("device")) == dev]
            minutes = sum(r["seconds"] for r in rs) / 60
            speed[dev] = round(sum(r["worker_s"] for r in rs) / minutes, 2)
        print("worker seconds per audio minute:", speed)
    _write_json(
        results / f"{args.tag}.json",
        {
            "engine": args.engine,
            "override": override,
            "worker": args.worker,
            "speed_s_per_min": speed,
            "summary": by_preset,
            "rows": rows,
        },
    )
    (results / f"{args.tag}.md").write_text(_table(by_preset), encoding="utf-8")
    print(_table(by_preset))


def cmd_sweep(args) -> None:
    """Try each parameter set in a JSON list on the tuning split only."""
    bench = args.bench
    trials = json.loads(Path(args.trials).read_text(encoding="utf-8"))
    items = _items(bench, "tune", args.only)
    if any(i["split"] != "tune" for i in items):
        sys.exit("sweep runs on the tuning split only")
    from concurrent.futures import ProcessPoolExecutor

    out = []
    pool = ProcessPoolExecutor(args.jobs, initializer=_pool_init, initargs=(bench,))
    for trial in trials:
        rows = run_score(
            bench,
            items,
            [args.preset],
            args.conditions,
            args.formats,
            False,
            trial,
            quiet=True,
            pool=pool,
            engine=args.engine,
        )
        summary = summarize(rows)
        line = {"trial": trial, "summary": summary}
        out.append(line)
        brief = "  ".join(
            f"{k.split('/')[2]}/{k.split('/')[3]}/{k.split('/')[4]} {v['f1']:.3f}|{v['f1_off']:.3f}"
            for k, v in summary.items()
        )
        print(json.dumps(trial), brief, flush=True)
    pool.shutdown()
    _write_json(bench / "results" / f"sweep-{args.tag}.json", out)


# -- which piano engine wins, and whether anything known at run time says so

# Signals a run could measure before it picks an engine (each is computed
# from the files the app has for a song: the separated stems, the piano stem,
# and what each engine makes of the piano stem).
SIGNALS = ("share_db", "ratio", "agree", "hr_conf")
# A unit is a song in one condition and format; the engines "differ" on it
# when their onset F1 is this far apart, and only those units count towards
# how often a rule picks the winner.
DIFFER = 0.02


def _energy(path: Path) -> float:
    import numpy as np
    import soundfile as sf

    y, _sr = sf.read(str(path), dtype="float32", always_2d=True)
    return float(np.mean(np.square(y, dtype=np.float64)))


def _share_db(bench: Path, item: dict, cond: str, fmt: str) -> float | None:
    """10 log10 of the piano stem's energy over all six separated stems', the
    way a run could read it from the song's stems folder. None for the clean
    condition, which has no separation of its own."""
    import math

    if cond != "sep":
        return None
    piano = _input(bench, item, cond, fmt)
    stems = [
        piano.with_name(f"{s}.wav") for s in ("vocals", "drums", "bass", "guitar", "piano", "other")
    ]
    if not all(p.is_file() for p in stems):
        return None
    total = sum(_energy(p) for p in stems)
    share = _energy(piano) / total if total > 0 else 0.0
    return 10 * math.log10(max(share, 1e-9))


def _hr_conf(cache: Path) -> float:
    """piano_hr's own confidence: the mean onset regression peak over the
    frames where it found an onset (reg_onset_output over 0.3, its threshold)."""
    import numpy as np

    with np.load(cache) as z:
        onset = np.array(z["reg_onset_output"])
    peaks = onset[onset >= 0.3]
    return float(peaks.mean()) if peaks.size else 0.0


def _compare_one(task: tuple) -> dict | None:
    bench, item, cond, fmt = task
    ref = json.loads((bench / item["ref"]).read_text(encoding="utf-8"))
    notes = {}
    row = {"id": item["id"], "set": item["set"], "split": item["split"], "cond": cond, "fmt": fmt}
    for engine in ("basic_pitch", "piano_hr"):
        cache = _cache_path(bench, item, cond, fmt, engine)
        if not cache.is_file():
            return None
        params = _params_for("piano", "balanced", None, engine)
        notes[engine] = _roundtrip(_decode(cache, params, engine), "piano", bench / "work")
        s = score_notes(ref, notes[engine], "piano")
        row[engine] = {"f1": s["f1"], "f1_off": s["f1_off"], "n": len(notes[engine])}
    agree = score_notes(notes["basic_pitch"], notes["piano_hr"], "piano")
    row["signals"] = {
        "share_db": _share_db(bench, item, cond, fmt),
        "ratio": len(notes["piano_hr"]) / max(1, len(notes["basic_pitch"])),
        "agree": agree["f1"],
        "hr_conf": _hr_conf(_cache_path(bench, item, cond, fmt, "piano_hr")),
    }
    return row


def _rule_score(units: list[dict], signal: str, cut: float, above: bool) -> dict:
    """piano_hr where the signal is above ``cut`` (or below it), else Basic
    Pitch: the mean onset F1 of what the rule picks, and how often it picks the
    winner where the engines differ."""
    picked, right, differ = [], 0, 0
    for u in units:
        value = u["signals"][signal]
        hr = (value > cut) if above else (value < cut)
        f1 = u["piano_hr"]["f1"] if hr else u["basic_pitch"]["f1"]
        picked.append(f1)
        delta = u["piano_hr"]["f1"] - u["basic_pitch"]["f1"]
        if abs(delta) > DIFFER:
            differ += 1
            right += (delta > 0) == hr
    return {
        "f1": sum(picked) / len(picked),
        "right": right,
        "differ": differ,
        "accuracy": right / differ if differ else None,
    }


def _fit(units: list[dict], signal: str) -> dict | None:
    """The cut and direction with the best mean F1 on ``units``."""
    values = sorted({u["signals"][signal] for u in units})
    if len(values) < 2:
        return None
    cuts = [(a + b) / 2 for a, b in zip(values, values[1:], strict=False)]
    cuts = [values[0] - 1, *cuts, values[-1] + 1]
    best = None
    for cut in cuts:
        for above in (True, False):
            s = _rule_score(units, signal, cut, above)
            if best is None or s["f1"] > best["f1"] + 1e-12:
                best = {"cut": cut, "above": above, **s}
    return best


def cmd_compare(args) -> None:
    """Per song: which piano engine wins (onset F1, balanced), and whether one
    of SIGNALS predicts it. Each signal's rule is fitted on the tuning split
    only and then applied, unchanged, to the held-out items. An ``auto`` engine
    is worth offering only if a rule does both: on the tuning split it picks
    the winner more often than always taking the better engine does, with a
    higher mean F1; and on held-out it is at or above the better fixed engine
    overall and within 0.01 of it in every set and condition.
    results/<tag>.json and .md hold the rules and the scores; the counts are
    of the units where the engines differ by more than DIFFER."""
    from concurrent.futures import ProcessPoolExecutor

    bench = args.bench
    items = [i for i in _items(bench) if i["stem"] == "piano"]
    tasks = [(bench, i, c, f) for i in items for c in CONDITIONS for f in FORMATS]
    with ProcessPoolExecutor(args.jobs, initializer=_pool_init, initargs=(bench,)) as pool:
        units = [u for u in pool.map(_compare_one, tasks, chunksize=1) if u is not None]
    tune = [u for u in units if u["split"] == "tune"]
    held = [u for u in units if u["split"] != "tune"]
    lines = [
        "| signal | units | fitted on tune | tune: F1 auto / BP / HR, winner picked "
        "| held: F1 auto / BP / HR, winner picked |",
        "|---|---|---|---|---|",
    ]
    report = {"differ": DIFFER, "signals": {}, "units": units}

    def fixed(us: list[dict], engine: str) -> float:
        return sum(u[engine]["f1"] for u in us) / len(us)

    for signal in SIGNALS:
        t = [u for u in tune if u["signals"][signal] is not None]
        h = [u for u in held if u["signals"][signal] is not None]
        rule = _fit(t, signal) if t else None
        if rule is None or not h:
            continue
        on_held = _rule_score(h, signal, rule["cut"], rule["above"])
        groups = {}
        for key in sorted({(u["set"], u["cond"]) for u in h}):
            g = [u for u in h if (u["set"], u["cond"]) == key]
            groups["/".join(key)] = {
                "units": len(g),
                "auto": _rule_score(g, signal, rule["cut"], rule["above"]),
                "basic_pitch": fixed(g, "basic_pitch"),
                "piano_hr": fixed(g, "piano_hr"),
            }
        report["signals"][signal] = {"rule": rule, "held": on_held, "held_groups": groups}
        side = ">" if rule["above"] else "<"

        def pct(s):
            return f"{s['right']}/{s['differ']}" if s["differ"] else "-"

        lines.append(
            f"| {signal} | {len(t)} + {len(h)} | piano_hr if {side} {rule['cut']:.3f} | "
            f"{rule['f1']:.3f} / {fixed(t, 'basic_pitch'):.3f} / {fixed(t, 'piano_hr'):.3f}, "
            f"{pct(rule)} | {on_held['f1']:.3f} / {fixed(h, 'basic_pitch'):.3f} / "
            f"{fixed(h, 'piano_hr'):.3f}, {pct(on_held)} |"
        )
        for name, g in groups.items():
            lines.append(
                f"| {signal}: {name} | {g['units']} | | | {g['auto']['f1']:.3f} / "
                f"{g['basic_pitch']:.3f} / {g['piano_hr']:.3f}, {pct(g['auto'])} |"
            )
    _write_json(bench / "results" / f"{args.tag}.json", report)
    text = "\n".join(lines) + "\n"
    (bench / "results" / f"{args.tag}.md").write_text(text, encoding="utf-8")
    print(text)


# -- the engine comparison's held-out sets (data/midi-bench/eng)

# GM drum notes to the five classes engine-scripts/common.py scores.
_ENG_DRUM_CLASSES = ("kick", "snare", "hihat", "tom", "cymbal")
_ENG_GM_DRUM = {
    35: "kick", 36: "kick",
    37: "snare", 38: "snare", 40: "snare",
    42: "hihat", 44: "hihat", 46: "hihat",
    41: "tom", 43: "tom", 45: "tom", 47: "tom", 48: "tom", 50: "tom",
    49: "cymbal", 51: "cymbal", 52: "cymbal", 53: "cymbal", 55: "cymbal", 57: "cymbal",
    59: "cymbal",
}  # fmt: skip
_ENG_IDMT_CLASSES = ("kick", "snare", "hihat")
# The label engine-scripts/score.py gave each engine's score of an input.
_ENG_LABEL = {
    ("ymt3", "drums"): "YourMT3+",
    ("ymt3", None): "YourMT3+ (class)",
    ("basic_pitch", "vocals"): "Basic Pitch (mono)",
    ("basic_pitch", None): "Basic Pitch",
}


def _eng_drums(notes) -> dict:
    out = {c: [] for c in _ENG_DRUM_CLASSES}
    for n in notes:
        c = _ENG_GM_DRUM.get(int(n[2]))
        if c:
            out[c].append(float(n[0]))
    return {c: sorted(v) for c, v in out.items()}


def _eng_score_notes(ref: list, est: list) -> dict:
    """engine-scripts/score.py score_notes: onset F1 at 50 ms on whole MIDI
    notes, with offsets, and the best of one octave shift per track."""
    tr = _load_mir_eval()
    import numpy as np

    def arrays(notes):
        if not notes:
            return np.zeros((0, 2)), np.zeros(0)
        iv = np.array([[n[0], max(n[1], n[0] + 1e-3)] for n in notes], dtype=float)
        hz = 440.0 * 2 ** ((np.round(np.array([n[2] for n in notes], dtype=float)) - 69) / 12)
        return iv, hz

    out = {"n_ref": len(ref), "n_est": len(est)}
    if not ref or not est:
        out.update({"f1": 0.0, "f1_off": 0.0, "f1_oct": 0.0})
        return out
    riv, rhz = arrays(ref)
    eiv, ehz = arrays(est)
    _p, _r, f, _ = tr.precision_recall_f1_overlap(
        riv, rhz, eiv, ehz, onset_tolerance=ONSET_TOL, offset_ratio=None
    )
    _p, _r, fo, _ = tr.precision_recall_f1_overlap(
        riv, rhz, eiv, ehz, onset_tolerance=ONSET_TOL, offset_ratio=0.2, offset_min_tolerance=0.05
    )
    best = f
    for k in (-24, -12, 12, 24):
        _p, _r, fk, _ = tr.precision_recall_f1_overlap(
            riv, rhz, eiv, ehz * 2 ** (k / 12), onset_tolerance=ONSET_TOL, offset_ratio=None
        )
        best = max(best, fk)
    out.update({"f1": f, "f1_off": fo, "f1_oct": best})
    return out


def _eng_score_drums(ref: dict, est: dict, classes) -> dict:
    """engine-scripts/score.py score_drums: hits summed over the classes."""
    import numpy as np

    _load_mir_eval()
    import mir_eval.util as mu

    tp = fp = fn = 0
    for c in classes:
        r = np.array(sorted(ref.get(c, [])))
        e = np.array(sorted(est.get(c, [])))
        if len(r) == 0 and len(e) == 0:
            continue
        hits = len(mu.match_events(r, e, ONSET_TOL)) if len(r) and len(e) else 0
        tp += hits
        fp += len(e) - hits
        fn += len(r) - hits
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    return {"f1": 2 * p * r / (p + r) if p + r else 0.0, "tp": tp, "fp": fp, "fn": fn}


def _eng_in_process(src: Path, stem: str, device: str) -> tuple[list, float]:
    """YourMT3+ on ``stem`` with the worker's own functions, in this process
    (the worker refuses a stem the engine is not offered for)."""
    from app.pipeline import ymt3

    global _ENG_MODEL
    if _ENG_MODEL is None:
        _ENG_MODEL = ymt3.load_weights(ymt3.build_model(), ymt3.weights_path(), device)
    started = time.monotonic()
    segs = ymt3.segments(ymt3.read_audio(src))
    preds = ymt3.model_output(_ENG_MODEL, segs, device, 8)
    events, _program = ymt3.stem_notes(ymt3.decode(_ENG_MODEL, preds, len(segs)), stem)
    took = time.monotonic() - started
    return [[e[0], e[1], e[2], 92] for e in events], took


_ENG_MODEL = None


def cmd_eng(args) -> None:
    """Every input of eng/inputs.json in the asked sets, conditions and
    stems, through the real worker, scored beside the engine comparison's
    own score of the same input (eng/scores.json)."""
    eng = args.bench / "eng"
    rows = json.loads((eng / "inputs.json").read_text(encoding="utf-8-sig"))
    old = json.loads((eng / "scores.json").read_text(encoding="utf-8-sig"))
    engine = args.engine
    device = _device(args.device) if engine != "basic_pitch" else "cpu"
    out_path = args.bench / "results" / f"{args.tag}.json"
    done = {}
    if out_path.is_file():
        for r in json.loads(out_path.read_text(encoding="utf-8")).get("rows", []):
            done[(r["key"], r["cond"])] = r
    from app.pipeline import midi as midi_run
    from app.pipeline.midi_file import read_notes

    work = args.bench / "work" / f"eng-{args.tag}"
    results = list(done.values())
    taken: dict[str, int] = {}
    for row in rows:
        stem, cond = row["stem"], row["cond"]
        if args.sets and row["set"] not in args.sets:
            continue
        if cond not in args.conds or (args.stems and stem not in args.stems):
            continue
        if engine == "basic_pitch" and stem == "drums":
            continue
        if (row["key"], cond) in done:
            continue
        if args.limit is not None:
            if taken.get(stem, 0) >= args.limit:
                continue
            taken[stem] = taken.get(stem, 0) + 1
        label = _ENG_LABEL.get((engine, stem)) or _ENG_LABEL[(engine, None)]
        before = next(
            (
                o
                for o in old
                if o["key"] == row["key"] and o["cond"] == cond and o["engine"] == label
            ),
            None,
        )
        src = Path(row["audio"])
        in_process = engine == "ymt3" and stem == "vocals"
        try:
            if in_process:
                notes, took = _eng_in_process(src, stem, device)
                ran_on, peak = device, None
            else:
                params = midi_run.run_params(stem, "balanced", engine)
                work.mkdir(parents=True, exist_ok=True)
                out = work / f"{stem}-{os.getpid()}.mid"
                from app.pipeline.midi_file import default_tempo

                cmd = midi_run._spawn_worker_cmd(
                    src, out, stem, params, default_tempo(), engine, device
                )
                events, result, took = _run_protocol(cmd)
                if not events or events[-1]["event"] != "done":
                    raise RuntimeError(f"{events[-1:]} {result.stderr[-600:]}")
                notes = read_notes(out)["notes"]
                out.unlink()
                ran_on, peak = events[-1].get("device"), events[-1].get("gpu_peak_mb")
        except Exception as exc:  # noqa: BLE001 -- one bad input is reported, not fatal
            print("FAILED", row["key"], cond, exc, flush=True)
            continue
        if stem == "drums":
            ref = json.loads(Path(row["ref"]).read_text(encoding="utf-8-sig"))
            if row["kind"] == "drums-notes":
                ref = _eng_drums(ref)
            classes = _ENG_IDMT_CLASSES if row["set"] == "idmt" else _ENG_DRUM_CLASSES
            score = _eng_score_drums(ref, _eng_drums(notes), classes)
        else:
            ref = json.loads(Path(row["ref"]).read_text(encoding="utf-8-sig"))
            score = _eng_score_notes(ref, notes)
            if row.get("ref_alt"):
                alt = json.loads(Path(row["ref_alt"]).read_text(encoding="utf-8-sig"))
                score["f1_alt"] = _eng_score_notes(alt, notes)["f1"]
        rec = {
            "key": row["key"],
            "set": row["set"],
            "stem": stem,
            "cond": cond,
            "engine": engine,
            "device": ran_on,
            "gpu_peak_mb": peak,
            "in_process": in_process,
            "seconds_audio": float(_audio_seconds(src)),
            "worker_s": round(took, 2),
            **score,
            "before": {k: before.get(k) for k in ("f1", "f1_off", "f1_oct")} if before else None,
            "before_alt": (before.get("alt") or {}).get("f1") if before else None,
        }
        results.append(rec)
        print(
            row["key"],
            cond,
            f"{score['f1']:.3f}",
            "was",
            f"{before['f1']:.3f}" if before else "-",
            ran_on,
            f"{took:.1f}s",
            flush=True,
        )
        _write_json(out_path, {"engine": engine, "rows": results})
    _write_json(out_path, {"engine": engine, "rows": results})
    text = _eng_table(results)
    (args.bench / "results" / f"{args.tag}.md").write_text(text, encoding="utf-8")
    print(text)


def _audio_seconds(path: Path) -> float:
    import soundfile as sf

    info = sf.info(str(path))
    return info.frames / info.samplerate


def _eng_table(rows: list[dict]) -> str:
    """Means per set, stem and condition: this run, the comparison's score of
    the same inputs, and the largest gap on one input; then the speed."""
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        groups.setdefault((r["set"], r["stem"], r["cond"]), []).append(r)
    lines = [
        "| set | stem | cond | inputs | F1 worker | F1 before | mean gap | max gap |"
        " F1 on+off | oct-aligned | F1 alt (A2) |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for (set_, stem, cond), rs in sorted(groups.items()):
        paired = [r for r in rs if r.get("before") and r["before"].get("f1") is not None]
        gaps = [r["f1"] - r["before"]["f1"] for r in paired]
        alt = [r["f1_alt"] for r in rs if "f1_alt" in r]
        pitched = stem != "drums"
        cells = [
            set_,
            stem,
            cond,
            str(len(rs)),
            f"{_mean(rs, 'f1'):.3f}",
            f"{sum(r['before']['f1'] for r in paired) / len(paired):.3f}" if paired else "-",
            f"{sum(gaps) / len(gaps):+.4f}" if gaps else "-",
            f"{max(gaps, key=abs):+.4f}" if gaps else "-",
            f"{_mean(rs, 'f1_off'):.3f}" if pitched else "-",
            f"{_mean(rs, 'f1_oct'):.3f}" if pitched else "-",
            f"{sum(alt) / len(alt):.3f}" if alt else "-",
        ]
        lines.append("| " + " | ".join(cells) + " |")
    lines += [
        "",
        "| device | inputs | audio min | worker s per audio min | peak GPU MB |",
        "|---|---|---|---|---|",
    ]
    by_dev: dict[str, list[dict]] = {}
    for r in rows:
        if not r.get("in_process"):
            by_dev.setdefault(str(r.get("device")), []).append(r)
    for dev, rs in sorted(by_dev.items()):
        minutes = sum(r["seconds_audio"] for r in rs) / 60
        peaks = [r["gpu_peak_mb"] for r in rs if r.get("gpu_peak_mb")]
        lines.append(
            f"| {dev} | {len(rs)} | {minutes:.1f} | "
            f"{sum(r['worker_s'] for r in rs) / max(minutes, 1e-9):.1f} | "
            f"{max(peaks) if peaks else '-'} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bench", type=Path, default=DEFAULT_BENCH)
    parser.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) // 2))
    parser.add_argument(
        "--engine", choices=("basic_pitch", "piano_hr", "ymt3"), default="basic_pitch"
    )
    parser.add_argument("--device", choices=("cpu", "cuda"), help="piano_hr and ymt3 only")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("prepare")
    p = sub.add_parser("fetch-piano-hr")
    p.add_argument("--from-url", help="fetch the same file from this address instead")
    p = sub.add_parser("fetch-ymt3")
    p.add_argument("--from-file", type=Path, help="a copy of the pinned checkpoint already here")
    p = sub.add_parser("eng")
    p.add_argument("--tag", required=True)
    p.add_argument("--sets", nargs="+", help="slakh-test mdb idmt vocadito guitartechs")
    p.add_argument("--conds", nargs="+", default=["sep", "clean", "clean-di"])
    p.add_argument("--stems", nargs="+")
    p.add_argument("--limit", type=int, help="the first N inputs of each stem only (a speed check)")
    p = sub.add_parser("separate")
    p.add_argument("--formats", nargs="+", default=list(FORMATS))
    p = sub.add_parser("cache")
    p.add_argument("--conditions", nargs="+", default=list(CONDITIONS))
    p.add_argument("--formats", nargs="+", default=list(FORMATS))
    p.add_argument("--split")
    p.add_argument("--only")
    p = sub.add_parser("score")
    p.add_argument("--tag", required=True)
    p.add_argument("--presets", nargs="+", default=["balanced", "detailed", "clean"])
    p.add_argument("--conditions", nargs="+", default=list(CONDITIONS))
    p.add_argument("--formats", nargs="+", default=list(FORMATS))
    p.add_argument("--split")
    p.add_argument("--only")
    p.add_argument("--worker", action="store_true")
    p.add_argument("--override", help='JSON {"all": {...}, "piano": {...}, "bass": {...}}')
    p.add_argument(
        "--est-dir", type=Path, help="score another engine's notes: <dir>/<cond>/<fmt>/<id>.json"
    )
    p = sub.add_parser("compare")
    p.add_argument("--tag", default="compare-engines")
    p = sub.add_parser("sweep")
    p.add_argument("--tag", required=True)
    p.add_argument("--trials", required=True, help="a JSON file: a list of override objects")
    p.add_argument("--preset", default="balanced")
    p.add_argument("--conditions", nargs="+", default=list(CONDITIONS))
    p.add_argument("--formats", nargs="+", default=["wav"])
    p.add_argument("--only")
    args = parser.parse_args(argv)
    _setup_env(args.bench, ymt3_models=args.engine == "ymt3" or args.cmd == "fetch-ymt3")
    _vendor_path(args.engine)
    {
        "prepare": cmd_prepare,
        "fetch-piano-hr": cmd_fetch_piano_hr,
        "fetch-ymt3": cmd_fetch_ymt3,
        "eng": cmd_eng,
        "separate": cmd_separate,
        "cache": cmd_cache,
        "score": cmd_score,
        "sweep": cmd_sweep,
        "compare": cmd_compare,
    }[args.cmd](args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
