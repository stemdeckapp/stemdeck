# Model licensing notes

Records the license basis for ML checkpoints StemDeck downloads at runtime.
Not exhaustive -- only entries where the decision wasn't a simple upstream
license file are documented here.

## Demucs (htdemucs_6s)

MIT, published by the `demucs` PyPI package (Meta/Facebook Research). No
audit needed -- an unambiguous upstream license.

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

A cleanly-licensed roformer alternative (Kimberley Jensen's
`Mel-Band-Roformer-Vocal-Model`) was also checked and found to have no
LICENSE file despite a claim to the contrary surfacing in a web search.

Since StemDeck is free/non-commercial, the practical risk of using an
unlicensed-but-freely-shared community checkpoint is low -- but a
Boosty-paywall origin is a step past "unlicensed," suggesting the author did
not intend it for free redistribution at all, and its already-dead HF mirror
makes it an unreliable thing to depend on regardless of the licensing
question. `STEMDECK_KARAOKE_MODEL` remains available as an env override for a
deployment that wants to accept that risk itself.

## BS-Roformer Chorus Male-Female (on-demand duet split)

- **File**: `model_chorus_bs_roformer_ep_267_sdr_24.1275.ckpt` plus its
  `config_chorus_male_female_bs_roformer.yaml`.
- **Distributed via**: `audio-separator`, the same MIT + credit-to-UVR channel
  as the karaoke model above. Community-trained (credited to Sucial); no
  separate LICENSE file of its own was found, which puts it in the same
  "unlicensed but freely and openly distributed" bucket the karaoke default
  sits in -- not the Boosty-paywall bucket that was rejected.
- **Credit**: Ultimate Vocal Remover (Anjok07) -- https://github.com/Anjok07/ultimatevocalremovergui

`DUET_SPLIT_MODEL` in `app/core/config.py`, overridable via
`STEMDECK_DUET_MODEL`.

### What it actually separates, measured

The model keys on **vocal weight/register**, not on singer identity. Measured
on the job's own vocals stem:

| material | result |
|---|---|
| "Shallow" (Cooper/Gaga, different registers) | clean split: 30-40 dB rejection of the idle stem through each singer's verse, per-frame energy share std 0.41 (real alternation), sum-to-mix residual 0.028 |
| "What Is This Feeling" (Wicked, two sopranos) | no split: leads with one stem in 76% of frames and the other in 7%, which is one voice with gaps. An earlier run of the same material recorded 93/7 by energy; a fresh run gave 24/76, so the totals move and who leads is the stable signal |

So this is a duet splitter for singers in different registers, and does
nothing for same-register duets. The stem names (`voice_1` / `voice_2`)
say that rather than promising per-singer separation.

### Same-register duets (two sopranos): what was tried

Splitting two singers of the *same* register -- the "Glinda and Elphaba" case --
is the thing the register model above cannot do. What follows is the record of
what failed, kept so the dead ends are not re-attempted, and then the model that
works, which is MedleyVox Conv-TasNet in the next section.

**What does not work.** Measured against an ideal-ratio-mask ceiling of
**+13.4 dB SI-SDRi**, so there is real headroom.

| approach | result |
|---|---|
| SepFormer (speech PIT separation) | two anti-correlated outputs whose sum does not reconstruct the input; out of domain on singing |
| supervised NMF (bases per singer from their own solo passages) | -2.3 dB on held-out audio: with enough components the two dictionaries explain each other |
| harmonic-comb masking from estimated multi-F0 | -2.7 dB. With *oracle* F0s it reaches **+8.2 dB**, so the idea is sound and estimation is what fails: multi-F0 recall 0.44-0.54, contour-to-singer assignment 0.57 (chance 0.5) |
| unsupervised 2-way diarization of the vocals stem | fails on any song with a chorus: the dominant split is leads-vs-ensemble, not lead-vs-lead |
| ECAPA enrolment (embed each singer from a solo passage, route windows) | routes lines well on alternating material but does not separate simultaneous singing, and did not improve a separated result: speaker distance 0.977 to 0.973. Removed. |
| zero-shot diffusion prior (`iamycy/duet-svs-diffusion`, MIT) | **-0.91 dB SI-SDRi**, worse than not splitting, at 60x slower than realtime. The paper's 11.75 dB uses oracle retry and ground-truth conditioning |

**A correction.** An earlier version of this file recorded MedleyVox as tested
and failing -- "no split on any real duet; also splits a *solo* voice across
both outputs". That was a mistake in how it was run, twice over. iSRNet is a
*refinement* stage cascaded after a Conv-TasNet separator, not a separator, so
running it alone does nothing. And the checkpoint used was from the
`singing_librispeech` training, which separates singing from *speech*. The
`multi_singing_librispeech` checkpoint is the one for duets and had not been
downloaded.

## MedleyVox Conv-TasNet (same-register duet split)

`app/pipeline/duet_medleyvox.py`, with the model code vendored into
`app/pipeline/_convtasnet.py`.

**What it does.** Separates two singers who share a vocal range, which the
register model above cannot do at all. Measured on ground-truth duets built from
real production audio, against a +13.3 dB ideal-mask ceiling on the same clips:

| | result |
|---|---|
| other singer suppressed by | **14.5 dB** (no split is 0; the ideal mask reaches 24.5) |
| SI-SDRi | **+7.3 dB**, about 55% of the masking ceiling |
| speed | 5-7x realtime on CPU, faster on GPU |
| output | full input rate and channel count, stems sum back to the recording |

Inference details that are worth their cost, each measured: two checkpoints of
the same run ensembled (+0.1 to +0.2 dB), each also run on the reversed signal
(+0.2 dB), single-pass rather than overlap-add (+0.6 dB), and masks applied to
the original-rate spectrogram rather than resampling the output (keeps
everything above 12 kHz, which the model itself cannot see).

