// The pitch stage of the SoundTouch worklet (#245).
//
// Pitch is the one audio feature where "sounds about right" is not a test: a
// transpose that lands two cents flat is inaudible and a transpose that lands
// thirty cents flat is unusable, and nothing in between announces itself. So
// this measures rather than eyeballs, by driving the processor exactly as an
// AudioWorklet would (128-frame blocks, k-rate params) and analysing what
// comes out.
//
// Frequency is estimated by autocorrelation, not by an FFT peak. WSOLA splices
// the signal periodically, which smears a pure tone's spectrum enough that a
// parabolic FFT peak reports errors of twenty cents on output that is actually
// correct to two. That false alarm is what motivated using a period estimator.
//
// Run:  node tests/js/pitch-shift.test.mjs

import { readFileSync } from 'node:fs';

const SR = 44100;
const BLOCK = 128;

let pass = 0,
  fail = 0;
const check = (name, cond, detail = '') => {
  if (cond) {
    pass++;
    console.log(`PASS  ${name}`);
  } else {
    fail++;
    console.log(`FAIL  ${name}${detail ? '  -- ' + detail : ''}`);
  }
};

const SRC = readFileSync(new URL('../../static/vendor/soundtouch-processor.js', import.meta.url), 'utf8');

/** Load the worklet with the AudioWorklet globals shimmed, optionally patched. */
function load(src = SRC) {
  let Cls = null;
  new Function('sampleRate', 'AudioWorkletProcessor', 'registerProcessor', src)(
    SR,
    class {
      constructor() {
        this.port = { onmessage: null, postMessage: (m) => this.port.sent.push(m), sent: [] };
      }
    },
    (_name, c) => {
      Cls = c;
    },
  );
  return Cls;
}

// The processor takes one input per semitone, so "transpose by n" is expressed
// by which input the signal is fed to. Input ZERO_INPUT is the plain delay line
// that carries drums and anything already in its own key.
const INPUT_COUNT = 13;
const ZERO_INPUT = 6;

/**
 * Build the `inputs` argument, given a map of semitone -> block.
 *
 * Unlisted inputs are handed an empty array, which is exactly what Web Audio
 * passes for an input nothing is connected to, and is how the processor decides
 * a chain is needed at all.
 */
function inputsFor(blocksBySemitone) {
  const inputs = [];
  for (let k = 0; k < INPUT_COUNT; k++) {
    const block = blocksBySemitone[k - ZERO_INPUT];
    inputs.push(block ? [block, block] : []);
  }
  return inputs;
}

/**
 * Collapse [semitone, block] pairs into one block per input.
 *
 * At semitone 0 the music and drum buses are the same input, and letting the
 * later entry win would silently drop the earlier signal -- which reads as a
 * missing onset rather than as a broken probe.
 */
function onBuses(pairs) {
  const out = {};
  for (const [semitone, block] of pairs) {
    if (!out[semitone]) { out[semitone] = block; continue; }
    const merged = new Float32Array(block.length);
    for (let i = 0; i < block.length; i++) merged[i] = out[semitone][i] + block[i];
    out[semitone] = merged;
  }
  return out;
}

/** Feed an existing buffer through the processor; return the output. */
function driveBuffer(Cls, input, { tempo = 1, pitch = 0 }) {
  const p = new Cls();
  const params = { tempo: [tempo] };
  const blocks = Math.floor(input.length / BLOCK);
  const out = new Float32Array(blocks * BLOCK);
  for (let b = 0; b < blocks; b++) {
    const inL = input.subarray(b * BLOCK, (b + 1) * BLOCK);
    const oL = new Float32Array(BLOCK);
    const oR = new Float32Array(BLOCK);
    p.process(inputsFor({ [pitch]: inL }), [[oL, oR]], params);
    out.set(oL, b * BLOCK);
  }
  return out;
}

