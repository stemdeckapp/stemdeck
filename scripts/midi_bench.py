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

``score`` decodes the cached activations in this process with the worker's own
functions (midi_worker.output_notes), writes and reads the .mid like the worker,
and so gives the worker's notes without running the model again; ``--worker``
spawns the worker exactly as midi.py does instead, to prove the two agree.
``sweep`` tries parameter sets on the tuning split only.

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


def _setup_env(bench: Path) -> None:
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
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))


def _vendor_path() -> None:
    from app.pipeline.midi_file import VENDOR_DIR
    from app.pipeline.midi_worker import _install_engine

    _install_engine(VENDOR_DIR)


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


def _cache_path(bench: Path, item: dict, cond: str, fmt: str) -> Path:
    stem = item["stem"] if cond == "sep" else "clean"
    return bench / "cache" / cond / fmt / f"{item['id']}-{stem}.npz"


def cmd_cache(args) -> None:
    import numpy as np

    from app.pipeline import midi_worker
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


def _decode(cache: Path, params: dict) -> list:
    import numpy as np

    from app.pipeline import midi_worker

    with np.load(cache) as z:
        output = {k: np.array(z[k]) for k in ("note", "onset", "contour")}
    return midi_worker.output_notes(output, params)


def _worker_notes(src: Path, stem: str, params: dict, workdir: Path) -> tuple[list, float]:
    """Spawn the MIDI worker exactly as midi.py does and read its file."""
    from app.pipeline import midi as midi_run
    from app.pipeline.midi_file import default_tempo, read_notes

    workdir.mkdir(parents=True, exist_ok=True)
    out = workdir / f"{stem}-{os.getpid()}.mid"
    cmd = midi_run._spawn_worker_cmd(src, out, stem, params, default_tempo())
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
    return notes, took


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


def _params_for(stem: str, preset: str, override: dict | None) -> dict:
    from app.pipeline.midi import run_params

    params = run_params(stem, preset)
    if override:
        params = {**params, **override.get("all", {}), **override.get(stem, {})}
    return params


def _score_one(task: tuple) -> dict | None:
    """One (item, preset, condition, format): its notes, scored. Top level, so
    a process pool can run it."""
    bench, item, preset, params, cond, fmt, worker, est_dir = task
    work = bench / "work"
    ref = json.loads((bench / item["ref"]).read_text(encoding="utf-8"))
    took = None
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
        notes, took = _worker_notes(src, item["stem"], params, work)
    else:
        cache = _cache_path(bench, item, cond, fmt)
        if not cache.is_file():
            return None
        notes = _roundtrip(_decode(cache, params), item["stem"], work)
    s = score_notes(ref, notes, item["stem"])
    row = {
        "id": item["id"],
        "set": item["set"],
        "split": item["split"],
        "stem": item["stem"],
        "cond": cond,
        "fmt": fmt,
        "preset": preset,
        "seconds": item["seconds"],
        **s,
    }
    if took is not None:
        row["worker_s"] = round(took, 2)
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
):
    from concurrent.futures import ProcessPoolExecutor

    tasks = [
        (
            bench,
            item,
            preset,
            _params_for(item["stem"], preset, override),
            cond,
            fmt,
            worker,
            est_dir,
        )
        for preset in presets
        for item in items
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
    )
    by_preset = {p: summarize([r for r in rows if r["preset"] == p]) for p in args.presets}
    results = bench / "results"
    _write_json(
        results / f"{args.tag}.json",
        {"override": override, "worker": args.worker, "summary": by_preset, "rows": rows},
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bench", type=Path, default=DEFAULT_BENCH)
    parser.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) // 2))
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("prepare")
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
    p = sub.add_parser("sweep")
    p.add_argument("--tag", required=True)
    p.add_argument("--trials", required=True, help="a JSON file: a list of override objects")
    p.add_argument("--preset", default="balanced")
    p.add_argument("--conditions", nargs="+", default=list(CONDITIONS))
    p.add_argument("--formats", nargs="+", default=["wav"])
    p.add_argument("--only")
    args = parser.parse_args(argv)
    _setup_env(args.bench)
    _vendor_path()
    {
        "prepare": cmd_prepare,
        "separate": cmd_separate,
        "cache": cmd_cache,
        "score": cmd_score,
        "sweep": cmd_sweep,
    }[args.cmd](args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
