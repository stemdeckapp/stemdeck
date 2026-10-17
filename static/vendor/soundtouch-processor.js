'use strict';
// SoundTouch WSOLA time-stretcher + pitch shifter - AudioWorkletProcessor
// Adapted from SoundTouch C++ by Olli Parviainen. MIT License.
// Self-contained; no imports required.
//
// ONE INPUT PER SEMITONE, ON PURPOSE (#245).
//
//   input k   everything to be shifted by (k - 6) semitones
//   input 6   semitone 0: drums, the click, and any lane at its own key
//
// Drums are unpitched. Resampling a snare does not move it to another key, it
// makes it a different drum, so the pitch stage has to skip them. What it may
// not skip is the time-stretch: at 0.75x the kit still has to slow down with
// the band.
//
// The obvious implementation, two independent stretch chains, is broken.
// Measured end-to-end latency of a single chain:
//
//     pitch  0, tempo 1       0 ms   (bypass)
//     pitch +2, tempo 1     115 ms
//     pitch -5, tempo 1     144 ms
//     pitch  0, tempo 0.75  437 ms
//
// Latency moves with both parameters, so independent chains put the drums up to
// 144 ms away from the band. This processor has one music-only pitch stage,
// then sends the combined music and drum buses through one shared tempo stage.
// There is only one output clock.
//
// An earlier revision also split attacks out of the music bus and passed them
// at unity to "protect" them from the WSOLA grain. That is unsound: the tonal
// half is delayed by the pitch stage and the attack half is not, so the two
// cannot sum back to the input. Measured against a mix with attacks it put a
// 0.64 full-scale step into the output at every detected onset, five times
// anything in the source, heard as a click. It also had nothing left to
// protect once drums got their own unpitched input.
//
// The two chains produce the same duration by construction. With s = speed,
// n = semitones and r = 2^(n/12), a resampler stepping r input samples per
// output sample multiplies pitch by r and divides duration by r, while WSOLA
// multiplies duration by 1/tempo:
//
//     music     WSOLA(1 / r), resample(r), then shared WSOLA(s)
//     drums     unity pitch, then shared WSOLA(s)
//
// Both have duration 1/s. Only music has pitch multiplied by r.

const SEQUENCE_MS = 82;
const SEEK_MS     = 28;
const OVERLAP_MS  = 12;

// Semitone range offered to the UI. Past roughly +/-5 this design audibly
// degrades, so the control stops before it starts sounding broken rather than
// offering an octave nobody would use.
const PITCH_MIN_SEMITONES = -6;
const PITCH_MAX_SEMITONES = 6;

// Below this, a parameter counts as "off" and its stage is bypassed.
const EPS = 1e-3;

// Spare samples kept queued past what this render quantum needs.
//
// A WSOLA sequence emits 70 ms in one go and then consumes input for the same
// 70 ms of playback before it can emit again, so supply and demand balance only
// on average, never within a quantum. Reading the pitch stage directly means a
// quantum occasionally comes up a few samples short and is written out as a
// partial block with a hard zero edge in the middle, which is a click.
//
// Raising the fill target alone cannot fix that: the stage produces exactly the
// input it is given, so there is no surplus to build a reserve out of. The
// reserve has to live downstream of the resampler, where it is filled during
// the priming silence and then absorbs the sequence sawtooth for good. 512
// samples is 12 ms, below the pitch stage's own latency and inaudible.
const OUTPUT_RESERVE = 512;

// Extra input a WSOLA chain banks before its first sequence.
//
// A sequence needs `needed` samples queued to run, and consumes exactly what it
// emits, so input arriving in 128-sample quanta lands the FIFO right back on
// the threshold every cycle. Whether it is a few samples over or a few under at
// the instant output runs dry is then pure phase, and under means the chain
// emits nothing for several quanta. Holding back the first sequence until a
// cushion has accumulated leaves that cushion in the FIFO for the rest of the
// run, because the rates are balanced from then on. Two quanta covers the block
// quantisation; the rest is the fractional advance, which is under a sample.
const PRIME_CUSHION = 512;