/** Feed a sine through the processor block by block; return the output. */
function drive(Cls, freq, { tempo = 1, pitch = 0 }, blocks = 500) {
  const p = new Cls();
  const params = { tempo: [tempo] };
  const out = new Float32Array(blocks * BLOCK);
  const step = (2 * Math.PI * freq) / SR;
  let phase = 0;
  for (let b = 0; b < blocks; b++) {
    const inL = new Float32Array(BLOCK);
    for (let i = 0; i < BLOCK; i++) {
      inL[i] = Math.sin(phase);
      phase += step;
    }
    const oL = new Float32Array(BLOCK);
    const oR = new Float32Array(BLOCK);
    p.process(inputsFor({ [pitch]: inL }), [[oL, oR]], params);
    out.set(oL, b * BLOCK);
  }
  return out;
}

/**
 * Fundamental frequency by autocorrelation, searched within 20% of `expect`.
 *
 * The window is deliberately narrow: a wide search locks onto a subharmonic on
 * WSOLA output and reports a clean -2400 cents, which looks like a catastrophic
 * bug and is only the estimator picking the wrong lag.
 */
function periodNear(x, expect) {
  const lagMin = Math.floor(SR / (expect * 1.2));
  const lagMax = Math.ceil(SR / (expect / 1.2));
  const ac = new Float64Array(lagMax + 2);
  let best = lagMin,
    bestV = -Infinity;
  for (let lag = lagMin; lag <= lagMax; lag++) {
    let s = 0;
    for (let i = 0; i < x.length - lag; i++) s += x[i] * x[i + lag];
    ac[lag] = s / (x.length - lag);
    if (ac[lag] > bestV) {
      bestV = ac[lag];
      best = lag;
    }
  }
  const y0 = ac[best - 1],
    y1 = ac[best],
    y2 = ac[best + 1];
  return SR / (best + (0.5 * (y0 - y2)) / (y0 - 2 * y1 + y2 || 1));
}

const cents = (a, b) => 1200 * Math.log2(a / b);

/** Energy at `f` via Goertzel, normalised by length. */
function energyAt(y, f) {
  const w = (2 * Math.PI * f) / SR;
  const c = 2 * Math.cos(w);
  let s1 = 0,
    s2 = 0;
  for (let i = 0; i < y.length; i++) {
    const s0 = y[i] + c * s1 - s2;
    s2 = s1;
    s1 = s0;
  }
  return (s1 * s1 + s2 * s2 - c * s1 * s2) / y.length;
}

const Proc = load();

// ── 1. Passthrough must be exact ────────────────────────────────────────────
// Nobody transposes most of the time, so the common path has to be free of
// both cost and colour. Not "close": identical.
{
  // Compared against the very samples that were fed, not a recomputed sine:
  // the input is float32 and its phase accumulates, so a float64 reference
  // disagrees by ~3e-8 even when the processor copied the block verbatim.
  const step = (2 * Math.PI * 440) / SR;
  const input = Float32Array.from({ length: 200 * BLOCK }, (_, i) => Math.sin(i * step));
  const y = driveBuffer(Proc, input, { tempo: 1, pitch: 0 });
  let maxDiff = 0;
  for (let i = 0; i < y.length; i++) maxDiff = Math.max(maxDiff, Math.abs(y[i] - input[i]));
  check('pitch 0 at speed 1 is bit-identical to the input', maxDiff === 0, `max diff ${maxDiff}`);
}

// ── 2. Every offered semitone lands where it claims ─────────────────────────
// 5 cents is about the smallest interval a trained ear reliably hears on a
// sustained tone, so it is the honest ceiling for "in tune".
{
  const F = 440;
  let worst = 0,
    worstN = 0;
  let worstTempo = 1;
  for (const tempo of [0.75, 1]) {
    for (let n = -6; n <= 6; n++) {
      if (n === 0) continue;
      const expect = F * Math.pow(2, n / 12);
      const y = drive(Proc, F, { tempo, pitch: n }, 900);
      const err = Math.abs(cents(periodNear(y.subarray(SR), expect), expect));
      if (err > worst) {
        worst = err;
        worstN = n;
        worstTempo = tempo;
      }
    }
  }
  check(
    'every semitone at every offered speed is within 5 cents of target',
    worst < 5,
    `worst ${worst.toFixed(2)} cents at ${worstN}, speed ${worstTempo}`,
  );
}

