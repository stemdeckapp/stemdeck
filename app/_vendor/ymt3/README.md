# Vendored YourMT3+ (ymt3)

The third MIDI engine (app/pipeline/ymt3.py): YourMT3+, Chang et al. 2024,
"YourMT3+: Multi-instrument Music Transcription with Enhanced Transformer
Architectures and Cross-dataset Stem Augmentation", the variant YPTF.MoE+Multi
trained without pitch-shift augmentation (experiment
`mc13_256_g4_all_v7_mt3f_sqr_rms_moe_wf4_n8k2_silu_rope_rp_b36_nops`). Only the
worker process (app/pipeline/midi_worker.py, with `--engine ymt3`) puts these
folders on `sys.path`; the server process never imports anything in them.

## Why it is vendored

The authors publish the model as a training repository and a Hugging Face
Space, not as a package. Its inference needs torch, torchaudio, einops and
numpy, all in uv.lock already, plus `transformers` and `pytorch_lightning`,
which are not. Adding those two (and their dependencies) would change uv.lock,
which sends every existing desktop install to a full reinstall (the
desktop-update-gate skill), for about 450 lines of T5 layers and one base
class. So the inference code is here, and `shim/` holds stand-ins for exactly
what it imports from those two. No change to pyproject.toml or uv.lock.

## What is here

| Folder | What | Licence |
|---|---|---|
| `src/` | 23 files of `amt/src` from the Space, two of them patched | Apache-2.0 |
| `src/**/__init__.py` | 4 empty files added by StemDeck | (none) |
| `shim/transformers/` | the T5 and Perceiver pieces the code imports | Apache-2.0 |
| `shim/pytorch_lightning/` | `LightningModule` as a plain `nn.Module` | written for StemDeck |
| `patches/yourmt3-space-5e66c1e.patch` | the whole of what changed in `src/` | |
| `LICENSE.*` | the licence texts below | |

Source of `src/`: the Hugging Face Space `mimbres/YourMT3` at revision
`5e66c1ea173a8186e0d20432b841d3180cc015b5` (2025-01-31), files under
`amt/src/`. Every file's git blob id at that revision is in `manifest.json`,
with its SHA-256 there and here. `manifest.json`'s `id` is the first 16 hex
characters of the SHA-256 of every shipped file's path and SHA-256 (one
`path:sha256` line each, sorted by path); every `midi.json` a ymt3 run writes
records it as `vendor_id`.

The files keep the Space's layout, so their own imports (`from model...`,
`from utils...`, `from config...`) work unchanged. Those are generic names, so
StemDeck adds an empty `__init__.py` to each folder (a regular package is
found before any namespace package of the same name), and the worker checks
that every module of those names it imported came from here.

## Licences

- Every file in `src/` that carries a header says "Copyright 2024 The YourMT3
  Authors. Licensed under the Apache License, Version 2.0". The Space's card
  says `license: apache-2.0`; it has no LICENSE file of its own. The GitHub
  repository `mimbres/YourMT3` (commit
  `8a4fbcabdf660f9f06bcb6f12bbf00d9b3139b98`) states GPL-3.0. StemDeck is
  AGPL-3.0 and can take either; both are recorded in THIRD_PARTY_NOTICES, and
  `LICENSE.yourmt3` holds the Apache-2.0 text under a note saying so.
- `src/model/RoPE/RoPE.py` is "based on" lucidrains/rotary-embedding-torch
  and `src/model/positional_encoding.py`'s ALiBi on lucidrains/x-transformers,
  both MIT, Copyright (c) 2021 and 2020 Phil Wang: `LICENSE.rotary-embedding-torch`
  and `LICENSE.x-transformers`, fetched from those repositories at the commits
  `manifest.json` names.
- `shim/transformers/` holds classes copied verbatim from Hugging Face
  transformers 4.45.1 (Apache-2.0, Copyright 2018- The Hugging Face team), each
  file saying which: `T5LayerNorm`, `T5DenseActDense`, `T5DenseGatedActDense`,
  `T5LayerFF`, `T5Attention`, `T5LayerSelfAttention` and `T5LayerCrossAttention`
  (models/t5/modeling_t5.py), `NewGELUActivation` (activations.py), and
  `prune_linear_layer`, `apply_chunking_to_forward` and
  `find_pruneable_heads_and_indices` (pytorch_utils.py). The rest is small
  rewrites. The wheel, its SHA-256 and the RECORD hashes of the three source
  files are in `manifest.json`; the licence is `LICENSE.transformers`, that
  wheel's own.