class FloatFifo {
  constructor() {
    this._d = new Float32Array(65536);
    this._r = 0;
    this._w = 0;
  }
  get avail() { return this._w - this._r; }
  clear() { this._r = this._w = 0; }
  peek(offset) { return this._d[this._r + offset]; }
  consume(n) { this._r = Math.min(this._r + n, this._w); }
  shift(dst, dstOff, n) {
    n = Math.min(n, this.avail);
    for (let i = 0; i < n; i++) dst[dstOff + i] = this._d[this._r++];
    return n;
  }
  push(src, srcOff, n) {
    this._ensureRoom(n);
    for (let i = 0; i < n; i++) this._d[this._w++] = src[srcOff + i];
  }
  _compact() {
    const av = this.avail;
    this._d.copyWithin(0, this._r, this._w);
    this._r = 0;
    this._w = av;
  }
  _ensureRoom(n) {
    if (this._r > 32768) this._compact();
    if (this._w + n > this._d.length) {
      this._compact();
      if (this._w + n > this._d.length) {
        const nd = new Float32Array(Math.max(this._d.length * 2, this._w + n + 4096));
        nd.set(this._d.subarray(0, this._w));
        this._d = nd;
      }
    }
  }
}

function findBestOffset(ref, refLen, fifo, seekLen) {
  let bestOff = 0, bestCorr = -Infinity;
  for (let off = 0; off < seekLen; off++) {
    let corr = 0;
    for (let i = 0; i < refLen; i++) corr += ref[i] * fifo.peek(off + i);
    if (corr > bestCorr) { bestCorr = corr; bestOff = off; }
  }
  return bestOff;
}

/**
 * Transposed-Direct-Form-II biquad, used as the anti-alias lowpass.
 *
 * Only engaged when pitching UP, where the resampler decimates and everything
 * above Nyquist/r folds back into the audible band as metallic grit. Pitching
 * down interpolates instead and needs no filter.
 *
 * 0.47 rather than 0.5 leaves a little room below the fold-back frequency, so
 * the filter is not merely 3 dB down exactly where the aliases arrive.
 */
class Biquad {
  constructor() { this.reset(); this.setPassthrough(); }
  reset() { this._z1 = 0; this._z2 = 0; }
  setPassthrough() { this._b0 = 1; this._b1 = 0; this._b2 = 0; this._a1 = 0; this._a2 = 0; }
  /** Lowpass at `freq` Hz with quality `q`, per the RBJ audio EQ cookbook. */
  setLowpass(freq, sr, q) {
    const w0 = 2 * Math.PI * Math.min(freq, sr * 0.49) / sr;
    const cos0 = Math.cos(w0);
    const alpha = Math.sin(w0) / (2 * q);
    const a0 = 1 + alpha;
    this._b0 = ((1 - cos0) / 2) / a0;
    this._b1 = (1 - cos0) / a0;
    this._b2 = this._b0;
    this._a1 = (-2 * cos0) / a0;
    this._a2 = (1 - alpha) / a0;
  }
  process(x) {
    const y = this._b0 * x + this._z1;
    this._z1 = this._b1 * x - this._a1 * y + this._z2;
    this._z2 = this._b2 * x - this._a2 * y;
    return y;
  }
}

// Butterworth pole Qs for an 8th-order cascade, Q_k = 1/(2*cos((2k+1)*pi/16)).
// Fourth order measured only 15 dB of alias rejection, because the frequency
// that folds sits a fifth of an octave above the cutoff, well inside a
// 4th-order transition band. Eight poles put the stopband where the aliases
// actually land, and four biquads on one bus cost nothing worth counting.
const BUTTERWORTH_Q8 = [0.50979558, 0.60134489, 0.89997622, 2.56291545];

// Attacks the tempo stage must not play twice (#728).
//
// Below 1x a sequence plays 70 ms of input but moves on by only 70 * tempo, so
// consecutive sequences overlap and whatever sits in the overlap is heard
// twice. Sustained sound hides that. A kick does not: at 0.75x it came out as a
// flam about 50 ms long. When the next sequence would replay an attack, the
// chain instead carries straight on from where the last sequence stopped, which
// splices identical samples and so is seamless, and borrows the time that costs.
// Later sequences stretch a little harder to pay it back.
//
// Nothing is split or filtered, so unlike the attack path this file once had,
// the output is still built only from whole grains of the input.
const ATTACK_FRAME_MS   = 2;   // energy frame the detector works in
const ATTACK_HISTORY_MS = 20;  // what an attack is measured against
const ATTACK_RATIO      = 4;   // 6 dB above the recent level
const ATTACK_FLOOR      = 1e-6; // ignore noise rising out of near silence
const ATTACK_GAP_MS     = 30;  // one attack per kick, not one per frame of it
// An attack is marked this far ahead of the frame that crossed the threshold,
// so the few milliseconds of rise before it count as part of the attack.
const ATTACK_LEAD_MS    = 4;
// Borrowed input the chain may be ahead of its nominal clock by. The whole
// mix, click included, goes through this one stage, so borrowing moves
// everything together and the only effect is the playhead trailing the audio,
// by this at most, until it is paid back.
const MAX_BORROW_MS     = 100;
// Share of a sequence's advance used to pay the loan back, so repayment is
// spread thinly rather than stretching one sequence hard.
const REPAY_SHARE       = 0.25;

