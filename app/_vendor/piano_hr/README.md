# Vendored piano engine (piano_hr)

The second engine for MIDI from the piano stem (app/pipeline/piano_hr.py):
ByteDance's high-resolution piano transcription, Kong et al. 2021,
"High-resolution Piano Transcription with Pedals by Regressing Onset and
Offset Times". Only the worker process (app/pipeline/midi_worker.py, with
`--engine piano_hr`) puts this folder on `sys.path`; the server process never
imports anything in it.

## Why it is vendored

Both packages are pure Python, about 2,300 lines here, and need nothing
StemDeck does not already ship: torch, numpy, librosa and mido (the last from
app/_vendor/transcribe). Declaring them in pyproject.toml would change
uv.lock, which sends every existing desktop install to a full reinstall (the
desktop-update-gate skill), and `piano-transcription-inference` declares
matplotlib, which nothing here uses. Under `app/` they ship with the in-app
update, the Docker image and every package.

## What is here

| Package | Version | Licence | sdist sha256 |
|---|---|---|---|
| piano-transcription-inference | 0.0.6 | MIT | `b6dd00f9b4bcacb6140725f03b4139a5e0f4acd35a69e492a5d1f734ecfbd231` |
| torchlibrosa | 0.1.0 | MIT | `62a8beedf9c9b4141a06234df3f10229f7ba86e67678ccee02489ec4ef044028` |

Source: the `sdist` of each release on PyPI (URL in `manifest.json`), checked
against the sha256 PyPI publishes. From each: the package folder, and in
`<name>-<version>.sdist/` its `PKG-INFO` (and torchlibrosa's `LICENSE.md`).
Their `README.md`, `setup.py`, `setup.cfg` and `.egg-info` are left out.
`manifest.json`'s `id` is the first 16 hex characters of the sha256 of the two
sdist hashes joined in table order; every `midi.json` a piano_hr run writes
records it as `vendor_id`.

The piano_transcription_inference sdist has no licence file. Its PKG-INFO
declares "License :: OSI Approved :: MIT License"; the MIT text in
`piano_transcription_inference-0.0.6.sdist/LICENSE` is the repository's own,
added upstream on 2026-11-12 (commit 566057930a8423aa31b91296e7002c513269446d,
git blob 4c9a472c5b695682e388f15e611cd23823844f36, both in `manifest.json`).

## What was changed

Left out: `piano_transcription_inference/inference.py`, the
`PianoTranscription` class. When its checkpoint is missing its `__init__`
downloads it with `os.system("wget ...")`, it builds its model with
`eval(model_type)`, and it loads the checkpoint with `torch.load` without
`weights_only`. StemDeck downloads the checkpoint itself, in the parent,
pinned by SHA-256, loads it with `weights_only=True` and builds the model
directly (app/pipeline/piano_hr.py, which also holds the class's two
instance-free steps, `enframe` and `deframe`, checked identical to the
sdist's). Its sha256 in the sdist is in `manifest.json` under `removed`.

`patches/piano_transcription_inference-0.0.6.patch` is the whole of what was
changed in the files that are here, and nothing else was. torchlibrosa is
unchanged.

- `models.py`: `import matplotlib.pyplot as plt` removed. `plt` is never used,
  and the worker blocks matplotlib.
- `utilities.py`: `import audioread` and `load_audio()` removed. The loader is
  never called (the worker reads audio with librosa.load, as Basic Pitch
  does), its default argument names `audioread.ffdec` at import time, and its
  resample call passes positional arguments that librosa 0.10 and later
  refuse.
- `__init__.py`: the imports of `PianoTranscription` and `load_audio` that
  went with them; `sample_rate` stays.

`manifest.json` lists every file with its sha256 in the sdist and here (the
two differ only for the three patched files), the left-out file, and the
patch's own sha256. `tests/test_midi_vendor.py` checks every file against
it, fails on anything changed, missing or added, and takes the patch out of
each patched file again to check it gives back the sdist's bytes.
`.gitattributes` marks this folder `-text`, so git never rewrites a line
ending in it.

## The weights

Not here: 172 MB, CC BY 4.0, on Zenodo (record 4034264,
doi:10.5281/zenodo.4034264, by Qiuqiang Kong), fetched on first use into
`<models dir>/piano_hr/` and checked against the size and SHA-256 in
app/core/config.py (PIANO_HR_CHECKPOINT_*) before every use.

## How to update

1. Pick the new versions on PyPI. Check each project's licence for changes,
   and that it is still pure Python.
2. Fetch each sdist's sha256 from `https://pypi.org/pypi/<name>/<version>/json`
   (`urls[]`, the `sdist` entry) and write it into the table above and into
   `EXPECTED_PIANO_HR` in `tests/test_midi_vendor.py`.
3. Download each sdist from the URL in that JSON into an empty folder outside
   the repository and check its sha256 against the published one.
4. Replace this folder's package folders and `.sdist` folders with the new
   ones, leave out `inference.py` again, apply the patch again (rewrite it if
   the files moved), and rewrite `manifest.json` (every file's two hashes,
   `removed`, the patch's hash, the new `id`). Compare the new
   `enframe`/`deframe` with piano_hr.py's.
5. Run `uv run pytest tests/test_midi_vendor.py tests/test_midi_worker.py`,
   then `scripts/midi_bench.py --engine piano_hr score --worker` on the held
   out items, and compare with the scores in app/core/config.py.