// ── 3. The stretcher must not move pitch on its own ─────────────────────────
// If it did, pitch and speed would interact and the two controls could not be
// used together, which is the whole point of pairing them.
{
  let worst = 0;
  for (const tempo of [0.75, 1.25, 1.5]) {
    const y = drive(Proc, 440, { tempo, pitch: 0 }, 700);
    worst = Math.max(worst, Math.abs(cents(periodNear(y.subarray(SR), 440), 440)));
  }
  check('changing speed alone leaves pitch alone', worst < 5, `worst ${worst.toFixed(2)} cents`);
}

// ── 4. Anti-aliasing actually rejects aliases ───────────────────────────────
// 17 kHz shifted up six semitones maps past Nyquist and folds back to about
// 20 kHz. Comparing against the same processor with the filter defeated is the
// only measurement that means anything: an absolute number cannot tell you
// whether the filter or the arithmetic is responsible.
{
  const Unfiltered = load(SRC.replace('0.47 * sr', '0 * sr'));
  const ALIAS_HZ = 44100 - 17000 * Math.pow(2, 6 / 12);
  const on = energyAt(drive(Proc, 17000, { pitch: 6 }, 400).subarray(SR), ALIAS_HZ);
  const off = energyAt(drive(Unfiltered, 17000, { pitch: 6 }, 400).subarray(SR), ALIAS_HZ);
  const dB = 10 * Math.log10(off / (on || 1e-30));
  check('the anti-alias filter rejects at least 20 dB', dB > 20, `only ${dB.toFixed(1)} dB`);
}

// ── 5. and costs nothing it does not have to ────────────────────────────────
// The filter sits before the resampler precisely so it does not attenuate
// content that was going to survive the shift. Placed after, it took 9 dB off
// this tone for no benefit.
{
  const Unfiltered = load(SRC.replace('0.47 * sr', '0 * sr'));
  const OUT_HZ = 11000 * Math.pow(2, 6 / 12);
  const on = energyAt(drive(Proc, 11000, { pitch: 6 }, 400).subarray(SR), OUT_HZ);
  const off = energyAt(drive(Unfiltered, 11000, { pitch: 6 }, 400).subarray(SR), OUT_HZ);
  const loss = Math.abs(10 * Math.log10(on / off));
  check('and takes less than 1 dB off content that survives the shift', loss < 1, `${loss.toFixed(2)} dB lost`);
}

// ── 6. Output stays finite ──────────────────────────────────────────────────
// A resonant biquad fed a transient can ring into NaN if its state is ever
// reset inconsistently, and a single NaN silences the whole graph until the
// page reloads.
{
  let bad = 0;
  for (const n of [-6, -1, 3, 6]) {
    const y = drive(Proc, 440, { tempo: 0.75, pitch: n }, 300);
    for (let i = 0; i < y.length; i++) if (!Number.isFinite(y[i]) || Math.abs(y[i]) > 4) bad++;
  }
  check('no NaN, no infinity, no runaway samples', bad === 0, `${bad} bad samples`);
}

// ── 7. Leaving transpose must not replay stale audio ────────────────────────
// Both stages buffer over a hundred milliseconds. Returning to the bypass path
// without dropping that would emit it a moment later as a click.
{
  const p = new Proc();
  const step = (2 * Math.PI * 440) / SR;
  let phase = 0;
  const feed = (pitch, blocks) => {
    let last = null;
    for (let b = 0; b < blocks; b++) {
      const inL = new Float32Array(BLOCK);
      for (let i = 0; i < BLOCK; i++) {
        inL[i] = Math.sin(phase);
        phase += step;
      }
      const oL = new Float32Array(BLOCK);
      const oR = new Float32Array(BLOCK);
      p.process(inputsFor({ [pitch]: inL }), [[oL, oR]], { tempo: [1] });
      last = { inL, oL };
    }
    return last;
  };
  feed(4, 200);
  // Leaving a key is a disconnection, so the chain keeps running until it has
  // played out what it buffered. Nothing should be spliced on the way.
  const draining = feed(0, 40);
  let drainStep = 0;
  for (let i = 1; i < BLOCK; i++) {
    drainStep = Math.max(drainStep, Math.abs(draining.oL[i] - draining.oL[i - 1]));
  }
  check('the chain being left drains without a splice', drainStep < 0.2, `step ${drainStep.toFixed(3)}`);

  // Once it is spent the processor drops it and the bypass path returns, which
  // is what makes an untransposed track bit-identical rather than merely close.
  const after = feed(0, 400);
  let maxDiff = 0;
  for (let i = 0; i < BLOCK; i++) maxDiff = Math.max(maxDiff, Math.abs(after.oL[i] - after.inL[i]));
  check('returning to pitch 0 resumes clean passthrough', maxDiff === 0, `max diff ${maxDiff}`);
}

