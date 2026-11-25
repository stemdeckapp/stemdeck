# Model licensing notes

Records the license basis for ML checkpoints StemDeck downloads at runtime.
Not exhaustive -- only entries where the decision wasn't a simple upstream
license file are documented here.

## Demucs (htdemucs_6s)

MIT, published by the `demucs` PyPI package (Meta/Facebook Research). No
audit needed -- an unambiguous upstream license.

This is the base model: it supplies all six stems, and every song made before
Settings > Separation existed was separated with it.

## Demucs fine-tuned (htdemucs_ft)

- **Supplies**: drums and bass (it also makes vocals and other, which
  StemDeck does not take from it).
- **License**: MIT, the same `demucs` package and upstream as `htdemucs_6s`.
- **Upstream**: https://github.com/facebookresearch/demucs
- **Download**: four checkpoints of 84,141,271 bytes each (about 336 MB in
  all), fetched through `torch.hub` the first time a song uses it. Not bundled.
  Each is pinned by its full SHA-256 (`app/core/stem_models.py`), as is
  `htdemucs_6s`'s: the download is kept only if it matches, and a cached copy
  that does not match is removed and fetched again before the model loads.

Optional, and never what "Auto" picks. Measured on the maintainer's machines
it takes about 4 times the CPU time of `htdemucs_6s` (a real-time factor of
1.84 against 0.50) for about 0.6 dB better drums and bass on a vendor test
set. StemDeck has not reproduced that figure. Whether the gain is worth the
time is left to you.

## Mel-Band RoFormer vocals (kim_mel_roformer)

- **Supplies**: vocals only.
- **Checkpoint**: `MelBandRoformer.ckpt`, Kimberley Jensen's Mel-Band RoFormer
  vocals model.
- **Source**: https://huggingface.co/KimberleyJSN/melbandroformer
- **License**: MIT, as the model card declares.
- **Runtime**: audio-separator's `MelBandRoformer` class (MIT,
  `nomadkaraoke/python-audio-separator`), the same package that runs Karaoke 2.
  StemDeck reads the checkpoint itself and runs it in its own separation worker.
- **Download**: 913,106,900 bytes (913 MB), fetched by StemDeck the first time
  a song uses it, into `<models>/roformer/MelBandRoformer.ckpt`. Not bundled in
  any installer.
- **Pinned**: Hugging Face revision `ac9b0614ab3cd7f77219e18ba494dfd93956c348`,
  SHA-256 `87201f4d31afb5bc79993230fc49446918425574db48c01c405e44f365c7559e`.
  The download URL names the revision, not `main`, and the download is kept
  only if its size and full SHA-256 match. A file of the wrong size is
  downloaded again. A file that does not load is checked against the SHA-256:
  when it does not match it is removed and downloaded again, once; when it
  matches it is kept, and the error says the file is intact.
- **Hardware**: a GPU is recommended. On an NVIDIA card, or an AMD card with a
  ROCm build of torch (both report as `cuda`), "Auto" picks it for vocals when
  the card has 4096 MB or more of memory, or when the memory is unknown. On a
  CPU it is possible but slow (a real-time factor of 3.4 against 0.50 for
  `htdemucs_6s`), and on Apple's MPS it is not recommended. On ROCm StemDeck
  makes one tensor contiguous before attention, which that build needs.

Why the revision and the hash are pinned: the model card's license field has
changed twice. A pinned revision means the file StemDeck checks is the file
whose card StemDeck read, and a later change to the card or the file cannot
reach an install silently. If you want to review the licence yourself, read
the card at the revision above.

<!-- credit pending: the maintainer decides how to credit the community implementation of the Kim engine -->

## Choosing a model per stem

Settings > Separation lists one row per stem. Vocals, drums, bass, guitar and
piano each take a model; "Auto" is the default and a missing setting means
"Auto".

| Stem | Choices | "Auto" picks |
|---|---|---|
| Vocals | `htdemucs_6s`, `kim_mel_roformer` | `kim_mel_roformer` on a GPU that can run it, else `htdemucs_6s` |
| Drums | `htdemucs_6s`, `htdemucs_ft` | `htdemucs_6s` |
| Bass | `htdemucs_6s`, `htdemucs_ft` | `htdemucs_6s` |
| Guitar | `htdemucs_6s` | `htdemucs_6s` |
| Piano | `htdemucs_6s` | `htdemucs_6s` |