## What was changed

`patches/yourmt3-space-5e66c1e.patch` is the whole of what changed in the
files that are here, and nothing else was:

- `src/model/ymt3.py`: the training, validation and test steps, the optimizer
  and scheduler, the teacher-forced `forward` with its loss, `setup` (metrics
  and the pitch-shift augmentation layer), `inference_file` (StemDeck loops
  over the batches itself, to report progress) and the module's test cases are
  removed, with their imports (wandb, torchaudio for debugging, the metrics,
  the optimizers, the conformer encoder this variant does not use, the
  pitch-shift layer). What stays is the model's construction and `inference`.
  The one branch of the construction that fetched a T5 configuration from the
  Hugging Face hub (`T5Config.from_pretrained`, for a T5 name `config.py`
  does not hold) now refuses that name instead: this variant's is held, and
  the worker has no network anyway.
- `src/utils/utils.py`: everything but `create_inverse_vocab` and
  `create_program2channel_vocab` is removed, with its imports: among them a
  dataset downloader using `requests` and `tarfile`, MIDI and NumPy writers,
  and the metric helpers.

Left out of the Space's `amt/src` entirely: training and evaluation
(`train.py`, `test.py`, `install_dataset.py`, `model/init_train.py`,
`model/optimizers.py`, `model/lr_scheduler.py`, `utils/metrics*.py`,
`utils/datasets*.py`, `utils/data_modules.py`, `utils/augment.py`,
`utils/preprocess/`, `utils/mirdata_dev/`, `config/data_presets.py`), the
pitch-shift layer and the conformer encoder (`model/pitchshift_layer.py`,
`model/conformer_*.py`), and `utils/audio.py` and `utils/midi.py`: StemDeck
reads the audio, cuts the segments (`slice_padded_array`'s arithmetic, in
app/pipeline/ymt3.py) and writes the MIDI itself. The variant's arguments,
which `model/init_train.py` turned into its configuration, are applied by
`ymt3.build_config()`.

`tests/test_midi_vendor.py` checks every file against `manifest.json`, fails on
anything changed, missing or added, checks each unpatched file's git blob id
against the Space's, and takes the patch out of each patched file again to
check it gives back the Space's bytes. `.gitattributes` marks this folder
`-text`, so git never rewrites a line ending in it.

## The weights

Not here. The checkpoint the authors published (`last.ckpt`, 561.5 MB, with
the optimizer's state; Apache-2.0 per the model card) is downloaded on the
first ymt3 run from huggingface.co/mimbres/YourMT3 at the pinned revision
`e45ebd70398682d54b7bb1901a5216e18f3b1824`, checked against its size and
SHA-256 (app/core/config.py, YMT3_CHECKPOINT_*), then converted on this
computer into a weights-only safetensors file of 188.4 MB
(app/pipeline/ymt3_convert.py, scripts/ymt3_weights.py), checked against its
own pinned SHA-256 (YMT3_WEIGHTS_*), and the checkpoint is deleted. The
checkpoint is read with torch's weights-only loader; its few pickled objects
(the training task's tokenizer) are given an inert stand-in, so nothing in it
runs. The worker reads only the safetensors file, without pickle.

## How to update

1. Pick the new Space revision (`https://huggingface.co/api/spaces/mimbres/YourMT3`,
   `sha`). Check the licence on the card and in the headers.
2. Download `amt/src` at that revision into an empty folder outside the
   repository, with its file list and blob ids
   (`.../api/spaces/mimbres/YourMT3/tree/<revision>?recursive=true`).
3. Copy the files listed in `manifest.json` (and any new one the inference now
   imports) into `src/`, apply the patch again (rewrite it if the files moved),
   and rewrite `manifest.json` (every file's blob id and both hashes, the
   patch's hash, the shim's hashes, the new `id`) and this table.
4. If the code imports more of transformers, copy it from the same transformers
   release into `shim/` and record it.
5. Run `uv run pytest tests/test_midi_vendor.py tests/test_midi_worker.py`, then
   `scripts/midi_bench.py --engine ymt3 eng` on the held-out sets, and compare
   with data/midi-bench/results/engines-per-stem.md.
