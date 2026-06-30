'use strict';
// SoundTouch WSOLA time-stretcher - AudioWorkletProcessor
// Adapted from SoundTouch C++ by Olli Parviainen. MIT License.
// Self-contained; no imports required.

const SEQUENCE_MS = 82;
const SEEK_MS     = 28;
const OVERLAP_MS  = 12;

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

class SoundTouchProcessor extends AudioWorkletProcessor {
  static get parameterDescriptors() {
    return [{
      name: 'tempo',
      defaultValue: 1.0,
      minValue: 0.25,
      maxValue: 4.0,
      automationRate: 'k-rate',
    }];
  }

  constructor() {
    super();
    const sr = sampleRate;
    this._ovLen   = Math.round(OVERLAP_MS  * sr / 1000);
    this._seekLen = Math.round(SEEK_MS     * sr / 1000);
    this._seqLen  = Math.round(SEQUENCE_MS * sr / 1000);
    this._midLen  = this._seqLen - 2 * this._ovLen;

    this._inL  = new FloatFifo();
    this._inR  = new FloatFifo();
    this._outL = new FloatFifo();
    this._outR = new FloatFifo();

    // Overlap carry-over buffers (crossfade region saved from previous sequence)
    this._carryL = new Float32Array(this._ovLen);
    this._carryR = new Float32Array(this._ovLen);

    // Pre-allocated temp output (seqLen - ovLen samples per sequence max)
    const outPerSeq = this._ovLen + this._midLen;
    this._tmpL = new Float32Array(outPerSeq);
    this._tmpR = new Float32Array(outPerSeq);
  }

  process(inputs, outputs, parameters) {
    const inp    = inputs[0];
    const outp   = outputs[0];
    const frames = 128;
    const tempo  = parameters.tempo[0];

    const inL  = inp?.[0]  || new Float32Array(frames);
    const inR  = inp?.[1]  || inL;
    const outL = outp[0];
    const outR = outp[1] || outL;

    // Passthrough at 1.0 - zero DSP cost
    if (Math.abs(tempo - 1.0) < 0.001) {
      outL.set(inL);
      if (outR !== outL) outR.set(inR);
      return true;
    }

    this._inL.push(inL, 0, frames);
    this._inR.push(inR, 0, frames);

    const needed = this._ovLen + this._seekLen + this._seqLen;
    while (this._inL.avail >= needed) this._processSeq(tempo);

    const got = this._outL.shift(outL, 0, frames);
    this._outR.shift(outR, 0, frames);
    for (let i = got; i < frames; i++) { outL[i] = 0; if (outR !== outL) outR[i] = 0; }

    return true;
  }

  _processSeq(tempo) {
    const ovLen   = this._ovLen;
    const midLen  = this._midLen;
    const seqLen  = this._seqLen;
    const outLen  = ovLen + midLen;

    const bestOff = findBestOffset(this._carryL, ovLen, this._inL, this._seekLen);

    // Crossfade carry-over with the new sequence start
    for (let i = 0; i < ovLen; i++) {
      const w = i / ovLen;
      this._tmpL[i] = this._carryL[i] * (1 - w) + this._inL.peek(bestOff + i) * w;
      this._tmpR[i] = this._carryR[i] * (1 - w) + this._inR.peek(bestOff + i) * w;
    }

    // Copy middle section verbatim
    for (let i = 0; i < midLen; i++) {
      this._tmpL[ovLen + i] = this._inL.peek(bestOff + ovLen + i);
      this._tmpR[ovLen + i] = this._inR.peek(bestOff + ovLen + i);
    }

    // Save last ovLen samples as new carry-over for next crossfade
    for (let i = 0; i < ovLen; i++) {
      this._carryL[i] = this._inL.peek(bestOff + ovLen + midLen + i);
      this._carryR[i] = this._inR.peek(bestOff + ovLen + midLen + i);
    }

    this._outL.push(this._tmpL, 0, outLen);
    this._outR.push(this._tmpR, 0, outLen);

    // Advance input: tempo controls how many input samples map to one output sequence
    const advance = Math.round((seqLen - ovLen) * tempo) + bestOff;
    this._inL.consume(advance);
    this._inR.consume(advance);
  }
}

registerProcessor('soundtouch-processor', SoundTouchProcessor);