Things tried that did not help: shift augmentation (-0.1 dB), multi-resolution
mask averaging (+0.04 dB), and fine-tuning on the song's own solo passages,
which overfits within 10 steps.

**Training on VocalSet made it worse.** Fine-tuning on same-register pairs from
VocalSet (CC BY 4.0) with synthetic reverb raised the held-out *produced* score
from +6.3 to +10.0 dB while the real-world test set fell from +6.9 to +4.9. The
model learned the synthetic reverb rather than production. Real impulse
responses are the open lead here.

**Choosing between the two models cannot be automated.** Five blind measures
were tried and every one scored the register model's failure as healthy; the
detail is in `duet_medleyvox.register_split_collapsed`. The short version is
that a wrong separation is confident and uncorrelated, exactly like a right one:
on the failed split, stem correlation scored 0.09 against the correct output's
0.17-0.29, and mask decisiveness scored 0.96, higher than anything else
measured. Only the unambiguous case -- one stem essentially silent -- is
detected automatically. `STEMDECK_DUET_SAME_REGISTER=1` forces this model for
anyone who has listened and knows which they want.

**Limits.** The checkpoints have two outputs, so an ensemble is beyond them:
stems from two-singer passages sit at 0.32 speaker distance, and across a full
musical number with a chorus they smear to 0.97. There is no reliable way to
detect that from the audio in advance.

### It does not work on real duets, and the eval that said otherwise was flawed

Tested on "Shallow" through the running app, then checked by ear: both singers
audible in the same stem. That report was right and the measurements agreeing
with it were not.

Speaker identity, which is the only measure that has ever caught a wrong split
here:

| | cross-stem cosine |
|---|---|
| clustering the **unsplit** vocals, confident windows | **0.08** |
| clustering the unsplit vocals, all windows | 0.567 |
| register model's stems | **0.917** |
| same-register model's stems | 0.958 |

**Naively clustering the raw vocals separates the two singers better than either
model does.** Split by section it is no kinder: 0.952 through the solo verses,
0.883 through the overlaps. It fails everywhere, not just where they sing at
once.

**Why the +7.3 dB and 14.5 dB figures did not predict this.** Those ground-truth
mixtures were built by summing two *unrelated* passages -- one singer from one
part of the song, the other from somewhere else. That is a much easier problem
than a duet. Real duet voices are musically correlated: harmonised pitch,
synchronised consonants, the same vowels at the same time, and a shared reverb
and compression bus. The model separates two arbitrary voices that happen to
overlap and does not separate two people singing together.

So the SI-SDR numbers above are real but measure the wrong task. An honest eval
needs genuine multitrack duets, where each singer's recorded track is available
separately, rather than constructed sums.

**Do not ship this as a duet splitter.** The register model's 30-40 dB figure
recorded earlier came from rejection measured through single-singer verses,
which is not the same claim as separating the two people.

### Routing beats separating, measured

Prompted by the obvious question after the failure above: if the singers can be
told apart well enough to *decide* who is singing, why separate at all? Cut the
vocal into windows, decide whose it is, and route it. Where both sing, say so.

On "Shallow", cross-stem speaker cosine, lower being further apart:

| approach | coverage | cosine |
|---|---|---|
| same-register model | 100% | 0.958 |
| register model | 100% | 0.917 |
| **routing, overlap kept in both stems** | 100% | **0.756** |
| routing, overlap silenced | 73% | 0.626 |
| routing, conservative | 51% | 0.467 |
| routing, very conservative | 33% | **0.351** |

**Every routing setting beats both separators**, and purity is a dial: the more
of the song you are willing to leave out, the cleaner what remains. It is also
far cheaper -- an ECAPA pass over the vocal, no 460 MB checkpoint, seconds
rather than nine minutes on CPU.

The reason is not subtle in hindsight. A separator has to *reconstruct* two
signals and destroys identity doing it. Routing never touches the audio; it only
decides where each second goes, so whatever identity was in the mixture survives
intact.

**What it still does not do**, and cannot: two people singing at once are in the
same samples and no routing extracts them. Routing's advantage is that it can be
honest about that, which the separators are not -- they return two confident
stems and nothing signals that both contain everyone.

**One caveat on these numbers.** At high confidence the two clusters come out
lopsided, 2% against 31%, which suggests the clustering finds one distinctive
voice and a remainder rather than a balanced two-singer partition. The 0.08
"floor" quoted earlier has the same asymmetry, 92 windows against 18, so it
flatters the ceiling. Treat the ordering above as solid and the absolute values
as optimistic.

### Licensing

- **MedleyVox weights** (`Cyru5/MedleyVox`) are **CC-BY 4.0**, trained by Carson
  Evans. Fetched at first use rather than bundled, the same pattern as Demucs.
- **The MedleyVox inference code** (`jeonchangbin49/MedleyVox`) carries no
  LICENSE and is **not used**. The model is four classes from `asteroid` (MIT)
  plus one published formula, vendored into `app/pipeline/_convtasnet.py` with
  attribution. Depending on `asteroid` directly would cost 23 transitive
  packages including pytorch-lightning and pandas.
- **`asteroid-filterbanks`** (MIT) is a real dependency and pulls in nothing of
  its own.
- **SepFormer** and the ECAPA speaker embedder (`speechbrain/*`) are Apache-2.0;
  neither is used any more.
- **DAMP-VSEP**, behind the singer-conditioned models in the literature, is
  research-only and reserves derivatives to Smule. Those checkpoints are
  unusable here regardless of quality.