/**
 * Marks attacks in a sample stream, by absolute sample position.
 *
 * Measured on the sample-to-sample difference rather than on the signal: the
 * difference is dominated by high frequencies, which is what an attack is made
 * of, while a bass line or a pad barely moves it. Plain energy missed most of
 * the attacks in a full mix and placed the rest 8 to 30 ms late.
 *
 * Positions are absolute so they survive the FIFO compacting under them.
 */
class AttackTracker {
  constructor(sr) {
    this.frameLen = Math.max(1, Math.round(ATTACK_FRAME_MS * sr / 1000));
    this.alpha = ATTACK_FRAME_MS / ATTACK_HISTORY_MS;
    this.gap = Math.round(ATTACK_GAP_MS * sr / 1000);
    this.lead = Math.round(ATTACK_LEAD_MS * sr / 1000);
    this.clear();
  }

  clear() {
    this.written = 0;    // absolute position of the next sample pushed
    this.frameAcc = 0;
    this.frameFill = 0;
    this.prevL = 0;
    this.prevR = 0;
    this.level = 0;
    this.lastAttack = -Infinity;
    this.attacks = [];
  }

  push(l, r, n) {
    for (let i = 0; i < n; i++) {
      const dl = l[i] - this.prevL, dr = r[i] - this.prevR;
      this.prevL = l[i];
      this.prevR = r[i];
      this.frameAcc += dl * dl + dr * dr;
      if (++this.frameFill < this.frameLen) continue;
      const e = this.frameAcc / this.frameLen;
      const start = this.written + i + 1 - this.frameLen - this.lead;
      if (e > ATTACK_RATIO * this.level && e > ATTACK_FLOOR && start - this.lastAttack >= this.gap) {
        this.attacks.push(start);
        this.lastAttack = start;
      }
      this.level += this.alpha * (e - this.level);
      this.frameAcc = 0;
      this.frameFill = 0;
    }
    this.written += n;
  }

  /** True if an attack starts in [from, to), absolute positions. */
  any(from, to) {
    // Sorted, and below 1x the unread backlog, and so this list, keeps growing.
    for (const a of this.attacks) {
      if (a >= to) return false;
      if (a >= from) return true;
    }
    return false;
  }

  /** Forget attacks the chain has consumed past. */
  drop(before) {
    let k = 0;
    while (k < this.attacks.length && this.attacks[k] < before) k++;
    if (k) this.attacks.splice(0, k);
  }
}

/**
 * One WSOLA time-stretch chain: stereo in, stereo out, its own buffers.
 *
 * Used once for the music-only pitch stage and once for the shared tempo stage.
 * `postFilter` runs over a finished sequence before it is queued, which is
 * where the anti-alias filter belongs:
 * ahead of the resampler, because filtering after decimation cannot undo
 * folding, only attenuate whatever the fold produced.
 */
class Wsola {
  /**
   * `keepAttacks` must stay off for the pitch stages. Borrowing moves a chain's
   * clock, and a pitch chain is aligned sample for sample with the unpitched
   * drums beside it, so there it would pull the band away from the kit. It is
   * safe on the shared tempo stage only because everything shares that clock.
   */
  constructor(sr, { keepAttacks = false } = {}) {
    this.ovLen   = Math.round(OVERLAP_MS  * sr / 1000);
    this.seekLen = Math.round(SEEK_MS     * sr / 1000);
    this.seqLen  = Math.round(SEQUENCE_MS * sr / 1000);
    this.midLen  = this.seqLen - 2 * this.ovLen;
    this.needed  = this.ovLen + this.seekLen + this.seqLen;

    this.inL  = new FloatFifo();
    this.inR  = new FloatFifo();
    this.outL = new FloatFifo();
    this.outR = new FloatFifo();

    this._carryL = new Float32Array(this.ovLen);
    this._carryR = new Float32Array(this.ovLen);
    const outPerSeq = this.ovLen + this.midLen;
    this._tmpL = new Float32Array(outPerSeq);
    this._tmpR = new Float32Array(outPerSeq);

    // The input advance carries its fractional part between sequences. The
    // effective tempo is irrational for every semitone but 0 and +/-12, so
    // rounding it away each time drifts the duration by a sample every few
    // sequences, which shows up as slow desync against the click track.
    this._advanceRem = 0;
    this._primed = false;

    this.postFilter = null;

    this._attacks = keepAttacks ? new AttackTracker(sr) : null;
    this._maxBorrow = Math.round(MAX_BORROW_MS * sr / 1000);
    this._head = 0;         // absolute position of inL's read head
    this._borrowed = 0;     // input consumed ahead of the nominal clock
    this._continue = false; // next sequence starts exactly where this one ended
  }