// ── 8. The engine and the processor agree on the input layout ───────────────
// A lane's transpose is which input it is connected to, and the two files
// decide that separately: pitchBus.js picks the index, the processor declares
// how many exist. If they disagreed, a lane at the edge of the range would be
// wired to an input that is not there and would simply go silent -- no error,
// no warning, just a missing instrument.
{
  const busSrc = readFileSync(new URL('../../static/js/pitchBus.js', import.meta.url), 'utf8');
  const num = (src, name) => {
    const m = src.match(new RegExp(`${name}\\s*=\\s*(-?\\d+)`));
    return m ? Number(m[1]) : null;
  };
  const workletMin = num(SRC, 'PITCH_MIN_SEMITONES');
  const workletMax = num(SRC, 'PITCH_MAX_SEMITONES');
  const engineMin = num(busSrc, 'PITCH_MIN');
  const engineMax = num(busSrc, 'PITCH_MAX');
  check(
    'the engine and the processor offer the same semitone range',
    workletMin === engineMin && workletMax === engineMax,
    `worklet ${workletMin}..${workletMax}, engine ${engineMin}..${engineMax}`,
  );
  check(
    'that range is the one the UI exposes',
    workletMin === -6 && workletMax === 6,
    `${workletMin}..${workletMax}`,
  );

  // Semitone 0 has to land on the input that does no resampling, or drums
  // would be handed to a chain.
  const zero = num(busSrc, 'ZERO_INPUT_FALLBACK');
  check('semitone 0 maps to the unresampled input', zero === null && engineMin === -6);

  // And the processor must report the buffering the engine uses to place the
  // playhead. Reading it from a message rather than recomputing it is what
  // keeps the two from drifting apart.
  const probe = new Proc();
  const latency = probe.port.sent.find((m) => m.type === 'latency');
  check(
    'the processor reports its own latency to the engine',
    latency && latency.frames > SR * 0.05 && latency.frames < SR * 0.5,
    JSON.stringify(latency),
  );
}


// ── 9. Drums are not transposed ─────────────────────────────────────────────
// The whole reason the processor takes two inputs. A resampled snare is not
// the same drum in another key, it is a different drum, so input 1 must come
// out at the pitch it went in at no matter what the pitch parameter says.
{
  const DRUM_HZ = 220;
  const musicStep = (2 * Math.PI * 440) / SR;
  const drumStep = (2 * Math.PI * DRUM_HZ) / SR;

  for (const n of [-5, 5]) {
    const p = new Proc();
    const params = { tempo: [1] };
    const blocks = 900;
    const out = new Float32Array(blocks * BLOCK);
    let mPhase = 0,
      dPhase = 0;
    for (let b = 0; b < blocks; b++) {
      const m = new Float32Array(BLOCK);
      const d = new Float32Array(BLOCK);
      for (let i = 0; i < BLOCK; i++) {
        m[i] = 0; // silence the pitched bus so only the drum tone is measurable
        mPhase += musicStep;
        d[i] = Math.sin(dPhase);
        dPhase += drumStep;
      }
      const oL = new Float32Array(BLOCK);
      const oR = new Float32Array(BLOCK);
      p.process(inputsFor({ [n]: m, 0: d }), [[oL, oR]], params);
      out.set(oL, b * BLOCK);
    }
    const got = periodNear(out.subarray(SR), DRUM_HZ);
    const err = Math.abs(cents(got, DRUM_HZ));
    check(`drums survive a ${n > 0 ? '+' : ''}${n} semitone transpose unshifted`, err < 5, `moved ${err.toFixed(2)} cents`);
  }
}

