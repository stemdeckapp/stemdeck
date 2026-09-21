# Training a singer-conditioned duet separator

A plan, not a record. Everything here is untried; the measured results it builds
on are in `models.md`, and the eval harness it depends on already exists.

The goal is the thing the shipped feature does not do: tell you *which* singer is
in which stem, reliably, for a whole song.

## Why conditioning, rather than a better separator

The shipped model separates blindly and leaves identity to be worked out
afterwards. That second step is where it fails, measured three ways:

- output order is arbitrary per chunk -- **11 of 25 chunks** had the singers
  swapped on a real song
- re-assigning them afterwards with ECAPA moved speaker distance from **0.977 to
  0.973**, which is nothing
- because separated audio is degraded enough that a speaker embedder cannot read
  it reliably, which is the same reason the first two are true

A conditioned model has no identification step. Give it a recording of a singer,
get that singer back. Stem 1 is whoever was enrolled, for the entire song, by
construction. There is no permutation to fix and nothing to infer.

[IWAENC 2026](https://arxiv.org/abs/2608.14516) demonstrates the idea, taking
target-singer SI-SDR from 0.33 to 5.58 dB. What matters is how weak their setup
is: an Open-Unmix backbone and a **two-layer GRU with hidden size 32** as the
singer encoder. They reached most of what this project measured (+7.3 dB) from a
far worse starting point, which says the conditioning is carrying the result and
nobody has yet put it on a strong backbone.

Their checkpoints cannot be used: they are trained on DAMP-VSEP, which is
research-only and reserves derivatives to Smule.

## The licensing position, which is what makes this possible

StemDeck is free, open source and never monetised. That is not incidental here,
it decides the corpus.

**NonCommercial is satisfied.** Every CC BY-NC corpus below is usable.

**ShareAlike is satisfied by publishing the weights the same way.** Trained
weights are data, not code. StemDeck already keeps them out of the repository and
fetches them at first use, which is how Demucs and the models in `models.md`
work. Publishing a checkpoint under CC BY-NC-SA next to an Apache-2.0 codebase
changes nothing about the licence of the code.

This is the difference between 20 singers and 200. Singer count is what governs
whether a separator generalises, and it is why the VocalSet-only attempt
recorded in `models.md` overfit.

**Research-only stays out.** DAMP-VSEP reserves derivatives to Smule, and no
amount of being open source changes that. It is the one corpus everyone else
trains on, and skipping it is the cost of being able to ship the result.

| corpus | singers | licence | note |
|---|---|---|---|
| OpenSinger | 93 | CC BY-NC-SA 2.0 | pop, 24 kHz |
| JVS-MuSiC | 100 | free, check terms before use | studio, 24 kHz |
| M4Singer | 20 | CC BY-NC-SA 4.0 | **labelled SATB**, 700 songs |
| VocalSet | 20 | CC BY 4.0 | exercises, not songs |
| vocadito | ~40 | CC BY 4.0 | short, real singing |
| CSD | 1 | CC BY-NC-SA 4.0 | two keys per song |
| GTSinger | 20 languages | CC BY-NC-SA 4.0 | 2024, multi-technique |

M4Singer earns its place despite being small: it is **labelled by voice type**,
so same-register pairs can be drawn deliberately rather than hoped for. Everywhere
else, register has to be estimated from F0.

## Architecture

Keep the shipped separator and add conditioning to it.

- **Backbone**: the Conv-TasNet in `app/pipeline/_convtasnet.py`, already loading
  the CC-BY-4.0 checkpoint as a starting point. A stronger backbone is the
  obvious follow-up, but changing two things at once makes the result
  uninterpretable.
- **Singer encoder**: ECAPA, which this project already runs and which is
  Apache-2.0. It is the single largest difference from the paper above.
- **Injection**: FiLM on the masker's bottleneck -- `gamma(z) * h + beta(z)`,
  with `gamma` and `beta` small MLPs over the 192-dimensional embedding. FiLM
  over concatenation because the paper found little between them and FiLM leaves
  the backbone's shapes untouched.
- **Output**: one stem, the enrolled singer. The second stem is the mixture minus
  the first, which keeps mixture consistency for free and halves what the model
  has to learn.

## Data

Mixtures are built, not found. Two solo recordings from **different singers of
the same register**, summed. Ground truth is exact because the sum was
constructed.

- **Pairing**: same voice type. Use M4Singer's labels where they exist, median F0
  elsewhere. Cross-register pairs are what the register model already handles, so
  they are worth at most a small fraction of the batch.
- **Enrolment**: a separate passage by the same singer, never overlapping the one
  in the mixture. The model must key on the voice, not on the passage.
- **Levels**: random, +/- 5 dB. Neither singer should be recoverable by being
  the loud one.

### Reverb is not the problem, and measuring it properly is why

The first attempt used exponentially-decaying noise as a room. It raised the
held-out *produced* score from +6.3 to +10.0 dB while real music fell from +6.9
to +4.9, and the obvious reading was that the model had learned a fake room and
that better rooms were the fix.

That reading was wrong, and rebuilding the eval set with **real measured rooms**
(OpenSLR 28, Apache-2.0: RWCP, REVERB, Aachen) showed why:

| condition | synthetic rooms | real rooms |
|---|---|---|
| VocalSet held-out, produced | +6.55 dB | **+10.31 dB** |

The synthetic reverb was far harsher than any real room. The "production gap" it
appeared to reveal was an artefact of the augmentation, and against real rooms
the model gives up only 1.5 dB against dry. **Reverb robustness is close to a
non-problem, and training harder on it buys almost nothing.**

Use real IRs anyway, since they are free and Apache-2.0, as a wet/dry send rather
than convolving outright -- those IRs are 16 kHz and convolving the signal would
teach the model that duets have no top end. But do not expect a result from it.

### Measured: training on VocalSet makes real music worse

Run to completion with real rooms, 8,000 steps, four singers held out:

| step | produced | real music |
|---|---|---|
| 0 | +8.69 | **+6.93** |
| 1500 | +9.89 | +6.06 |
| 4000 | +9.42 | +5.52 |
| 8000 | +9.37 | **+5.52** |

Real music falls **1.4 dB and flatlines**, never recovering across the whole
run, while the in-domain score drifts up by less than a decibel. Real rooms
halved the damage against the synthetic-reverb attempt but did not change its
direction.

This is the clean version of the experiment: one variable, a held-out eval, and
an out-of-domain check. **VocalSet cannot close the gap and actively hurts.** The
stock checkpoint remains the one to ship, and the next attempt needs sung songs
rather than better augmentation of exercises.

### The gap that is real: exercises against songs

What the corrected numbers leave is this:

| | |
|---|---|
| VocalSet in a real room | +10.31 dB |
| actual produced music | +6.93 dB |

**3.4 dB, and none of it acoustic.** VocalSet is scales, arpeggios and sustained
vowels. Real material is sung phrases with consonants, phrasing, vibrato that
means something, doubling, compression, and two people reacting to each other.

That is a training-data problem, not an augmentation one, and it is the reason
the corpus table above matters more than anything else on this page. M4Singer is
700 songs; OpenSinger is 50 hours of pop from 93 singers. Both are sung music
rather than exercises, and both are reachable only because NonCommercial is
acceptable here.

## Evaluation

The harness exists and is the reason the failed run was caught in five minutes
rather than eight hours. Rebuild it from `models.md` if it has been lost:

- **60 mixtures per condition**, four singers held out of training entirely
- **two conditions**, dry and produced, because the gap between them is the
  finding
- **real out-of-domain audio**, produced music the model never saw
- **95% confidence intervals**, so a sub-dB change is readable

Baselines to beat, measured on the shipped model:

| | |
|---|---|
| held-out dry | +11.78 +/- 1.38 dB |
| held-out produced, real rooms | +10.31 +/- 1.57 dB |
| real produced music | +6.93 +/- 1.60 dB |
| masking ceiling | +13.3 dB |

The produced figure is against real measured rooms. The +6.55 dB recorded
earlier was against synthetic reverb and says more about that reverb than about
the model.

For a conditioned model, add the measure the whole exercise is for: **does stem 1
hold the enrolled singer for the whole song**. Speaker distance between the
output and the enrolment, per 30-second window, across a full track. The shipped
model scores 0.977 there, where the true sources score 0.153.

## Cost

Measured on an RTX 3080, 10 GB, training only the masker (20.7M parameters):

| batch | ms/step | steps/hour | VRAM |
|---|---|---|---|
| 16 | 130 | 27,800 | 2.9 GB |
| 32 | 206 | 17,500 | 5.5 GB |
| 58 | 14,200 | 254 | 9.5 GB, thrashing |

Batch 16 is the working point; 58, the original recipe's batch size, does not fit
in 10 GB. A first run of 30-60k steps is one to two hours. A serious one is days,
not weeks.

Data preparation is the part that will surprise you. The first attempt ran at 3%
GPU because reverb used `np.convolve`: **2.6 s per step against a 130 ms step**.
FFT convolution and a precomputed bank of rooms fixed it.

## What would say this worked

1. Real-world produced audio improves, **not just the produced eval set**. The
   first attempt failed exactly here.
2. Enrolment identity holds across a whole song: well under 0.5 speaker distance,
   against 0.977 today.
3. It degrades honestly on a chorus rather than returning two confident stems
   that both contain everyone.

## What this does not fix

Ensembles. Extracting one enrolled singer from twelve is a harder problem than
the two-source case, and nothing measured here suggests it comes for free. Expect
to need more than two sources at the output and a great deal more data.