  clear() {
    this.inL.clear(); this.inR.clear();
    this.outL.clear(); this.outR.clear();
    this._carryL.fill(0); this._carryR.fill(0);
    this._advanceRem = 0;
    this._primed = false;
    this._attacks?.clear();
    this._head = 0;
    this._borrowed = 0;
    this._continue = false;
  }

  push(l, r, n) {
    this.inL.push(l, 0, n);
    this.inR.push(r, 0, n);
    this._attacks?.push(l, r, n);
  }

  _consume(n) {
    this.inL.consume(n);
    this.inR.consume(n);
    this._head += n;
    this._attacks?.drop(this._head);
  }

  /** Run sequences until `want` output samples are queued, or input runs out. */
  fill(tempo, want) {
    if (!this._primed) {
      if (this.inL.avail < this.needed + PRIME_CUSHION) return;
      this._primed = true;
    }
    while (this.outL.avail < want && this.inL.avail >= this.needed) this._sequence(tempo);
  }

  _sequence(tempo) {
    const ovLen = this.ovLen, midLen = this.midLen, seqLen = this.seqLen;
    const outLen = ovLen + midLen;
    // After carrying straight on, the carry is the very samples at the head,
    // so offset 0 splices them onto themselves. A search could land elsewhere.
    const bestOff = this._continue ? 0 : findBestOffset(this._carryL, ovLen, this.inL, this.seekLen);
    this._continue = false;

    for (let i = 0; i < ovLen; i++) {
      const w = i / ovLen;
      this._tmpL[i] = this._carryL[i] * (1 - w) + this.inL.peek(bestOff + i) * w;
      this._tmpR[i] = this._carryR[i] * (1 - w) + this.inR.peek(bestOff + i) * w;
    }
    for (let i = 0; i < midLen; i++) {
      this._tmpL[ovLen + i] = this.inL.peek(bestOff + ovLen + i);
      this._tmpR[ovLen + i] = this.inR.peek(bestOff + ovLen + i);
    }
    for (let i = 0; i < ovLen; i++) {
      this._carryL[i] = this.inL.peek(bestOff + ovLen + midLen + i);
      this._carryR[i] = this.inR.peek(bestOff + ovLen + midLen + i);
    }

    if (this.postFilter) this.postFilter(this._tmpL, this._tmpR, outLen);

    this.outL.push(this._tmpL, 0, outLen);
    this.outR.push(this._tmpR, 0, outLen);

    const nominal = (seqLen - ovLen) * tempo;
    const repay = this._attacks ? Math.min(this._borrowed, nominal * REPAY_SHARE) : 0;
    const exact = nominal - repay + this._advanceRem;
    const whole = Math.floor(exact);

    // Input this sequence played in full ends at `played`; the carry after it
    // is faded out under the next sequence's head. The next sequence starts no
    // earlier than `whole`, so an attack in between would be heard twice.
    if (this._attacks && tempo < 1) {
      const played = bestOff + outLen;
      const cost = played - nominal;
      if (this._borrowed + cost <= this._maxBorrow
        && this._attacks.any(this._head + whole, this._head + played + ovLen)) {
        this._borrowed += cost;
        this._continue = true;
        this._consume(played);
        return;
      }
    }

    this._borrowed -= repay;
    this._advanceRem = exact - whole;
    // The similarity search is local to this sequence. Folding bestOff into
    // the nominal advance makes content-dependent offsets accumulate into a
    // clock error, which is especially audible against unpitched drums.
    this._consume(whole);
  }
}

// The shared tempo stage, when the Signalsmith core loaded (#729).
//
// WSOLA slows audio down by repeating overlapping fragments of it, which is
// heard as an echo on anything sustained. Signalsmith Stretch works in the
// frequency domain and repeats nothing. 40 ms blocks were chosen by ear against
// 30, 60 and the library's 120 ms default: 120 softened a quarter of the drum
// attacks in a full mix, 30 blurred the low end.
const SIGNALSMITH_BLOCK_MS    = 40;
const SIGNALSMITH_INTERVAL_MS = 10;
// Largest input one render quantum can ask for is 128 * the tempo maximum.
const SIGNALSMITH_MAX_BLOCK   = 128 * 4;

/**
 * The same shape as a Wsola chain, as far as the processor uses one: push,
 * fill, clear and an output FIFO pair. Anything else would mean a second code
 * path through process().
 *
 * The core stretches by whatever ratio it is handed, so each quantum feeds it
 * 128 * tempo input samples for 128 output samples. The fraction carries over,
 * so the rate is exact on average and nothing drifts against the click.
 */