// ── 10. Drums stay in time with the band ────────────────────────────
// Test each bus in its own processor. Opposite impulses in one summed output
// cancel when they are aligned, which can turn a correct result into a false
// failure or make filter ringing look like a second onset.
//
// The marker is a 250 ms tone burst, never a lone impulse. WSOLA relocates an
// isolated impulse wherever its correlator likes, so an impulse measures the
// grain search rather than the timing. An earlier revision of this file did
// exactly that and reported 2 ms, but only because the processor then split
// attacks out and passed them at unity delay: the probe was reading the
// bypass, not the pitch chain. Removing the splitter took the same measurement
// to 38 ms with no change in alignment, which is what a test measuring the
// wrong signal looks like.
//
// Two properties are worth asserting, and they are not the same one:
//
//   drift    the buses must not walk apart as a track plays. This is what
//            ruins a four-minute song, and it is checked a minute in.
//   offset   a bounded, non-accumulating error is inherent. WSOLA copies a
//            sequence verbatim and jumps by `outLen * tempo`, so the source
//            position sawtooths around the mean even though the mean is exact.
//            `_alignBuses` centres that sawtooth; what is left is its width.
{
  const BURST = Math.round(0.25 * SR);
  const FADE = Math.round(0.002 * SR);
  const WIN = Math.round(0.002 * SR);

  /** Output time of a single tone burst fed to one bus, in samples. */
  const onsetAt = (pitch, tempo, bus, at, seconds) => {
    const p = new Proc();
    const params = { tempo: [tempo] };
    const blocks = Math.ceil((seconds * SR) / BLOCK);
    const silence = new Float32Array(BLOCK);
    const env = [];
    const acc = new Float32Array(WIN);
    let ai = 0;
    let peak = 0;
    for (let b = 0; b < blocks; b++) {
      const sig = new Float32Array(BLOCK);
      for (let i = 0; i < BLOCK; i++) {
        const k = b * BLOCK + i - at;
        if (k < 0 || k >= BURST) continue;
        const shape = Math.min(1, k / FADE) * Math.min(1, (BURST - k) / FADE);
        sig[i] = shape * 0.5 * Math.sin((2 * Math.PI * 300 * (b * BLOCK + i)) / SR);
      }
      const m = bus === 0 ? sig : silence;
      const d = bus === 1 ? sig : silence;
      const oL = new Float32Array(BLOCK);
      const oR = new Float32Array(BLOCK);
      p.process(inputsFor(onBuses([[pitch, m], [0, d]])), [[oL, oR]], params);
      for (let i = 0; i < BLOCK; i++) {
        acc[ai++] = oL[i];
        if (ai < WIN) continue;
        let sum = 0;
        for (let q = 0; q < WIN; q++) sum += acc[q] * acc[q];
        const rms = Math.sqrt(sum / WIN);
        env.push(rms);
        if (rms > peak) peak = rms;
        ai = 0;
      }
    }
    if (peak < 0.02) return -1;
    for (let j = 0; j < env.length; j++) if (env[j] > peak * 0.25) return j * WIN;
    return -1;
  };

  const skewMs = (pitch, tempo, at) => {
    const seconds = (at / SR + 2) / Math.min(1, tempo) + 3;
    const music = onsetAt(pitch, tempo, 0, at, seconds);
    const drums = onsetAt(pitch, tempo, 1, at, seconds);
    if (music < 0 || drums < 0) return NaN;
    return ((music - drums) / SR) * 1000;
  };

  // Pitch 0 is a pure bypass on both buses, so it is exact, and saying so
  // guards the one case a user hears most.
  let bypassWorst = 0;
  for (const tempo of [0.75, 1]) {
    for (const t of [0.7, 2.3, 5.1]) {
      const s = Math.abs(skewMs(0, tempo, Math.round(t * SR)));
      if (!(s < Infinity)) { bypassWorst = Infinity; break; }
      bypassWorst = Math.max(bypassWorst, s);
    }
  }
  check('at pitch 0 the buses are sample-aligned', bypassWorst === 0, `worst ${bypassWorst} ms`);

  // The offset budget. `outLen * (1 - tempo)` is the sawtooth width, 20 ms at
  // six semitones; the burst is read at 2 ms resolution. 30 ms leaves room for
  // both without admitting a real desync, which would show as tens of ms at
  // small intervals or as drift below.
  let worst = 0;
  let worstCase = '';
  for (const tempo of [0.75, 1]) {
    for (let n = -6; n <= 6; n++) {
      for (const t of [0.7, 2.3, 5.1]) {
        const s = Math.abs(skewMs(n, tempo, Math.round(t * SR)));
        if (!(s <= worst) && !(s > worst)) { worst = Infinity; worstCase = `${n} @ ${tempo}: no onset`; continue; }
        if (s > worst) {
          worst = s;
          worstCase = `${n > 0 ? '+' : ''}${n}, speed ${tempo}, onset ${t.toFixed(2)}s`;
        }
      }
    }
  }
  check(
    'drums stay within a grain of the band for every pitch and offered speed',
    worst <= 30,
    `worst ${Number.isFinite(worst) ? worst.toFixed(2) + ' ms' : 'missing onset'} at ${worstCase}`,
  );

  // The one that matters over a whole track. A chain whose rates are even
  // slightly unequal looks fine in the first bar and is a beat out by the last.
  let worstDrift = 0;
  let driftCase = '';
  for (const tempo of [0.75, 1]) {
    for (const n of [-6, -2, 2, 6]) {
      const early = skewMs(n, tempo, Math.round(1 * SR));
      const late = skewMs(n, tempo, Math.round(60 * SR));
      const drift = Math.abs(late - early);
      if (!(drift <= worstDrift) && !(drift > worstDrift)) { worstDrift = Infinity; driftCase = `${n} @ ${tempo}: no onset`; continue; }
      if (drift > worstDrift) {
        worstDrift = drift;
        driftCase = `${n > 0 ? '+' : ''}${n}, speed ${tempo}`;
      }
    }
  }
  check(
    'the buses do not drift apart over a minute of playback',
    worstDrift <= 30,
    `worst ${Number.isFinite(worstDrift) ? worstDrift.toFixed(2) + ' ms' : 'missing onset'} at ${driftCase}`,
  );
}

