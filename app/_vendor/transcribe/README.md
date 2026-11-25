# Vendored note engine

What makes MIDI from the piano and bass stems (app/pipeline/midi.py). Only the
worker process (app/pipeline/midi_worker.py) puts this folder on `sys.path`;
the server process never imports anything in it.

## Why it is vendored

`pip install basic-pitch` pulls TensorFlow (several hundred MB) on Windows and
Linux. Basic Pitch runs its ONNX model without it when onnxruntime is
installed, and onnxruntime, librosa, numpy, scipy, numba, soundfile, resampy,
decorator, six and packaging are already in uv.lock. Declaring the five
packages below in pyproject.toml would change uv.lock, which sends every
existing desktop install to a full reinstall (the desktop-update-gate skill).
Here, under `app/`, they ship with the in-app update, the Docker image and
every package with no packaging change.

## What is here

Each wheel unpacked as PyPI ships it: the package folder and its `.dist-info`
(METADATA, RECORD, the licence files). Nothing in them is edited. Behaviour is
changed only in the worker: it blocks TensorFlow, CoreML and TFLite from
importing, passes the ONNX model by path, keeps stdout for its own protocol
and refuses network access.

| Package | Version | Licence | Wheel sha256 |
|---|---|---|---|
| basic-pitch | 0.4.0 | Apache-2.0 | `738adb503aae7fdfc7d1e1511aa0ce35052315f260a19531ef4c356708425db0` |
| pretty-midi | 0.2.11.post0 | MIT | `a3e2321736245a9c92259716d85a4762f16b7bde2f591fb0dba95bdb6e793d8a` |
| mir-eval | 0.8.2 | MIT | `114cda33d8e17408c170598e0b36ed0d71ff4a2fee8eaf9e165b58ecf1c87170` |
| mido | 1.3.3 | MIT | `01033c9b10b049e4436fca2762194ca839b09a4334091dd3c34e7f4ae674fd8a` |
| importlib-resources | 7.1.0 | Apache-2.0 | `1bd7b48b4088eddb2cd16382150bb515af0bd2c70128194392725f82ad2c96a1` |

Source: the `bdist_wheel` file of each release on PyPI, URL in
`manifest.json`. Basic Pitch 0.4.0 was released 2024-08-16; the head of its
main branch (commit fa5997af0a8210982619003269994a1be25eddf3) gave the same
notes in the 2026-11-25 spike. `manifest.json`'s `id` is the first 16 hex
characters of the sha256 of the five wheel hashes joined in table order. Every
`midi.json` records it as `vendor_id`.

Left out, and listed per package in `manifest.json` under `removed`:

- `basic_pitch/saved_models/icassp_2022/nmp/`, `nmp.mlpackage/`, `nmp.tflite`:
  the TensorFlow, CoreML and TFLite exports of the model. Only `nmp.onnx`
  (230 KB) is ever loaded, so no runtime can pick another.
- `pretty_midi/TimGM6mb.sf2`: a 6 MB General MIDI soundfont, used only to
  synthesise audio, with no licence stated in the package.
- `importlib_resources/tests/`: the package's own test suite.

`.gitattributes` marks this folder `-text`, so git never rewrites a line
ending in it. `tests/test_midi_vendor.py` checks every file against its
wheel's RECORD hash and fails on anything changed, missing or added.

## Licences

Apache-2.0 for basic-pitch and importlib-resources, MIT for pretty-midi,
mir-eval and mido. The texts are in each `.dist-info` folder (`LICENSE`,
`LICENSE.txt` or `licenses/`), and Basic Pitch's `NOTICE` is in
`basic_pitch-0.4.0.dist-info/NOTICE`. The packaged third-party notices
(packaging/*/THIRD_PARTY_NOTICES.txt) name them.

The model's weights are Basic Pitch's own, under its Apache-2.0 licence. Some
of the datasets it was trained on are published under non-commercial terms,
going by the dataset loaders in `basic_pitch/data/datasets/`; whether that
reaches the weights is not settled. docs/models.md says what is and is not
known.

## How to update

1. Pick the new versions on PyPI. Check each project's licence and NOTICE for
   changes, and that the wheel is still pure Python (`py3-none-any`).
2. Fetch each wheel's sha256 from `https://pypi.org/pypi/<name>/<version>/json`
   (`urls[]`, the `bdist_wheel` entry) and write it into the table above and
   into `EXPECTED` in `tests/test_midi_vendor.py`.
3. Download each wheel from the URL in that JSON into an empty folder outside
   the repository and check its sha256 against the published one.
4. Delete this folder's package and `.dist-info` folders and unpack each wheel
   here: the package folder and its `.dist-info` only, minus the files listed
   above. Check each unpacked file against the wheel's RECORD.
5. Rewrite `manifest.json` (name, version, wheel, url, sha256, size, licence,
   packages, dist_info, removed, files, and the new `id`).
6. Run `uv run pytest tests/test_midi_vendor.py tests/test_midi_worker.py`,
   then a real song through POST /api/jobs/{id}/midi, and compare the notes.