class SignalsmithTempo {
  constructor(core, sr) {
    this._core = core;
    core._main();
    core._configure(
      2,
      Math.round(SIGNALSMITH_BLOCK_MS * sr / 1000),
      Math.round(SIGNALSMITH_INTERVAL_MS * sr / 1000),
      false,
    );
    core._reset();
    this.inputLatency = core._inputLatency();
    this.outputLatency = core._outputLatency();
    this._len = SIGNALSMITH_MAX_BLOCK;
    this._ptr = core._setBuffers(2, this._len);
    this._heap = null;
    this.inL = new FloatFifo();
    this.inR = new FloatFifo();
    this.outL = new FloatFifo();
    this.outR = new FloatFifo();
    this._carry = 0;
  }

  // Views onto the core's buffers, rebuilt only if its memory was replaced.
  _views() {
    const buffer = this._core.HEAP8.buffer;
    if (!this._heap || this._heap.buffer !== buffer) {
      this._heap = new Float32Array(buffer, this._ptr, this._len * 4);
    }
    return this._heap;
  }

  clear() {
    this.inL.clear(); this.inR.clear();
    this.outL.clear(); this.outR.clear();
    this._core._reset();
    this._carry = 0;
  }

  push(l, r, n) {
    this.inL.push(l, 0, n);
    this.inR.push(r, 0, n);
  }

  fill(tempo, want) {
    const block = 128;
    while (this.outL.avail < want) {
      const exact = block * tempo + this._carry;
      const take = Math.min(Math.floor(exact), this._len);
      if (this.inL.avail < take) return;
      this._carry = exact - take;
      const heap = this._views();
      const len = this._len;
      for (let i = 0; i < take; i++) {
        heap[i] = this.inL.peek(i);
        heap[len + i] = this.inR.peek(i);
      }
      this.inL.consume(take);
      this.inR.consume(take);
      this._core._process(take, block);
      const out = this._views();
      this.outL.push(out, 2 * len, block);
      this.outR.push(out, 3 * len, block);
    }
  }

  /** Output samples between a sample entering and leaving, at this tempo. */
  latencyFrames(tempo) {
    return this.inputLatency / tempo + this.outputLatency;
  }
}

/** FIFO sample at `idx`, falling back to the carried history for idx < 0. */
function sampleAt(fifo, hist, idx) {
  if (idx >= 0) return fifo.peek(idx);
  const h = hist.length + idx;
  return h >= 0 ? hist[h] : 0;
}

/** 4-point, 3rd-order Hermite (Catmull-Rom) interpolation. */
function hermite(ym1, y0, y1, y2, t) {
  const c0 = y0;
  const c1 = 0.5 * (y1 - ym1);
  const c2 = ym1 - 2.5 * y0 + 2 * y1 - 0.5 * y2;
  const c3 = 0.5 * (y2 - ym1) + 1.5 * (y0 - y1);
  return ((c3 * t + c2) * t + c1) * t + c0;
}

const SILENCE = new Float32Array(128);

// One input per semitone the UI offers, and input `ZERO_INPUT` is semitone 0.
//
// Per-lane transpose means several different shifts have to be audible at once,
// and the obvious build -- one worklet per lane -- is the same mistake the
// two-bus design was written to avoid, only worse. Latency moves with the
// shift, so six independently clocked chains put six stems up to 144 ms apart
// from each other. Here every bus is a chain into one shared tempo stage, so
// there is still exactly one output clock no matter how many different keys are
// playing at once.
//
// Semitone 0 needs no resampling, so it is not a chain at all: it is the plain
// delay line carrying drums, the click, and any lane whose net transpose works
// out to zero. That caps the design at twelve chains, and means the common case
// of one global shift with drums held out of it allocates exactly one.
const INPUT_COUNT = PITCH_MAX_SEMITONES - PITCH_MIN_SEMITONES + 1;
const ZERO_INPUT = -PITCH_MIN_SEMITONES;

// How long a chain is kept after its lane leaves. Its buffers still hold that
// lane's last couple of hundred milliseconds, and dropping it early would cut
// the tail off mid-note. Past this it is silent, so keeping it would only cost
// memory and a correlation search per block.
const CHAIN_LINGER_BLOCKS = 256;

/**
 * How far ahead of true time a chain at `ratio` runs, in samples.
 *
 * WSOLA copies a sequence verbatim and then jumps its read position by
 * `outLen * tempo`, so inside a sequence the source runs at 1:1 while across
 * sequences it runs at `tempo`. The mean is exact, which is why nothing drifts,
 * but the sawtooth in between leaves a fixed offset that scales with the
 * interval: measured at 18 ms for six semitones before this was corrected.
 */