// ── 11. No clicks, pops or dropouts while transposing ───────────────
// The reported symptom (#245 follow-up) was "choppy, static for
// microseconds" while transposed. Both causes were structural, and both are
// invisible to a test that only measures pitch:
//
//   1. attacks were split out of the music bus and passed at unity while the
//      tonal half went through the ~120 ms pitch chain. The two halves cannot
//      sum back to the input, so every onset punched a step into the output.
//      Measured 0.64 full scale against a source whose own largest step was
//      0.10, five times anything present in the input.
//   2. the pitch chain produced exactly one quantum per quantum with no
//      slack, so a quantum occasionally came up a few samples short and was
//      emitted as a partial block, zero-padded. A hard edge to zero in the
//      middle of a block is a click.
//
// So this measures the output the way an ear does: the largest sample-to-
// sample step, against the same measure taken on the bypass path. Anything
// the DSP adds beyond what the source already contains is an artefact.
{
  const chord = (n) => {
    const a = new Float32Array(n);
    for (let i = 0; i < n; i++) {
      const t = i / SR;
      a[i] = 0.2 * Math.sin(2 * Math.PI * 220 * t)
        + 0.15 * Math.sin(2 * Math.PI * 277.18 * t)
        + 0.15 * Math.sin(2 * Math.PI * 329.63 * t);
      // An attack every 500 ms: this is what triggered the splitter, and any
      // real mix has them several times a second.
      const phase = i % Math.round(SR * 0.5);
      if (phase < SR * 0.02) {
        a[i] += 0.5 * Math.exp(-phase / (SR * 0.004)) * Math.sin(2 * Math.PI * 1200 * t);
      }
    }
    return a;
  };

  /** Largest step, and any block emitted short and zero-padded. */
  const artefacts = (Cls, pitch, tempo, blocks = 1200) => {
    const p = new Cls();
    const params = { tempo: [tempo] };
    const src = chord(blocks * BLOCK);
    const out = new Float32Array(blocks * BLOCK);
    let partial = 0;
    for (let b = 0; b < blocks; b++) {
      const inL = src.subarray(b * BLOCK, (b + 1) * BLOCK);
      const oL = new Float32Array(BLOCK);
      const oR = new Float32Array(BLOCK);
      p.process(inputsFor({ [pitch]: inL }), [[oL, oR]], params);
      out.set(oL, b * BLOCK);
      let zeros = 0;
      for (let i = BLOCK - 1; i >= 0 && oL[i] === 0; i--) zeros++;
      if (zeros > 0 && zeros < BLOCK) partial++;
    }
    let step = 0;
    for (let i = Math.round(SR * 0.6) + 1; i < out.length; i++) {
      const d = Math.abs(out[i] - out[i - 1]);
      if (d > step) step = d;
    }
    return { step, partial };
  };

  const Proc11 = load();
  const baseline = artefacts(Proc11, 0, 1).step;

  let worstStep = 0;
  let worstStepCase = '';
  let partials = 0;
  let partialCase = '';
  for (const [pitch, tempo] of [[2, 1], [-2, 1], [5, 1], [-5, 1], [6, 1], [-6, 1], [2, 0.75], [-5, 0.75]]) {
    const a = artefacts(Proc11, pitch, tempo);
    if (a.step > worstStep) {
      worstStep = a.step;
      worstStepCase = `${pitch > 0 ? '+' : ''}${pitch}, speed ${tempo}`;
    }
    if (a.partial > 0 && !partialCase) partialCase = `${pitch > 0 ? '+' : ''}${pitch}, speed ${tempo}`;
    partials += a.partial;
  }

  check(
    'no quantum is emitted short and zero-padded',
    partials === 0,
    `${partials} partial blocks, first at ${partialCase}`,
  );
  // Pitching up resamples, which steepens every slope by the shift ratio, so
  // some headroom over the bypass is correct. 2x covers the 1.41x at six
  // semitones; the splitter bug sat at 6x and could not hide under this.
  check(
    'transposing adds no step the source did not already contain',
    worstStep < baseline * 2,
    `worst ${worstStep.toFixed(3)} at ${worstStepCase} vs ${baseline.toFixed(3)} on bypass`,
  );

  // Prove the gate above can actually see the regression it exists for: with
  // the input cushion removed, the chain runs at zero slack again and starves.
  const starved = load(SRC
    .replace('const PRIME_CUSHION = 512;', 'const PRIME_CUSHION = 0;')
    .replace('const OUTPUT_RESERVE = 512;', 'const OUTPUT_RESERVE = 0;')
    .replace('this._commonPrime = this._tempo.needed + PRIME_CUSHION;', 'this._commonPrime = 0;'));
  let starvedPartials = 0;
  for (const [pitch, tempo] of [[2, 1], [-2, 1], [5, 1], [-5, 1]]) {
    starvedPartials += artefacts(starved, pitch, tempo).partial;
  }
  check(
    'and that gate fails when the input cushion is removed',
    starvedPartials > 0,
    `saw ${starvedPartials} partial blocks with the cushion gone`,
  );
}

// A transport restart keeps the same parameters, so parameter-change flushing
// cannot protect it. The explicit message must clear all buffered audio.
{
  const p = new Proc();
  const params = { tempo: [0.75] };
  const silence = new Float32Array(BLOCK);
  const impulse = new Float32Array(BLOCK);
  impulse[0] = 1;
  p.process(inputsFor({ 4: impulse }), [[new Float32Array(BLOCK), new Float32Array(BLOCK)]], params);
  for (let i = 0; i < 80; i++) {
    p.process(inputsFor({ 4: silence }), [[new Float32Array(BLOCK), new Float32Array(BLOCK)]], params);
  }
  p.port.onmessage?.({ data: { type: 'reset' } });
  let peak = 0;
  for (let i = 0; i < 100; i++) {
    const out = new Float32Array(BLOCK);
    p.process(inputsFor({ 4: silence }), [[out, new Float32Array(BLOCK)]], params);
    for (const sample of out) peak = Math.max(peak, Math.abs(sample));
  }
  check('an explicit transport reset cannot emit stale buffered audio', peak === 0, `peak ${peak}`);
}

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