A model you added is listed too, after these, for each stem it declares
("Auto" never picks it; see [My models](#my-models-bring-your-own)).

Two stems are never chosen. Other is what is left of the song after the five
chosen stems are taken out, so the stems always add up to the song. Lead and
backing stay the Karaoke 2 split of the finished vocals, so a better vocal stem
feeds it with no change.

The recommendation follows the device a song will separate on:

- **NVIDIA or ROCm GPU with enough memory**: Kim for vocals.
- **GPU under 4096 MB**: `htdemucs_6s`, and Settings says the memory is the reason.
- **CPU or Apple MPS**: `htdemucs_6s`.
- **Kim not installable here** (for example Intel macOS, where audio-separator
  is not installed): `htdemucs_6s`.

The 4096 MB floor is a margin over the 2017 MB Kim used in a measurement. It
is not a measured failure point. A card that is too small fails loudly and the
song retries on the CPU. On that retry the stems left on "Auto" use what "Auto"
picks on a CPU (`htdemucs_6s`); a model chosen in Settings is kept.

Each song records the models it was made with, and Fill stems reuses them.
Songs separated before this version fill missing stems with the new scaling;
levels can differ slightly. A
choice made in Settings applies to the next song, not to one already running.
Best quality (Settings > Separation) widens the shift averaging of the Demucs
passes only; the Kim pass is unchanged.

### One stem of a finished song

In Advanced mode each of these five lanes has a **Model** button (a
microchip) in its header, in a column of its own between the Key controls and
the M, S, EQ and download buttons. Its name says the model the song's stem was
made with, and a dot on it shows when that is not the model Settings uses for
new songs. It opens a row under the lane, on its own or after the lane's EQ
and effects rows, with the same list as Settings, starting at the song's own
model. **Re-extract** separates that one stem again from the original track with
the chosen model (progress and **Cancel** while it runs); Other is rebuilt so
the stems still add up to the song, and the song reloads with every lane's
volume, mute, solo and EQ kept. **Undo** puts back the stem from before the
last re-extract; **Keep** (pressed twice) discards that earlier version for
good. A Vocals re-extract sets the Lead + Backing pair aside, since it was made
from the old vocals: split again for a new pair, or Undo for the old one. A song whose original is not kept and has no link to fetch it
again cannot be re-extracted, and the row says so. The routes are in
[docs/api.md](api.md).

### Benchmark

The static rule above is a guess about your machine. Settings > Separation has
a **Run benchmark** button that measures it instead. It takes about a minute,
runs only while nothing is separating, uses a short signal StemDeck generates
(none of your audio, and nothing is downloaded) and can be cancelled. It times
`htdemucs_6s` on each device this machine has, and the write speed of the
folder your songs are saved in.

Models that are not downloaded yet are not timed. Their speed is estimated from
the `htdemucs_6s` result and marked as an estimate. An already downloaded
`htdemucs_ft` is timed for real when that takes under about 40 seconds (on a
GPU, not on a CPU); Kim is always estimated. Every speed is for a 4-minute
song: Demucs works in 7.8 s segments, so a 12 s clip is mostly padding, and
its timing is scaled by the segment count. The benchmark then proposes a device and a model per
stem that fits about 3 minutes for a 4-minute song, and nothing changes until
you press **Apply recommendation**. The result is stored in `settings.json`
and is used for "Auto" only while it matches this machine's CPU name and core count, GPU name
and memory, torch version and device list; after a hardware change it is marked stale and
the page asks you to run it again.

## My models (bring your own)

Settings > Separation > **Advanced: My models** adds a separation model you
found yourself. StemDeck ships none of these, downloads none on its own, and
checks no licence. The notice there says it plainly: the models come from third
parties, and the model you load, its licence and your right to use it are your
own responsibility. The licence a Hugging Face card declares is shown as the
card states it; StemDeck does not verify it.

**What can be added.** Mel-Band RoFormer and BS-RoFormer models only, for
44.1 kHz stereo. Custom Demucs models are not allowed: a Demucs `.th` file is a
full Python pickle, and loading one runs whatever code it holds.

**Two ways to add one.**

- *Option 1, a Hugging Face link* (`https://huggingface.co/<owner>/<repo>`, or
  a `tree`, `blob` or `resolve` address in it). StemDeck downloads the model
  file and its config file, pinned to the repository's commit at that moment.
  It needs Online search and link import (Settings > Experimental) to be on,
  because it makes network calls. A model that needs a Hugging Face account
  cannot be fetched: download it yourself and use Option 2.
- *Option 2, files from your computer*: the model file (`.safetensors`,
  `.ckpt`, `.pth` or `.pt`) **and** its config file (`.yaml`). The config is
  required: it says how the model is built, and the weights alone cannot.

You tick the stems the model separates, and the box that says you have read its
licence and may use it. **Add model** stays off, with a line saying what is
still needed, until both are done.

**What is checked before it can be used.**

- Size: at most 2 GiB for the model file and 256 KiB for the config; at most 12
  models; at most 500 million parameters (a 2 GiB fp16 file could hold about a
  billion).
- Format, from the file's own bytes, not its name: safetensors (preferred), or
  a PyTorch checkpoint loaded with `weights_only=True`, whose keys must match
  the model exactly. Anything else, a legacy pickle included, is refused
  unopened.
- The config is read as plain YAML (one inert tag, `!!python/tuple`, is read as
  a list; no other tag is ever built), then every key is checked against a
  whitelist with ranges. An unknown key under `model:` is refused, never ignored.
- A declared stem must be one of the config's `training.instruments`.
- Linear attention (`linear_transformer_depth` above 0) is refused: the
  RoFormer code StemDeck uses cannot build it. A checkpoint trained without the
  attention gates that code always has is refused too (`gates_missing`), rather
  than loaded with parts missing.
- A test run: the model is built and run once on a 3 second generated clip, on
  the CPU, in a separate process with a time limit. The output must have the
  right shape and only finite numbers.

The test run proves that the model loads and runs. It cannot judge how well it
separates, and it cannot tell whether the config's STFT settings are the ones
the model was trained with: those leave no trace in the weights.

**How it is used.** A model you added shows in the stem lists as "{name}
(yours)", only for the stems you declared, and is never recommended or chosen
by "Auto". It runs as its own pass on the whole song, like Kim, and only the
stems it was chosen for are kept; Other is still what is left, so the stems
still add up to the song.

**When it fails at run time.** The song is separated again with `htdemucs_6s`
for that model's stems, on the same device, and the song records it
(`stem_model_fallbacks`). Fill stems never does this: a stem from another model
would not add up with the stems already made, so the fill fails instead, and a
song whose model was removed cannot be filled.

**Disk.** Adding from files briefly needs about twice the file's size (the
upload is received, then copied into place). The models live under the models
folder (see [Configuration](configuration.md#my-models)). On Windows the test
run has no memory cap of its own; the parameter limit is the guard there.

**Removing** a model deletes its files and sets every stem that used it back to
"Auto".

## All-In-One (automatic song sections)

- **Runtime**: `all-in-one-infer` 3.x, the cross-platform inference fork of
  the All-In-One music-structure model.
- **Checkpoint**: `harmonix-all`, downloaded from the upstream Hugging Face
  repository during desktop warmup or on first use elsewhere.
- **License**: MIT for both the original All-In-One project and the
  `all-in-one-infer` runtime.
- **Upstream**: https://github.com/mir-aidj/all-in-one and
  https://github.com/openmirlab/all-in-one-infer

StemDeck runs this model on CPU after separation and passes its existing stems.
The checkpoint is not bundled in StemDeck installers.

## Whisper (lyrics transcription)

- **Runtime**: `openai-whisper` (PyPI, MIT, `openai/whisper`), on the torch
  StemDeck already ships for Demucs.
- **Checkpoints**: `large-v3-turbo` (1.6 GB) on a GPU with room for it,
  `small` (0.5 GB) on a CPU or a smaller GPU. Overridable with
  `STEMDECK_WHISPER_MODEL_GPU` / `STEMDECK_WHISPER_MODEL_CPU`.
- **License**: MIT for both the code and the published weights.
- **Upstream**: https://github.com/openai/whisper

Runs only when no lyrics were found for a job and the `transcribe_lyrics`
setting allows it (by default on a GPU only). Weights download on first use
into `<TORCH_HOME>/whisper`, or during desktop setup when transcription would
run, and are verified by Whisper's own SHA-256 check. Not bundled in StemDeck
installers.

## MIDI from a stem

- **Engine**: Spotify's Basic Pitch 0.4.0 (PyPI `basic-pitch`, released
  2024-08-16, `spotify/basic-pitch`), its ICASSP 2022 model as ONNX
  (`nmp.onnx`, 230 KB), run by the onnxruntime StemDeck already ships for the
  Lead + Backing split. CPU only.
- **Shipped in**: `app/_vendor/transcribe/`, with pretty-midi 0.2.11.post0,
  mir-eval 0.8.2, mido 1.3.3 and importlib-resources 7.1.0, each unpacked
  unchanged from its PyPI wheel and checked against the wheel's sha256 and
  RECORD. Vendored rather than declared in `pyproject.toml`: the PyPI
  package pulls TensorFlow, and a new dependency would change `uv.lock` and
  stop the desktop's in-app update. The folder's `README.md` has the versions,
  hashes and how to update.
- **License**: Apache-2.0 for Basic Pitch's code and its published model
  weights (its `LICENSE` and `NOTICE` ship beside it); MIT for pretty-midi,
  mir-eval and mido; Apache-2.0 for importlib-resources.
- **Upstream**: https://github.com/spotify/basic-pitch

**Training data: not settled.** The weights are published under Apache-2.0,
but Basic Pitch's own dataset loaders (`basic_pitch/data/datasets/`) name the
sets it was trained on: GuitarSet, iKala, MAESTRO, MedleyDB-Pitch and Slakh.
Some of those are distributed for non-commercial use only (MAESTRO and
MedleyDB under CC BY-NC-SA, iKala for non-commercial research, going by their
publishers; not verified for this note). Whether a non-commercial dataset
licence reaches a model trained on it is a legal question with no settled
answer, and Spotify publishes the weights without such a restriction.
StemDeck is free and never sold, which keeps this out of the commercial case
those terms are about; anyone redistributing StemDeck commercially should
check it for themselves.

What it does: the notes of a finished song's piano or bass stem, on request
(the stem's menu in the studio, `POST /api/jobs/{id}/midi`), as a standard
MIDI file. Each run is a fresh worker process with no network access, with the
pitch range set per stem (piano 27.5 to 4200 Hz, bass 30 to 400 Hz) and one of
three detail presets (`transcribe_detail`). Measured on the CPU of the
development machine: a 6 minute song took 6 to 12 s per stem and about 0.5 GB
of memory, a 30 s excerpt about 6 s, most of it starting the process. Not
available on Intel macOS, whose runtime has no onnxruntime.

Basic Pitch is one model for every instrument. It hears overtones as quiet
extra notes and a held note can come out as several; the file is a starting
point to edit, not a score.

## UVR-MDX-NET Karaoke 2 (on-demand lead/backing vocal split, #275)

- **File**: `UVR_MDXNET_KARA_2.onnx`
- **Distributed via**: `audio-separator` (PyPI, MIT,
  `nomadkaraoke/python-audio-separator`), which bundles/downloads models
  trained as part of the Ultimate Vocal Remover (UVR) project by Anjok07.
- **License**: MIT + attribution, per the `audio-separator` README:
  > "If you choose to integrate this project into some other project using
  > the default model or any other model trained as part of the UVR project,
  > please honor the MIT license by providing credit to UVR and its
  > developers."
- **Credit**: Ultimate Vocal Remover (Anjok07) -- https://github.com/Anjok07/ultimatevocalremovergui

This is the shipped default (`VOCAL_SPLIT_MODEL` in `app/core/config.py`,
overridable via `STEMDECK_KARAOKE_MODEL`).

### Rejected alternative: mel_band_roformer_karaoke (aufr33/viperx)

`audio-separator`'s community-trained roformer checkpoint
(`mel_band_roformer_karaoke_aufr33_viperx_sdr_10.1956.ckpt`) has meaningfully
better reported SDR than the MDX-Net Karaoke 2 model above, and was the
originally preferred choice while scoping this feature. It was rejected after
directly verifying:

- No LICENSE file was ever published for this checkpoint, nor a stated
  license anywhere in its distribution.
- It was originally released through UVR's Boosty supporter-paywall page, not
  as a public open release.
- The public Hugging Face mirror (`jarredou/aufr33-viperx-karaoke-melroformer-model`)
  now returns 401 (gated/removed) -- confirmed directly, not secondhand.

Kimberley Jensen's Mel-Band RoFormer vocals model was checked at the same
time and was found to have no LICENSE file in its repository. Its Hugging Face
model card does declare MIT, so it is now offered for vocals; see
"Mel-Band RoFormer vocals (kim_mel_roformer)" above for the pinned revision and
hash that make that declaration safe to rely on.

Since StemDeck is free/non-commercial, the practical risk of using an
unlicensed-but-freely-shared community checkpoint is low -- but a
Boosty-paywall origin is a step past "unlicensed," suggesting the author did
not intend it for free redistribution at all, and its already-dead HF mirror
makes it an unreliable thing to depend on regardless of the licensing
question. `STEMDECK_KARAOKE_MODEL` remains available as an env override for a
deployment that wants to accept that risk itself.