function runsEarlyBy(outLen, ratio) {
  return (outLen * (1 - 1 / ratio)) / 2;
}

/**
 * Silence a bus is primed with so that every bus ends up on the same clock.
 *
 * Referenced against the most negative offset the *offered range* can produce
 * rather than against whichever buses happen to exist right now, so a bus
 * created later lands in step with the ones already playing instead of shifting
 * them.
 */
function alignmentPad(outLen, ratio) {
  const latest = runsEarlyBy(outLen, Math.pow(2, PITCH_MIN_SEMITONES / 12));
  return Math.max(0, Math.round(runsEarlyBy(outLen, ratio) - latest));
}

/** WSOLA at 1/r, anti-aliased, then resampled by r: pitch without tempo. */
class PitchChain {
  constructor(sr, semitones, commonPrime) {
    this.ratio = Math.pow(2, semitones / 12);
    this.wsola = new Wsola(sr);
    this.readPos = 0;
    this.histL = new Float32Array(3);
    this.histR = new Float32Array(3);
    this._commonPrime = commonPrime;

    // Only pitching up decimates, and only decimation folds. Filtering after
    // the resampler cannot undo a fold, only attenuate what it produced, so
    // this runs on finished sequences, ahead of it.
    // A zero cutoff means no filter, not a filter that removes everything: a
    // biquad configured at 0 Hz has all-zero numerator coefficients and
    // silences the bus. The measurement in tests/js/pitch-shift.test.mjs
    // defeats the filter exactly this way to get its reference reading.
    const cutoff = (0.47 * sr) / this.ratio;
    if (this.ratio > 1 && cutoff > 0) {
      const aaL = BUTTERWORTH_Q8.map(() => new Biquad());
      const aaR = BUTTERWORTH_Q8.map(() => new Biquad());
      for (let i = 0; i < BUTTERWORTH_Q8.length; i++) {
        aaL[i].setLowpass(cutoff, sr, BUTTERWORTH_Q8[i]);
        aaR[i].setLowpass(cutoff, sr, BUTTERWORTH_Q8[i]);
      }
      this._aaL = aaL;
      this._aaR = aaR;
      this.wsola.postFilter = (l, r, n) => {
        for (let i = 0; i < n; i++) {
          let a = l[i];
          let b = r[i];
          for (let k = 0; k < aaL.length; k++) {
            a = aaL[k].process(a);
            b = aaR[k].process(b);
          }
          l[i] = a;
          r[i] = b;
        }
      };
    }
    this.reset();
  }

  /**
   * Prime with silence so the chain is productive from its very first block.
   *
   * A chain priming on real audio emits nothing for its first ~130 ms. Blocking
   * the mix on it would stall every other lane for that long; not blocking
   * would leave its lane permanently 130 ms late. Feeding it that much silence
   * up front makes it behave exactly like a chain that has been running since
   * the transport started, so a lane can change key mid-playback and the tail
   * still draining from its previous chain meets the new one seamlessly.
   */
  reset() {
    this.wsola.clear();
    this.readPos = 0;
    this.histL.fill(0);
    this.histR.fill(0);
    if (this._aaL) {
      for (let i = 0; i < this._aaL.length; i++) {
        this._aaL[i].reset();
        this._aaR[i].reset();
      }
    }
    const outLen = this.wsola.ovLen + this.wsola.midLen;
    const pad = new Float32Array(this._commonPrime + alignmentPad(outLen, this.ratio));
    this.wsola.push(pad, pad, pad.length);
  }

  push(l, r, n) { this.wsola.push(l, r, n); }

  /** Output samples this chain could hand over right now. */
  available() {
    return Math.max(0, Math.floor((this.wsola.outL.avail - 3) / this.ratio));
  }

  fill(want) { this.wsola.fill(1 / this.ratio, Math.ceil(want * this.ratio) + 4); }

  /** Resample `n` samples and sum them into the mix buffers. */
  addInto(dstL, dstR, n) {
    const mL = this.wsola.outL;
    const mR = this.wsola.outR;
    const histL = this.histL;
    const histR = this.histR;
    const ratio = this.ratio;
    let pos = this.readPos;
    for (let i = 0; i < n; i++) {
      const idx = Math.floor(pos);
      const frac = pos - idx;
      dstL[i] += hermite(sampleAt(mL, histL, idx - 1), sampleAt(mL, histL, idx),
                         sampleAt(mL, histL, idx + 1), sampleAt(mL, histL, idx + 2), frac);
      dstR[i] += hermite(sampleAt(mR, histR, idx - 1), sampleAt(mR, histR, idx),
                         sampleAt(mR, histR, idx + 1), sampleAt(mR, histR, idx + 2), frac);
      pos += ratio;
    }
    const consumed = Math.floor(pos);
    if (consumed > 0) {
      for (let k = 0; k < 3; k++) {
        const src = consumed - 3 + k;
        histL[k] = src >= 0 ? mL.peek(src) : histL[histL.length + src];
        histR[k] = src >= 0 ? mR.peek(src) : histR[histR.length + src];
      }
      mL.consume(consumed);
      mR.consume(consumed);
    }
    this.readPos = pos - consumed;
  }
}

class SoundTouchProcessor extends AudioWorkletProcessor {
  static get parameterDescriptors() {
    return [
      {
        name: 'tempo',
        defaultValue: 1.0,
        minValue: 0.25,
        maxValue: 4.0,
        automationRate: 'k-rate',
      },
    ];
  }

  constructor() {
    super();
    // WSOLA stays as the tempo stage until the Signalsmith core is ready, and
    // for good if it never is: a browser without WebAssembly in worklets, or a
    // CSP that blocks it. Its sizes also set the priming below either way.
    this._wsola = new Wsola(sampleRate, { keepAttacks: true });
    this._tempo = this._wsola;
    this._signalsmith = null;
    // Silence every bus is primed with, before its own alignment pad, so they
    // share one clock and each chain can produce from its first block.
    this._commonPrime = this._wsola.needed + PRIME_CUSHION;
    this._chains = new Array(INPUT_COUNT).fill(null);
    this._idle = new Array(INPUT_COUNT).fill(0);
    this._unpitchedL = new FloatFifo();
    this._unpitchedR = new FloatFifo();
    this._mixL = new Float32Array(128 + OUTPUT_RESERVE);
    this._mixR = new Float32Array(128 + OUTPUT_RESERVE);
    this._mixFifoL = new FloatFifo();
    this._mixFifoR = new FloatFifo();

    this._bypassing = true;
    this._lastTempo = 1;
    this._resetRequested = false;
    // Set by the engine's teardown (tempoStage.js disposeStretchNode). Chromium
    // keeps a node whose processor is still active alive, and the node keeps
    // its context: closed contexts piled up, one per song load, until the page
    // went away. A processor that has returned false is inactive, so both can go.
    this._disposed = false;
    if (this.port) {
      this.port.onmessage = (event) => {
        const type = event?.data?.type;
        if (type === 'reset') this._resetRequested = true;
        else if (type === 'dispose') this._disposed = true;
      };
    }
    this._primeUnpitched();
    // The engine needs this to place the playhead, and deriving it there would
    // mean keeping a copy of every constant above in sync by hand.
    if (this.port) {
      this.port.postMessage({
        type: 'latency',
        frames: this._commonPrime
          + alignmentPad(this._wsola.ovLen + this._wsola.midLen, 1),
      });
    }
    this._reportTempoStage();

    // Loaded as its own worklet module ahead of this one; see
    // signalsmith-stretch.js. Instantiating is asynchronous, so the stage is
    // only swapped in at the next flush, never under audio already playing.
    const core = globalThis.SignalsmithStretchCore;
    if (typeof core === 'function') {
      core().then((module) => {
        this._signalsmith = new SignalsmithTempo(module, sampleRate);
      }).catch((err) => {
        this.port?.postMessage({ type: 'tempoStageFailed', message: String(err?.message || err) });
      });
    }
  }

  // The tempo stage's own latency, which the engine adds when tempo is not 1.
  // Signalsmith's depends on the tempo, so it is sent as its two parts.
  _reportTempoStage() {
    if (!this.port) return;
    const ss = this._tempo === this._signalsmith ? this._signalsmith : null;
    this.port.postMessage({
      type: 'tempoStage',
      stage: ss ? 'signalsmith' : 'wsola',
      inputFrames: ss ? ss.inputLatency : 0,
      outputFrames: ss ? ss.outputLatency : 0,
    });
  }

  _primeUnpitched() {
    const outLen = this._wsola.ovLen + this._wsola.midLen;
    const pad = new Float32Array(this._commonPrime + alignmentPad(outLen, 1));
    this._unpitchedL.clear();
    this._unpitchedR.clear();
    this._unpitchedL.push(pad, 0, pad.length);
    this._unpitchedR.push(pad, 0, pad.length);
  }

  _flush() {
    if (this._signalsmith && this._tempo !== this._signalsmith) {
      this._tempo = this._signalsmith;
      this._reportTempoStage();
    }
    this._tempo.clear();
    this._mixFifoL.clear();
    this._mixFifoR.clear();
    for (let k = 0; k < INPUT_COUNT; k++) if (this._chains[k]) this._chains[k].reset();
    this._primeUnpitched();
  }

  process(inputs, outputs, parameters) {
    if (this._disposed) {
      // Once, then never called again: the output stays silent.
      if (this.port && !this._disposedSent) {
        this._disposedSent = true;
        this.port.postMessage({ type: 'disposed' });
      }
      return false;
    }
    const frames = 128;
    const tempo = parameters.tempo[0];
    const tempoActive = Math.abs(tempo - 1) >= EPS;

    const outp = outputs[0];
    const outL = outp[0];
    const outR = outp[1] || outL;
    const stereo = outR !== outL;

    if (this._resetRequested || Math.abs(tempo - this._lastTempo) >= EPS) {
      this._flush();
      this._resetRequested = false;
    }
    this._lastTempo = tempo;

    // An unconnected input arrives as an empty array, which is how a lane
    // moving between keys is noticed without any message from the main thread.
    let pitched = false;
    for (let k = 0; k < INPUT_COUNT; k++) {
      if (k === ZERO_INPUT) continue;
      const connected = !!(inputs[k] && inputs[k].length);
      if (connected) {
        if (!this._chains[k]) {
          this._chains[k] = new PitchChain(sampleRate, k - ZERO_INPUT, this._commonPrime);
        }
        this._idle[k] = 0;
      } else if (this._chains[k] && ++this._idle[k] > CHAIN_LINGER_BLOCKS) {
        this._chains[k] = null;
      }
      if (this._chains[k]) pitched = true;
    }

    const zero = inputs[ZERO_INPUT];
    const uL = (zero && zero[0]) || SILENCE;
    const uR = (zero && zero[1]) || uL;

    // Nothing to do to the signal: hand it back sample for sample.
    if (!pitched && !tempoActive) {
      if (!this._bypassing) {
        this._flush();
        this._bypassing = true;
      }
      for (let i = 0; i < frames; i++) {
        outL[i] = uL[i];
        if (stereo) outR[i] = uR[i];
      }
      return true;
    }
    this._bypassing = false;

    this._unpitchedL.push(uL, 0, frames);
    this._unpitchedR.push(uR, 0, frames);
    for (let k = 0; k < INPUT_COUNT; k++) {
      const chain = this._chains[k];
      if (!chain) continue;
      const inp = inputs[k];
      const l = (inp && inp[0]) || SILENCE;
      const r = (inp && inp[1]) || l;
      chain.push(l, r, frames);
    }

    const wanted = frames + OUTPUT_RESERVE - this._mixFifoL.avail;
    if (wanted > 0) {
      let n = Math.min(wanted, this._unpitchedL.avail);
      for (let k = 0; k < INPUT_COUNT; k++) {
        const chain = this._chains[k];
        if (!chain) continue;
        chain.fill(wanted);
        n = Math.min(n, chain.available());
      }
      if (n > 0) {
        this._unpitchedL.shift(this._mixL, 0, n);
        this._unpitchedR.shift(this._mixR, 0, n);
        for (let k = 0; k < INPUT_COUNT; k++) {
          if (this._chains[k]) this._chains[k].addInto(this._mixL, this._mixR, n);
        }
        this._mixFifoL.push(this._mixL, 0, n);
        this._mixFifoR.push(this._mixR, 0, n);
      }
    }
    const mixedFrames = this._mixFifoL.shift(this._mixL, 0, frames);
    this._mixFifoR.shift(this._mixR, 0, frames);

    if (tempoActive) {
      if (mixedFrames > 0) this._tempo.push(this._mixL, this._mixR, mixedFrames);
      this._tempo.fill(tempo, frames);
      const n = Math.min(frames, this._tempo.outL.avail);
      this._tempo.outL.shift(outL, 0, n);
      if (stereo) this._tempo.outR.shift(outR, 0, n);
      else this._tempo.outR.consume(n);
      for (let i = n; i < frames; i++) {
        outL[i] = 0;
        if (stereo) outR[i] = 0;
      }
    } else {
      for (let i = 0; i < mixedFrames; i++) {
        outL[i] = this._mixL[i];
        if (stereo) outR[i] = this._mixR[i];
      }
      for (let i = mixedFrames; i < frames; i++) {
        outL[i] = 0;
        if (stereo) outR[i] = 0;
      }
    }
    return true;
  }
}

registerProcessor('soundtouch-processor', SoundTouchProcessor);
